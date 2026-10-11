"""Recoverable publication of one approved maintenance transaction."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
from types import MappingProxyType
from typing import Mapping

from .ids import canonical_json_bytes
from .ledger import LedgerStore
from .maintenance_context import MaintenanceRunBundle, recheck_committed_source
from .maintenance_models import CommittedSourceGuard, MaintenanceJob, ResolvedDeferredEvent
from .maintenance_store import MaintenanceStore
from .models import ExportJournal, RepositoryIdentity


@dataclass(frozen=True, slots=True)
class ApplyJournal:
    jobId: str
    status: str
    expectedManifestSetId: str | None
    expectedOutputHashes: Mapping[str, str]
    intendedRunId: str
    intendedStageRevision: int
    intendedManifestSetId: str | None
    intendedOutputHashes: Mapping[str, str | None]
    exportId: str | None
    completedAt: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "expectedOutputHashes", MappingProxyType(dict(self.expectedOutputHashes)))
        object.__setattr__(self, "intendedOutputHashes", MappingProxyType(dict(self.intendedOutputHashes)))


@dataclass(frozen=True, slots=True)
class ApplyResult:
    status: str
    jobId: str
    runId: str | None = None
    exportId: str | None = None
    manifestSetId: str | None = None
    message: str = ""


def _prefix(job_id: str) -> str:
    return f"maintenance/apply/{job_id}"


def _deferred_resolution(store: LedgerStore, job: MaintenanceJob, event, manifest, budget) -> str | None:
    from .maintenance_git import MaintenanceGitError, _proves_ancestor

    try:
        tree_bootstrap = manifest.get("impactProjection") == "tree-bootstrap"
        if tree_bootstrap and _proves_ancestor(store.root, event.targetCommit, job.targetCommit, budget):
            return "covered-by-tree-bootstrap"
        if event.targetCommit == job.targetCommit:
            return "target-applied"
        if not _proves_ancestor(store.root, event.targetCommit, job.targetCommit, budget):
            return None
        if (job.baselineCommit is not None
                and _proves_ancestor(store.root, job.baselineCommit, event.targetCommit, budget)):
            return "covered-by-applied-interval"
    except (MaintenanceGitError, ValueError, OSError):
        return None
    return None


_MAX_DEFERRED_RESOLUTION_CANDIDATES = 32


def _plan_deferred_resolutions(store: LedgerStore, job: MaintenanceJob, events):
    """Prove a bounded deferred-event prefix without holding the ledger mutation."""
    from .maintenance_context import _context_manifest
    from .maintenance_git import GitReadBudget, MaintenanceGitError

    candidates = tuple(events[:_MAX_DEFERRED_RESOLUTION_CANDIDATES])
    if not candidates:
        return tuple()
    budget = GitReadBudget()
    try:
        manifest = _context_manifest(store, job)
    except (MaintenanceGitError, ValueError, OSError):
        return tuple((event, None) for event in candidates)
    resolutions = []
    for event in candidates:
        # One shared deadline/output allowance covers the entire candidate batch.
        try:
            budget.limits(1)
        except MaintenanceGitError:
            resolutions.append((event, None))
            break
        resolutions.append((event, _deferred_resolution(store, job, event, manifest, budget)))
    return tuple(resolutions)


def _read_apply_journal(store: LedgerStore, job_id: str) -> ApplyJournal | None:
    maintenance = MaintenanceStore(store.root, store)
    for status in ("applied", "exported", "export-prepared", "staged", "intent", "failed"):
        raw = maintenance.read_apply_phase(job_id, status)
        if raw is None:
            continue
        try:
            value = json.loads(raw.decode("utf-8"))
            if value.get("schemaVersion") != "1.0" or value.get("status") != status or value.get("jobId") != job_id:
                raise ValueError("apply journal phase binding is corrupt")
            return ApplyJournal(
                value["jobId"], value["status"], value["expectedManifestSetId"],
                value["expectedOutputHashes"], value["intendedRunId"],
                value["intendedStageRevision"], value["intendedManifestSetId"],
                value["intendedOutputHashes"], value["exportId"], value["completedAt"],
            )
        except (UnicodeError, json.JSONDecodeError, KeyError, AttributeError, TypeError) as error:
            raise ValueError("apply journal phase is malformed") from error
    return None


def _persist_apply_journal(store: LedgerStore, journal: ApplyJournal) -> None:
    if not store._mutation_active:
        with store.mutation():
            _persist_apply_journal(store, journal)
        return
    payload = canonical_json_bytes({"schemaVersion": "1.0", **{
        name: getattr(journal, name) for name in ApplyJournal.__dataclass_fields__
    }}) + b"\n"
    try:
        MaintenanceStore(store.root, store).write_apply_phase(journal.jobId, journal.status, payload)
    except FileExistsError as error:
        raise ValueError("apply journal phase already exists with different bindings") from error


def _active_targets(store: LedgerStore, bundle: MaintenanceRunBundle):
    from .ownership import _active_manifests, _ownership
    state = store.read_state()
    manifest_set, manifests = _active_manifests(store, state)
    target_ids = tuple(spec.targetId for spec in bundle.selectedSpecs)
    return manifest_set, manifests, {key: value for key, value in _ownership(manifests).items() if key in target_ids}


def _guarded_live_projection(journal: ExportJournal, manifests) -> dict[str, str | None]:
    projected = {file.path: file.exactHash for manifest in manifests.values() for file in manifest.files}
    for operation in journal.operations:
        if operation.action == "replace":
            projected[operation.path] = operation.stagedNewHash
        elif operation.action == "delete":
            projected[operation.path] = None
    # mkdir operations represent directories and are intentionally absent from this map.
    return projected


def _check_projection(store: LedgerStore, journal: ApplyJournal) -> None:
    from .output_fs import read_live_file
    for path, expected in journal.intendedOutputHashes.items():
        actual = read_live_file(store.root, path, limit=2 * 1024 * 1024)
        digest = None if actual is None else "sha256:" + hashlib.sha256(actual).hexdigest()
        if digest != expected:
            raise ValueError(f"maintenance intended output differs from live bytes: {path}")


def _verify_active_apply(store: LedgerStore, intent: ApplyJournal):
    from .ownership import _active_manifests

    project_state = store.read_state()
    if project_state is None:
        raise ValueError("initialized project state is required to finalize maintenance")
    if (project_state.currentRunId != intent.intendedRunId
            or project_state.activeStageRevision != intent.intendedStageRevision
            or project_state.activeExportId != intent.exportId
            or project_state.activeManifestSetId != intent.intendedManifestSetId):
        raise ValueError("active P0 pointers do not match the durable apply intent")
    manifest_set, manifests = _active_manifests(store, project_state)
    if manifest_set is None or manifest_set.manifestSetId != intent.intendedManifestSetId:
        raise ValueError("active manifest set does not match the durable apply intent")
    _check_projection(store, intent)
    return project_state, manifest_set, manifests


def _finalize(store: LedgerStore, job: MaintenanceJob, intent: ApplyJournal, guard: CommittedSourceGuard) -> str:
    from .maintenance_approval import verify_approval_binding
    maintenance = MaintenanceStore(store.root, store)
    proof_snapshot = maintenance.read_state()
    if proof_snapshot is None:
        raise ValueError("maintenance state disappeared before deferred resolution")
    captured_deferred = proof_snapshot.deferredEvents
    deferred_proofs = _plan_deferred_resolutions(store, job, captured_deferred)
    with store.mutation():
        current = maintenance.read_state()
        lifecycle = current.jobs.get(job.jobId)
        if lifecycle is None:
            raise ValueError("maintenance lifecycle disappeared during apply")
        if lifecycle.status == "applied":
            if current.appliedBaseline != job.targetCommit:
                raise ValueError("applied lifecycle has a different baseline")
            _verify_active_apply(store, intent)
            completed = replace(intent, status="applied", completedAt=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
            _persist_apply_journal(store, completed)
            return "applied"
        # Recheck authorization/source while holding the same ledger lock as the baseline pointer.
        recheck_committed_source(store, guard)
        verify_approval_binding(store, job, lifecycle)
        run = store.read_run(intent.intendedRunId)
        if run is None or run.applicationAuthorizationId != guard.authorizationId:
            raise ValueError("intended run does not match the exact application authorization")
        from .maintenance_context import verify_application_authorization
        verify_application_authorization(store, run)
        _verify_active_apply(store, intent)
        current = maintenance.read_state()
        lifecycle = current.jobs.get(job.jobId)
        if lifecycle is None:
            raise ValueError("maintenance lifecycle disappeared during finalization")
        if current.deferredEvents != captured_deferred:
            raise ValueError("deferred-event proof changed before baseline finalization")
        jobs = dict(current.jobs)
        jobs[job.jobId] = replace(lifecycle, status="applied", failureCode=None)
        proof_by_id = {item.eventId: resolution for item, resolution in deferred_proofs}
        covered_deferred = tuple((item, proof_by_id.get(item.eventId))
                                 for item in current.deferredEvents)
        resolved_deferred = tuple(item for item, resolution in covered_deferred if resolution is None)
        resolved_history = current.resolvedDeferredEvents + tuple(
            ResolvedDeferredEvent(item, resolution, job.jobId)
            for item, resolution in covered_deferred if resolution is not None
        )
        maintenance.replace_state(replace(current, appliedBaseline=job.targetCommit,
                                           jobs=jobs, runningJobId=None,
                                           deferredEvents=resolved_deferred,
                                           resolvedDeferredEvents=resolved_history))
        completed = replace(intent, status="applied", completedAt=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        _persist_apply_journal(store, completed)
    return "applied"


def apply_job(store: LedgerStore, job: MaintenanceJob, bundle: MaintenanceRunBundle,
              guard: CommittedSourceGuard) -> ApplyResult:
    """Stage and export one fully validated, already authorized run bundle."""
    from .ids import effective_policy_revision
    from .maintenance_approval import verify_approval_binding, verify_job_fingerprints
    from .maintenance_source_fragment import maintenance_extractor_versions
    from .ownership import export_staged
    from .stage import stage_run

    maintenance = MaintenanceStore(store.root, store)
    if bundle.run.applicationAuthorizationId != bundle.authorization.authorizationId:
        raise ValueError("run bundle authorization does not match its immutable run")
    if guard.jobId != job.jobId or guard.authorizationId != bundle.authorization.authorizationId:
        raise ValueError("source guard does not match the approved run bundle")
    authoritative = maintenance.read_job(job.jobId)
    if authoritative != job:
        raise ValueError("maintenance job differs from its immutable record")
    lifecycle = verify_approval_binding(store, job)
    if lifecycle.approvalBindingHash != bundle.authorization.approvalBindingHash:
        raise ValueError("application authorization is bound to a different approval")
    verify_job_fingerprints(store, job)
    prior = _read_apply_journal(store, job.jobId)
    if prior is not None and prior.status != "applied":
        state_now = store.read_state()
        if (prior.status != "intent" or bundle.run.runId != prior.intendedRunId
                or (state_now is not None and state_now.currentRunId == prior.intendedRunId)):
            return recover_apply(store, job.jobId, guard)
    if prior is not None and prior.status == "applied":
        status = _finalize(store, job, prior, guard)
        return ApplyResult(status, job.jobId, prior.intendedRunId, prior.exportId, prior.intendedManifestSetId)

    project = store.read_project()
    state = store.read_state()
    if project is None or state is None:
        raise ValueError("initialized project state is required")
    initial = ApplyJournal(
        job.jobId, "intent", job.expectedManifestSetId, dict(job.expectedOutputHashes),
        bundle.run.runId, 1, None, {}, None, None,
    )
    if prior is None:
        _persist_apply_journal(store, initial)
    else:
        initial = prior
    snapshot = bundle.run.snapshot
    identity = RepositoryIdentity(snapshot.repositoryId, store.root, "head", job.targetCommit,
                                  snapshot.branch, False, {})
    versions = maintenance_extractor_versions()
    policy_revision = effective_policy_revision(project.scanPolicy, project.claimPolicy,
                                                project.targets, generation_options={})
    excluded = tuple(sorted({project.stateDir, *(target.livePath for target in project.targets)}))

    with store.mutation():
        verify_approval_binding(store, job)
        verify_job_fingerprints(store, job)
        stage_run(bundle.rendered, bundle.run, store=store, current_identity=identity,
                  current_root_label=project.rootLabel, current_policy_revision=policy_revision,
                  current_extractor_versions=versions, excluded_paths=excluded,
                  selected_specs=bundle.selectedSpecs, source_guard=guard)
        staged = replace(initial, status="staged")
        _persist_apply_journal(store, staged)

    from .ownership import manifest_set_id_for_export
    manifests = {}
    ownership = {}
    def bind_export(export_journal: ExportJournal) -> None:
        projection = _guarded_live_projection(export_journal, manifests)
        bound = ApplyJournal(
            job.jobId, "export-prepared", job.expectedManifestSetId, dict(job.expectedOutputHashes),
            bundle.run.runId, 1, manifest_set_id_for_export(export_journal.exportId),
            projection, export_journal.exportId, None,
        )
        _persist_apply_journal(store, bound)

    with store.mutation():
        verify_approval_binding(store, job)
        verify_job_fingerprints(store, job)
        manifest_set, manifests, ownership = _active_targets(store, bundle)
        exported = export_staged(
            bundle.run.runId, tuple(spec.targetId for spec in bundle.selectedSpecs), ownership,
            store=store, current_identity=identity, current_root_label=project.rootLabel,
            current_policy_revision=policy_revision, current_extractor_versions=versions,
            excluded_paths=excluded, source_guard=guard, on_prepared_export=bind_export,
        )
    bound_intent = _read_apply_journal(store, job.jobId)
    if bound_intent is None:
        raise ValueError("prepared export did not persist its apply binding")
    if exported.status != "complete":
        return ApplyResult("conflict" if exported.status == "conflict" else "incomplete", job.jobId,
                           bundle.run.runId, exported.exportId, bound_intent.intendedManifestSetId,
                           "export did not complete")
    exported_intent = replace(bound_intent, status="exported")
    _persist_apply_journal(store, exported_intent)
    status = _finalize(store, job, exported_intent, guard)
    return ApplyResult(status, job.jobId, bundle.run.runId, exported.exportId,
                       exported_intent.intendedManifestSetId)


def recover_apply(store: LedgerStore, job_id: str, guard: CommittedSourceGuard) -> ApplyResult:
    """Recover only a durable apply/export binding; never construct a new request."""
    from .maintenance_approval import verify_approval_binding
    from .ownership import _active_manifests, _ownership, resume_export
    from .maintenance_source_fragment import maintenance_extractor_versions
    from .ids import effective_policy_revision

    maintenance = MaintenanceStore(store.root, store)
    job = maintenance.read_job(job_id)
    intent = _read_apply_journal(store, job_id)
    if job is None or intent is None or guard.jobId != job_id:
        raise ValueError("matching durable apply intent is required for recovery")
    if intent.status == "applied":
        # A previously committed application is a historical success; no new authorization is exercised.
        return ApplyResult("applied", job_id, intent.intendedRunId, intent.exportId,
                           intent.intendedManifestSetId)
    current_maintenance = maintenance.read_state()
    current_lifecycle = current_maintenance.jobs.get(job_id)
    if current_lifecycle is not None and current_lifecycle.status == "applied":
        state = store.read_state()
        if (current_maintenance.appliedBaseline != job.targetCommit or state is None
                or state.currentRunId != intent.intendedRunId
                or state.activeStageRevision != intent.intendedStageRevision
                or state.activeExportId != intent.exportId
                or state.activeManifestSetId != intent.intendedManifestSetId):
            raise ValueError("applied baseline pointers differ from durable apply intent")
        _verify_active_apply(store, intent)
        completed = replace(intent, status="applied", completedAt=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
        _persist_apply_journal(store, completed)
        return ApplyResult("applied", job_id, intent.intendedRunId, intent.exportId,
                           intent.intendedManifestSetId)
    if (intent.intendedRunId == "" or guard.authorizationId == ""):
        raise ValueError("apply intent or source guard is malformed")
    recheck_committed_source(store, guard)
    verify_approval_binding(store, job)
    project = store.read_project()
    if project is None:
        raise ValueError("project configuration is missing")
    state = store.read_state()
    if state is None:
        raise ValueError("project state is missing")
    run = store.read_run(intent.intendedRunId)
    if run is None:
        raise ValueError("intended immutable run is missing; response recovery is required")
    if run.applicationAuthorizationId != guard.authorizationId:
        raise ValueError("intended run authorization differs from source guard")
    identity = RepositoryIdentity(run.snapshot.repositoryId, store.root, "head", job.targetCommit,
                                  run.snapshot.branch, False, {})
    revision = effective_policy_revision(project.scanPolicy, project.claimPolicy, project.targets,
                                         generation_options={})
    exclusions = tuple(sorted({project.stateDir, *(target.livePath for target in project.targets)}))
    _set, manifests = _active_manifests(store, state)
    ownership = _ownership(manifests)
    if intent.exportId is None:
        if (state.currentRunId != intent.intendedRunId
                or state.activeStageRevision != intent.intendedStageRevision):
            raise ValueError("stage activation does not match the apply intent")
        from .ownership import export_staged, manifest_set_id_for_export
        selected_specs = tuple(spec for spec in project.targets if spec.kind in set(job.targetKinds))
        selected_ids = tuple(spec.targetId for spec in selected_specs)
        ownership = {key: value for key, value in ownership.items() if key in selected_ids}
        def bind_export(export_journal: ExportJournal) -> None:
            projected = _guarded_live_projection(export_journal, manifests)
            _persist_apply_journal(store, ApplyJournal(
                job.jobId, "export-prepared", job.expectedManifestSetId, dict(job.expectedOutputHashes),
                intent.intendedRunId, intent.intendedStageRevision,
                manifest_set_id_for_export(export_journal.exportId), projected, export_journal.exportId, None,
            ))
        from .maintenance_approval import verify_job_fingerprints
        with store.mutation():
            verify_approval_binding(store, job)
            verify_job_fingerprints(store, job)
            result = export_staged(
                run.runId, selected_ids, ownership, store=store, current_identity=identity,
                current_root_label=project.rootLabel, current_policy_revision=revision,
                current_extractor_versions=maintenance_extractor_versions(), excluded_paths=exclusions,
                source_guard=guard, on_prepared_export=bind_export,
            )
        bound = _read_apply_journal(store, job_id)
        if bound is None or bound.exportId is None:
            raise ValueError("recovered export did not persist its apply binding")
        if result.status != "complete":
            return ApplyResult("conflict" if result.status == "conflict" else "incomplete", job_id,
                               intent.intendedRunId, result.exportId, bound.intendedManifestSetId,
                               "export did not complete")
        exported = replace(bound, status="exported")
        _persist_apply_journal(store, exported)
        status = _finalize(store, job, exported, guard)
        return ApplyResult(status, job_id, intent.intendedRunId, result.exportId,
                           exported.intendedManifestSetId)
    if intent.intendedManifestSetId is None:
        raise ValueError("bound apply intent is missing its intended manifest-set ID")
    journal = store.read_export_journal(f"exports/{intent.exportId}.json")
    if journal is None:
        raise ValueError("bound export journal is missing; explicit repair is required")
    if journal.runId != intent.intendedRunId or journal.manifestSetId not in {None, intent.intendedManifestSetId}:
        raise ValueError("export journal fingerprints differ from apply intent")
    if state.activeExportId == intent.exportId and state.activeManifestSetId == intent.intendedManifestSetId:
        recheck_committed_source(store, guard)
        _check_projection(store, intent)
        updated = replace(intent, status="exported")
        status = _finalize(store, job, updated, guard)
        return ApplyResult(status, job_id, intent.intendedRunId, intent.exportId,
                           intent.intendedManifestSetId)
    with store.mutation():
        result = resume_export(
            intent.exportId, ownership, store=store, current_identity=identity,
            current_root_label=project.rootLabel, current_policy_revision=revision,
            current_extractor_versions=maintenance_extractor_versions(),
            excluded_paths=exclusions, source_guard=guard,
        )
    if result.status != "complete":
        return ApplyResult("conflict" if result.status == "conflict" else "incomplete", job_id,
                           intent.intendedRunId, intent.exportId, intent.intendedManifestSetId,
                           "export did not complete")
    updated = replace(intent, status="exported")
    _persist_apply_journal(store, updated)
    status = _finalize(store, job, updated, guard)
    return ApplyResult(status, job_id, intent.intendedRunId, intent.exportId,
                       intent.intendedManifestSetId)
