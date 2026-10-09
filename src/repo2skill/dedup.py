"""Proof-preserving exact normalized-line deduplication for valid claim candidates."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
import re

from .claim_templates import format_claim_text
from .ids import CanonicalIdContext, canonical_record_id
from .models import Claim, DedupDecision, DedupResult, Fact, SourceSnapshot


RULE_ID = "dedup.normalized-claim-text"
RULE_VERSION = "claim-line-v1"


def _normalized_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _documentation_line(fact: Fact) -> str:
    value = fact.value
    if (fact.factType != "documentation.line" or not isinstance(value, dict) or
            set(value) != {"category", "text"} or value.get("category") != "documentation" or
            not isinstance(value.get("text"), str)):
        raise ValueError("dedup documentation facts must use the documentation.line shape")
    if tuple(sorted(set(fact.evidenceIds))) != fact.evidenceIds:
        raise ValueError("documentation evidence IDs must be sorted and unique")
    return _normalized_line(value["text"])


def deduplicate(
    candidates: Sequence[Claim],
    docs: Sequence[Fact],
    snapshot: SourceSnapshot,
    *,
    id_context: CanonicalIdContext | None = None,
) -> DedupResult:
    """Select one candidate per normalized line and suppress exact documentation matches."""
    context = id_context or CanonicalIdContext()
    documentation: dict[str, set[str]] = defaultdict(set)
    seen_docs: set[str] = set()
    for fact in docs:
        if fact.factId in seen_docs:
            raise ValueError("duplicate documentation fact ID")
        seen_docs.add(fact.factId)
        if (fact.snapshotId != snapshot.snapshotId or fact.scope != "." or
                fact.derivation != "observed" or fact.ruleId != "docs.normalized-line" or
                fact.ruleVersion != "docs-lines-v1"):
            raise ValueError("documentation fact provenance is inconsistent with the snapshot")
        line = _documentation_line(fact)
        if line:
            documentation[line].update(fact.evidenceIds)

    grouped: dict[str, list[Claim]] = defaultdict(list)
    seen_claims: set[str] = set()
    for claim in candidates:
        if claim.claimId in seen_claims:
            raise ValueError("duplicate claim ID in dedup candidates")
        seen_claims.add(claim.claimId)
        if claim.proposalStatus not in {"pending", "accepted", "rejected", "contested"}:
            raise ValueError("dedup candidates must be policy-validated claims")
        if claim.lastVerifiedSnapshotId != snapshot.snapshotId:
            raise ValueError("dedup candidate snapshot differs from source snapshot")
        if claim.templateKey is None or claim.renderedText is not None:
            raise ValueError("P0 dedup candidates must use a versioned claim template")
        line = _normalized_line(format_claim_text(claim.templateKey, claim.payload))
        if not line:
            raise ValueError("claim formatter produced empty text")
        grouped[line].append(claim)

    renderable: list[Claim] = []
    decisions: list[DedupDecision] = []
    for line, group in sorted(grouped.items()):
        ordered = sorted(group, key=lambda item: (item.semanticKey, item.scope, item.claimId, item.claimRevision))
        eligible = [item for item in ordered if item.proposalStatus in {"pending", "accepted"}]
        claim_ids = tuple(sorted({item.claimId for item in ordered}))
        fact_ids = tuple(sorted({fact_id for item in ordered for fact_id in item.factIds}))
        doc_ids = tuple(sorted(documentation.get(line, ())))
        if doc_ids:
            result = "suppress"
            reason = "documentation-match"
        elif len(ordered) > 1:
            result = "merge"
            reason = "duplicate-candidate-text"
            if eligible:
                renderable.append(eligible[0])
        else:
            result = "render"
            reason = "unique-candidate-text"
            if eligible:
                renderable.append(eligible[0])
        identity = {
            "repositoryId": snapshot.repositoryId,
            "ruleId": RULE_ID,
            "ruleVersion": RULE_VERSION,
            "candidateClaimIds": list(claim_ids),
            "candidateFactIds": list(fact_ids),
            "documentationEvidenceIds": list(doc_ids),
        }
        decision = DedupDecision(
            1, context.record_id("dedup", identity), snapshot.snapshotId, claim_ids, fact_ids,
            doc_ids, RULE_ID, RULE_VERSION, result, reason,
        )
        decisions.append(decision)
    decisions.sort(key=lambda item: item.decisionId)
    renderable.sort(key=lambda item: (item.semanticKey, item.scope, item.claimId, item.claimRevision))
    return DedupResult(tuple(renderable), tuple(decisions), ())
