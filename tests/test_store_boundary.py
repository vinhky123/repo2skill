from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from repo2skill import store_io
from repo2skill.ledger import LedgerStore
from repo2skill.models import ExportJournal, ProjectState


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
