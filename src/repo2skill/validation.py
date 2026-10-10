"""Chain validation for deterministic claims against one immutable run ledger."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
import hashlib
import re
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath

from .claim_templates import format_claim_text, template_key_for
from .ids import canonical_json_bytes, canonical_record_id, effective_policy_revision
from .models import (
    Claim, Diagnostic, Evidence, EvidenceLocator, Fact, InputSelector, JsonValue,
    RunLedger, ValidationResult,
)


_RULE_TYPES = {
    "generic.structural": {"artifact.presence", "task.definition", "config.structure"},
    "javascript.package": {"artifact.presence", "task.definition", "command.invocation"},
    "python.project": {"artifact.presence", "command.invocation"},
    "go.module": {"artifact.presence", "command.invocation"},
    "rust.cargo": {"artifact.presence", "command.invocation"},
}


def _selector_projection(selector: InputSelector) -> dict[str, object]:
    return {"type": selector.type, "value": unicodedata.normalize("NFC", selector.value),
            "scope": selector.scope, "required": selector.required}


def _read_projection(read: object) -> dict[str, object]:
    return {"extractorId": read.extractorId, "extractorVersion": read.extractorVersion,
            "ruleId": read.ruleId, "ruleVersion": read.ruleVersion,
            "dependencyType": read.dependencyType, "selector": _selector_projection(read.selector),
            "required": read.required, "outcome": read.outcome, "contentHash": read.contentHash,
            "matchPaths": sorted(unicodedata.normalize("NFC", path) for path in read.matchPaths),
            "matchSetHash": read.matchSetHash}


def _read_sort(read: object) -> tuple[object, ...]:
    return (read.extractorId, read.ruleId, read.dependencyType,
            canonical_json_bytes(_selector_projection(read.selector)), read.selector.scope)


def _snapshot_reads_error(run: RunLedger) -> str | None:
    snapshot = run.snapshot
    try:
        policy_revision = effective_policy_revision(
            run.effectiveScanPolicy, run.effectiveClaimPolicy, run.configuredTargets,
            generation_options={},
        )
    except (TypeError, ValueError):
        return "run-policy-invalid"
    if policy_revision != snapshot.policyRevision:
        return "snapshot-policy-mismatch"
    seen: set[tuple[str, str, bytes]] = set()
    for read in run.reads:
        if read.snapshotId != snapshot.snapshotId:
            return "read-snapshot-mismatch"
        if snapshot.extractorVersions.get(read.extractorId) != read.extractorVersion:
            return "read-extractor-version-mismatch"
        if read.dependencyType != read.selector.type or read.required != read.selector.required:
            return "read-selector-mismatch"
        if read.selector.scope != ".":
            return "read-scope-invalid"
        if read.selector.type == "file":
            path = read.selector.value
            if (path.startswith("/") or PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()
                    or PureWindowsPath(path).drive or "\\" in path
                    or any(part in {"", ".", ".."} for part in path.split("/"))
                    or unicodedata.normalize("NFC", path) != path):
                return "read-path-invalid"
        elif read.selector.type == "glob":
            value = read.selector.value
            if value.startswith(("!", "/")) or "\\" in value or any(part in {".", ".."} for part in value.split("/")):
                return "read-glob-invalid"
        elif not read.selector.value.startswith("filename-absent:"):
            return "read-negative-selector-invalid"
        if "." not in read.ruleId or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", read.ruleId):
            return "read-rule-invalid"
        if tuple(sorted(set(read.matchPaths))) != read.matchPaths or any(
                unicodedata.normalize("NFC", path) != path for path in read.matchPaths):
            return "read-match-paths-invalid"
        key = (read.extractorId, read.ruleId, canonical_json_bytes(_selector_projection(read.selector)))
        if key in seen:
            return "duplicate-read"
        seen.add(key)
    projections = [_read_projection(read) for read in sorted(run.reads, key=_read_sort)]
    identity = {"repositoryId": snapshot.repositoryId, "gitMode": snapshot.gitMode,
                "commitId": snapshot.commitId, "branch": snapshot.branch,
                "dirty": snapshot.dirty, "dirtySummary": snapshot.dirtySummary,
                "rootLabel": snapshot.rootLabel}
    digest_value = {"repository": identity, "inventory": snapshot.inventoryHash,
                    "policy": snapshot.policyRevision,
                    "extractors": dict(sorted(snapshot.extractorVersions.items())), "reads": projections}
    digest = "sha256:" + hashlib.sha256(canonical_json_bytes(digest_value)).hexdigest()
    return None if digest == snapshot.inputDigest else "snapshot-input-digest-mismatch"


def _locator_error(locator: EvidenceLocator, path: str) -> str | None:
    value = locator.value
    if locator.kind == "file-presence":
        return None if value == path else "locator-path-mismatch"
    if locator.kind == "line-range":
        if (not isinstance(value, list) or len(value) != 2 or
                any(isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in value) or
                value[0] > value[1]):
            return "locator-line-range-invalid"
        if locator.lineHint is not None and locator.lineHint != value[0]:
            return "locator-line-hint-mismatch"
        return None
    if locator.kind == "json-pointer":
        if not isinstance(value, str) or (value and not value.startswith("/")):
            return "locator-json-pointer-invalid"
        if re.search(r"~(?![01])", value):
            return "locator-json-pointer-invalid"
        if locator.lineHint is not None:
            return "locator-line-hint-invalid"
        return None
    if locator.kind == "key-path":
        if isinstance(value, str):
            return None if value else "locator-key-path-invalid"
        if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
            if locator.lineHint is not None:
                return "locator-line-hint-invalid"
            return None
        return "locator-key-path-invalid"
    if locator.kind == "inventory-predicate":
        return None if isinstance(value, (str, dict, list)) else "locator-predicate-invalid"
    return "locator-kind-invalid"


def _unique_index(records: Sequence[object], field: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for record in records:
        key = getattr(record, field)
        if key in result:
            result.pop(key, None)
            result[key] = None
        else:
            result[key] = record
    return result


def _support_error(claim: Claim, run: RunLedger, facts_by_id: dict[str, object],
                   evidence_by_id: dict[str, object], reads_by_rule: dict[str, list[object]],
                   run_error: str | None) -> str | None:
    if claim.supportMode == "evidence-backed-prose":
        return _prose_support_error(claim, run, facts_by_id, evidence_by_id, reads_by_rule, run_error)
    if run_error:
        return run_error
    snapshot = run.snapshot
    if claim.lastVerifiedSnapshotId != snapshot.snapshotId:
        return "claim-snapshot-mismatch"
    if not claim.factIds or len(set(claim.factIds)) != len(claim.factIds):
        return "claim-fact-ids-invalid"
    if tuple(sorted(set(claim.factIds))) != claim.factIds:
        return "claim-fact-ids-unsorted"
    selected_facts = []
    for fact_id in claim.factIds:
        fact = facts_by_id.get(fact_id)
        if fact is None:
            return "unknown-fact"
        selected_facts.append(fact)
    if any(fact.scope != claim.scope or fact.snapshotId != snapshot.snapshotId for fact in selected_facts):
        return "fact-scope-or-snapshot-mismatch"
    if any(fact.ruleId not in _RULE_TYPES or fact.factType not in _RULE_TYPES[fact.ruleId] for fact in selected_facts):
        return "unsupported-fact-rule"
    if any(fact.value != claim.payload for fact in selected_facts):
        return "claim-payload-mismatch"
    if any(fact.factType != selected_facts[0].factType or fact.semanticKey != claim.semanticKey
           for fact in selected_facts) or claim.semanticKey != selected_facts[0].semanticKey:
        return "claim-fact-identity-mismatch"
    try:
        expected_template = template_key_for(selected_facts[0].factType, claim.payload)
        expected_text = format_claim_text(expected_template, claim.payload)
    except (TypeError, ValueError, KeyError):
        return "unsupported-claim-payload"
    if claim.templateKey != expected_template or claim.renderedText is not None:
        return "claim-template-mismatch"
    if not expected_text:
        return "claim-text-empty"
    if claim.kind not in {"observed", "inferred", "human_policy"} or claim.origin != "deterministic":
        return "claim-origin-invalid"
    expected_kind = "observed" if all(fact.derivation == "observed" for fact in selected_facts) else "inferred"
    if claim.kind != expected_kind:
        return "claim-kind-derivation-mismatch"
    expected_id = canonical_record_id("claim", {"repository": snapshot.repositoryId, "scope": claim.scope,
                                                  "recordType": "claim", "semanticKey": claim.semanticKey})
    if claim.claimId != expected_id:
        return "claim-identity-mismatch"

    expected_evidence: set[str] = set()
    for fact in selected_facts:
        if tuple(sorted(set(fact.evidenceIds))) != fact.evidenceIds or not fact.evidenceIds:
            return "fact-evidence-ids-invalid"
        expected_evidence.update(fact.evidenceIds)
        for evidence_id in fact.evidenceIds:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                return "unknown-evidence"
            if evidence.snapshotId != snapshot.snapshotId or evidence.availability != "available":
                return "evidence-snapshot-or-availability-invalid"
            if fact.factType == "artifact.presence" and fact.value.get("path") != evidence.path:
                return "artifact-evidence-path-mismatch"
            locator_error = _locator_error(evidence.locator, evidence.path)
            if locator_error:
                return locator_error
            matching_reads = [read for read in reads_by_rule.get(fact.ruleId, ())
                              if read.ruleVersion == fact.ruleVersion and read.snapshotId == evidence.snapshotId
                              and read.dependencyType == "file" and read.outcome == "present"
                              and read.selector.type == "file" and read.selector.scope == fact.scope
                              and read.selector.value == evidence.path and read.contentHash == evidence.contentHash]
            if len(matching_reads) != 1:
                return "evidence-read-provenance-mismatch"
    if tuple(sorted(expected_evidence)) != claim.evidenceIds:
        return "claim-evidence-chain-mismatch"
    if len(set(claim.evidenceIds)) != len(claim.evidenceIds):
        return "claim-evidence-ids-duplicate"
    return None


def _prose_support_error(claim: Claim, run: RunLedger, facts_by_id: dict[str, object],
                         evidence_by_id: dict[str, object], reads_by_rule: dict[str, list[object]],
                         run_error: str | None) -> str | None:
    """Validate model-authored text against captured source-fragment records."""
    if run_error:
        return run_error
    snapshot = run.snapshot
    if claim.lastVerifiedSnapshotId != snapshot.snapshotId:
        return "claim-snapshot-mismatch"
    if claim.kind != "inferred" or claim.origin != "llm":
        return "prose-origin-invalid"
    if claim.renderedText is None or not claim.renderedText.strip() or claim.templateKey is not None:
        return "prose-text-invalid"
    if claim.payload != {"text": claim.renderedText}:
        return "prose-payload-mismatch"
    if not claim.factIds or tuple(sorted(set(claim.factIds))) != claim.factIds:
        return "claim-fact-ids-invalid"
    selected_facts = []
    for fact_id in claim.factIds:
        fact = facts_by_id.get(fact_id)
        if fact is None:
            return "unknown-fact"
        selected_facts.append(fact)
    if any(fact.scope != claim.scope or fact.snapshotId != snapshot.snapshotId
           or fact.factType != "source.fragment.v1" or fact.ruleId != "maintenance.source-fragment"
           for fact in selected_facts):
        return "prose-fact-support-invalid"
    expected_id = canonical_record_id("claim", {"repository": snapshot.repositoryId,
                                                   "scope": claim.scope,
                                                   "recordType": "claim",
                                                   "semanticKey": claim.semanticKey})
    if claim.claimId != expected_id:
        return "claim-identity-mismatch"
    expected_evidence: set[str] = set()
    for fact in selected_facts:
        if not fact.evidenceIds or tuple(sorted(set(fact.evidenceIds))) != fact.evidenceIds:
            return "fact-evidence-ids-invalid"
        expected_evidence.update(fact.evidenceIds)
        for evidence_id in fact.evidenceIds:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                return "unknown-evidence"
            if (evidence.snapshotId != snapshot.snapshotId or evidence.availability != "available"
                    or evidence.contentHash is None or evidence.locator.kind != "line-range"):
                return "prose-evidence-invalid"
            if _locator_error(evidence.locator, evidence.path):
                return "prose-evidence-locator-invalid"
            matching_reads = [read for read in reads_by_rule.get(fact.ruleId, ())
                              if read.extractorId == "maintenance.source-fragment"
                              and read.ruleVersion == fact.ruleVersion
                              and read.snapshotId == evidence.snapshotId
                              and read.dependencyType == "file" and read.outcome == "present"
                              and read.selector.type == "file" and read.selector.scope == fact.scope
                              and read.selector.value == evidence.path
                              and read.contentHash == evidence.contentHash]
            if len(matching_reads) != 1:
                return "evidence-read-provenance-mismatch"
    if tuple(sorted(expected_evidence)) != claim.evidenceIds or len(set(claim.evidenceIds)) != len(claim.evidenceIds):
        return "claim-evidence-chain-mismatch"
    return None


def validate_claims(claims: Sequence[Claim], run: RunLedger) -> ValidationResult:
    """Validate complete claim-to-read proof chains and apply initial auto-accept policy."""
    facts_by_id = _unique_index(run.facts, "factId")
    evidence_by_id = _unique_index(run.evidence, "evidenceId")
    reads_by_rule: dict[str, list[object]] = defaultdict(list)
    for read in run.reads:
        reads_by_rule[read.ruleId].append(read)
    run_error = _snapshot_reads_error(run)
    valid: list[Claim] = []
    rejected: list[Claim] = []
    diagnostics: list[Diagnostic] = []
    for claim in claims:
        reason = _support_error(claim, run, facts_by_id, evidence_by_id, reads_by_rule, run_error)
        if reason is not None:
            rejected.append(replace(claim, proposalStatus="pending"))
            diagnostics.append(Diagnostic(1, f"claim.invalid.{reason}", "warning", reason.replace("-", " "),
                                          None, None, {"claimId": claim.claimId, "reasonCode": reason}))
            continue
        status = ("accepted" if claim.kind == "observed" and claim.origin == "deterministic"
                  and run.effectiveClaimPolicy.autoAcceptObserved else "pending")
        valid.append(replace(claim, proposalStatus=status))
    return ValidationResult(tuple(valid), tuple(rejected), tuple(diagnostics))
