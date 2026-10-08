from dataclasses import replace
from pathlib import Path

from repo2skill.config import default_claim_policy, default_scan_policy
from repo2skill.dependencies import build_snapshot, recheck_dependencies
from repo2skill.extractors.base import ExtractionSession
from repo2skill.identity import capture_repository
from repo2skill.ids import effective_policy_revision
from repo2skill.inventory import collect_inventory
from repo2skill.ids import canonical_record_id
from repo2skill.models import Claim, DedupDecision, Evidence, EvidenceLocator, RunLedger, InputSelector


def _capture(tmp_path: Path):
    (tmp_path / "input.txt").write_bytes(b"one")
    policy = default_scan_policy()
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    identity = capture_repository(tmp_path, inventory, "no-git", project_id="project_000000000000000000000000", policy=policy)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id=identity.repositoryId, prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        read = session.bind("sample", "1", "sample.input", "1").read(InputSelector("file", "input.txt", ".", True)).dependency
    policy_revision = effective_policy_revision(policy, default_claim_policy(), (), generation_options={})
    snapshot = build_snapshot("snapshot_000000000000000000000000", identity, inventory, {"sample": "1"}, [read], policy_revision=policy_revision, root_label="repo")
    return policy, identity, inventory, read, snapshot, policy_revision


def test_snapshot_digest_ignores_snapshot_and_read_record_ids(tmp_path: Path):
    policy, identity, inventory, read, snapshot, policy_revision = _capture(tmp_path)
    another_read = replace(read, snapshotId="snapshot_111111111111111111111111", readId="read_111111111111111111111111")
    another = build_snapshot("snapshot_111111111111111111111111", identity, inventory, {"sample": "1"}, [another_read], policy_revision=policy_revision, root_label="repo")
    assert another.inputDigest == snapshot.inputDigest


def test_recheck_changes_only_recorded_selector_and_marks_changed_file_stale(tmp_path: Path):
    policy, identity, inventory, read, snapshot, policy_revision = _capture(tmp_path)
    claim_policy = default_claim_policy()
    run = RunLedger("1.0", "0.1.0", "run_000000000000000000000000", snapshot, policy, claim_policy, (),
                    (read,), (), (), (), (), (), (), {}, True)
    (tmp_path / "unobserved.txt").write_bytes(b"irrelevant")
    assert recheck_dependencies(run, identity, policy_revision, {"sample": "1"}, root=tmp_path, excluded_paths=()).status == "fresh"
    (tmp_path / "input.txt").write_bytes(b"two")
    result = recheck_dependencies(run, identity, policy_revision, {"sample": "1"}, root=tmp_path, excluded_paths=())
    assert result.status == "stale"
    assert any(item.code == "dependency.changed" for item in result.findings)


def test_documentation_change_affects_dedup_candidates_without_fact_link(tmp_path: Path):
    (tmp_path / "README.md").write_text("A useful sentence.\n", encoding="utf-8")
    policy = default_scan_policy()
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    identity = capture_repository(tmp_path, inventory, "no-git", project_id="project_000000000000000000000000", policy=policy)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_222222222222222222222222", inventory=inventory,
            policy=policy, repository_id=identity.repositoryId, prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        read = session.bind("docs", "1", "docs.normalized-line", "1").read(InputSelector("file", "README.md", ".", True)).dependency
    claim_id = canonical_record_id("claim", {"test": "claim"})
    evidence_id = canonical_record_id("evidence", {"test": "evidence"})
    claim = Claim(1, claim_id, 1, "claim", ".", {"text": "A claim"}, "A claim", None, "observed", "deterministic", (), (), "pending", (), (), "snapshot_222222222222222222222222")
    evidence = Evidence(1, evidence_id, 1, read.snapshotId, "README.md", read.contentHash, "documentation-line:A useful sentence.", "text/markdown", "utf-8-sig", EvidenceLocator("line-range", [1, 1], 1), "A useful sentence.", "available")
    policy_revision = effective_policy_revision(policy, default_claim_policy(), (), generation_options={})
    snapshot = build_snapshot(read.snapshotId, identity, inventory, {"docs": "1"}, [read], policy_revision=policy_revision, root_label="repo")
    decision = DedupDecision(1, canonical_record_id("decision", {"test": "dedup"}), read.snapshotId, (claim_id,), (), (evidence_id,), "dedup.docs", "1", "suppress", "documented")
    run = RunLedger("1.0", "0.1.0", "run_111111111111111111111111", snapshot, policy, default_claim_policy(), (), (read,), (evidence,), (), (claim,), (decision,), (), (), {}, True)
    (tmp_path / "README.md").write_text("A changed sentence.\n", encoding="utf-8")
    result = recheck_dependencies(run, identity, policy_revision, {"docs": "1"}, root=tmp_path, excluded_paths=())
    assert result.status == "stale"
    assert result.affectedClaimIds == (claim_id,)
