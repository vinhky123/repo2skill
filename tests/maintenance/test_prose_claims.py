from __future__ import annotations

from dataclasses import replace
import dataclasses
import hashlib
import json
from pathlib import Path
import subprocess
import pytest

import repo2skill.maintenance_context as maintenance_context
from repo2skill.claims import derive_claims
from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.dependencies import build_snapshot
from repo2skill.ids import canonical_record_id
from repo2skill.ledger import LedgerStore, decode_root_document, encode_record
from repo2skill.inventory import IgnoreControlObservation
from repo2skill.models import (
    EvidenceLocator, InputSelector, InventoryEntry, InventoryResult, ProjectDocument,
    ProjectState, RepositoryIdentity, RunLedger,
)
from repo2skill.maintenance_git import compare_commits, enqueue_event
from repo2skill.maintenance_models import ClaimOperation, CommittedSourceGuard, GitEvent, MaintenancePolicy
from repo2skill.maintenance_store import MaintenanceStore
from repo2skill.maintenance_approval import approve_job
from repo2skill.maintenance_context import (
    authorize_claim_revisions, build_committed_context, build_maintenance_run,
    build_prose_claim, recheck_committed_source,
)
from repo2skill.pipeline import collect_scan
from repo2skill.maintenance_source_fragment import RULE_ID
from repo2skill.extractors.base import ExtractionSession
from repo2skill.maintenance_source_fragment import SourceFragmentExtractor
from repo2skill.validation import validate_claims
from test_validation import _run

PATH = "pyproject.toml"


def test_legacy_claim_defaults_to_typed() -> None:
    run, fact, _evidence = _run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=run.snapshot.repositoryId).claims[0]
    assert claim.supportMode == "typed"


def test_old_p0_run_fixture_decodes_with_new_optional_defaults() -> None:
    run, fact, _evidence = _run()
    claim = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=run.snapshot.repositoryId).claims[0]
    raw = encode_record(replace(run, claims=(claim,)))
    raw.pop("applicationAuthorizationId")
    raw["claims"][0].pop("supportMode")
    reopened = decode_root_document(raw, RunLedger)
    assert reopened.applicationAuthorizationId is None
    assert reopened.claims[0].supportMode == "typed"


def test_evidence_backed_prose_requires_exact_fact_evidence_union() -> None:
    run, fact, _evidence = _run()
    prose_fact = replace(fact, factType="source.fragment.v1", ruleId=RULE_ID, ruleVersion="1",
                         semanticKey="source.fragment.v1:pyproject", value={"path": PATH, "start": 1, "end": 1, "text": "Use pytest."})
    evidence = replace(_evidence, locator=EvidenceLocator("line-range", [1, 1], 1))
    old_read = run.reads[0]
    read = replace(old_read, extractorId="maintenance.source-fragment", extractorVersion="1",
                   ruleId=RULE_ID, ruleVersion="1")
    versions = {**run.snapshot.extractorVersions, "maintenance.source-fragment": "1"}
    identity = RepositoryIdentity(run.snapshot.repositoryId, Path("."), "no-git", None, None, False, {})
    inventory = InventoryResult(Path("."), (), run.snapshot.inventoryHash, (), ())
    snapshot = build_snapshot(run.snapshot.snapshotId, identity, inventory, versions, (read,),
                              policy_revision=run.snapshot.policyRevision, root_label=run.snapshot.rootLabel)
    prose_run = replace(run, snapshot=snapshot, reads=(read,), facts=(prose_fact,), evidence=(evidence,))
    base = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=run.snapshot.repositoryId).claims[0]
    claim_id = canonical_record_id("claim", {"repository": snapshot.repositoryId, "scope": prose_fact.scope,
                                               "recordType": "claim", "semanticKey": prose_fact.semanticKey})
    prose_claim = replace(base, claimId=claim_id, semanticKey=prose_fact.semanticKey, payload={"text": "Use pytest."},
                          renderedText="Use pytest.", templateKey=None, kind="inferred", origin="llm",
                          factIds=(prose_fact.factId,), supportMode="evidence-backed-prose")
    result = validate_claims((prose_claim,), prose_run)
    assert len(result.validClaims) == 1
    assert result.validClaims[0].proposalStatus == "pending"

    forged = replace(prose_claim, evidenceIds=())
    result = validate_claims((forged,), prose_run)
    assert result.validClaims == ()
    assert result.rejectedClaims == (replace(forged, proposalStatus="pending"),)


def test_prose_cannot_be_observed_or_human_policy() -> None:
    run, fact, _evidence = _run()
    prose_fact = replace(fact, factType="source.fragment.v1", ruleId=RULE_ID, ruleVersion="1",
                         semanticKey="source.fragment.v1:pyproject", value={"path": PATH, "start": 1, "end": 1, "text": "Use pytest."})
    evidence = replace(_evidence, locator=EvidenceLocator("line-range", [1, 1], 1))
    read = replace(run.reads[0], extractorId="maintenance.source-fragment", extractorVersion="1",
                   ruleId=RULE_ID, ruleVersion="1")
    versions = {**run.snapshot.extractorVersions, "maintenance.source-fragment": "1"}
    identity = RepositoryIdentity(run.snapshot.repositoryId, Path("."), "no-git", None, None, False, {})
    inventory = InventoryResult(Path("."), (), run.snapshot.inventoryHash, (), ())
    snapshot = build_snapshot(run.snapshot.snapshotId, identity, inventory, versions, (read,),
                              policy_revision=run.snapshot.policyRevision, root_label=run.snapshot.rootLabel)
    prose_run = replace(run, snapshot=snapshot, reads=(read,), facts=(prose_fact,), evidence=(evidence,))
    typed = derive_claims((fact,), run.effectiveClaimPolicy, repository_id=run.snapshot.repositoryId).claims[0]
    claim_id = canonical_record_id("claim", {"repository": snapshot.repositoryId, "scope": prose_fact.scope,
                                               "recordType": "claim", "semanticKey": prose_fact.semanticKey})
    claim = replace(typed, claimId=claim_id, semanticKey=prose_fact.semanticKey, payload={"text": "Use pytest."},
                    renderedText="Use pytest.", templateKey=None, kind="inferred", origin="llm",
                    factIds=(prose_fact.factId,), supportMode="evidence-backed-prose")
    for kind, origin in (("observed", "llm"), ("human_policy", "human")):
        result = validate_claims((replace(claim, kind=kind, origin=origin),), prose_run)
        assert result.validClaims == ()


def test_source_fragment_fact_registers_captured_blob_hash_and_locator(tmp_path: Path) -> None:
    raw = b"from pathlib import Path\nprint('captured')\n"
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    snapshot_id = "snapshot_aaaaaaaaaaaaaaaaaaaaaaaa"
    empty_controls = (IgnoreControlObservation(InputSelector("glob", "**/.gitignore", ".", True),
                                               "no-match", None, (),
                                               "sha256:" + hashlib.sha256(b"[]").hexdigest(), 0, 0),)
    inventory = InventoryResult(tmp_path, (InventoryEntry(PATH, "file", len(raw), digest, False, False, None),),
                                "sha256:" + "a" * 64, (), ())
    with ExtractionSession(root=tmp_path, snapshot_id=snapshot_id, inventory=inventory,
                           policy=default_scan_policy(), repository_id="repo_0123456789abcdef01234567",
                           prior_evidence=(), prior_facts=(), excluded_paths=(),
                           control_observations=empty_controls,
                           committed_reader=lambda path, cap: raw) as session:
        extractor = SourceFragmentExtractor(session)
        ctx = session.bind("maintenance.source-fragment", "1", RULE_ID, "1")
        batch = extractor.extract(ctx, extractor.discover(ctx))
    assert batch.facts[0].factType == "source.fragment.v1"
    assert batch.evidence[0].contentHash == digest
    assert batch.evidence[0].locator == EvidenceLocator("line-range", [1, 2], 1)


@pytest.mark.parametrize(("reader", "expected"), [
    (lambda _path, _cap: b"too large", "capped"),
    (lambda _path, _cap: (_ for _ in ()).throw(OSError("fixture read failed")), "io-error"),
])
def test_incomplete_source_fragment_read_rejects_context_before_agent_use(
    tmp_path: Path, reader, expected: str,
) -> None:
    raw = b"oversized input"
    inventory = InventoryResult(
        tmp_path, (InventoryEntry(PATH, "file", len(raw), None, False, False, None),),
        "sha256:" + "a" * 64, (), (),
    )
    controls = (IgnoreControlObservation(InputSelector("glob", "**/.gitignore", ".", True),
                                         "no-match", None, (),
                                         "sha256:" + hashlib.sha256(b"[]").hexdigest(), 0, 0),)
    policy = replace(default_scan_policy(), maxFileBytes=4)
    with ExtractionSession(
        root=tmp_path, snapshot_id="snapshot_aaaaaaaaaaaaaaaaaaaaaaaa", inventory=inventory,
        policy=policy, repository_id="repo_0123456789abcdef01234567", prior_evidence=(),
        prior_facts=(), excluded_paths=(), control_observations=controls,
        committed_reader=reader,
    ) as session:
        extractor = SourceFragmentExtractor(session)
        ctx = session.bind("maintenance.source-fragment", "1", RULE_ID, "1")
        batch = extractor.extract(ctx, extractor.discover(ctx))

    assert batch.reads[0].outcome == expected
    with pytest.raises(ValueError, match="maintenance.source-fragment-incomplete"):
        maintenance_context.require_complete_source_fragments(batch.reads, batch.coverage)


def test_committed_inventory_applies_entry_cap_and_reports_required_coverage(tmp_path: Path) -> None:
    policy = replace(default_scan_policy(), maxInventoryEntries=2)
    manifest = {"files": [
        {"path": path, "size": 1, "rawHash": "sha256:" + "a" * 64}
        for path in ("a.py", "b.py", "c.py")
    ]}

    inventory = maintenance_context._inventory_from_manifest(tmp_path, manifest, policy, ())

    assert tuple(entry.path for entry in inventory.entries) == ("a.py", "b.py")
    assert any(item.code == "inventory.entry-cap" for item in inventory.diagnostics)
    assert any(item.status == "capped" and item.reasonCode == "inventory.entry-cap"
               for item in inventory.coverage)
    with pytest.raises(ValueError, match="inventory.entry-cap"):
        maintenance_context.require_complete_inventory(inventory)


def test_changed_blob_creates_new_evidence_revision_from_captured_bytes(tmp_path: Path) -> None:
    repo_id = "repo_0123456789abcdef01234567"
    selector = InputSelector("glob", "**/.gitignore", ".", True)
    controls = (IgnoreControlObservation(selector, "no-match", None, (),
                                         "sha256:" + hashlib.sha256(b"[]").hexdigest(), 0, 0),)

    def extract(raw: bytes, snapshot_id: str, prior_evidence=(), prior_facts=()):
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        inventory = InventoryResult(tmp_path, (InventoryEntry(PATH, "file", len(raw), digest, False, False, None),),
                                    "sha256:" + "a" * 64, (), ())
        with ExtractionSession(root=tmp_path, snapshot_id=snapshot_id, inventory=inventory,
                               policy=default_scan_policy(), repository_id=repo_id,
                               prior_evidence=prior_evidence, prior_facts=prior_facts,
                               excluded_paths=(), control_observations=controls,
                               committed_reader=lambda path, cap: raw) as session:
            extractor = SourceFragmentExtractor(session)
            ctx = session.bind("maintenance.source-fragment", "1", RULE_ID, "1")
            return extractor.extract(ctx, extractor.discover(ctx))

    before = extract(b"line one\nline two\n", "snapshot_aaaaaaaaaaaaaaaaaaaaaaaa")
    after = extract(b"changed one\nline two\n", "snapshot_bbbbbbbbbbbbbbbbbbbbbbbb",
                    before.evidence, before.facts)
    old, new = before.evidence[0], after.evidence[0]
    expected_id = canonical_record_id("evidence", {
        "repository": repo_id, "path": PATH, "locator": new.locator,
        "semanticKey": new.semanticKey,
    })
    assert new.evidenceId == old.evidenceId == expected_id
    assert new.revision == old.revision + 1
    assert new.contentHash != old.contentHash
    assert new.snapshotId == "snapshot_bbbbbbbbbbbbbbbbbbbbbbbb"


def test_build_committed_context_uses_target_commit_even_with_dirty_worktree(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    def git(*args: str) -> bytes:
        return subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True).stdout
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (root / ".gitignore").write_text(
        ".repo2skill/\n.agents/\nAGENTS.md\nignored-force.txt\nexcluded.bin\n", encoding="utf-8",
    )
    source = root / "src.py"
    source.write_text("print('base')\n", encoding="utf-8")
    (root / "ignored-force.txt").write_text("committed despite ignore\n", encoding="utf-8")
    (root / "excluded.bin").write_bytes(b"x" * (17 * 1024 * 1024))
    git("add", ".gitignore", "src.py")
    git("add", "-f", "excluded.bin")
    git("add", "-f", "ignored-force.txt")
    git("commit", "-qm", "base")
    baseline = git("rev-parse", "HEAD").decode().strip()

    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    policy = replace(default_scan_policy(), exclude=("excluded.bin",))
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture",
                               ".repo2skill", "worktree", policy,
                               default_claim_policy(), default_targets(root))
    initial_state = ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(initial_state)), encoding="utf-8")
    store = LedgerStore(root, state_dir)
    products = collect_scan(root, project)
    with store.mutation():
        store.write_immutable(f"runs/{products.run.runId}.json", encode_record(products.run))
        store.replace_state(replace(store.read_state(), currentRunId=products.run.runId))
    maint = MaintenanceStore(root, store)
    with store.mutation():
        maint.replace_state(replace(maint.read_state(), appliedBaseline=baseline, enabled=True,
                                    policy=MaintenancePolicy("ask", 1, ("skill",))))

    source.write_text("print('committed target')\n", encoding="utf-8")
    (root / "excluded.bin").write_bytes(b"y" * (17 * 1024 * 1024))
    git("add", "src.py")
    git("add", "-f", "excluded.bin")
    git("commit", "-qm", "target")
    target = git("rev-parse", "HEAD").decode().strip()
    event = GitEvent("git-event_0123456789abcdef01234567", "post-commit", "cursor-target", target)
    job = enqueue_event(store, event, compare_commits(root, baseline, target))
    assert job is not None
    approve_job(store, job.jobId)
    source.write_text("print('uncommitted secret')\n", encoding="utf-8")
    context = build_committed_context(store, job)
    fragments = [fact for fact in context.facts if fact.factType == "source.fragment.v1"]
    assert fragments
    assert any("committed target" in fact.value["text"] for fact in fragments)
    assert all("uncommitted secret" not in fact.value["text"] for fact in fragments)
    assert context.snapshot.commitId == target
    fragment_paths = {fact.value["path"] for fact in fragments}
    assert "ignored-force.txt" in fragment_paths
    assert "excluded.bin" not in fragment_paths
    assert "excluded.bin" not in context.changedDiff.decode("utf-8")
    change_projection = json.loads(context.changedDiff.decode("utf-8"))
    assert change_projection["projection"] == "committed-change-status"
    assert change_projection["changeStatuses"] == [["M", "src.py"]]

    fragment = next(fact for fact in fragments if fact.value["path"] == "src.py")
    operation = ClaimOperation("add", "maintenance:source-guidance:python", ".",
                               "The committed Python source prints a message.",
                               (fragment.factId,), tuple(sorted(fragment.evidenceIds)), None)
    candidate = build_prose_claim(operation, context)
    state_before = store.read_state()
    assert state_before is not None
    event_ids_before = state_before.reviewEvents
    lifecycle = maint.read_state().jobs[job.jobId]
    assert lifecycle.approvalBindingHash is not None
    no_claim_authorization = authorize_claim_revisions(
        job, lifecycle.approvalBindingHash, "sha256:" + "e" * 64, (),
    )
    carried_context = replace(
        context,
        priorClaims=(replace(candidate, lastVerifiedSnapshotId="snapshot_aaaaaaaaaaaaaaaaaaaaaaaa",
                              proposalStatus="accepted"),),
        priorFacts=tuple(replace(item, snapshotId="snapshot_aaaaaaaaaaaaaaaaaaaaaaaa")
                         for item in context.facts),
        priorEvidence=tuple(replace(item, snapshotId="snapshot_aaaaaaaaaaaaaaaaaaaaaaaa")
                            for item in context.evidence),
    )
    carried_bundle = build_maintenance_run(store, job, carried_context, (), no_claim_authorization)
    carried = next(claim for claim in carried_bundle.run.claims if claim.claimId == candidate.claimId)
    assert carried.lastVerifiedSnapshotId == context.snapshot.snapshotId
    assert all(fact.snapshotId == context.snapshot.snapshotId for fact in carried_bundle.run.facts)

    changed_hash = "sha256:" + hashlib.sha256(b"different committed source bytes").hexdigest()
    changed_fragment = replace(
        fragment, factRevision=fragment.factRevision + 1,
        value={**fragment.value, "text": "different committed source bytes"},
    )
    fragment_evidence = next(item for item in context.evidence
                             if item.evidenceId in fragment.evidenceIds)
    changed_evidence = replace(fragment_evidence, revision=fragment_evidence.revision + 1,
                               contentHash=changed_hash)
    changed_facts = tuple(changed_fragment if item.factId == fragment.factId else item
                          for item in context.facts)
    changed_evidence_records = tuple(changed_evidence if item.evidenceId == fragment_evidence.evidenceId else item
                                     for item in context.evidence)
    changed_reads = tuple(
        replace(read, contentHash=changed_hash)
        if read.extractorId == "maintenance.source-fragment" and read.selector.value == "src.py"
        else read for read in context.reads
    )
    changed_identity = RepositoryIdentity(
        context.snapshot.repositoryId, context.inventory.root, context.snapshot.gitMode,
        context.snapshot.commitId, context.snapshot.branch, context.snapshot.dirty,
        context.snapshot.dirtySummary,
    )
    changed_snapshot = build_snapshot(
        context.snapshot.snapshotId, changed_identity, context.inventory,
        context.snapshot.extractorVersions, changed_reads,
        policy_revision=context.snapshot.policyRevision, root_label=context.snapshot.rootLabel,
    )
    changed_context = replace(
        carried_context, snapshot=changed_snapshot, facts=changed_facts,
        evidence=changed_evidence_records, reads=changed_reads,
    )
    changed_bundle = build_maintenance_run(store, job, changed_context, (), no_claim_authorization)
    assert all(claim.claimId != candidate.claimId for claim in changed_bundle.run.claims)
    assert any(item.reasonCode == "prior-claim-support-unavailable"
               for item in changed_bundle.run.coverage)

    unsupported = replace(candidate, factIds=("fact_aaaaaaaaaaaaaaaaaaaaaaaa",),
                          evidenceIds=("evidence_aaaaaaaaaaaaaaaaaaaaaaaa",),
                          lastVerifiedSnapshotId="snapshot_aaaaaaaaaaaaaaaaaaaaaaaa")
    omitted_bundle = build_maintenance_run(
        store, job, replace(context, priorClaims=(unsupported,)), (), no_claim_authorization,
    )
    assert all(claim.claimId != unsupported.claimId for claim in omitted_bundle.run.claims)
    assert any(item.reasonCode == "prior-claim-support-unavailable" for item in omitted_bundle.run.coverage)

    invalid_carried = replace(candidate, renderedText="", payload={"text": ""},
                              lastVerifiedSnapshotId="snapshot_aaaaaaaaaaaaaaaaaaaaaaaa")
    invalid_bundle = build_maintenance_run(
        store, job, replace(context, priorClaims=(invalid_carried,)), (), no_claim_authorization,
    )
    assert all(claim.claimId != invalid_carried.claimId for claim in invalid_bundle.run.claims)
    assert any(item.reasonCode == "prior-claim-support-unavailable" for item in invalid_bundle.run.coverage)

    second_operation = replace(operation, semanticKey="maintenance:source-guidance:python:second",
                               renderedText="The committed Python source is a message example.")
    second_candidate = build_prose_claim(second_operation, context)
    authorization = authorize_claim_revisions(
        job, lifecycle.approvalBindingHash, "sha256:" + "f" * 64, (candidate, second_candidate),
    )
    bundle = build_maintenance_run(store, job, context, (operation, second_operation), authorization)
    prose = [claim for claim in bundle.run.claims if claim.supportMode == "evidence-backed-prose"]
    assert {claim.proposalStatus for claim in prose} == {"accepted"}
    assert len(prose) == 2
    validated_again = validate_claims(bundle.run.claims, bundle.run)
    assert all(claim.proposalStatus == "pending" for claim in validated_again.validClaims
               if claim.supportMode == "evidence-backed-prose")
    state_after = store.read_state()
    assert state_after is not None and state_after.reviewEvents == event_ids_before
    guard = CommittedSourceGuard(job.jobId, target, job.sourceContextHash,
                                 job.policyRevision, authorization.authorizationId)
    recheck_committed_source(store, guard)
    source.write_text("print('later commit')\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "-qm", "later")
    with pytest.raises(ValueError, match="HEAD differs"):
        recheck_committed_source(store, guard)
