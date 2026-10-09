from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

import pytest

from repo2skill.claims import derive_claims
from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ids import canonical_json_bytes, effective_policy_revision
from repo2skill.models import (
    Claim, DedupDecision, Evidence, EvidenceLocator, ExtractionRead, Fact, InputSelector, LedgerStoreSnapshot,
    ManifestBlock, ManifestFile, ProjectDocument,
    ProjectState, ReviewEvent, RunLedger, SourceSnapshot, TargetManifest,
)
from repo2skill.checks import explain_claim



def _validation_run():
    repo = "repo_0123456789abcdef01234567"
    snapshot_id = "snapshot_0123456789abcdef01234567"
    fact_id = "fact_0123456789abcdef01234567"
    evidence_id = "evidence_0123456789abcdef01234567"
    path = "package.json"
    digest = "sha256:" + "a" * 64
    fact = Fact(1, fact_id, 1, snapshot_id, "artifact.presence", "artifact.presence:package.v1:package.json",
                ".", {"category": "package.v1", "name": "example@1.0", "path": path}, "observed",
                "generic.structural", "generic-allowlist-v1", (evidence_id,))
    evidence = Evidence(1, evidence_id, 1, snapshot_id, path, digest, "config:json", "application/json",
                        "utf-8", EvidenceLocator("json-pointer", "/name", None), None, "available")
    selector = InputSelector("file", path, ".", False)
    read = ExtractionRead(1, "read_0123456789abcdef01234567", snapshot_id, "generic", "1.0",
                          "generic.structural", "generic-allowlist-v1", "file", selector, False,
                          "present", digest, (), None, ())
    scan = default_scan_policy()
    claim_policy = default_claim_policy()
    targets = default_targets(Path("."))
    policy_revision = effective_policy_revision(scan, claim_policy, targets, generation_options={})
    projection = {"extractorId": "generic", "extractorVersion": "1.0", "ruleId": "generic.structural",
                  "ruleVersion": "generic-allowlist-v1", "dependencyType": "file",
                  "selector": {"type": "file", "value": path, "scope": ".", "required": False},
                  "required": False, "outcome": "present", "contentHash": digest,
                  "matchPaths": [], "matchSetHash": None}
    identity = {"repositoryId": repo, "gitMode": "no-git", "commitId": None, "branch": None,
                "dirty": False, "dirtySummary": {}}
    input_digest = "sha256:" + hashlib.sha256(canonical_json_bytes({
        "repository": {**identity, "rootLabel": "repo"}, "inventory": digest,
        "policy": policy_revision, "extractors": {"generic": "1.0"}, "reads": [projection],
    })).hexdigest()
    source = SourceSnapshot(snapshot_id, repo, "repo", "no-git", None, None, False, {}, digest,
                            input_digest, policy_revision, {"generic": "1.0"}, "2026-01-01T00:00:00Z")
    run = RunLedger("1.0", "0.1.0", "run_0123456789abcdef01234567", source, scan, claim_policy,
                    targets, (read,), (evidence,), (fact,), (), (), (), (), {}, True)
    return run, fact, evidence


def test_explain_resolves_historical_run_and_only_validated_locations() -> None:
    run, fact, evidence = _validation_run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy,
                          repository_id=run.snapshot.repositoryId).claims[0]
    claim = replace(claim, proposalStatus="pending")
    run = replace(run, runId="run_0123456789abcdef01234567", claims=(claim,))
    # The snapshot is a selected history view; active-only storage semantics remain unchanged.
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "repo", ".repo2skill",
                              "no-git", run.effectiveScanPolicy, run.effectiveClaimPolicy, ())
    state = ProjectState("1.0", "0.1.0", project.projectId, None, None, (ReviewEvent(1, "event_0123456789abcdef01234567", claim.claimId, claim.claimRevision, "contest", "recheck", "2026-01-01T00:00:00Z"),), {}, None, None)
    snapshot = LedgerStoreSnapshot(project, state, {run.runId: run}, {}, {})
    target_id = "agents-main"
    path = "AGENTS.md"
    reference = f"{claim.claimId}@{claim.claimRevision}"
    descriptor = {"runId": run.runId, "stageRevision": 1, "targets": {target_id: {
        "spec": {"targetId": target_id, "kind": "agents-md", "livePath": path,
                  "ownershipMode": "managed-blocks", "emptyBlockPolicy": "keep"},
        "accepted": {path: {"sha256": "sha256:" + "a" * 64, "claimSpans": {reference: [[3, 4]]}}},
        "preview": {},
    }}}
    manifest = TargetManifest("1.0", "0.1.0", "manifest_0123456789abcdef01234567",
        "export_0123456789abcdef01234567", target_id, run.runId, 1,
        (ManifestFile(1, path, "managed-blocks", "sha256:" + "b" * 64, "lf",
                      (ManifestBlock(1, "block_0123456789abcdef01234567", "sha256:" + "c" * 64,
                                     (claim.claimId,)),), {reference: ((8, 9),)}),), (),
        "2026-01-01T00:00:00Z")
    result = explain_claim(claim.claimId, snapshot, run_id=run.runId,
                           staged_descriptors={(run.runId, 1): descriptor}, target_manifests=(manifest,))
    assert result.claim.proposalStatus == "contested"
    assert result.facts == (fact,)
    assert result.evidence == (evidence,)
    assert result.stagedLocations == (path,)
    assert result.liveLocations == (path,)
    assert any(item.code.startswith("explain.stage") and item.details["lineSpans"] == [[3, 4]]
               and item.details["artifactHash"] == "sha256:" + "a" * 64 for item in result.diagnostics)
    assert any(item.code.startswith("explain.live") and item.details["lineSpans"] == [[8, 9]]
               for item in result.diagnostics)


def test_explain_rejects_multiple_matching_dedup_decisions() -> None:
    run, fact, evidence = _validation_run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy,
                          repository_id=run.snapshot.repositoryId).claims[0]
    first = DedupDecision(1, "decision_0123456789abcdef01234567", run.snapshot.snapshotId,
                          (claim.claimId,), (), (), "dedup.docs", "1", "suppress", "documented")
    second = replace(first, decisionId="decision_1123456789abcdef01234567")
    run = replace(run, claims=(claim,), dedupDecisions=(first, second))
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "repo", ".repo2skill",
                              "no-git", run.effectiveScanPolicy, run.effectiveClaimPolicy, ())
    state = ProjectState("1.0", "0.1.0", project.projectId, run.runId, None, (), {}, None, None)
    snapshot = LedgerStoreSnapshot(project, state, {run.runId: run}, {}, {})
    with pytest.raises(ValueError, match="dedup"):
        explain_claim(claim.claimId, snapshot, staged_descriptors={}, target_manifests=())



def test_review_overlay_applies_to_exact_revision_and_preserves_provenance() -> None:
    from repo2skill.claims import apply_review_dispositions
    from repo2skill.models import ReviewEvent

    claim = Claim(1, "claim_0123456789abcdef01234567", 1, "semantic", ".", {"text": "x"}, "x", None,
                  "observed", "deterministic", (), (), "pending", (), (), "snapshot_0123456789abcdef01234567")
    next_revision = replace(claim, claimRevision=2, proposalStatus="pending")
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "repo", ".repo2skill",
                              "no-git", default_scan_policy(), default_claim_policy(), ())
    state = ProjectState("1.0", "0.1.0", project.projectId, None, None,
                         (ReviewEvent(1, "event_0123456789abcdef01234567", claim.claimId, 1,
                                      "contest", "needs review", "2026-01-01T00:00:00Z"),),
                         {claim.claimId: 1}, None, None)
    effective = apply_review_dispositions((claim, next_revision), state)
    assert [item.proposalStatus for item in effective] == ["contested", "pending"]
    assert [(item.kind, item.origin) for item in effective] == [("observed", "deterministic")] * 2




def test_history_selection_uses_highest_revision_but_exact_revision_prefers_current() -> None:
    run, fact, _ = _validation_run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy,
                          repository_id=run.snapshot.repositoryId).claims[0]
    current = replace(run, runId="run_0123456789abcdef01234567", claims=(claim,))
    newer_claim = replace(claim, claimRevision=2)
    historical = replace(run, runId="run_1123456789abcdef01234567",
                         snapshot=replace(run.snapshot, completedAt="2026-02-01T00:00:00Z"),
                         claims=(newer_claim,))
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "repo", ".repo2skill",
                              "no-git", run.effectiveScanPolicy, run.effectiveClaimPolicy, ())
    state = ProjectState("1.0", "0.1.0", project.projectId, current.runId, None, (), {}, None, None)
    snapshot = LedgerStoreSnapshot(project, state, {current.runId: current, historical.runId: historical}, {}, {})
    latest = explain_claim(claim.claimId, snapshot, staged_descriptors={}, target_manifests=())
    exact = explain_claim(claim.claimId, snapshot, revision=1, staged_descriptors={}, target_manifests=())
    assert latest.claim.claimRevision == 2
    assert exact.claim.claimRevision == 1
    assert exact.snapshot == current.snapshot


def test_review_overlay_does_not_apply_to_a_different_claim_revision() -> None:
    from repo2skill.models import ReviewEvent

    run, fact, _ = _validation_run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy,
                          repository_id=run.snapshot.repositoryId).claims[0]
    claim_v2 = replace(claim, claimRevision=2, proposalStatus="pending")
    run = replace(run, claims=(claim, claim_v2))
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "repo", ".repo2skill",
                              "no-git", run.effectiveScanPolicy, run.effectiveClaimPolicy, ())
    state = ProjectState("1.0", "0.1.0", project.projectId, run.runId, None,
                         (ReviewEvent(1, "event_0123456789abcdef01234567", claim.claimId, 1,
                                      "contest", "reviewed prior revision", "2026-01-01T00:00:00Z"),),
                         {claim.claimId: 1}, None, None)
    snapshot = LedgerStoreSnapshot(project, state, {run.runId: run}, {}, {})
    result = explain_claim(claim.claimId, snapshot, revision=2, run_id=run.runId,
                           staged_descriptors={}, target_manifests=())
    assert result.claim.claimRevision == 2
    assert result.claim.proposalStatus == "pending"


def test_explain_includes_documentation_evidence_for_recorded_suppression() -> None:
    run, fact, source_evidence = _validation_run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy,
                          repository_id=run.snapshot.repositoryId).claims[0]
    documentation = Evidence(1, "evidence_1123456789abcdef01234567", 1, run.snapshot.snapshotId,
                             "README.md", "sha256:" + "b" * 64, "documentation-line:documented",
                             "text/markdown", "utf-8", EvidenceLocator("line-range", [2, 2], 2),
                             "documented text", "available")
    decision = DedupDecision(1, "decision_0123456789abcdef01234567", run.snapshot.snapshotId,
                             (claim.claimId,), (fact.factId,), (documentation.evidenceId,),
                             "dedup.docs", "1", "suppress", "documented")
    run = replace(run, claims=(claim,), evidence=(source_evidence, documentation), dedupDecisions=(decision,))
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "repo", ".repo2skill",
                              "no-git", run.effectiveScanPolicy, run.effectiveClaimPolicy, ())
    state = ProjectState("1.0", "0.1.0", project.projectId, run.runId, None, (), {}, None, None)
    snapshot = LedgerStoreSnapshot(project, state, {run.runId: run}, {}, {})
    result = explain_claim(claim.claimId, snapshot, run_id=run.runId,
                           staged_descriptors={}, target_manifests=())
    assert result.dedupDecision == decision
    assert result.facts == (fact,)
    assert result.evidence == (source_evidence, documentation)
