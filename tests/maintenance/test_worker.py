from __future__ import annotations

import dataclasses
import json
import multiprocessing
from pathlib import Path
import subprocess

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore
from repo2skill.ids import effective_policy_revision
from repo2skill.maintenance_approval import approve_job
from repo2skill.maintenance_models import MaintenanceJob, MaintenanceLifecycle, MaintenanceState
from repo2skill.maintenance_store import MaintenanceStore
from repo2skill.maintenance_worker import _worker_lease, run_worker
from repo2skill.models import ProjectDocument, ProjectState


def _store(root: Path) -> tuple[Path, LedgerStore]:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project = ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture", ".repo2skill",
        "worktree", default_scan_policy(), default_claim_policy(), default_targets(root),
    )
    state = ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    return root, LedgerStore(root, state_dir)


def _approved_job(store: LedgerStore) -> str:
    maint = MaintenanceStore(store.root, store)
    subprocess.run(
        ["git", "-C", str(store.root), "-c", "user.name=Repo2Skill Tests",
         "-c", "user.email=tests@example.invalid", "commit", "--allow-empty", "-m", "fixture"],
        check=True, capture_output=True, text=True,
    )
    target_commit = subprocess.run(
        ["git", "-C", str(store.root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    project = store.read_project()
    assert project is not None
    job = MaintenanceJob(
        "maintenance-job_0123456789abcdef01234567", None, target_commit,
        ("git-event_0123456789abcdef01234567",), ("src.py",), ("skill",),
        "sha256:" + "1" * 64, "sha256:" + "2" * 64,
        effective_policy_revision(project.scanPolicy, project.claimPolicy, project.targets, generation_options={}),
        ("skill",), None, {},
    )
    lifecycle = MaintenanceLifecycle("pending", None, None, "pending", None, None, None,
                                    queuedGrantRevision=0)
    with store.mutation():
        maint.write_job(job)
        current = maint.read_state()
        maint.replace_state(dataclasses.replace(current, enabled=True, jobs={job.jobId: lifecycle}))
    approve_job(store, job.jobId)
    return job.jobId


def _hold_lease(root: str, ready, hold) -> None:
    with _worker_lease(Path(root)):
        ready.set()
        hold.wait()


def test_worker_recovers_dead_lease(tmp_path: Path) -> None:
    root, store = _store(tmp_path / "repo")
    _approved_job(store)
    ready = multiprocessing.Event()
    hold = multiprocessing.Event()
    process = multiprocessing.Process(target=_hold_lease, args=(str(root), ready, hold))
    process.start()
    assert ready.wait(5)
    process.terminate()
    process.join(5)

    result = run_worker(store, foreground=True)

    assert result.status == "agent-unavailable"
    assert result.jobId == "maintenance-job_0123456789abcdef01234567"
    assert "not applied" in result.message
    state = MaintenanceStore(root, store).read_state()
    assert state.appliedBaseline is None
    assert state.jobs[result.jobId].status == "approved"


def test_disabled_worker_does_not_claim_approved_job(tmp_path: Path) -> None:
    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    maint = MaintenanceStore(root, store)
    with store.mutation():
        maint.replace_state(dataclasses.replace(maint.read_state(), enabled=False))

    result = run_worker(store, foreground=True, job_id=job_id)

    assert result.status == "disabled"
    assert maint.read_state().jobs[job_id].status == "approved"
