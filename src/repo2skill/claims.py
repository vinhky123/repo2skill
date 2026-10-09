"""Deterministic conversion of approved Fact records into pending claims."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from .claim_templates import format_claim_text, template_key_for
from .dedup import deduplicate
from .dependencies import ActivationFreshnessError, recheck_dependencies
from .ids import CanonicalIdContext, canonical_json_bytes, canonical_record_id, latest_by_id, revise_record
from .ledger import LedgerStore
from .models import (
    Claim, ClaimPolicy, ClaimSet, Fact, ProjectState, RenderBundle, RepositoryIdentity,
    ReviewEvent, RunLedger, StagedRun, TargetSpec,
)
from .render import render_targets
from .stage import publish_stage_revision, read_stage_descriptor
from .validation import validate_claims


def _claim_identity(repository_id: str, scope: str, semantic_key: str) -> dict[str, str]:
    return {"repository": repository_id, "scope": scope,
            "recordType": "claim", "semanticKey": semantic_key}


def derive_claims(
    facts: Sequence[Fact],
    policy: ClaimPolicy,
    *,
    repository_id: str,
    prior_claims: Sequence[Claim] = (),
    id_context: CanonicalIdContext | None = None,
) -> ClaimSet:
    """Build one deterministic, pending candidate for each supported non-documentation fact."""
    if not repository_id:
        raise ValueError("repository_id is required")
    context = id_context if id_context is not None else CanonicalIdContext()
    for item in prior_claims:
        expected_id = context.record_id(
            "claim", _claim_identity(repository_id, item.scope, item.semanticKey),
        )
        if expected_id != item.claimId:
            raise ValueError(f"prior claim ID does not match reconstructed canonical identity: {item.claimId}")
    prior = latest_by_id(
        prior_claims, id_field="claimId", revision_field="claimRevision",
        semantic_payload=lambda item: {"payload": item.payload, "kind": item.kind, "origin": item.origin},
    )
    claims: list[Claim] = []
    fact_ids: set[str] = set()
    claim_ids: set[str] = set()
    for fact in facts:
        if fact.factId in fact_ids:
            raise ValueError(f"duplicate fact ID in claim input: {fact.factId}")
        fact_ids.add(fact.factId)
        if fact.factType == "documentation.line":
            continue
        template_key = template_key_for(fact.factType, fact.value)
        rendered = format_claim_text(template_key, fact.value)
        kind = "observed" if fact.derivation == "observed" else "inferred"
        identity = _claim_identity(repository_id, fact.scope, fact.semanticKey)
        claim_id = context.record_id("claim", identity)
        if claim_id in claim_ids:
            raise ValueError(f"duplicate claim stable identity in facts: {claim_id}")
        claim_ids.add(claim_id)
        evidence_ids = tuple(sorted(set(fact.evidenceIds)))
        candidate = Claim(
            1, claim_id, 1, fact.semanticKey, fact.scope, fact.value, None, template_key,
            kind, "deterministic", (fact.factId,), evidence_ids, "pending", (), (), fact.snapshotId,
        )
        candidate = revise_record(
            prior.get(claim_id), candidate,
            semantic_payload=lambda item: {"payload": item.payload, "kind": item.kind, "origin": item.origin},
            revision_field="claimRevision",
        )
        # Formatting is checked at derivation so invalid templates never enter a candidate set.
        if rendered != format_claim_text(candidate.templateKey or "", candidate.payload):
            raise ValueError("claim formatter output is inconsistent")
        claims.append(candidate)
    claims.sort(key=lambda item: (item.semanticKey, item.scope, item.claimId))
    return ClaimSet(tuple(claims), ())


def _claim_revision_index(claims: Sequence[Claim]) -> dict[tuple[str, int], Claim]:
    indexed: dict[tuple[str, int], Claim] = {}
    for claim in claims:
        key = (claim.claimId, claim.claimRevision)
        if key in indexed:
            raise ValueError(f"run contains duplicate claim revision: {claim.claimId}@{claim.claimRevision}")
        indexed[key] = claim
    return indexed


def _effective_review_claims(state: ProjectState, run: RunLedger) -> tuple[Claim, ...]:
    """Apply persisted dispositions transiently, without changing the immutable run."""
    validated = validate_claims(run.claims, run)
    return apply_review_dispositions(validated.validClaims, state)


def apply_review_dispositions(claims: Sequence[Claim], state: ProjectState) -> tuple[Claim, ...]:
    """Overlay persisted dispositions onto caller-validated exact claim revisions."""
    statuses: dict[tuple[str, int], str] = {}
    for claim in claims:
        reference = (claim.claimId, claim.claimRevision)
        if reference in statuses:
            raise ValueError(f"duplicate validated claim revision: {claim.claimId}@{claim.claimRevision}")
        statuses[reference] = claim.proposalStatus
        if state.acceptedClaimRevisions.get(claim.claimId) == claim.claimRevision:
            statuses[reference] = "accepted"
    for event in state.reviewEvents:
        reference = (event.claimId, event.claimRevision)
        if reference in statuses:
            statuses[reference] = {"accept": "accepted", "reject": "rejected", "contest": "contested"}[event.action]
    return tuple(
        replace(claim, proposalStatus=statuses[(claim.claimId, claim.claimRevision)])
        for claim in claims
    )


def _validate_review_batch(
    state: ProjectState,
    run: RunLedger,
    events: Sequence[ReviewEvent],
    effective_claims: Sequence[Claim],
) -> tuple[Claim, ...]:
    """Validate every explicit event and transition before any publication."""
    if not events:
        raise ValueError("review event batch must not be empty")
    run_index = _claim_revision_index(run.claims)
    effective_by_ref = {(claim.claimId, claim.claimRevision): claim for claim in effective_claims}
    statuses = {reference: claim.proposalStatus for reference, claim in effective_by_ref.items()}
    known_event_ids: set[str] = set()
    for previous in state.reviewEvents:
        if previous.eventId in known_event_ids:
            raise ValueError(f"duplicate review event ID in state: {previous.eventId}")
        known_event_ids.add(previous.eventId)
    batch_ids: set[str] = set()
    for event in events:
        if not isinstance(event, ReviewEvent):
            raise TypeError("review events must be ReviewEvent records")
        if event.recordRevision != 1:
            raise ValueError("unsupported review event record revision")
        if event.eventId in known_event_ids or event.eventId in batch_ids:
            raise ValueError(f"duplicate review event ID: {event.eventId}")
        batch_ids.add(event.eventId)
        reference = (event.claimId, event.claimRevision)
        if reference not in run_index:
            raise ValueError(f"unknown claim revision: {event.claimId}@{event.claimRevision}")
        if reference not in effective_by_ref:
            raise ValueError(f"claim revision is invalid for this run: {event.claimId}@{event.claimRevision}")
        if event.action in {"reject", "contest"} and (event.reason is None or not event.reason.strip()):
            raise ValueError(f"{event.action} reason is required")
        if event.action == "accept" and event.reason is not None:
            raise ValueError("accept reason must be null")
        status = statuses[reference]
        if event.action in {"accept", "reject"} and status != "pending":
            raise ValueError(f"{event.action} applies only to pending claims")
        if event.action == "contest" and status not in {"pending", "accepted"}:
            raise ValueError("contest applies only to pending or accepted claims")
        statuses[reference] = {
            "accept": "accepted", "reject": "rejected", "contest": "contested",
        }[event.action]
    return tuple(
        replace(claim, proposalStatus=statuses[(claim.claimId, claim.claimRevision)])
        for claim in effective_claims
    )


def _validate_review_spans(bundle: RenderBundle, effective_claims: Sequence[Claim]) -> None:
    """Fail closed if accepted stage spans point at a non-accepted overlay claim."""
    accepted_refs = {
        f"{claim.claimId}@{claim.claimRevision}"
        for claim in effective_claims if claim.proposalStatus == "accepted"
    }
    for target in bundle.targets:
        for path, spans in target.claimSpans.items():
            if not set(spans).issubset(accepted_refs):
                raise ValueError(f"accepted stage spans disagree with review overlay: {target.targetId} ({path})")


def review_claims(
    state: ProjectState,
    run: RunLedger,
    events: Sequence[ReviewEvent],
    specs: Sequence[TargetSpec],
    *,
    store: LedgerStore,
    current_identity: RepositoryIdentity,
    current_root_label: str,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[str],
) -> tuple[ProjectState, StagedRun]:
    """Review a persisted run and activate one new immutable rendered revision."""
    store._require_mutation()
    store.require_no_unresolved_export()
    authoritative_state = store.read_state()
    project = store.read_project()
    if authoritative_state is None or project is None:
        raise FileNotFoundError("project and state documents are required before claim review")
    if state != authoritative_state:
        raise ValueError("supplied project state is stale")
    if project.projectId != state.projectId:
        raise ValueError("project and state IDs do not match")
    if state.currentRunId is None or state.activeStageRevision is None:
        raise ValueError("claim review requires an active run and stage revision")
    if run.runId != state.currentRunId:
        raise ValueError("supplied run is not the active run")
    authoritative_run = store.read_run(state.currentRunId)
    if authoritative_run is None:
        raise FileNotFoundError("active immutable run is missing")
    if run != authoritative_run:
        raise ValueError("supplied run is stale")
    active_descriptor = read_stage_descriptor(store, run.runId, state.activeStageRevision)
    if active_descriptor.get("runId") != state.currentRunId:
        raise ValueError("active stage descriptor does not match the current run")

    effective_claims = _effective_review_claims(state, run)
    updated_claims = _validate_review_batch(state, run, events, effective_claims)
    updated_valid = updated_claims

    id_context = CanonicalIdContext()
    documentation = tuple(fact for fact in run.facts if fact.factType == "documentation.line")
    render_eligible = tuple(
        claim for claim in updated_valid if claim.proposalStatus in {"pending", "accepted"}
    )
    deduped = deduplicate(render_eligible, documentation, run.snapshot, id_context=id_context)
    bundle = render_targets(
        deduped.renderableClaims, specs, policy=run.effectiveClaimPolicy,
        project_id=project.projectId, id_context=id_context,
    )
    _validate_review_spans(bundle, updated_valid)

    accepted_revisions = dict(state.acceptedClaimRevisions)
    active_claim_ids = {claim.claimId for claim in run.claims}
    for claim_id in active_claim_ids:
        accepted_revisions.pop(claim_id, None)
    accepted_revisions.update({
        claim.claimId: claim.claimRevision
        for claim in updated_valid if claim.proposalStatus == "accepted"
    })
    next_revision = state.activeStageRevision + 1
    while store.immutable_path_exists(f"staging/{run.runId}/r{next_revision}"):
        next_revision += 1
    new_state = replace(
        state,
        reviewEvents=(*state.reviewEvents, *events),
        acceptedClaimRevisions=accepted_revisions,
        activeStageRevision=next_revision,
    )

    staged = publish_stage_revision(
        bundle, run, store=store, selected_specs=specs, stage_revision=next_revision,
    )
    descriptor = read_stage_descriptor(store, run.runId, next_revision)
    if descriptor.get("runId") != run.runId or descriptor.get("stageRevision") != next_revision:
        raise ValueError("published review stage descriptor identity is invalid")

    freshness = recheck_dependencies(
        run, current_identity, current_policy_revision, current_extractor_versions,
        root=store.root, current_root_label=current_root_label, excluded_paths=excluded_paths,
    )
    if freshness.status != "fresh":
        raise ActivationFreshnessError(freshness)
    store.replace_state(new_state)
    return new_state, staged
