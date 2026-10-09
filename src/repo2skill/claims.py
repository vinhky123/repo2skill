"""Deterministic conversion of approved Fact records into pending claims."""
from __future__ import annotations

from collections.abc import Sequence

from .claim_templates import format_claim_text, template_key_for
from .ids import CanonicalIdContext, canonical_record_id, latest_by_id, revise_record
from .models import Claim, ClaimPolicy, ClaimSet, Fact


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
    context = id_context or CanonicalIdContext()
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
        identity = {"repository": repository_id, "scope": fact.scope,
                    "recordType": "claim", "semanticKey": fact.semanticKey}
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
