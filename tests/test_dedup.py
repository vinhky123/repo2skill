from __future__ import annotations

from dataclasses import replace

from repo2skill.config import default_claim_policy
from repo2skill.claims import derive_claims
from repo2skill.dedup import deduplicate
from repo2skill.models import Fact, SourceSnapshot


REPO = "repo_0123456789abcdef01234567"
SNAPSHOT_ID = "snapshot_0123456789abcdef01234567"


def _snapshot(snapshot_id: str = SNAPSHOT_ID) -> SourceSnapshot:
    return SourceSnapshot(snapshot_id, REPO, "repo", "no-git", None, None, False, {},
                          "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64,
                          {}, "2026-01-01T00:00:00Z")


def _command_fact(suffix: str) -> Fact:
    return Fact(1, f"fact_{suffix:0>24}"[-29:], 1, SNAPSHOT_ID, "command.invocation",
                f"command.invocation:test:pnpm:{suffix}", ".",
                {"category": "test", "argv": ["pnpm", "test"]}, "template", "javascript.package",
                "javascript-shallow-v2", (f"evidence_{suffix:0>24}"[-33:],))


def _doc(evidence_ids: tuple[str, ...]) -> Fact:
    return Fact(1, "fact_aaaaaaaaaaaaaaaaaaaaaaaa", 1, SNAPSHOT_ID, "documentation.line",
                "documentation:test-line", ".", {"category": "documentation", "text": "Test command: `pnpm test`."},
                "observed", "docs.normalized-line", "docs-lines-v1", evidence_ids)


def test_document_match_suppresses_render_without_changing_candidates_or_proof() -> None:
    facts = (_command_fact("1"), _command_fact("2"))
    claims = derive_claims(facts, default_claim_policy(), repository_id=REPO).claims
    docs = (_doc(("evidence_111111111111111111111111", "evidence_222222222222222222222222")),)

    result = deduplicate(claims, docs, _snapshot())

    assert result.renderableClaims == ()
    assert len(result.decisions) == 1
    decision = result.decisions[0]
    assert decision.result == "suppress"
    assert decision.candidateClaimIds == tuple(sorted(claim.claimId for claim in claims))
    assert decision.candidateFactIds == tuple(sorted(fact.factId for fact in facts))
    assert decision.documentationEvidenceIds == docs[0].evidenceIds
    assert all(claim.proposalStatus == "pending" for claim in claims)


def test_decision_identity_excludes_snapshot_and_input_order() -> None:
    first_fact, second_fact = _command_fact("1"), _command_fact("2")
    claims = derive_claims((first_fact, second_fact), default_claim_policy(), repository_id=REPO).claims
    docs = (_doc(("evidence_111111111111111111111111",)),)
    first = deduplicate(claims, docs, _snapshot()).decisions[0]
    other_snapshot = _snapshot("snapshot_bbbbbbbbbbbbbbbbbbbbbbbb")
    other_docs = (replace(docs[0], snapshotId=other_snapshot.snapshotId),)
    other_claims = tuple(replace(item, lastVerifiedSnapshotId=other_snapshot.snapshotId) for item in claims)
    second = deduplicate(tuple(reversed(other_claims)), other_docs, other_snapshot).decisions[0]
    assert first.decisionId == second.decisionId
    assert first.snapshotId != second.snapshotId


def test_unmatched_candidate_remains_renderable() -> None:
    fact = _command_fact("1")
    claims = derive_claims((fact,), default_claim_policy(), repository_id=REPO).claims
    result = deduplicate(claims, (), _snapshot())
    assert result.renderableClaims == claims
    assert result.decisions[0].result == "render"


def test_identical_candidate_text_merges_for_render_and_keeps_all_candidate_ids() -> None:
    facts = (_command_fact("1"), _command_fact("2"))
    claims = derive_claims(facts, default_claim_policy(), repository_id=REPO).claims
    result = deduplicate(claims, (), _snapshot())
    assert len(result.renderableClaims) == 1
    assert result.decisions[0].result == "merge"
    assert result.decisions[0].candidateClaimIds == tuple(sorted(claim.claimId for claim in claims))
    assert all(claim.proposalStatus == "pending" for claim in claims)
