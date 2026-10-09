"""Shared local scan and generation orchestration."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import uuid

from . import SCHEMA_VERSION, __version__
from .claims import apply_review_dispositions, derive_claims
from .dependencies import build_snapshot
from .dedup import deduplicate
from .extractors.base import ExtractionSession
from .extractors.registry import ecosystem_extractors, run_extractors
from .extractors.docs import DocumentationExtractor
from .extractors.generic import GenericExtractor
from .identity import capture_repository
from .ids import CanonicalIdContext, effective_policy_revision
from .inventory import INVENTORY_VERSION, IgnoreControlObservation, collect_inventory
from .models import (
    Claim, ProjectDocument, ProjectState, RepositoryIdentity,
    RunLedger, ScanPolicy, TargetSpec,
)
from .render import render_targets
from .validation import validate_claims


@dataclass(frozen=True, slots=True)
class ScanProducts:
    run: RunLedger
    identity: RepositoryIdentity
    policy_revision: str
    extractor_versions: dict[str, str]
    excluded_paths: tuple[str, ...]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def is_global_cap(item) -> bool:
    reason = item.reasonCode
    return reason in {"open-budget", "file-cap", "decoded-budget", "snippet-budget"} or (
        reason.startswith("inventory.") and ("cap" in reason or "capped" in reason)
    )


def extractor_versions() -> dict[str, str]:
    # Read version metadata from the concrete registered implementations.
    from .extractors.go import GoExtractor
    from .extractors.javascript import JavaScriptExtractor
    from .extractors.python import PythonExtractor
    from .extractors.rust import RustExtractor

    extractors = (GoExtractor, JavaScriptExtractor, PythonExtractor, RustExtractor,
                  GenericExtractor, DocumentationExtractor)
    return {"inventory": INVENTORY_VERSION, **{item.extractor_id: item.version for item in extractors}}


def effective_policy(project: ProjectDocument, scan_policy: ScanPolicy | None = None) -> str:
    return effective_policy_revision(
        scan_policy or project.scanPolicy, project.claimPolicy, project.targets,
        generation_options={},
    )


def collect_scan(
    root: Path,
    project: ProjectDocument,
    *,
    git_mode: str | None = None,
    include: Sequence[str] = (),
    exclude: Sequence[str] = (),
    prior_runs: Sequence[RunLedger] = (),
    id_context: CanonicalIdContext | None = None,
) -> ScanProducts:
    """Capture identity, inventory, reads, evidence, and facts as one completed run."""
    context = id_context or CanonicalIdContext()
    scan_policy = project.scanPolicy
    if include or exclude:
        scan_policy = replace(
            scan_policy,
            include=tuple(dict.fromkeys((*scan_policy.include, *include))),
            exclude=tuple(dict.fromkeys((*scan_policy.exclude, *exclude))),
        )
    state_path = project.stateDir
    exclusions = tuple(sorted({state_path, *(target.livePath for target in project.targets)}))
    controls: list[IgnoreControlObservation] = []
    inventory = collect_inventory(
        root, scan_policy, excluded_paths=exclusions, control_sink=controls.append,
    )
    identity = capture_repository(
        root, inventory, git_mode or project.gitMode, project_id=project.projectId,
        policy=scan_policy, excluded_paths=exclusions,
    )
    snapshot_id = context.record_id("snapshot", {"repository": identity.repositoryId, "nonce": str(uuid.uuid4())})
    prior_evidence = tuple(item for prior in prior_runs if prior.snapshot.repositoryId == identity.repositoryId for item in prior.evidence)
    prior_facts = tuple(item for prior in prior_runs if prior.snapshot.repositoryId == identity.repositoryId for item in prior.facts)
    with ExtractionSession(
        root=root, snapshot_id=snapshot_id, inventory=inventory, policy=scan_policy,
        repository_id=identity.repositoryId, prior_evidence=prior_evidence,
        prior_facts=prior_facts, excluded_paths=exclusions,
        control_observations=tuple(controls), id_context=context,
    ) as session:
        extractors = (*ecosystem_extractors(session), GenericExtractor(session), DocumentationExtractor(session))
        versions = extractor_versions()
        batch = run_extractors(session, extractors)
    policy_revision = effective_policy(project, scan_policy)
    snapshot = build_snapshot(
        snapshot_id, identity, inventory, versions, batch.reads,
        policy_revision=policy_revision, root_label=project.rootLabel,
    )
    run_id = context.record_id("run", {"snapshot": snapshot_id, "nonce": str(uuid.uuid4())})
    diagnostics = tuple((*inventory.diagnostics, *batch.diagnostics))
    coverage = tuple((*inventory.coverage, *batch.coverage))
    run = RunLedger(
        SCHEMA_VERSION, __version__, run_id, snapshot, scan_policy, project.claimPolicy,
        project.targets, batch.reads, batch.evidence, batch.facts, (), (), diagnostics,
        coverage, {}, True,
    )
    return ScanProducts(run, identity, policy_revision, versions, exclusions)


def coverage_is_unusable(run: RunLedger, *, strict: bool) -> bool:
    gaps = tuple(item for item in run.coverage if item.status not in {"supported", "generated"})
    return bool(
        (strict and gaps)
        or any(item.selector.required for item in gaps)
        or any(item.status == "capped" and is_global_cap(item) for item in gaps)
    )


def generate_run(
    scan_run: RunLedger,
    *,
    state: ProjectState,
    prior_runs: Sequence[RunLedger],
    selected_specs: Sequence[TargetSpec],
    project: ProjectDocument,
    id_context: CanonicalIdContext | None = None,
) -> tuple[RunLedger, object]:
    """Derive, validate, overlay, deduplicate, and render candidates for staging."""
    context = id_context or CanonicalIdContext()
    prior_claims = tuple(
        claim for prior in prior_runs
        if prior.snapshot.repositoryId == scan_run.snapshot.repositoryId
        for claim in prior.claims
    )
    candidate_set = derive_claims(
        scan_run.facts, scan_run.effectiveClaimPolicy,
        repository_id=scan_run.snapshot.repositoryId, prior_claims=prior_claims,
        id_context=context,
    )
    draft = replace(scan_run, claims=candidate_set.claims)
    validation = validate_claims(candidate_set.claims, draft)
    valid = tuple(
        replace(claim, proposalStatus="accepted")
        if claim.kind == "observed" and draft.effectiveClaimPolicy.autoAcceptObserved
        else claim
        for claim in validation.validClaims
    )
    effective_valid = apply_review_dispositions(valid, state)
    status_by_ref = {
        (claim.claimId, claim.claimRevision): claim.proposalStatus
        for claim in effective_valid
    }
    candidates = tuple(
        replace(claim, proposalStatus=status_by_ref.get((claim.claimId, claim.claimRevision), "pending"))
        for claim in candidate_set.claims
    )
    documentation = tuple(fact for fact in scan_run.facts if fact.factType == "documentation.line")
    render_eligible = tuple(
        claim for claim in effective_valid if claim.proposalStatus in {"pending", "accepted"}
    )
    deduped = deduplicate(render_eligible, documentation, scan_run.snapshot, id_context=context)
    bundle = render_targets(
        deduped.renderableClaims, selected_specs,
        policy=scan_run.effectiveClaimPolicy, project_id=project.projectId,
        id_context=context,
    )
    staged_targets = {
        target.targetId: {
            "accepted": dict(target.exactHashes),
            "preview": dict(target.previewHashes),
        }
        for target in bundle.targets
    }
    run = replace(
        scan_run,
        runId=context.record_id("run", {"snapshot": scan_run.snapshot.snapshotId, "generationNonce": str(uuid.uuid4())}),
        claims=candidates, dedupDecisions=deduped.decisions,
        diagnostics=tuple((*scan_run.diagnostics, *candidate_set.diagnostics,
                           *validation.diagnostics, *deduped.diagnostics,
                           *(diagnostic for target in bundle.targets for diagnostic in target.diagnostics))),
        stagedTargets=staged_targets,
    )
    return run, bundle


def current_repository_context(
    root: Path, project: ProjectDocument, run: RunLedger,
) -> tuple[RepositoryIdentity, str, dict[str, str], tuple[str, ...]]:
    """Capture current identity and explicit R5 context for an existing run."""
    exclusions = tuple(sorted({project.stateDir, *(item.livePath for item in run.configuredTargets),
                               *(item.livePath for item in project.targets)}))
    inventory = collect_inventory(root, run.effectiveScanPolicy, excluded_paths=exclusions)
    identity = capture_repository(
        root, inventory, run.snapshot.gitMode, project_id=project.projectId,
        policy=run.effectiveScanPolicy, excluded_paths=exclusions,
    )
    return identity, effective_policy(project, run.effectiveScanPolicy), extractor_versions(), exclusions
