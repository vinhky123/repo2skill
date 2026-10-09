from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from repo2skill.config import default_claim_policy, default_scan_policy
from repo2skill.dependencies import build_snapshot
from repo2skill.ids import canonical_json_bytes, effective_policy_revision
from repo2skill.identity import capture_repository
from repo2skill.inventory import collect_inventory
from repo2skill.ledger import LedgerStore, encode_record
from repo2skill.models import Claim, RunLedger
from repo2skill.checks import check_freshness


def _run(root: Path) -> tuple[RunLedger, object, str]:
    scan = default_scan_policy()
    claim = default_claim_policy()
    inventory = collect_inventory(root, scan)
    identity = capture_repository(root, inventory, "no-git", project_id="project_0123456789abcdef01234567", policy=scan)
    revision = effective_policy_revision(scan, claim, (), generation_options={})
    snapshot = build_snapshot("snapshot_0123456789abcdef01234567", identity, inventory, {}, (),
                              policy_revision=revision, root_label="repo")
    run = RunLedger("1.0", "0.1.0", "run_0123456789abcdef01234567", snapshot,
                    scan, claim, (), (), (), (), (), (), (), (), {}, True)
    return run, identity, revision


def test_check_freshness_passes_explicit_root_label_to_shared_recheck(tmp_path: Path) -> None:
    run, identity, revision = _run(tmp_path)
    claim_ids = ("claim_0123456789abcdef01234567", "claim_1123456789abcdef01234567")
    claims = tuple(Claim(1, claim_id, 1, claim_id, ".", {"text": claim_id}, claim_id, None,
                         "observed", "deterministic", (), (), "pending", (), (), run.snapshot.snapshotId)
                   for claim_id in claim_ids)
    run = replace(run, claims=claims)
    result = check_freshness(run, identity, revision, {}, root=tmp_path,
                             current_root_label="renamed", excluded_paths=())
    assert result.sourceStatus == "stale"
    assert result.affectedClaimIds == claim_ids
    assert result.reviewStatus == "pending"
    assert any(finding.code == "dependency.root-label-changed" for finding in result.findings)


def test_list_run_ids_is_read_only_and_fails_closed_on_corrupt_history(tmp_path: Path) -> None:
    state_dir = tmp_path / ".repo2skill"
    runs = state_dir / "runs"
    runs.mkdir(parents=True)
    run_id = "run_0123456789abcdef01234567"
    run, _, _ = _run(tmp_path)
    # The document decodes fully, then fails the path/document identity check.
    run = replace(run, runId="run_1123456789abcdef01234567")
    (runs / f"{run_id}.json").write_bytes(canonical_json_bytes(encode_record(run)))
    store = LedgerStore(tmp_path, state_dir)
    with pytest.raises(ValueError, match="run document ID"):
        store.list_run_ids()
    assert not (state_dir / "lock").exists()
    assert not (state_dir / "state.json").exists()


def test_list_run_ids_rejects_unexpected_entries_in_history_directory(tmp_path: Path) -> None:
    runs = tmp_path / ".repo2skill" / "runs"
    runs.mkdir(parents=True)
    (runs / "notes.txt").write_text("not a run", encoding="utf-8")
    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    with pytest.raises(ValueError):
        store.list_run_ids()


def test_list_run_ids_returns_sorted_valid_history_without_creating_lock(tmp_path: Path) -> None:
    state_dir = tmp_path / ".repo2skill"
    runs = state_dir / "runs"
    runs.mkdir(parents=True)
    run, _, _ = _run(tmp_path)
    (runs / f"{run.runId}.json").write_bytes(canonical_json_bytes(encode_record(run)))
    store = LedgerStore(tmp_path, state_dir)
    assert store.list_run_ids() == (run.runId,)
    assert not (state_dir / "lock").exists()
    assert not (state_dir / "state.json").exists()


def test_list_run_ids_with_absent_state_directory_does_not_create_it(tmp_path: Path) -> None:
    state_dir = tmp_path / ".repo2skill"
    store = LedgerStore(tmp_path, state_dir)
    assert store.list_run_ids() == ()
    assert not state_dir.exists()
