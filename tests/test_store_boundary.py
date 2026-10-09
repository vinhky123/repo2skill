from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import subprocess

import pytest

from repo2skill import ledger
from repo2skill import store_io
from repo2skill.config import default_claim_policy, default_scan_policy
from repo2skill.dependencies import build_snapshot
from repo2skill.ids import canonical_json_bytes, effective_policy_revision
from repo2skill.identity import capture_repository
from repo2skill.inventory import collect_inventory
from repo2skill.ledger import LedgerStore, encode_record
from repo2skill.models import ExportJournal, ProjectState
from repo2skill.models import RunLedger
from repo2skill.safe_fs import GuardedDirectory


def _make_directory_link(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except OSError:
        if os.name != "nt":
            return False
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0


@pytest.mark.parametrize("payload_kind", ["json", "bytes"])
def test_immutable_write_remains_inside_store_when_parent_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload_kind: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    store = LedgerStore(root, root / ".repo2skill")
    runs = store.state_dir / "runs"
    parked = store.state_dir / "runs-parked"
    original_mkstemp = store_io.tempfile.mkstemp
    swap_completed = False

    def swap_then_create(*args: object, **kwargs: object) -> tuple[int, str]:
        nonlocal swap_completed
        if not swap_completed:
            try:
                runs.rename(parked)
            except OSError:
                # Windows GuardedDirectory handles deny delete sharing.
                pass
            else:
                if _make_directory_link(runs, outside):
                    swap_completed = True
        return original_mkstemp(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_io.tempfile, "mkstemp", swap_then_create)
    with store.mutation():
        if payload_kind == "json":
            store.write_immutable("runs/probe.json", {"probe": "inside-only"})
        else:
            store.write_immutable_bytes("runs/probe.json", b"inside-only\n")

    assert not (outside / "probe.json").exists()
    assert (parked / "probe.json").exists() or (runs / "probe.json").exists()


def test_state_and_journal_replacement_create_parents_through_guarded_store_io(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = LedgerStore(root, root / ".repo2skill")
    state = ProjectState(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", None, None, (), {}, None, None
    )
    stamp = "2026-10-09T00:00:00Z"
    journal = ExportJournal(
        "1.0", "0.1.0", "export_0123456789abcdef01234567",
        "run_0123456789abcdef01234567", 1, (), (), {}, None,
        "prepared", stamp, stamp,
    )

    with store.mutation():
        assert store.read_state() is None
        store.replace_state(state)
        store.write_export_journal(journal)

    assert (store.state_dir / "state.json").is_file()
    assert (store.state_dir / "exports" / f"{journal.exportId}.json").is_file()


def test_failed_immutable_publication_removes_private_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = LedgerStore(root, root / ".repo2skill")

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected link failure")

    monkeypatch.setattr(store_io.os, "link", fail_link)
    with store.mutation(), pytest.raises(OSError, match="injected link failure"):
        store.write_immutable_bytes("runs/probe.bin", b"payload")

    assert list((store.state_dir / "runs").iterdir()) == []


def test_list_run_ids_rejects_dangling_state_redirect(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = LedgerStore(root, root / ".repo2skill")
    target = tmp_path / "state-target"
    target.mkdir()
    try:
        if not _make_directory_link(store.state_dir, target):
            pytest.skip("directory symlink or junction unavailable")
        target.rmdir()
    except OSError as error:
        pytest.skip(f"cannot create dangling directory redirect: {error}")
    assert not store.state_dir.exists()

    with pytest.raises(OSError):
        store.list_run_ids()


def test_mutation_revalidates_state_directory_created_concurrently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = LedgerStore(root, root / ".repo2skill")
    original_mkdir = store_io.os.mkdir
    raced = False

    def mkdir_after_competing_creator(path: object, *args: object, **kwargs: object) -> None:
        nonlocal raced
        if not raced and Path(path).name == ".repo2skill":
            raced = True
            original_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]
            raise FileExistsError(path)
        original_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(store_io.os, "mkdir", mkdir_after_competing_creator)
    with store.mutation():
        assert store.state_dir.is_dir()

    assert raced
    assert (store.state_dir / ".lock").is_file()


def _valid_run(root: Path) -> RunLedger:
    scan_policy = default_scan_policy()
    claim_policy = default_claim_policy()
    inventory = collect_inventory(root, scan_policy)
    identity = capture_repository(
        root,
        inventory,
        "no-git",
        project_id="project_0123456789abcdef01234567",
        policy=scan_policy,
    )
    policy_revision = effective_policy_revision(
        scan_policy, claim_policy, (), generation_options={}
    )
    snapshot = build_snapshot(
        "snapshot_0123456789abcdef01234567",
        identity,
        inventory,
        {},
        (),
        policy_revision=policy_revision,
        root_label="repo",
    )
    return RunLedger(
        "1.0", "0.1.0", "run_0123456789abcdef01234567", snapshot,
        scan_policy, claim_policy, (), (), (), (), (), (), (), (), {}, True,
    )


def test_list_run_ids_reads_each_run_under_its_guarded_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".repo2skill"
    runs = state_dir / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    run_id = "run_0123456789abcdef01234567"
    run = _valid_run(root)
    (runs / f"{run_id}.json").write_bytes(canonical_json_bytes(encode_record(run)))
    outside_run = dataclasses.replace(
        run, runId="run_1123456789abcdef01234567"
    )
    (outside / f"{run_id}.json").write_bytes(
        canonical_json_bytes(encode_record(outside_run))
    )
    store = LedgerStore(root, state_dir)
    original_reject = ledger._reject_redirected_ancestors
    swapped = False

    def validate_then_swap(path: Path) -> None:
        nonlocal swapped
        original_reject(path)
        if Path(path) == runs / f"{run_id}.json" and not swapped:
            runs.rename(state_dir / "runs-parked")
            if not _make_directory_link(runs, outside):
                pytest.skip("directory symlink or junction unavailable")
            swapped = True

    monkeypatch.setattr(ledger, "_reject_redirected_ancestors", validate_then_swap)
    assert store.list_run_ids() == (run_id,)
    assert not swapped


def test_load_json_reads_from_the_checked_parent_after_junction_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    runs = root / ".repo2skill" / "runs"
    runs.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    document = runs / "document.json"
    document.write_text('{"source":"inside"}', encoding="utf-8")
    (outside / document.name).write_text('{"source":"outside"}', encoding="utf-8")
    original_open_file = GuardedDirectory.open_file
    attempted_swap = False
    swapped = False

    def swap_before_guarded_open(parent: GuardedDirectory, name: str):
        nonlocal attempted_swap, swapped
        if parent.path == runs and name == document.name and not attempted_swap:
            attempted_swap = True
            try:
                runs.rename(runs.parent / "runs-parked")
            except OSError:
                # The retained Windows directory handle denies delete sharing.
                pass
            else:
                if not _make_directory_link(runs, outside):
                    pytest.skip("directory symlink or junction unavailable")
                swapped = True
        return original_open_file(parent, name)

    monkeypatch.setattr(GuardedDirectory, "open_file", swap_before_guarded_open)

    assert ledger._load_json(document) == {"source": "inside"}
    assert attempted_swap
    assert (not swapped) or (runs / document.name).read_text(encoding="utf-8") == '{"source":"outside"}'
