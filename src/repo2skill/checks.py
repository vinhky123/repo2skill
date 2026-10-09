"""Read-only dependency freshness and recorded claim explanations."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .claims import apply_review_dispositions
from .dependencies import recheck_dependencies
from .models import (
    CheckResult, Claim, ClaimId, DedupDecision, Diagnostic, Evidence, Explanation,
    Fact, Finding, LedgerStoreSnapshot, RepositoryIdentity, RunId, RunLedger,
    TargetManifest,
)
from .validation import validate_claims


def check_freshness(
    run: RunLedger,
    current_identity: RepositoryIdentity,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str],
    *,
    root: Path,
    current_root_label: str,
    excluded_paths: Sequence[str],
) -> CheckResult:
    """Return current selector freshness plus immutable-run review/coverage status."""
    source = recheck_dependencies(
        run, current_identity, current_policy_revision, current_extractor_versions,
        root=root, current_root_label=current_root_label, excluded_paths=excluded_paths,
    )
    review_claim_ids = tuple(sorted({
        claim.claimId for claim in run.claims
        if claim.proposalStatus in {"pending", "contested"}
    }))
    review_status = "pending" if review_claim_ids else "clear"
    required_gap = any(
        entry.status not in {"supported", "generated"} and entry.selector.required
        for entry in run.coverage
    )
    any_gap = any(entry.status not in {"supported", "generated"} for entry in run.coverage)
    coverage_status = "required-gap" if required_gap else "informational-gaps" if any_gap else "complete"
    findings = list(source.findings)
    if review_claim_ids:
        findings.append(Finding(1, "check.review-pending", "pending",
                                "the immutable run has claims awaiting review", None, None,
                                review_claim_ids, {}))
    if required_gap:
        findings.append(Finding(1, "check.coverage-required-gap", "incomplete",
                                "required input coverage is incomplete", None, None, (), {}))
    elif any_gap:
        findings.append(Finding(1, "check.coverage-informational-gap", "info",
                                "some input coverage is informationally incomplete", None, None, (), {}))
    return CheckResult(source.status, review_status, coverage_status,
                       source.affectedClaimIds, tuple(findings))


def _latest_claims(run: RunLedger, claim_id: ClaimId) -> dict[int, Claim]:
    found: dict[int, Claim] = {}
    for claim in run.claims:
        if claim.claimId != claim_id:
            continue
        if claim.claimRevision in found:
            raise ValueError(f"duplicate canonical claim record: {claim.claimId}@{claim.claimRevision}")
        found[claim.claimRevision] = claim
    return found


def _assert_unique_records(run: RunLedger) -> None:
    for name, records, key_fn in (
        ("claim", run.claims, lambda item: (item.claimId, item.claimRevision)),
        ("fact", run.facts, lambda item: item.factId),
        ("evidence", run.evidence, lambda item: item.evidenceId),
        ("dedup decision", run.dedupDecisions, lambda item: item.decisionId),
    ):
        seen: set[object] = set()
        for record in records:
            record_key = key_fn(record)
            if record_key in seen:
                raise ValueError(f"duplicate canonical {name} record: {record_key}")
            seen.add(record_key)


def _choose_run(
    claim_id: ClaimId, snapshot: LedgerStoreSnapshot, revision: int | None, run_id: RunId | None,
) -> tuple[RunLedger, Claim]:
    runs = snapshot.runs
    if run_id is not None:
        selected = runs.get(run_id)
        if selected is None or selected.runId != run_id:
            raise LookupError(f"run not found: {run_id}")
        _assert_unique_records(selected)
        candidates = _latest_claims(selected, claim_id)
        if revision is not None:
            candidates = {revision: candidates[revision]} if revision in candidates else {}
        if not candidates:
            raise LookupError(f"claim revision not found: {claim_id}")
        return selected, candidates[max(candidates)]

    valid: list[tuple[RunLedger, Claim]] = []
    for key, candidate_run in runs.items():
        if key != candidate_run.runId:
            raise ValueError("run mapping key does not match ledger ID")
        _assert_unique_records(candidate_run)
        revisions = _latest_claims(candidate_run, claim_id)
        if revision is not None:
            revisions = {revision: revisions[revision]} if revision in revisions else {}
        if not revisions:
            continue
        validation = validate_claims(candidate_run.claims, candidate_run)
        valid_refs = {(item.claimId, item.claimRevision) for item in validation.validClaims}
        invalid = [claim for rev, claim in revisions.items() if (claim_id, rev) not in valid_refs]
        if invalid:
            bad_ref = f"{invalid[0].claimId}@{invalid[0].claimRevision}"
            raise ValueError(f"broken claim support lineage for {bad_ref}")
        valid.extend((candidate_run, claim) for rev, claim in revisions.items()
                     if (claim_id, rev) in valid_refs)
    if not valid:
        raise LookupError(f"claim not found: {claim_id}")
    current_id = snapshot.state.currentRunId
    current = [(candidate_run, claim) for candidate_run, claim in valid
               if candidate_run.runId == current_id]
    # An explicit revision favors the current run when it contains that exact pair.
    # Without a revision, history selection is by highest semantic revision first.
    candidates = current if revision is not None and current else valid
    return max(candidates, key=lambda pair: (
        pair[1].claimRevision,
        pair[0].snapshot.completedAt,
        pair[0].runId,
    ))


def _lineage_records(
    run: RunLedger, claim: Claim,
) -> tuple[tuple[Fact, ...], tuple[Evidence, ...], DedupDecision | None]:
    facts_by_id = {fact.factId: fact for fact in run.facts}
    evidence_by_id = {item.evidenceId: item for item in run.evidence}
    if len(facts_by_id) != len(run.facts) or len(evidence_by_id) != len(run.evidence):
        raise ValueError("duplicate canonical support record")
    if any(fact_id not in facts_by_id for fact_id in claim.factIds):
        raise ValueError(f"broken fact lineage for {claim.claimId}@{claim.claimRevision}")
    direct_facts = tuple(facts_by_id[fact_id] for fact_id in claim.factIds)
    direct_evidence_ids = {evidence_id for fact in direct_facts for evidence_id in fact.evidenceIds}
    if any(evidence_id not in evidence_by_id for evidence_id in direct_evidence_ids):
        raise ValueError(f"broken evidence lineage for {claim.claimId}@{claim.claimRevision}")
    if tuple(sorted(set(claim.evidenceIds))) != claim.evidenceIds or set(claim.evidenceIds) != direct_evidence_ids:
        raise ValueError(f"broken claim evidence lineage for {claim.claimId}@{claim.claimRevision}")
    matching = [item for item in run.dedupDecisions if claim.claimId in item.candidateClaimIds]
    if len(matching) > 1:
        raise ValueError(f"multiple matching dedup decisions for {claim.claimId}@{claim.claimRevision}")
    decision = matching[0] if matching else None
    related_fact_ids = set(claim.factIds)
    related_evidence_ids = set(direct_evidence_ids)
    if decision is not None:
        for values in (decision.candidateClaimIds, decision.candidateFactIds,
                       decision.documentationEvidenceIds):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"invalid dedup lineage for {claim.claimId}")
        if any(item not in facts_by_id for item in decision.candidateFactIds):
            raise ValueError(f"broken dedup fact lineage for {claim.claimId}")
        if any(item not in evidence_by_id for item in decision.documentationEvidenceIds):
            raise ValueError(f"broken dedup evidence lineage for {claim.claimId}")
        claims_by_id: dict[str, list[Claim]] = {}
        for candidate in run.claims:
            claims_by_id.setdefault(candidate.claimId, []).append(candidate)
        candidate_claims: list[Claim] = []
        for candidate_id in decision.candidateClaimIds:
            matches = claims_by_id.get(candidate_id, [])
            if len(matches) != 1:
                raise ValueError(f"broken dedup claim lineage for {claim.claimId}")
            candidate_claims.append(matches[0])
        expected_fact_ids = tuple(sorted({fact_id for candidate in candidate_claims
                                          for fact_id in candidate.factIds}))
        if decision.candidateFactIds != expected_fact_ids:
            raise ValueError(f"broken dedup fact lineage for {claim.claimId}")
        related_fact_ids.update(decision.candidateFactIds)
        related_evidence_ids.update(decision.documentationEvidenceIds)
        for fact_id in decision.candidateFactIds:
            related_evidence_ids.update(facts_by_id[fact_id].evidenceIds)
    if any(evidence_id not in evidence_by_id for evidence_id in related_evidence_ids):
        raise ValueError(f"broken evidence lineage for {claim.claimId}@{claim.claimRevision}")
    return (tuple(facts_by_id[item] for item in sorted(related_fact_ids)),
            tuple(evidence_by_id[item] for item in sorted(related_evidence_ids)), decision)


def _reference_is_valid(reference: str, all_claim_refs: set[str]) -> bool:
    return reference in all_claim_refs


def explain_claim(
    claim_id: ClaimId,
    store: LedgerStoreSnapshot,
    revision: int | None = None,
    run_id: RunId | None = None,
    *,
    staged_descriptors: Mapping[tuple[RunId, int], Mapping[str, object]],
    target_manifests: Sequence[TargetManifest],
) -> Explanation:
    """Resolve a claim using only immutable run, stage, and manifest provenance."""
    if revision is not None and (isinstance(revision, bool) or revision < 1):
        raise ValueError("revision must be a positive integer")
    for key, run in store.runs.items():
        if key != run.runId:
            raise ValueError("run mapping key does not match ledger ID")
        _assert_unique_records(run)
    selected_run, selected_claim = _choose_run(claim_id, store, revision, run_id)
    all_refs = {f"{claim.claimId}@{claim.claimRevision}"
                for run in store.runs.values() for claim in run.claims}
    if any(not _reference_is_valid(ref, all_refs)
           for ref in (*selected_claim.supersedes, *selected_claim.derivedFrom)):
        raise ValueError(f"broken claim revision lineage for {selected_claim.claimId}@{selected_claim.claimRevision}")

    validation = validate_claims(selected_run.claims, selected_run)
    valid_by_ref = {(item.claimId, item.claimRevision): item for item in validation.validClaims}
    if (selected_claim.claimId, selected_claim.claimRevision) not in valid_by_ref:
        raise ValueError(f"broken claim support lineage for {selected_claim.claimId}@{selected_claim.claimRevision}")
    effective_claim = apply_review_dispositions((selected_claim,), store.state)[0]
    facts, evidence, decision = _lineage_records(selected_run, selected_claim)

    ref = f"{selected_claim.claimId}@{selected_claim.claimRevision}"
    staged_paths: set[str] = set()
    live_paths: set[str] = set()
    diagnostics: list[Diagnostic] = []
    manifest_revisions = {item.stageRevision for item in target_manifests if item.runId == selected_run.runId}
    relevant_revisions = {1, *manifest_revisions}
    if store.state.currentRunId == selected_run.runId and store.state.activeStageRevision is not None:
        relevant_revisions.add(store.state.activeStageRevision)
    for (descriptor_run_id, stage_revision), descriptor in sorted(staged_descriptors.items()):
        if descriptor_run_id != selected_run.runId or stage_revision not in relevant_revisions:
            continue
        if descriptor.get("runId") != descriptor_run_id or descriptor.get("stageRevision") != stage_revision:
            raise ValueError("stage descriptor identity does not match its mapping key")
        targets = descriptor.get("targets")
        if not isinstance(targets, Mapping):
            raise ValueError("stage descriptor targets are invalid")
        for target_id, target in sorted(targets.items()):
            if not isinstance(target, Mapping):
                raise ValueError("stage descriptor target is invalid")
            for mode in ("accepted", "preview"):
                files = target.get(mode, {})
                if not isinstance(files, Mapping):
                    raise ValueError("stage descriptor mode is invalid")
                for path, file_info in sorted(files.items()):
                    if not isinstance(file_info, Mapping):
                        raise ValueError("stage descriptor file is invalid")
                    spans = file_info.get("claimSpans", {})
                    if not isinstance(spans, Mapping) or ref not in spans:
                        continue
                    line_spans = spans[ref]
                    if not isinstance(line_spans, (list, tuple)):
                        raise ValueError("stage descriptor line spans are invalid")
                    staged_paths.add(path)
                    diagnostics.append(Diagnostic(
                        1, "explain.stage-location.v1", "info", "claim appears in a recorded staged file",
                        path, None, {"targetId": target_id, "mode": mode, "stageRevision": stage_revision,
                                    "claimRevisionRef": ref, "lineSpans": [list(span) for span in line_spans],
                                    "artifactHash": file_info.get("sha256")},
                    ))

    seen_manifests: set[tuple[str, str, str]] = set()
    for manifest in sorted(target_manifests, key=lambda item: (item.runId, item.targetId,
                                                               item.stageRevision, item.manifestId)):
        if manifest.runId != selected_run.runId:
            continue
        manifest_key = (manifest.manifestId, manifest.targetId, manifest.runId)
        if manifest_key in seen_manifests:
            raise ValueError("duplicate target manifest record")
        seen_manifests.add(manifest_key)
        for file in sorted(manifest.files, key=lambda item: item.path):
            spans = file.claimSpans.get(ref, ())
            in_block = any(selected_claim.claimId in block.claimIds for block in file.blocks)
            if not spans and not in_block:
                continue
            live_paths.add(file.path)
            diagnostics.append(Diagnostic(
                1, "explain.live-location.v1", "info", "claim appears in a recorded live manifest",
                file.path, None, {"targetId": manifest.targetId, "manifestId": manifest.manifestId,
                                  "stageRevision": manifest.stageRevision, "claimRevisionRef": ref,
                                  "lineSpans": [list(span) for span in spans],
                                  "artifactHash": file.exactHash},
            ))

    return Explanation(effective_claim, facts, evidence, selected_run.snapshot, decision,
                       tuple(sorted(staged_paths)), tuple(sorted(live_paths)), tuple(diagnostics))
