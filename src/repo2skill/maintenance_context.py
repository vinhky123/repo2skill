"""Committed-source context, prose claims, and maintenance run construction."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unicodedata

from . import SCHEMA_VERSION, __version__
from .claims import derive_claims
from .dependencies import build_snapshot
from .dedup import deduplicate
from .extractors.base import ExtractionSession
from .extractors.docs import DocumentationExtractor
from .extractors.generic import GenericExtractor
from .extractors.registry import ecosystem_extractors, run_extractors
from .ids import canonical_json_bytes, canonical_record_id
from .inventory import IgnoreControlObservation
from .maintenance_models import (
    ApplicationAuthorization, ClaimOperation, CommittedSourceGuard, MaintenanceJob,
)
from .maintenance_source_fragment import (
    EXTRACTOR_ID, SourceFragmentExtractor, maintenance_extractor_versions,
)
from .models import (
    Claim, CoverageEntry, Diagnostic, Evidence, ExtractionRead, Fact, InputSelector,
    InventoryEntry, InventoryResult, RenderBundle, RunLedger, SourceSnapshot,
    TargetSpec,
)
from .render import render_targets
from .validation import validate_claims


@dataclass(frozen=True, slots=True)
class MaintenanceContext:
    job: MaintenanceJob
    snapshot: SourceSnapshot
    facts: tuple[Fact, ...]
    evidence: tuple[Evidence, ...]
    priorClaims: tuple[Claim, ...]
    changedDiff: bytes
    sourceContextHash: str
    reads: tuple[ExtractionRead, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    coverage: tuple[CoverageEntry, ...] = ()
    inventory: InventoryResult | None = None
    scanPolicy: object | None = None
    claimPolicy: object | None = None
    configuredTargets: tuple[TargetSpec, ...] = ()
    priorFacts: tuple[Fact, ...] = ()
    priorEvidence: tuple[Evidence, ...] = ()


def build_agent_request(context: MaintenanceContext, snapshot_root: Path, *, source_root: Path):
    """Frame the complete bounded committed context as an exact structured stdin prompt."""
    import base64
    from .maintenance_agent import AgentRequest, PROMPT_MAX_BYTES, SCHEMA_MAX_BYTES, request_hash

    schema = {
        "type": "object", "additionalProperties": False,
        "required": ["jobId", "snapshotId", "operations"],
        "properties": {
            "jobId": {"type": "string"}, "snapshotId": {"type": "string"},
            "operations": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["op", "semanticKey", "scope", "renderedText", "factIds", "evidenceIds", "claimRef"],
                "properties": {
                    "op": {"enum": ["add", "update", "delete"]},
                    "semanticKey": {"type": "string"}, "scope": {"type": "string"},
                    "renderedText": {"type": ["string", "null"]},
                    "factIds": {"type": "array", "items": {"type": "string"}},
                    "evidenceIds": {"type": "array", "items": {"type": "string"}},
                    "claimRef": {"type": ["string", "null"]},
                },
            }},
        },
    }
    schema_bytes = canonical_json_bytes(schema)
    prompt = {
        "task": "Propose only evidence-backed maintenance claim operations. Use exact fact/evidence IDs and scope from this committed context.",
        "jobId": context.job.jobId,
        "snapshotId": context.snapshot.snapshotId,
        "sourceContextHash": context.sourceContextHash,
        "changedDiffBase64": base64.b64encode(context.changedDiff).decode("ascii"),
        "facts": context.facts,
        "evidence": context.evidence,
        "priorClaims": context.priorClaims,
    }
    prompt_bytes = canonical_json_bytes(prompt)
    if len(prompt_bytes) > PROMPT_MAX_BYTES:
        raise ValueError("maintenance agent prompt exceeds 1 MiB; context must be rebuilt within configured bounds")
    if len(schema_bytes) > SCHEMA_MAX_BYTES:
        raise ValueError("maintenance agent schema exceeds 128 KiB")
    return AgentRequest(context.job.jobId, Path(snapshot_root), prompt_bytes, schema_bytes,
                        request_hash(context.job.jobId, prompt_bytes, schema_bytes), Path(source_root))


@dataclass(frozen=True, slots=True)
class MaintenanceRunBundle:
    run: RunLedger
    selectedSpecs: tuple[TargetSpec, ...]
    rendered: RenderBundle
    authorization: ApplicationAuthorization


def _hash(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _context_manifest(store, job: MaintenanceJob) -> dict[str, object]:
    context_id = canonical_record_id("source-context", {"contextHash": job.sourceContextHash})
    payload = store.read_immutable_bytes(f"maintenance/contexts/{context_id}.json")
    if payload is None:
        raise ValueError("saved committed source context is missing")
    try:
        envelope = json.loads(payload.decode("utf-8"))
        manifest = envelope["manifest"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise ValueError("saved committed source context is malformed") from error
    if not isinstance(manifest, dict) or manifest.get("contextHash") != job.sourceContextHash:
        raise ValueError("saved committed source context hash differs from job binding")
    if manifest.get("commit") != job.targetCommit:
        raise ValueError("saved committed source context commit differs from job binding")
    if "impactProjection" in manifest and manifest["impactProjection"] != "tree-bootstrap":
        raise ValueError("saved committed source context projection is unknown")
    return manifest


def _validate_manifest_versions(manifest: dict[str, object], job: MaintenanceJob) -> dict[str, str]:
    versions = maintenance_extractor_versions()
    if manifest.get("policyRevision") != job.policyRevision:
        raise ValueError("saved committed context policy differs from job binding")
    saved_versions = manifest.get("extractorRevisions")
    if not isinstance(saved_versions, dict) or saved_versions != versions:
        raise ValueError("saved committed context extractor versions are stale; enqueue a fresh job")
    return versions


def _inventory_from_manifest(root: Path, manifest: dict[str, object], policy, excluded: Sequence[str]) -> InventoryResult:
    from .inventory import _final_exclusion, _generated_or_security, _normalize_exclude
    from pathspec import GitIgnoreSpec

    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("saved context file list is malformed")
    exclude_spec = GitIgnoreSpec.from_lines(
        (_normalize_exclude(item) for item in policy.exclude), backend="simple",
    )
    entries = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("saved context file entry is malformed")
        path, size, raw_hash = item.get("path"), item.get("size"), item.get("rawHash")
        if not isinstance(path, str) or not isinstance(size, int) or not isinstance(raw_hash, str):
            raise ValueError("saved context file metadata is malformed")
        if (_final_exclusion(path, False, tuple(excluded), exclude_spec) is not None
                or _generated_or_security(path, False) is not None):
            raise ValueError("saved context contains a path excluded by current P0 eligibility")
        entries.append(InventoryEntry(path, "file", size, raw_hash, False, False, None))
    entries.sort(key=lambda item: item.path)
    if len({item.path for item in entries}) != len(entries):
        raise ValueError("saved context contains duplicate paths")
    diagnostics = ()
    coverage = ()
    if len(entries) > policy.maxInventoryEntries:
        omitted = len(entries) - policy.maxInventoryEntries
        entries = entries[:policy.maxInventoryEntries]
        diagnostics = (Diagnostic(
            1, "inventory.entry-cap", "error", "inventory entry cap was reached", None,
            "inventory", {"limit": policy.maxInventoryEntries, "omittedCount": omitted},
        ),)
        coverage = (CoverageEntry(
            1, InputSelector("negative", "inventory.entry-cap", ".", True),
            "inventory", "capped", "inventory.entry-cap", ("inventory.entry-cap",),
        ),)
    projection = [{"path": item.path, "kind": item.kind, "sizeBytes": item.sizeBytes,
                   "contentHash": item.contentHash, "ignored": item.ignored,
                   "generated": item.generated, "symlinkTarget": item.symlinkTarget}
                  for item in entries]
    inventory_hash = _hash(canonical_json_bytes(projection))
    return InventoryResult(root, tuple(entries), inventory_hash, diagnostics, coverage)


def require_complete_inventory(inventory: InventoryResult) -> None:
    """Stop before extraction when committed input exceeded P0 inventory coverage."""
    capped = next((item for item in inventory.coverage
                   if item.status in {"capped", "failed"} and item.collector == "inventory"), None)
    if capped is not None:
        diagnostic = next((item for item in inventory.diagnostics if item.code == capped.reasonCode), None)
        detail = diagnostic.details if diagnostic is not None else {}
        raise ValueError(f"{capped.reasonCode}: committed inventory coverage is incomplete ({detail})")


def require_complete_source_fragments(reads: Sequence[ExtractionRead],
                                     coverage: Sequence[CoverageEntry]) -> None:
    """Reject capped or failed eligible blob reads before maintenance analysis."""
    failed = tuple(read for read in reads
                   if read.extractorId == EXTRACTOR_ID and read.outcome != "present")
    incomplete = tuple(item for item in coverage
                       if item.collector == EXTRACTOR_ID and item.status in {"capped", "failed"})
    if failed or incomplete:
        details = tuple((item.selector.value, item.outcome) for item in failed)
        reasons = tuple(item.reasonCode for item in incomplete)
        raise ValueError(
            "maintenance.source-fragment-incomplete: eligible committed source reads are incomplete "
            f"(reads={details}, coverage={reasons})"
        )


def _same_fact_support(prior: Fact, current: Fact) -> bool:
    return (
        prior.factId == current.factId
        and prior.factRevision == current.factRevision
        and prior.factType == current.factType
        and prior.semanticKey == current.semanticKey
        and prior.scope == current.scope
        and prior.value == current.value
        and prior.derivation == current.derivation
        and prior.ruleId == current.ruleId
        and prior.ruleVersion == current.ruleVersion
        and prior.evidenceIds == current.evidenceIds
    )


def _same_evidence_support(prior: Evidence, current: Evidence) -> bool:
    return (
        prior.evidenceId == current.evidenceId
        and prior.revision == current.revision
        and prior.path == current.path
        and prior.contentHash == current.contentHash
        and prior.semanticKey == current.semanticKey
        and prior.mediaType == current.mediaType
        and prior.encoding == current.encoding
        and prior.locator == current.locator
        and prior.redactedPreview == current.redactedPreview
        and prior.availability == current.availability
    )


def _prior_prose_support_unchanged(prior: Claim, context: MaintenanceContext) -> bool:
    prior_facts = {fact.factId: fact for fact in context.priorFacts}
    current_facts = {fact.factId: fact for fact in context.facts}
    prior_evidence = {item.evidenceId: item for item in context.priorEvidence}
    current_evidence = {item.evidenceId: item for item in context.evidence}
    if not prior.factIds or not prior.evidenceIds:
        return False
    old_fact_records = [prior_facts.get(fact_id) for fact_id in prior.factIds]
    new_fact_records = [current_facts.get(fact_id) for fact_id in prior.factIds]
    if any(old is None or new is None for old, new in zip(old_fact_records, new_fact_records)):
        return False
    if any(not _same_fact_support(old, new) for old, new in zip(old_fact_records, new_fact_records)):
        return False
    old_evidence_ids = tuple(sorted({item for fact in old_fact_records for item in fact.evidenceIds}))
    new_evidence_ids = tuple(sorted({item for fact in new_fact_records for item in fact.evidenceIds}))
    if old_evidence_ids != prior.evidenceIds or new_evidence_ids != prior.evidenceIds:
        return False
    for evidence_id in prior.evidenceIds:
        old = prior_evidence.get(evidence_id)
        new = current_evidence.get(evidence_id)
        if old is None or new is None or not _same_evidence_support(old, new):
            return False
    return True


def _control_observations(paths: Sequence[str], reader, policy) -> tuple[IgnoreControlObservation, ...]:
    controls = sorted(path for path in paths if path.rsplit("/", 1)[-1] == ".gitignore")
    observations = []
    total = 0
    for path in controls:
        if len(observations) >= policy.maxOpenedFiles:
            raise ValueError("committed ignore controls exceed opened-file cap")
        raw = reader(path, policy.maxFileBytes)
        if len(raw) > policy.maxFileBytes or total + len(raw) > policy.maxDecodedBytes:
            raise ValueError("committed ignore controls exceed source byte caps")
        total += len(raw)
        # P0's captured ignore controls are UTF-8; preserve exact hash and budget.
        raw.decode("utf-8-sig")
        observations.append(IgnoreControlObservation(
            InputSelector("file", path, ".", True), "present", _hash(raw), (), None, 1, len(raw),
        ))
    aggregate_paths = tuple(controls)
    aggregate_hash = "sha256:" + hashlib.sha256(canonical_json_bytes(list(aggregate_paths))).hexdigest()
    aggregate = IgnoreControlObservation(
        InputSelector("glob", "**/.gitignore", ".", True),
        "matched" if controls else "no-match", None, aggregate_paths,
        aggregate_hash, 0, 0,
    )
    return (aggregate, *observations)


def build_committed_context(store, job: MaintenanceJob) -> MaintenanceContext:
    """Build extraction facts exclusively from the immutable target Git tree."""
    from .maintenance_approval import verify_approval_binding
    from .maintenance_git import (
        MAX_CONTEXT_FILES, MAX_CONTEXT_TOTAL_BYTES, GitReadBudget,
        _git, committed_change_statuses, materialize_commit, read_commit_blob,
    )
    from .maintenance_store import MaintenanceStore
    from .models import RepositoryIdentity
    from .ids import effective_policy_revision

    if not store._mutation_active:
        with store.mutation():
            return build_committed_context(store, job)
    maint = MaintenanceStore(store.root, store)
    authoritative_job = maint.read_job(job.jobId)
    if authoritative_job != job:
        raise ValueError("maintenance job is stale or differs from the immutable job")
    verify_approval_binding(store, job)
    project = store.read_project()
    state = store.read_state()
    if project is None or state is None:
        raise ValueError("initialized project and state are required")
    policy_revision = effective_policy_revision(project.scanPolicy, project.claimPolicy,
                                                project.targets, generation_options={})
    if policy_revision != job.policyRevision:
        raise ValueError("maintenance policy changed; enqueue a fresh job")
    manifest = _context_manifest(store, job)
    versions = _validate_manifest_versions(manifest, job)
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > MAX_CONTEXT_FILES:
        raise ValueError("saved source context is malformed or exceeds file cap")
    omitted_paths = manifest.get("omittedPaths", ())
    if not isinstance(omitted_paths, list):
        raise ValueError("saved source context omitted-path list is malformed")
    paths = tuple(sorted(item["path"] for item in files if isinstance(item, dict) and isinstance(item.get("path"), str)))
    if len(paths) != len(files):
        raise ValueError("saved source context path list is malformed")
    total = sum(item["size"] for item in files if isinstance(item, dict) and isinstance(item.get("size"), int))
    if total > MAX_CONTEXT_TOTAL_BYTES:
        raise ValueError("saved source context exceeds byte cap")
    prior_run = store.read_run(state.currentRunId) if state.currentRunId else None
    budget = GitReadBudget(timeout_seconds=30.0, max_output_bytes=MAX_CONTEXT_TOTAL_BYTES * 3)
    if prior_run is not None:
        repository_id = prior_run.snapshot.repositoryId
        prior_claims, prior_facts, prior_evidence = prior_run.claims, prior_run.facts, prior_run.evidence
    else:
        # Explicit bootstrap has no P0 run yet. Derive the same stable repository
        # identity from Git metadata; source bytes below still come only from the
        # named committed tree.
        top = Path(_git(store.root, ("rev-parse", "--path-format=absolute", "--show-toplevel"), 4096, budget).decode("utf-8").strip())
        common = Path(_git(store.root, ("rev-parse", "--path-format=absolute", "--git-common-dir"), 4096, budget).decode("utf-8").strip())
        common_text = os.path.abspath(common).replace("\\", "/")
        if os.name == "nt":
            common_text = os.path.normcase(common_text)
        common_text = unicodedata.normalize("NFC", common_text)
        scan_root = store.root.relative_to(top).as_posix() or "."
        repository_id = canonical_record_id("repo", {
            "kind": "git", "commonDir": common_text,
            "scanRoot": unicodedata.normalize("NFC", scan_root),
        })
        prior_claims, prior_facts, prior_evidence = (), (), ()
    snapshot_id = canonical_record_id("snapshot", {
        "repository": repository_id, "maintenanceJob": job.jobId,
        "targetCommit": job.targetCommit, "sourceContextHash": job.sourceContextHash,
        "policyRevision": job.policyRevision,
        "extractorRevisions": dict(sorted(maintenance_extractor_versions().items())),
    })
    current_head = _git(store.root, ("rev-parse", "--verify", "HEAD^{commit}"), 256, budget).strip().decode("ascii")
    if current_head != job.targetCommit:
        raise ValueError("current committed HEAD differs from maintenance target")
    impact_projection = manifest.get("impactProjection")
    change_statuses = committed_change_statuses(
        store.root, job.baselineCommit, job.targetCommit, job.diffHash, job.changedPaths,
        budget=budget, impact_projection=impact_projection,
    )
    for omitted in omitted_paths:
        try:
            read_commit_blob(store.root, job.targetCommit, omitted, MAX_CONTEXT_TOTAL_BYTES, budget=budget)
        except Exception as error:
            if getattr(error, "code", None) == "maintenance.git-object-missing":
                continue
            raise ValueError("saved source context omitted an eligible committed file; rebuild the job") from error
        raise ValueError("saved source context omitted an eligible committed file; rebuild the job")
    def read_blob(path: str, cap: int) -> bytes:
        if path not in paths:
            raise ValueError("committed source read is outside saved context")
        return read_commit_blob(store.root, job.targetCommit, path, cap, budget=budget)

    temp_context = tempfile.TemporaryDirectory(prefix="repo2skill-maintenance-")
    try:
        root = Path(temp_context.name)
        rebuilt = materialize_commit(
            store.root, job.targetCommit, tuple(sorted(set(paths) | set(omitted_paths))), root,
            policy_revision=job.policyRevision, extractor_revisions=versions, budget=budget,
            impact_projection=impact_projection,
        )
        if rebuilt.contextHash != job.sourceContextHash:
            raise ValueError("target commit no longer matches the saved source context")
        exclusions = tuple(sorted({project.stateDir, "AGENTS.md",
                                   *(target.livePath for target in project.targets)}))
        inventory = _inventory_from_manifest(root, manifest, project.scanPolicy, exclusions)
        require_complete_inventory(inventory)
        controls = _control_observations(paths, read_blob, project.scanPolicy)
        identity = RepositoryIdentity(repository_id, root, "head", job.targetCommit, None, False, {})
        with ExtractionSession(
            root=root, snapshot_id=snapshot_id, inventory=inventory, policy=project.scanPolicy,
            repository_id=repository_id, prior_evidence=prior_evidence, prior_facts=prior_facts,
            excluded_paths=exclusions, control_observations=controls,
            committed_reader=read_blob,
        ) as session:
            extractors = (*ecosystem_extractors(session), GenericExtractor(session),
                          DocumentationExtractor(session), SourceFragmentExtractor(session))
            batch = run_extractors(session, extractors)
        require_complete_source_fragments(batch.reads, batch.coverage)
        snapshot = build_snapshot(
            snapshot_id, identity, inventory, versions, batch.reads,
            policy_revision=policy_revision, root_label=project.rootLabel,
        )
        changed_diff = canonical_json_bytes({
            "projection": "bootstrap-inventory" if job.baselineCommit is None or impact_projection == "tree-bootstrap" else "committed-change-status",
            "diffHash": job.diffHash,
            "changedPaths": list(job.changedPaths),
            "changeStatuses": [[status, path] for status, path in change_statuses],
            "targetCommit": job.targetCommit,
        })
        return MaintenanceContext(
            job, snapshot, batch.facts, batch.evidence, prior_claims,
            changed_diff, job.sourceContextHash, batch.reads,
            tuple((*inventory.diagnostics, *batch.diagnostics)),
            tuple((*inventory.coverage, *batch.coverage)), inventory,
            project.scanPolicy, project.claimPolicy, project.targets,
            prior_facts, prior_evidence,
        )
    finally:
        temp_context.cleanup()


def _claim_id(repository_id: str, operation: ClaimOperation) -> str:
    return canonical_record_id("claim", {"repository": repository_id, "scope": operation.scope,
                                          "recordType": "claim", "semanticKey": operation.semanticKey})


def build_prose_claim(operation: ClaimOperation, context: MaintenanceContext) -> Claim:
    """Build a pending prose claim after checking the operation's exact support union."""
    if operation.op == "delete":
        raise ValueError("delete operations do not create claims")
    text = operation.renderedText
    if not isinstance(text, str) or not text.strip():
        raise ValueError("prose claim text must be nonempty")
    facts = {fact.factId: fact for fact in context.facts}
    evidence = {item.evidenceId: item for item in context.evidence}
    if not operation.factIds or tuple(sorted(set(operation.factIds))) != operation.factIds:
        raise ValueError("claim operation fact IDs must be sorted, unique, and nonempty")
    selected = [facts.get(item) for item in operation.factIds]
    if any(item is None for item in selected):
        raise ValueError("claim operation refers to an unknown source fact")
    if any(item.snapshotId != context.snapshot.snapshotId or item.scope != operation.scope
           or item.factType != "source.fragment.v1" for item in selected):
        raise ValueError("claim operation support is outside the committed source context")
    expected_evidence = tuple(sorted({evidence_id for fact in selected for evidence_id in fact.evidenceIds}))
    if operation.evidenceIds != expected_evidence:
        raise ValueError("claim operation evidence IDs must equal its exact fact evidence union")
    if any(item not in evidence or evidence[item].snapshotId != context.snapshot.snapshotId
           for item in expected_evidence):
        raise ValueError("claim operation refers to evidence outside the committed snapshot")
    claim_id = _claim_id(context.snapshot.repositoryId, operation)
    prior = next((item for item in context.priorClaims if item.claimId == claim_id), None)
    supersedes: tuple[str, ...] = ()
    revision = 1
    if operation.op == "update":
        if prior is None or operation.claimRef != f"{prior.claimId}@{prior.claimRevision}":
            raise ValueError("update operation does not reference the current claim revision")
        revision = prior.claimRevision + 1
        supersedes = (f"{prior.claimId}@{prior.claimRevision}",)
    elif prior is not None:
        raise ValueError("add operation duplicates an existing semantic claim")
    return Claim(
        1, claim_id, revision, operation.semanticKey, operation.scope,
        {"text": text}, text, None, "inferred", "llm", operation.factIds,
        operation.evidenceIds, "pending", supersedes, (), context.snapshot.snapshotId,
        "evidence-backed-prose",
    )


def authorize_claim_revisions(job: MaintenanceJob, approval_binding_hash: str,
                              response_hash: str, claims: Sequence[Claim]) -> ApplicationAuthorization:
    refs = tuple(sorted(f"{claim.claimId}@{claim.claimRevision}" for claim in claims
                        if claim.kind == "inferred" and claim.origin == "llm"))
    identity = {"jobId": job.jobId, "approvalBindingHash": approval_binding_hash,
                "responseHash": response_hash, "acceptedClaimRefs": list(refs),
                "targetKinds": list(job.targetKinds)}
    auth_id = canonical_record_id("application-auth", identity)
    return ApplicationAuthorization(auth_id, job.jobId, approval_binding_hash,
                                    response_hash, refs, job.targetKinds)


def _persist_authorization(store, authorization: ApplicationAuthorization) -> None:
    from .ledger import encode_record
    relative = f"maintenance/authorizations/{authorization.authorizationId}.json"
    payload = canonical_json_bytes({"schemaVersion": "1.0", "authorization": encode_record(authorization)}) + b"\n"
    if store._mutation_active:
        existing = store.read_immutable_bytes(relative)
        if existing is None:
            store.write_immutable_bytes(relative, payload)
        elif existing != payload:
            raise ValueError("persisted application authorization ID collides with different content")
    else:
        with store.mutation():
            existing = store.read_immutable_bytes(relative)
            if existing is None:
                store.write_immutable_bytes(relative, payload)
            elif existing != payload:
                raise ValueError("persisted application authorization ID collides with different content")


def _parse_claim_ref(value: str | None) -> tuple[str, int]:
    if not isinstance(value, str) or "@" not in value:
        raise ValueError("claim operation requires a claimId@revision reference")
    claim_id, raw_revision = value.rsplit("@", 1)
    try:
        revision = int(raw_revision)
    except ValueError as error:
        raise ValueError("claim operation has an invalid claim revision reference") from error
    if revision < 1 or f"{claim_id}@{revision}" != value:
        raise ValueError("claim operation has an invalid claim revision reference")
    return claim_id, revision


def build_maintenance_run(store, job: MaintenanceJob, context: MaintenanceContext,
                          operations: tuple[ClaimOperation, ...],
                          authorization: ApplicationAuthorization) -> MaintenanceRunBundle:
    from .maintenance_approval import verify_approval_binding
    from .maintenance_store import MaintenanceStore
    from .models import RunLedger

    if context.job != job or context.sourceContextHash != job.sourceContextHash:
        raise ValueError("committed context differs from the immutable job")
    require_complete_source_fragments(context.reads, context.coverage)
    if context.inventory is not None:
        require_complete_inventory(context.inventory)
    if authorization.jobId != job.jobId or authorization.targetKinds != job.targetKinds:
        raise ValueError("application authorization differs from job binding")
    with store.mutation():
        authoritative = MaintenanceStore(store.root, store).read_job(job.jobId)
        if authoritative != job:
            raise ValueError("maintenance job changed before run construction")
        lifecycle = verify_approval_binding(store, job)
        if lifecycle.approvalBindingHash != authorization.approvalBindingHash:
            raise ValueError("application authorization approval binding is stale")
        _persist_authorization(store, authorization)
        recheck_committed_source(store, CommittedSourceGuard(
            job.jobId, job.targetCommit, job.sourceContextHash,
            job.policyRevision, authorization.authorizationId,
        ))
        project = store.read_project()
        state = store.read_state()
        prior_run = store.read_run(state.currentRunId) if state and state.currentRunId else None
        if project is None or state is None:
            raise ValueError("initialized project and state are required")
        context_claim_policy = context.claimPolicy or project.claimPolicy
        context_scan_policy = context.scanPolicy or project.scanPolicy
        if context.inventory is None:
            raise ValueError("committed context does not include inventory metadata")

        # Re-derive typed claims only from facts whose deterministic templates exist.
        typed_facts = tuple(fact for fact in context.facts if fact.factType != "source.fragment.v1")
        typed_candidates = derive_claims(
            typed_facts, context_claim_policy, repository_id=context.snapshot.repositoryId,
            prior_claims=context.priorClaims,
        ).claims
        candidates: dict[str, Claim] = {claim.claimId: claim for claim in typed_candidates}
        exact_prior: dict[str, Claim] = {claim.claimId: claim for claim in context.priorClaims}
        deleted: set[str] = set()
        operation_keys: set[tuple[object, ...]] = set()
        for operation in operations:
            key = ((operation.op, operation.scope, operation.semanticKey) if operation.op == "add"
                   else (operation.op, operation.claimRef))
            if key in operation_keys:
                raise ValueError("duplicate claim operation reference")
            operation_keys.add(key)
            if operation.op == "delete":
                claim_id, revision = _parse_claim_ref(operation.claimRef)
                prior = exact_prior.get(claim_id)
                if prior is None or prior.claimRevision != revision:
                    raise ValueError("delete operation does not reference an existing claim revision")
                deleted.add(claim_id)
                candidates.pop(claim_id, None)
                continue
            claim = build_prose_claim(operation, context)
            if operation.op == "update":
                _parse_claim_ref(operation.claimRef)
            elif operation.claimRef is not None:
                raise ValueError("add operation cannot carry a claim reference")
            candidates[claim.claimId] = claim

        # A prior prose claim is carried only when every source fact and evidence
        # record is present and the target-snapshot proof chain validates.
        current_fact_ids = {fact.factId for fact in context.facts}
        current_evidence_ids = {item.evidenceId for item in context.evidence}
        for prior in context.priorClaims:
            if prior.claimId in candidates or prior.claimId in deleted or prior.supportMode != "evidence-backed-prose":
                continue
            if (set(prior.factIds).issubset(current_fact_ids)
                    and set(prior.evidenceIds).issubset(current_evidence_ids)
                    and _prior_prose_support_unchanged(prior, context)):
                candidates[prior.claimId] = replace(prior, lastVerifiedSnapshotId=context.snapshot.snapshotId,
                                                    proposalStatus="pending")
        unsupported_prior = tuple(
            prior for prior in context.priorClaims
            if prior.claimId not in candidates and prior.claimId not in deleted
        )

        # Drop unsupported carried claims; the coverage record makes the omission visible.
        draft = RunLedger(
            SCHEMA_VERSION, __version__, canonical_record_id("run", {"snapshot": context.snapshot.snapshotId,
                                                                       "job": job.jobId}),
            context.snapshot, context_scan_policy, context_claim_policy,
            context.configuredTargets or project.targets, context.reads, context.evidence,
            context.facts, tuple(sorted(candidates.values(), key=lambda item: (item.semanticKey, item.claimId))),
            (), context.diagnostics, context.coverage, {}, True, authorization.authorizationId,
        )
        validation = validate_claims(draft.claims, draft)
        valid_by_id = {claim.claimId: claim for claim in validation.validClaims}
        claims = []
        rejected = {claim.claimId for claim in validation.rejectedClaims}
        for claim in draft.claims:
            checked = valid_by_id.get(claim.claimId)
            if checked is None:
                if claim.claimId in exact_prior and claim.claimId not in deleted:
                    continue
                raise ValueError(f"new claim failed proof validation: {claim.claimId}")
            reference = f"{checked.claimId}@{checked.claimRevision}"
            if checked.kind == "inferred" and checked.origin == "llm":
                if reference in authorization.acceptedClaimRefs:
                    checked = replace(checked, proposalStatus="accepted")
                else:
                    checked = replace(checked, proposalStatus="pending")
            claims.append(checked)
        omitted_prior_ids = {prior.claimId for prior in unsupported_prior}
        omitted_prior_ids.update(
            prior.claimId for prior in context.priorClaims
            if prior.claimId in rejected and prior.claimId not in deleted
        )
        omitted_coverage = tuple(
            CoverageEntry(1, InputSelector("glob", "**/*", ".", False),
                          "maintenance.claim-support", "unsupported", "prior-claim-support-unavailable", ())
            for _claim_id in sorted(omitted_prior_ids)
        )
        actual_refs = tuple(sorted(f"{claim.claimId}@{claim.claimRevision}" for claim in claims
                                   if claim.origin == "llm" and claim.kind == "inferred"
                                   and claim.proposalStatus == "accepted"))
        if actual_refs != tuple(sorted(authorization.acceptedClaimRefs)):
            raise ValueError("authorization claim refs do not exactly match validated claim revisions")

        documentation = tuple(fact for fact in context.facts if fact.factType == "documentation.line")
        deduped = deduplicate(tuple(claims), documentation, context.snapshot)
        selected_specs = tuple(target for target in (context.configuredTargets or project.targets)
                               if target.kind in set(job.targetKinds))
        rendered = render_targets(deduped.renderableClaims, selected_specs,
                                  policy=context_claim_policy, project_id=project.projectId)
        staged_targets = {target.targetId: {"accepted": dict(target.exactHashes),
                                            "preview": dict(target.previewHashes)}
                          for target in rendered.targets}
        run = replace(
            draft, claims=tuple(claims), dedupDecisions=deduped.decisions,
            diagnostics=tuple((*draft.diagnostics, *validation.diagnostics, *deduped.diagnostics,
                               *(diagnostic for target in rendered.targets for diagnostic in target.diagnostics))),
            coverage=tuple((*draft.coverage, *omitted_coverage)),
            stagedTargets=staged_targets,
        )
        return MaintenanceRunBundle(run, selected_specs, rendered, authorization)


def recheck_committed_source(store, guard: CommittedSourceGuard) -> None:
    """Revalidate commit, policy, extractor, context, and live job authorization."""
    from .ids import effective_policy_revision
    from .maintenance_approval import verify_approval_binding
    from .maintenance_git import GitReadBudget, _git, _resolve_commit
    from .maintenance_store import MaintenanceStore

    job = MaintenanceStore(store.root, store).read_job(guard.jobId)
    if job is None or job.targetCommit != guard.targetCommit or job.sourceContextHash != guard.sourceContextHash:
        raise ValueError("committed source guard no longer matches immutable job")
    def check_locked() -> None:
        if not MaintenanceStore(store.root, store).read_state().enabled:
            raise ValueError("maintenance is disabled")
        lifecycle = verify_approval_binding(store, job)
        if lifecycle.approvalBindingHash is None:
            raise ValueError("maintenance approval binding is missing")
        project = store.read_project()
        if project is None:
            raise ValueError("project configuration is missing")
        policy_revision = effective_policy_revision(project.scanPolicy, project.claimPolicy,
                                                    project.targets, generation_options={})
        if policy_revision != guard.policyRevision or policy_revision != job.policyRevision:
            raise ValueError("committed source policy changed")
        manifest = _context_manifest(store, job)
        if manifest.get("extractorRevisions") != maintenance_extractor_versions():
            raise ValueError("committed source extractor versions changed")
        authorization_bytes = store.read_immutable_bytes(
            f"maintenance/authorizations/{guard.authorizationId}.json",
        )
        if authorization_bytes is None:
            raise ValueError("committed source authorization is missing")
        try:
            authorization_raw = json.loads(authorization_bytes.decode("utf-8"))
            authorization = authorization_raw["authorization"]
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
            raise ValueError("committed source authorization is malformed") from error
        if (not isinstance(authorization, dict) or authorization.get("authorizationId") != guard.authorizationId
                or authorization.get("jobId") != job.jobId
                or authorization.get("approvalBindingHash") != lifecycle.approvalBindingHash
                or authorization.get("targetKinds") != list(job.targetKinds)):
            raise ValueError("committed source authorization does not match the live job binding")
        refs = authorization.get("acceptedClaimRefs")
        response_hash = authorization.get("responseHash")
        if not isinstance(refs, list) or not isinstance(response_hash, str):
            raise ValueError("committed source authorization fields are malformed")
        expected_authorization_id = canonical_record_id("application-auth", {
            "jobId": job.jobId,
            "approvalBindingHash": lifecycle.approvalBindingHash,
            "responseHash": response_hash,
            "acceptedClaimRefs": refs,
            "targetKinds": list(job.targetKinds),
        })
        if expected_authorization_id != guard.authorizationId:
            raise ValueError("committed source authorization ID is invalid")
        budget = GitReadBudget()
        resolved = _git(store.root, ("rev-parse", "--verify", "HEAD^{commit}"), 256, budget).strip().decode("ascii")
        if resolved != guard.targetCommit:
            raise ValueError("current committed HEAD differs from maintenance target")
        _resolve_commit(store.root, guard.targetCommit, budget)
        if manifest.get("contextHash") != guard.sourceContextHash:
            raise ValueError("committed source context hash changed")

    if store._mutation_active:
        check_locked()
    else:
        with store.mutation():
            check_locked()
