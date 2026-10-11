from __future__ import annotations

import dataclasses
import json
import multiprocessing
from pathlib import Path
import subprocess

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore, LockBusyError
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
    target_kinds = maint.read_state().policy.targetKinds
    job = MaintenanceJob(
        "maintenance-job_0123456789abcdef01234567", None, target_commit,
        ("git-event_0123456789abcdef01234567",), ("src.py",), ("skill",),
        "sha256:" + "1" * 64, "sha256:" + "2" * 64,
        effective_policy_revision(project.scanPolicy, project.claimPolicy, project.targets, generation_options={}),
        target_kinds, None, {},
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

    assert result.status == "rebuild-required"
    assert result.jobId == "maintenance-job_0123456789abcdef01234567"
    assert "source context" in result.message
    state = MaintenanceStore(root, store).read_state()
    assert state.appliedBaseline is None
    assert state.jobs[result.jobId].status == "failed"


def test_worker_lease_rejects_redirected_lock_without_touching_target(tmp_path: Path) -> None:
    import pytest
    from repo2skill.safe_fs import UnsafePathError

    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"protected")
    lease_dir = root / ".repo2skill" / "maintenance"
    lease_dir.mkdir(parents=True, exist_ok=True)
    link = lease_dir / "worker.lock"
    try:
        link.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"native symlink fixture is unavailable: {error}")

    calls = []
    with pytest.raises((UnsafePathError, ValueError)):
        run_worker(
            store, foreground=True, job_id=job_id,
            probe=lambda *_args, **_kwargs: calls.append("probe"),
            invoke=lambda *_args, **_kwargs: calls.append("invoke"),
        )

    assert outside.read_bytes() == b"protected"
    assert calls == []


def test_live_windows_lease_contention_waits_then_drains_without_replacing_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    import os
    import pytest
    from repo2skill import maintenance_worker
    from repo2skill.maintenance_approval import configure_auto_approval
    from repo2skill.maintenance_worker import WorkerResult

    if os.name != "nt":
        pytest.skip("native Windows byte-range contention regression")
    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    maint = MaintenanceStore(root, store)
    grant = configure_auto_approval(store)
    with store.mutation():
        state = maint.read_state()
        jobs = dict(state.jobs)
        jobs[job_id] = dataclasses.replace(
            jobs[job_id], status="pending", approvalSource=None,
            approvalBindingHash=None, queuedGrantRevision=grant.policy.grantRevision,
        )
        maint.replace_state(dataclasses.replace(state, jobs=jobs))

    lock_path = store.state_dir / "maintenance" / "worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_bytes(b"\0")
    identity = lock_path.stat()
    ready = multiprocessing.Event()
    release = multiprocessing.Event()
    owner = multiprocessing.Process(target=_hold_lease, args=(str(root), ready, release))
    owner.start()
    assert ready.wait(10)
    calls = []
    ran = []

    def fake_probe(*_args, **_kwargs):
        calls.append("probe")
        return type("DeniedCapability", (), {"supported": False})()

    def fake_invoke(*_args, **_kwargs):
        calls.append("invoke")
        raise AssertionError("lease fixture must never invoke a provider")

    def fake_run_one(ledger, selected, **_kwargs):
        ran.append(selected)
        with ledger.mutation():
            state = maint.read_state()
            jobs = dict(state.jobs)
            jobs[selected] = dataclasses.replace(jobs[selected], status="applied")
            maint.replace_state(dataclasses.replace(
                state, jobs=jobs, appliedBaseline=maint.read_job(selected).targetCommit,
            ))
        return WorkerResult("applied", selected, "fake drain")

    monkeypatch.setattr(maintenance_worker, "_run_one", fake_run_one)
    sleeps = []

    def release_after_busy(seconds):
        sleeps.append(seconds)
        release.set()
        owner.join(10)
        assert owner.exitcode == 0

    try:
        result = maintenance_worker.run_worker(
            store, auto_drain=True, wait_budget_seconds=2,
            sleep=release_after_busy, retry_delay=lambda _attempt: 0.01,
            probe=fake_probe, invoke=fake_invoke,
        )
    finally:
        release.set()
        owner.join(10)
    assert result.status == "idle"
    assert sleeps == [0.01]
    assert ran == [job_id]
    assert calls == []
    after_identity = lock_path.stat()
    assert (after_identity.st_dev, after_identity.st_ino, after_identity.st_size) == (
        identity.st_dev, identity.st_ino, identity.st_size,
    )
    assert lock_path.read_bytes() == b"\0"


def test_worker_lease_rejects_redirected_maintenance_directory(tmp_path: Path) -> None:
    import os
    import subprocess
    import pytest
    from repo2skill.safe_fs import UnsafePathError

    root, store = _store(tmp_path / "repo")
    maintenance_dir = root / ".repo2skill" / "maintenance"
    outside = tmp_path / "outside-maintenance"
    outside.mkdir()
    marker = outside / "protected"
    marker.write_bytes(b"protected")
    try:
        maintenance_dir.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            pytest.skip(f"native directory redirect unavailable: {error}")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(maintenance_dir), str(outside)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode:
            pytest.skip(f"directory junction unavailable: {completed.stderr.strip()}")

    calls = []
    try:
        with pytest.raises((UnsafePathError, ValueError)):
            run_worker(
                store, foreground=True, job_id="maintenance-job_0123456789abcdef01234567",
                probe=lambda *_args, **_kwargs: calls.append("probe"),
                invoke=lambda *_args, **_kwargs: calls.append("invoke"),
            )
        assert marker.read_bytes() == b"protected"
        assert not (outside / "worker.lock").exists()
        assert calls == []
    finally:
        if os.name == "nt":
            maintenance_dir.rmdir()
        else:
            maintenance_dir.unlink()


def test_worker_lease_guarded_open_failure_precedes_adapter_entry(tmp_path: Path, monkeypatch) -> None:
    import pytest
    from repo2skill.safe_fs import GuardedDirectory

    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    outside = tmp_path / "outside-lock"
    outside.write_bytes(b"protected")
    original_open_file = GuardedDirectory.open_file

    def deny_worker_lock(self, name):
        if name == "worker.lock":
            raise PermissionError("injected guarded lease-open denial")
        return original_open_file(self, name)

    monkeypatch.setattr(GuardedDirectory, "open_file", deny_worker_lock)
    calls = []
    with pytest.raises(PermissionError, match="injected guarded lease-open denial"):
        run_worker(
            store, foreground=True, job_id=job_id,
            probe=lambda *_args, **_kwargs: calls.append("probe"),
            invoke=lambda *_args, **_kwargs: calls.append("invoke"),
        )

    assert outside.read_bytes() == b"protected"
    assert calls == []


def test_disabled_worker_does_not_claim_approved_job(tmp_path: Path) -> None:
    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    maint = MaintenanceStore(root, store)
    with store.mutation():
        maint.replace_state(dataclasses.replace(maint.read_state(), enabled=False))

    result = run_worker(store, foreground=True, job_id=job_id)

    assert result.status == "disabled"
    assert maint.read_state().jobs[job_id].status == "approved"


def test_begin_invocation_rechecks_enabled_state_after_probe_boundary(tmp_path: Path) -> None:
    import pytest
    from repo2skill.maintenance_attempts import begin_invocation, prepare_attempt

    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    attempt = prepare_attempt(store, job_id, "sha256:" + "a" * 64)
    maint = MaintenanceStore(root, store)
    with store.mutation():
        maint.replace_state(dataclasses.replace(maint.read_state(), enabled=False))

    with pytest.raises(ValueError, match="disabled"):
        begin_invocation(store, job_id, attempt.attemptId)

    assert maint.read_state().jobs[job_id].status == "approved"


def test_worker_revalidates_auto_grant_under_final_mutation_lock(tmp_path: Path, monkeypatch) -> None:
    from repo2skill import maintenance_approval, maintenance_worker
    from repo2skill.maintenance_approval import configure_auto_approval

    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    maint = MaintenanceStore(root, store)
    auto_state = configure_auto_approval(store)
    with store.mutation():
        state = maint.read_state()
        current = state.jobs[job_id]
        jobs = dict(state.jobs)
        jobs[job_id] = dataclasses.replace(
            current, status="pending", approvalSource=None, approvalBindingHash=None,
            queuedGrantRevision=auto_state.policy.grantRevision,
        )
        maint.replace_state(dataclasses.replace(state, jobs=jobs))

    original_fingerprint_check = maintenance_worker.verify_job_fingerprints
    transitions = []

    def revoke_after_initial_validation(ledger, job):
        original_fingerprint_check(ledger, job)
        transitions.append(maint.read_state().jobs[job_id].approvalSource)
        maintenance_approval.revoke_auto_approval(ledger, enabled=True)

    monkeypatch.setattr(maintenance_worker, "verify_job_fingerprints", revoke_after_initial_validation)

    result = maintenance_worker.run_worker(store, foreground=True, job_id=job_id)

    assert transitions == ["auto"]
    assert result.status == "stale"
    assert "approval binding" in result.message or "policy" in result.message
    assert maint.read_state().enabled is True
    assert maint.read_state().policy.approval == "ask"


def test_hidden_auto_worker_waits_for_busy_lease_then_drains_current_job(tmp_path: Path, monkeypatch) -> None:
    from contextlib import contextmanager
    from repo2skill import maintenance_worker
    from repo2skill.maintenance_approval import configure_auto_approval
    from repo2skill.maintenance_worker import WorkerResult

    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    maint = MaintenanceStore(root, store)
    auto_state = configure_auto_approval(store)
    with store.mutation():
        state = maint.read_state()
        lifecycle = state.jobs[job_id]
        jobs = dict(state.jobs)
        jobs[job_id] = dataclasses.replace(lifecycle, status="pending", approvalSource=None,
                                           approvalBindingHash=None,
                                           queuedGrantRevision=auto_state.policy.grantRevision)
        maint.replace_state(dataclasses.replace(state, jobs=jobs))

    lease_calls = []
    @contextmanager
    def fake_lease(*_args, **_kwargs):
        lease_calls.append(True)
        if len(lease_calls) == 1:
            raise LockBusyError("held")
        yield
    calls = []
    refreshes = []
    def fake_run_one(ledger, selected, **_kwargs):
        calls.append((selected, ledger.state_dir))
        with ledger.mutation():
            state = maint.read_state()
            jobs = dict(state.jobs)
            jobs[selected] = dataclasses.replace(jobs[selected], status="applied")
            maint.replace_state(dataclasses.replace(state, jobs=jobs, appliedBaseline=maint.read_job(selected).targetCommit))
        return WorkerResult("applied", selected, "applied")

    monkeypatch.setattr(maintenance_worker, "_worker_lease", fake_lease)
    monkeypatch.setattr(maintenance_worker, "_run_one", fake_run_one)
    monkeypatch.setattr("repo2skill.maintenance_git.refresh_queued_successor",
                        lambda ledger: refreshes.append(ledger.state_dir))
    sleeps = []
    result = maintenance_worker.run_worker(store, auto_drain=True, wait_budget_seconds=1,
                                          sleep=lambda seconds: sleeps.append(seconds),
                                          retry_delay=lambda _attempt: 0.01)

    assert result.status == "idle"
    assert len(lease_calls) == 3
    assert sleeps == [0.01]
    assert calls == [(job_id, store.state_dir)]
    assert refreshes == [store.state_dir]
    assert len(maint.read_state().jobs) == 1


def test_hidden_ask_worker_never_runs_a_user_approved_job(tmp_path: Path) -> None:
    root, store = _store(tmp_path / "repo")
    job_id = _approved_job(store)
    calls = []

    result = run_worker(store, auto_drain=True, wait_budget_seconds=0,
                        invoke=lambda *_args, **_kwargs: calls.append(True))

    assert result.status == "idle"
    assert calls == []
    assert MaintenanceStore(root, store).read_state().jobs[job_id].status == "approved"
