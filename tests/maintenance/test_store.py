from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore
from repo2skill.maintenance_models import (
    GitEvent,
    MaintenanceJob,
    MaintenanceLifecycle,
    MaintenancePolicy,
    MaintenanceState,
)
from repo2skill.maintenance_store import MaintenanceStore
from repo2skill.models import ProjectDocument, ProjectState


PROJECT_ID = "project_0123456789abcdef01234567"
JOB_ID = "maintenance-job_0123456789abcdef01234567"
EVENT_ID = "git-event_0123456789abcdef01234567"
OLD_CURSOR = "cursor-old"


def _project(root: Path) -> ProjectDocument:
    return ProjectDocument(
        "1.0", "0.1.0", PROJECT_ID, "fixture", ".repo2skill", "worktree",
        default_scan_policy(), default_claim_policy(), default_targets(root),
    )


def _initial_state() -> ProjectState:
    return ProjectState("1.0", "0.1.0", PROJECT_ID, None, None, (), {}, None, None)


def _make_store(root: Path) -> MaintenanceStore:
    root.mkdir()
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    (state_dir / "project.json").write_text(
        json.dumps(dataclasses.asdict(_project(root))), encoding="utf-8"
    )
    (state_dir / "state.json").write_text(
        json.dumps(dataclasses.asdict(_initial_state())), encoding="utf-8"
    )
    return MaintenanceStore(root, LedgerStore(root, state_dir))


def _job(job_id: str = JOB_ID, event_id: str = EVENT_ID) -> MaintenanceJob:
    return MaintenanceJob(
        job_id, "a" * 40, "b" * 40, (event_id,), ("src/app.py",), ("skill",),
        "sha256:" + "1" * 64, "sha256:" + "2" * 64, "policy-v1", ("skill",),
        None, {".agents/skills/fixture/SKILL.md": "sha256:" + "3" * 64},
    )


def _lifecycle() -> MaintenanceLifecycle:
    return MaintenanceLifecycle("pending", None, None, "pending", None, None, None)


def _state(*, cursor: str | None, job_id: str | None = None) -> MaintenanceState:
    jobs = {} if job_id is None else {job_id: _lifecycle()}
    return MaintenanceState(
        "1.0", cursor, "a" * 40,
        MaintenancePolicy("ask", 1, ("skill",)), (), jobs, None,
    )


def test_old_project_without_maintenance_reads_empty_state(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "repo")

    state = store.read_state()

    assert state.schemaVersion == "1.0"
    assert state.observedEventCursor is None
    assert state.appliedBaseline is None
    assert state.policy.approval == "ask"
    assert state.jobs == {}
    assert not (tmp_path / "repo" / ".repo2skill" / "maintenance").exists()


def test_old_maintenance_document_defaults_enabled_to_false(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "repo")
    path = store.ledger.state_dir / "maintenance" / "state.json"
    path.parent.mkdir()
    raw = dataclasses.asdict(_state(cursor=None))
    raw.pop("enabled")
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert store.read_state().enabled is False


def test_job_is_immutable_and_identical_retry_is_idempotent(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "repo")
    job = _job()
    with store.ledger.mutation():
        store.write_job(job)
        store.write_job(job)
        with pytest.raises(FileExistsError):
            store.write_job(dataclasses.replace(job, targetCommit="c" * 40))

    assert store.read_job(JOB_ID) == job


def test_job_hashes_are_defensively_immutable_across_round_trip(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "repo")
    path = ".agents/skills/fixture/SKILL.md"
    original_hash = "sha256:" + "3" * 64
    changed_hash = "sha256:" + "4" * 64
    source_hashes = {path: original_hash}
    job = dataclasses.replace(_job(), expectedOutputHashes=source_hashes)

    source_hashes[path] = changed_hash
    assert job.expectedOutputHashes[path] == original_hash
    with pytest.raises(TypeError):
        job.expectedOutputHashes[path] = changed_hash

    with store.ledger.mutation():
        store.write_job(job)
    reopened = store.read_job(JOB_ID)
    assert reopened == job
    with pytest.raises(TypeError):
        reopened.expectedOutputHashes[path] = changed_hash


def test_guarded_immutable_reader_rejects_oversized_file_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _make_store(tmp_path / "repo")
    relative = "maintenance/attempts/maintenance-attempt_0123456789abcdef01234567/response.bin"
    with store.ledger.mutation():
        store.ledger.write_immutable_bytes(relative, b"x" * 1024)
    from repo2skill.safe_fs import GuardedFile
    monkeypatch.setattr(GuardedFile, "read_bounded", lambda *_args: pytest.fail("oversized bytes were read before cap rejection"))
    with pytest.raises(ValueError, match="exceeds byte limit"):
        store.ledger.read_immutable_bytes(relative, max_bytes=32)


def test_single_pointer_commits_cursor_and_job_together(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "repo")
    job = _job()
    event = GitEvent(EVENT_ID, "post-commit", "cursor-new", job.targetCommit)
    state = dataclasses.replace(
        _state(cursor=event.cursor, job_id=job.jobId), deferredEvents=(event,),
    )
    with store.ledger.mutation():
        store.publish_job(job, state)

    reopened = MaintenanceStore(store.ledger.root, LedgerStore(store.ledger.root, store.ledger.state_dir))
    committed = reopened.read_state()
    assert committed.observedEventCursor == event.cursor
    assert committed.jobs[job.jobId].status == "pending"


def test_crash_after_job_write_before_pointer_leaves_only_orphan(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "repo")
    old_cursor = store.read_state().observedEventCursor
    job = _job()
    with store.ledger.mutation():
        store.write_job(job)
        # Simulated crash before the single state-pointer replacement.

    reopened = MaintenanceStore(store.ledger.root, LedgerStore(store.ledger.root, store.ledger.state_dir))
    current = reopened.read_state()
    assert current.observedEventCursor == old_cursor
    assert job.jobId not in current.jobs


def test_unknown_maintenance_major_is_rejected(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "repo")
    path = store.ledger.state_dir / "maintenance" / "state.json"
    path.parent.mkdir()
    raw = dataclasses.asdict(_state(cursor=None))
    raw["schemaVersion"] = "2.0"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="schema major"):
        store.read_state()


def test_duplicate_event_id_is_idempotent(tmp_path: Path) -> None:
    store = _make_store(tmp_path / "repo")
    event = GitEvent(EVENT_ID, "post-commit", "cursor-1", "b" * 40)
    job = _job()
    initial = dataclasses.replace(
        _state(cursor="cursor-1", job_id=job.jobId), deferredEvents=(event,),
    )
    with store.ledger.mutation():
        store.publish_job(job, initial)
    before = (store.ledger.state_dir / "maintenance" / "state.json").read_bytes()

    duplicate = GitEvent(EVENT_ID, "post-commit", "cursor-2", "c" * 40)
    replacement = dataclasses.replace(
        _state(cursor="cursor-2", job_id=job.jobId), deferredEvents=(duplicate,),
    )
    with store.ledger.mutation():
        result = store.publish_job(job, replacement)

    after = (store.ledger.state_dir / "maintenance" / "state.json").read_bytes()
    assert result.observedEventCursor == "cursor-1"
    assert after == before

