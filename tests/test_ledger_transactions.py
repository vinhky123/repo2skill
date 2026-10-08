from __future__ import annotations

import dataclasses
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys

import pytest

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore, LockBusyError
from repo2skill.models import ProjectDocument, ProjectState, ReviewEvent


def _state(*, run: str = "run_0123456789abcdef01234567", events: tuple[ReviewEvent, ...] = ()) -> ProjectState:
    return ProjectState(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", run, 1,
        events, {}, None, None,
    )


def _hold_lock(root: str, state_dir: str, ready: multiprocessing.Queue[str], release: multiprocessing.Event) -> None:
    store = LedgerStore(Path(root), Path(state_dir))
    with store.mutation():
        current = store.read_state()
        assert current is not None
        ready.put("locked")
        release.wait(15)
        store.replace_state(dataclasses.replace(current, activeStageRevision=2))


def test_construction_and_read_only_access_do_not_create_state_directory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".repo2skill"
    store = LedgerStore(root, state_dir)

    assert store.read_state() is None
    with pytest.raises(FileNotFoundError):
        store.snapshot()
    assert not state_dir.exists()


def test_unknown_major_and_malformed_nested_state_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    state_dir = root / ".repo2skill"
    state_dir.mkdir(parents=True)
    state = dataclass_json(_state())
    state["schemaVersion"] = "2.0"
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="schema major"):
        LedgerStore(root, state_dir).read_state()

    state["schemaVersion"] = "1.0"
    state["reviewEvents"] = [{
        "recordRevision": 1,
        "eventId": "event_0123456789abcdef01234567",
        "claimId": "broken",
        "claimRevision": 1,
        "action": "accept",
        "reason": None,
        "createdAt": "2026-10-07T12:00:00Z",
    }]
    (state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(ValueError, match="claimId"):
        LedgerStore(root, state_dir).read_state()


def test_state_rewrite_preserves_root_and_existing_nested_additive_fields(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".repo2skill"
    event = ReviewEvent(
        1, "event_0123456789abcdef01234567",
        "claim_0123456789abcdef01234567", 1, "accept", None,
        "2026-10-07T12:00:00Z",
    )
    store = LedgerStore(root, state_dir)
    with store.mutation():
        assert store.read_state() is None
        store.replace_state(_state(events=(event,)))

    raw = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    raw["futureRoot"] = {"kept": True}
    raw["reviewEvents"][0]["futureNested"] = ["kept"]
    (state_dir / "state.json").write_text(json.dumps(raw), encoding="utf-8")

    store = LedgerStore(root, state_dir)
    with store.mutation():
        current = store.read_state()
        assert current is not None
        store.replace_state(dataclasses.replace(current, activeStageRevision=2))

    rewritten = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
    assert rewritten["futureRoot"] == {"kept": True}
    assert rewritten["reviewEvents"][0]["futureNested"] == ["kept"]


def test_immutable_orphan_does_not_advance_state_pointer(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".repo2skill"
    store = LedgerStore(root, state_dir)
    with store.mutation():
        assert store.read_state() is None
        store.replace_state(_state())
    with store.mutation():
        old = store.read_state()
        assert old is not None
        store.write_immutable("runs/run_aaaaaaaaaaaaaaaaaaaaaaaa.json", {"orphan": True})
        # Simulated interruption: immutable artifact exists; state replacement never runs.

    assert LedgerStore(root, state_dir).read_state() == _state()


def test_two_processes_contend_fail_fast_then_reread_and_update(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".repo2skill"
    initial = LedgerStore(root, state_dir)
    with initial.mutation():
        assert initial.read_state() is None
        initial.replace_state(_state())
    ready: multiprocessing.Queue[str] = multiprocessing.Queue()
    release = multiprocessing.Event()
    process = multiprocessing.Process(
        target=_hold_lock, args=(str(root), str(state_dir), ready, release)
    )
    process.start()
    assert ready.get(timeout=10) == "locked"

    contender = LedgerStore(root, state_dir)
    with pytest.raises(LockBusyError):
        with contender.mutation():
            raise AssertionError("busy lock entered")
    release.set()
    process.join(10)
    assert process.exitcode == 0

    with contender.mutation():
        current = contender.read_state()
        assert current is not None
        assert current.activeStageRevision == 2
        contender.replace_state(dataclasses.replace(current, activeStageRevision=3))
    assert LedgerStore(root, state_dir).read_state().activeStageRevision == 3  # type: ignore[union-attr]
    assert (state_dir / ".lock").is_file()


def test_os_releases_lock_when_process_dies(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".repo2skill"
    code = (
        "from pathlib import Path; from repo2skill.ledger import LedgerStore; "
        f"s=LedgerStore(Path({str(root)!r}),Path({str(state_dir)!r})); "
        "c=s.mutation(); c.__enter__(); print('locked', flush=True); "
        "import os; os._exit(0)"
    )
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert child.returncode == 0
    assert child.stdout == "locked\n"
    with LedgerStore(root, state_dir).mutation():
        pass


def test_writes_require_lock_and_reject_redirected_state_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = LedgerStore(root, root / ".repo2skill")
    with pytest.raises(RuntimeError, match="mutation"):
        store.replace_state(_state())

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = root / "linked"
    try:
        linked.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(linked), str(outside)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode:
            pytest.skip(f"directory link unavailable: {error}")
    with pytest.raises(ValueError, match="symlink|reparse"):
        LedgerStore(root, linked)


def test_project_reader_validates_nested_records_and_nonfinite_unknowns(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project = ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "repo",
        ".repo2skill", "worktree", default_scan_policy(), default_claim_policy(),
        default_targets(root),
    )
    raw = dataclass_json(project)
    raw["targets"][0]["livePath"] = "../escape"  # type: ignore[index]
    (state_dir / "project.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="livePath"):
        LedgerStore(root, state_dir).read_project()

    (state_dir / "project.json").write_text(
        json.dumps(dataclass_json(project))[:-1] + ',"future":NaN}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="non-finite"):
        LedgerStore(root, state_dir).read_project()


def test_immutable_documents_never_overwrite_existing_content(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    store = LedgerStore(root, root / ".repo2skill")
    with store.mutation():
        store.read_state()
        store.write_immutable("runs/run_aaaaaaaaaaaaaaaaaaaaaaaa.json", {"value": 1})
        with pytest.raises(FileExistsError):
            store.write_immutable("runs/run_aaaaaaaaaaaaaaaaaaaaaaaa.json", {"value": 2})
    assert json.loads((root / ".repo2skill/runs/run_aaaaaaaaaaaaaaaaaaaaaaaa.json").read_text()) == {"value": 1}


def dataclass_json(value: object) -> dict[str, object]:
    return dataclasses.asdict(value)
