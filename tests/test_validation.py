from __future__ import annotations

from dataclasses import replace
import hashlib
from pathlib import Path

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ids import canonical_json_bytes, canonical_record_id, effective_policy_revision
from repo2skill.models import Evidence, EvidenceLocator, ExtractionRead, Fact, InputSelector, RunLedger, SourceSnapshot
from repo2skill.claims import derive_claims
from repo2skill.validation import validate_claims


REPO = "repo_0123456789abcdef01234567"
SNAPSHOT_ID = "snapshot_0123456789abcdef01234567"
FACT_ID = "fact_0123456789abcdef01234567"
EVIDENCE_ID = "evidence_0123456789abcdef01234567"
RULE = "generic.structural"
PATH = "package.json"
HASH = "sha256:" + "a" * 64


def _run(*, fact_derivation: str = "observed", selector_scope: str = ".",
         auto_accept_observed: bool = True) -> tuple[RunLedger, Fact, Evidence]:
    fact = Fact(1, FACT_ID, 1, SNAPSHOT_ID, "artifact.presence", "artifact.presence:package.v1:package.json",
                ".", {"category": "package.v1", "name": "example@1.0", "path": PATH}, fact_derivation,
                RULE, "generic-allowlist-v1", (EVIDENCE_ID,))
    evidence = Evidence(1, EVIDENCE_ID, 1, SNAPSHOT_ID, PATH, HASH, "config:json", "application/json",
                        "utf-8", EvidenceLocator("json-pointer", "/name", None), None, "available")
    selector = InputSelector("file", PATH, selector_scope, False)
    read = ExtractionRead(1, canonical_record_id("read", {"snapshot": SNAPSHOT_ID}), SNAPSHOT_ID,
                          "generic", "1.0", RULE, "generic-allowlist-v1", "file", selector, False,
                          "present", HASH, (), None, ())
    digest_projection = {
        "extractorId": read.extractorId, "extractorVersion": read.extractorVersion,
        "ruleId": read.ruleId, "ruleVersion": read.ruleVersion, "dependencyType": read.dependencyType,
        "selector": {"type": selector.type, "value": selector.value, "scope": selector.scope, "required": selector.required},
        "required": read.required, "outcome": read.outcome, "contentHash": read.contentHash,
        "matchPaths": [], "matchSetHash": None,
    }
    identity = {"repositoryId": REPO, "gitMode": "no-git", "commitId": None, "branch": None,
                "dirty": False, "dirtySummary": {}}
    scan_policy = default_scan_policy()
    claim_policy = replace(default_claim_policy(), autoAcceptObserved=auto_accept_observed)
    targets = default_targets(Path("."))
    policy_revision = effective_policy_revision(scan_policy, claim_policy, targets, generation_options={})
    digest = "sha256:" + hashlib.sha256(canonical_json_bytes({
        "repository": {**identity, "rootLabel": "repo"}, "inventory": HASH,
        "policy": policy_revision, "extractors": {"generic": "1.0"}, "reads": [digest_projection],
    })).hexdigest()
    snapshot = SourceSnapshot(SNAPSHOT_ID, REPO, "repo", "no-git", None, None, False, {}, HASH, digest,
                              policy_revision, {"generic": "1.0"}, "2026-01-01T00:00:00Z")
    run = RunLedger("1.0", "0.1", canonical_record_id("run", {"snapshot": SNAPSHOT_ID}), snapshot,
                    scan_policy, claim_policy, targets,
                    (read,), (evidence,), (fact,), (), (), (), (), {}, True)
    return run, fact, evidence


def test_valid_observed_claim_is_autoaccepted_only_after_chain_validation() -> None:
    run, fact, _ = _run()
    candidate = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=REPO).claims
    result = validate_claims(candidate, run)
    assert result.rejectedClaims == ()
    assert len(result.validClaims) == 1
    assert result.validClaims[0].proposalStatus == "accepted"


def test_derived_support_cannot_back_an_observed_claim() -> None:
    run, fact, _ = _run(fact_derivation="template")
    candidate = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=REPO).claims
    forged_observed = replace(candidate[0], kind="observed", proposalStatus="accepted")
    result = validate_claims((forged_observed,), run)
    assert result.validClaims == ()
    assert result.rejectedClaims == (replace(forged_observed, proposalStatus="pending"),)
    assert result.rejectedClaims[0].proposalStatus == "pending"


def test_valid_inferred_claim_stays_pending_and_autoaccept_can_be_disabled() -> None:
    run, fact, _ = _run()
    inferred_fact = replace(fact, derivation="template")
    inferred_run = replace(run, facts=(inferred_fact,))
    inferred = derive_claims((inferred_fact,), run.effectiveClaimPolicy, repository_id=REPO).claims
    inferred_result = validate_claims(inferred, inferred_run)
    assert inferred_result.rejectedClaims == ()
    assert inferred_result.validClaims[0].proposalStatus == "pending"

    no_autoaccept, no_autoaccept_fact, _ = _run(auto_accept_observed=False)
    observed = derive_claims((no_autoaccept_fact,), no_autoaccept.effectiveClaimPolicy, repository_id=REPO).claims
    result = validate_claims(observed, no_autoaccept)
    assert result.validClaims[0].proposalStatus == "pending"


def test_unknown_fact_and_wrong_scope_are_rejected() -> None:
    run, fact, _ = _run(selector_scope="other")
    claim = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=REPO).claims[0]
    dangling = replace(claim, factIds=("fact_aaaaaaaaaaaaaaaaaaaaaaaa",))
    result = validate_claims((dangling, claim), run)
    assert result.validClaims == ()
    assert result.rejectedClaims == (dangling, claim)


def test_evidence_from_another_snapshot_and_payload_mismatch_are_rejected() -> None:
    run, fact, evidence = _run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=REPO).claims[0]
    wrong_payload = replace(claim, payload={"category": "package.v1", "name": "other", "path": PATH})
    wrong_snapshot_run = replace(run, evidence=(replace(evidence, snapshotId="snapshot_aaaaaaaaaaaaaaaaaaaaaaaa"),))
    result = validate_claims((claim, wrong_payload), wrong_snapshot_run)
    assert result.validClaims == ()
    assert result.rejectedClaims == (claim, wrong_payload)


def test_wrong_read_hash_and_unknown_template_are_rejected() -> None:
    run, fact, _ = _run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=REPO).claims[0]
    wrong_read = replace(run.reads[0], contentHash="sha256:" + "d" * 64)
    bad_run = replace(run, reads=(wrong_read,))
    wrong_template = replace(claim, templateKey="unknown.v99")
    bad_chain = validate_claims((claim,), bad_run)
    bad_template = validate_claims((wrong_template,), run)
    assert bad_chain.validClaims == () and bad_chain.rejectedClaims == (claim,)
    assert bad_template.validClaims == () and bad_template.rejectedClaims == (wrong_template,)


def test_artifact_claim_path_must_match_its_source_evidence() -> None:
    run, fact, _ = _run()
    mismatched_fact = replace(fact, value={"category": "package.v1", "name": "example@1.0", "path": "other.json"})
    run = replace(run, facts=(mismatched_fact,))
    claim = derive_claims((mismatched_fact,), run.effectiveClaimPolicy, repository_id=REPO).claims
    result = validate_claims(claim, run)
    assert result.validClaims == ()
    assert len(result.rejectedClaims) == 1
