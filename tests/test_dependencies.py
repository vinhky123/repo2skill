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
    assert recheck_dependencies(run, identity, policy_revision, {"sample": "1"}, root=tmp_path, current_root_label="repo", excluded_paths=()).status == "fresh"
    (tmp_path / "input.txt").write_bytes(b"two")
    result = recheck_dependencies(run, identity, policy_revision, {"sample": "1"}, root=tmp_path, current_root_label="repo", excluded_paths=())
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
    result = recheck_dependencies(run, identity, policy_revision, {"docs": "1"}, root=tmp_path, current_root_label="repo", excluded_paths=())
    assert result.status == "stale"
    assert result.affectedClaimIds == (claim_id,)


def test_root_label_change_is_stale_and_affects_every_claim(tmp_path: Path):
    policy, identity, inventory, read, snapshot, policy_revision = _capture(tmp_path)
    claim_id = canonical_record_id("claim", {"test": "root-label"})
    claim = Claim(1, claim_id, 1, "root-label", ".", {"text": "A claim"}, "A claim", None, "observed", "deterministic", (), (), "pending", (), (), snapshot.snapshotId)
    run = RunLedger("1.0", "0.1.0", "run_222222222222222222222222", snapshot, policy, default_claim_policy(), (), (read,), (), (), (claim,), (), (), (), {}, True)
    result = recheck_dependencies(run, identity, policy_revision, {"sample": "1"}, root=tmp_path, current_root_label="renamed", excluded_paths=())
    assert result.status == "stale"
    assert result.affectedClaimIds == (claim_id,)
    assert any(item.code == "dependency.root-label-changed" for item in result.findings)


def _run_for_selectors(tmp_path: Path, selectors):
    policy = default_scan_policy()
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    identity = capture_repository(tmp_path, inventory, "no-git", project_id="project_000000000000000000000000", policy=policy)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_333333333333333333333333", inventory=inventory,
            policy=policy, repository_id=identity.repositoryId, prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        reads = tuple(session.bind("sample", "1", f"sample.rule{index}", "1").read(selector).dependency for index, selector in enumerate(selectors))
    policy_revision = effective_policy_revision(policy, default_claim_policy(), (), generation_options={})
    snapshot = build_snapshot("snapshot_333333333333333333333333", identity, inventory, {"sample": "1"}, reads, policy_revision=policy_revision, root_label="repo")
    run = RunLedger("1.0", "0.1.0", "run_333333333333333333333333", snapshot, policy, default_claim_policy(), (), reads, (), (), (), (), (), (), {}, True)
    return policy, identity, reads, policy_revision, run


def test_glob_addition_and_deletion_change_only_recorded_glob(tmp_path: Path):
    (tmp_path / "one.json").write_text("{}", encoding="utf-8")
    selector = InputSelector("glob", "*.json", ".", False)
    policy, identity, reads, revision, run = _run_for_selectors(tmp_path, (selector,))
    (tmp_path / "two.json").write_text("{}", encoding="utf-8")
    added = recheck_dependencies(run, identity, revision, {"sample": "1"}, root=tmp_path, current_root_label="repo", excluded_paths=())
    assert added.status == "stale" and any(item.code == "dependency.changed" for item in added.findings)
    (tmp_path / "two.json").unlink()
    deleted = recheck_dependencies(run, identity, revision, {"sample": "1"}, root=tmp_path, current_root_label="repo", excluded_paths=())
    assert deleted.status == "fresh"


def test_negative_predicate_change_is_stale(tmp_path: Path):
    selector = InputSelector("negative", "filename-absent:Cargo.toml", ".", False)
    _, identity, _, revision, run = _run_for_selectors(tmp_path, (selector,))
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    result = recheck_dependencies(run, identity, revision, {"sample": "1"}, root=tmp_path, current_root_label="repo", excluded_paths=())
    assert result.status == "stale"
    assert any(item.code == "dependency.changed" for item in result.findings)


def test_policy_identity_and_extractor_changes_are_stale_for_all_claims(tmp_path: Path):
    policy, identity, _, revision, run = _run_for_selectors(tmp_path, (InputSelector("glob", "*.json", ".", False),))
    claim_ids = (canonical_record_id("claim", {"case": "one"}), canonical_record_id("claim", {"case": "two"}))
    claims = tuple(Claim(1, claim_id, 1, claim_id, ".", {"text": claim_id}, claim_id, None, "observed", "deterministic", (), (), "pending", (), (), run.snapshot.snapshotId) for claim_id in claim_ids)
    run = replace(run, claims=claims)
    assert len(run.claims) == 2
    cases = (
        (revision + "x", identity, {"sample": "1"}, True),
        (revision, replace(identity, branch="changed"), {"sample": "1"}, True),
        (revision, identity, {"sample": "2"}, False),
    )
    for current_revision, current_identity, versions, affects_all in cases:
        result = recheck_dependencies(run, current_identity, current_revision, versions, root=tmp_path, current_root_label="repo", excluded_paths=())
        assert result.status == "stale"
        if affects_all:
            assert result.affectedClaimIds == tuple(sorted(claim_ids))


def test_recheck_keeps_evaluating_after_unknown_and_stale_wins(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    selectors = (InputSelector("file", "a.txt", ".", True), InputSelector("file", "b.txt", ".", True))
    _, identity, _, revision, run = _run_for_selectors(tmp_path, selectors)
    (tmp_path / "a.txt").unlink()
    (tmp_path / "b.txt").write_bytes(b"\xff")
    result = recheck_dependencies(run, identity, revision, {"sample": "1"}, root=tmp_path, current_root_label="repo", excluded_paths=())
    assert result.status == "stale"
    assert any(item.code == "dependency.changed" for item in result.findings)
    assert any(item.code == "dependency.read-unknown" for item in result.findings)
