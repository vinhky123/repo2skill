"""Immutable source snapshot identity and the single source freshness recheck."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import unicodedata
from typing import Literal

from .config import default_scan_policy
from .extractors.base import ExtractionSession
from .ids import canonical_json_bytes
from .inventory import IgnoreControlObservation, collect_inventory
from .models import (
    ClaimId, ExtractionRead, Finding, InventoryResult, RepositoryIdentity,
    RunLedger, ScanPolicy, Sha256, SnapshotId, SourceSnapshot,
)


@dataclass(frozen=True, slots=True)
class DependencyRecheckResult:
    status: Literal["fresh", "stale", "unknown"]
    affectedClaimIds: tuple[ClaimId, ...]
    findings: tuple[Finding, ...]


class ActivationFreshnessError(ValueError):
    """Final activation failed because the shared source recheck was not fresh."""

    def __init__(self, result: DependencyRecheckResult):
        self.result = result
        super().__init__(f"source dependencies are {result.status}; activation was not performed")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _identity_projection(identity: RepositoryIdentity) -> dict[str, object]:
    return {"repositoryId": identity.repositoryId, "gitMode": identity.gitMode,
            "commitId": identity.commitId, "branch": identity.branch,
            "dirty": identity.dirty, "dirtySummary": identity.dirtySummary}


def _selector_projection(read: ExtractionRead) -> dict[str, object]:
    selector = read.selector
    return {"type": selector.type, "value": unicodedata.normalize("NFC", selector.value),
            "scope": selector.scope, "required": selector.required}


def _read_projection(read: ExtractionRead) -> dict[str, object]:
    return {"extractorId": read.extractorId, "extractorVersion": read.extractorVersion,
            "ruleId": read.ruleId, "ruleVersion": read.ruleVersion,
            "dependencyType": read.dependencyType, "selector": _selector_projection(read),
            "required": read.required, "outcome": read.outcome,
            "contentHash": read.contentHash,
            "matchPaths": sorted(unicodedata.normalize("NFC", path) for path in read.matchPaths),
            "matchSetHash": read.matchSetHash}


def _read_sort(read: ExtractionRead) -> tuple[object, ...]:
    return (read.extractorId, read.ruleId, read.dependencyType,
            canonical_json_bytes(_selector_projection(read)), read.selector.scope)


def build_snapshot(snapshot_id: SnapshotId, identity: RepositoryIdentity,
                   inventory: InventoryResult, extractor_versions: Mapping[str, str],
                   reads: Sequence[ExtractionRead], *, policy_revision: str,
                   root_label: str) -> SourceSnapshot:
    if Path(os.path.abspath(identity.root)) != Path(os.path.abspath(inventory.root)):
        raise ValueError("identity and inventory roots differ")
    if not policy_revision.startswith("sha256:") or len(policy_revision) != 71:
        raise ValueError("policy_revision must be a sha256 digest")
    if not root_label:
        raise ValueError("root_label is required")
    logical: set[tuple[str, str, bytes]] = set()
    for read in reads:
        if read.snapshotId != snapshot_id:
            raise ValueError("read snapshotId differs from snapshot")
        if read.extractorId not in extractor_versions or extractor_versions[read.extractorId] != read.extractorVersion:
            raise ValueError("read extractor version differs from registered versions")
        if read.dependencyType != read.selector.type or read.required != read.selector.required:
            raise ValueError("read selector provenance mismatch")
        if read.selector.scope != ".":
            raise ValueError("P0 selector scope must be root '.'")
        value = read.selector.value
        if read.selector.type == "file":
            if "\\" in value or value.startswith("/") or any(part in {"", ".", ".."} for part in value.split("/")) or unicodedata.normalize("NFC", value) != value:
                raise ValueError("file selector must be a canonical NFC repository path")
        elif read.selector.type == "glob":
            if value.startswith("!") or value.startswith("/") or "\\" in value or any(part in {".", ".."} for part in value.split("/")):
                raise ValueError("glob selector must use positive root-relative syntax")
        elif not value.startswith("filename-absent:"):
            raise ValueError("unknown negative selector predicate")
        if "." not in read.ruleId or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", read.ruleId):
            raise ValueError("ruleId must be globally namespaced")
        if tuple(sorted(set(read.matchPaths))) != read.matchPaths or any(unicodedata.normalize("NFC", path) != path for path in read.matchPaths):
            raise ValueError("matchPaths must be sorted and unique")
        key = (read.extractorId, read.ruleId, canonical_json_bytes(_selector_projection(read)))
        if key in logical:
            raise ValueError("duplicate logical extraction read")
        logical.add(key)
    projections = [_read_projection(item) for item in sorted(reads, key=_read_sort)]
    digest = _digest({"repository": {**_identity_projection(identity), "rootLabel": root_label},
                      "inventory": inventory.inventoryHash, "policy": policy_revision,
                      "extractors": dict(sorted(extractor_versions.items())), "reads": projections})
    return SourceSnapshot(snapshot_id, identity.repositoryId, root_label, identity.gitMode,
                          identity.commitId, identity.branch, identity.dirty, identity.dirtySummary,
                          inventory.inventoryHash, digest, policy_revision,
                          dict(sorted(extractor_versions.items())), datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))


def _affected(run: RunLedger, changed_rules: set[str], changed_paths: set[str], *, global_change: bool = False) -> tuple[ClaimId, ...]:
    if global_change or "inventory.ignore-control" in changed_rules:
        return tuple(sorted(claim.claimId for claim in run.claims))
    facts = {fact.factId for fact in run.facts if fact.ruleId in changed_rules}
    claims = {claim.claimId for claim in run.claims if set(claim.factIds) & facts}
    docs_evidence = {e.evidenceId for e in run.evidence if e.semanticKey.startswith("documentation-line:") and e.path in changed_paths}
    for decision in run.dedupDecisions:
        if docs_evidence.intersection(decision.documentationEvidenceIds) or (any(rule.startswith("docs.") for rule in changed_rules) and decision.documentationEvidenceIds):
            claims.update(decision.candidateClaimIds)
    return tuple(sorted(claims))


def recheck_dependencies(run: RunLedger, current_identity: RepositoryIdentity,
                         current_policy_revision: str, current_extractor_versions: Mapping[str, str],
                         *, root: Path, current_root_label: str,
                         excluded_paths: Sequence[str]) -> DependencyRecheckResult:
    root = Path(os.path.abspath(root))
    if Path(os.path.abspath(current_identity.root)) != root:
        raise ValueError("current identity root differs from recheck root")
    if not current_root_label:
        raise ValueError("current_root_label is required")
    policy_changed = current_policy_revision != run.snapshot.policyRevision
    identity_changed = _identity_projection(current_identity) != {
        "repositoryId": run.snapshot.repositoryId, "gitMode": run.snapshot.gitMode,
        "commitId": run.snapshot.commitId, "branch": run.snapshot.branch,
        "dirty": run.snapshot.dirty, "dirtySummary": run.snapshot.dirtySummary}
    changed_rules: set[str] = set()
    changed_paths: set[str] = set()
    findings: list[Finding] = []
    stale = policy_changed or identity_changed
    unknown = False
    if policy_changed:
        findings.append(Finding(1, "dependency.policy-changed", "stale", "effective policy changed", None, None, (), {}))
    if identity_changed:
        findings.append(Finding(1, "dependency.identity-changed", "stale", "repository identity changed", None, None, (), {}))
    root_label_changed = current_root_label != run.snapshot.rootLabel
    stale = stale or root_label_changed
    if root_label_changed:
        findings.append(Finding(1, "dependency.root-label-changed", "stale", "repository root label changed", None, None, (), {}))
    versions = dict(current_extractor_versions)
    if versions != dict(run.snapshot.extractorVersions):
        changed_ids = {key for key in set(versions) | set(run.snapshot.extractorVersions) if versions.get(key) != run.snapshot.extractorVersions.get(key)}
        stale = True
        changed_rules.update(read.ruleId for read in run.reads if read.extractorId in changed_ids)
        findings.append(Finding(1, "dependency.extractor-changed", "stale", "extractor versions changed", None, None, (), {"extractors": sorted(changed_ids)}))
    current_controls: list[IgnoreControlObservation] = []
    current_inventory = collect_inventory(root, run.effectiveScanPolicy, excluded_paths=excluded_paths, control_sink=current_controls.append)
    with ExtractionSession(root=root, snapshot_id=run.snapshot.snapshotId, inventory=current_inventory,
                           policy=run.effectiveScanPolicy, repository_id=run.snapshot.repositoryId,
                           prior_evidence=run.evidence, prior_facts=run.facts,
                           excluded_paths=excluded_paths, control_observations=current_controls) as session:
        for index, prior in enumerate(run.reads):
            context = session.bind(prior.extractorId, prior.extractorVersion, prior.ruleId, prior.ruleVersion)
            current = context.read(prior.selector).dependency
            path = prior.selector.value if prior.selector.type == "file" else None
            if current.outcome in {"capped", "decode-error", "io-error"}:
                unknown = True
                changed_rules.add(prior.ruleId)
                if path:
                    changed_paths.add(path)
                findings.append(Finding(1, "dependency.read-unknown", "unknown", "recorded dependency could not be checked", path, None, (), {"ruleId": prior.ruleId, "required": prior.required}))
                continue
            if (current.outcome, current.contentHash, current.matchPaths, current.matchSetHash) != (prior.outcome, prior.contentHash, prior.matchPaths, prior.matchSetHash):
                stale = True
                changed_rules.add(prior.ruleId)
                if path:
                    changed_paths.add(path)
                changed_paths.update(set(prior.matchPaths) ^ set(current.matchPaths))
                findings.append(Finding(1, "dependency.changed", "stale", "recorded dependency changed", path, None, (), {"ruleId": prior.ruleId, "selectorType": prior.dependencyType}))
    affected = _affected(run, changed_rules, changed_paths, global_change=policy_changed or identity_changed or root_label_changed)
    status = "stale" if stale else "unknown" if unknown else "fresh"
    return DependencyRecheckResult(status, affected, tuple(findings))
