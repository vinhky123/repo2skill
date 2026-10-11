from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

import pytest

from repo2skill.cli import build_parser, main
from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore
from repo2skill.maintenance_models import MaintenanceJob, MaintenanceLifecycle
from repo2skill.maintenance_store import MaintenanceStore
from repo2skill.models import ProjectDocument, ProjectState


JOB_ID = "maintenance-job_0123456789abcdef01234567"


def _repo(root: Path) -> tuple[LedgerStore, MaintenanceStore]:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture", ".repo2skill",
                              "worktree", default_scan_policy(), default_claim_policy(), default_targets(root))
    state = ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    ledger = LedgerStore(root, state_dir)
    return ledger, MaintenanceStore(root, ledger)


def _pending(ledger: LedgerStore, maint: MaintenanceStore, *, stale: bool = False) -> None:
    project = ledger.read_project()
    current_state = maint.read_state()
    policy_revision = "old-policy"
    if not stale and project is not None:
        from repo2skill.ids import effective_policy_revision
        policy_revision = effective_policy_revision(project.scanPolicy, project.claimPolicy, project.targets,
                                                    generation_options={})
    job = MaintenanceJob(JOB_ID, None, "b" * 40, ("git-event_0123456789abcdef01234567",),
                         ("src/app.py",), ("python",), "sha256:" + "1" * 64,
                         "sha256:" + "2" * 64, policy_revision, current_state.policy.targetKinds, None, {})
    with ledger.mutation():
        maint.write_job(job)
        state = maint.read_state()
        maint.replace_state(dataclasses.replace(
            state, enabled=True,
            jobs={JOB_ID: MaintenanceLifecycle("pending", None, None, "pending", None, None, None,
                                                queuedGrantRevision=0)},
        ))


def _invoke(root: Path, capsys, *argv: str):
    code = main(["--root", str(root), "--format", "json", "maintain", *argv])
    output = capsys.readouterr()
    return code, json.loads(output.out), output.err


def test_maintain_command_grammar() -> None:
    parser = build_parser()
    assert parser.parse_args(["maintain", "setup", "--auto-approve"]).auto_approve
    assert parser.parse_args(["maintain", "status"]).maintain_command == "status"
    assert parser.parse_args(["maintain", "approve", "job_0123456789abcdef01234567"]).job_id.startswith("job_")
    assert parser.parse_args(["maintain", "retry", "job_0123456789abcdef01234567"]).maintain_command == "retry"
    assert parser.parse_args(["maintain", "skip", "job_0123456789abcdef01234567"]).maintain_command == "skip"
    assert parser.parse_args(["maintain", "catch-up", "--rebuild"]).rebuild


def test_default_status_requires_approval(tmp_path: Path, capsys) -> None:
    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)

    code, payload, stderr = _invoke(tmp_path / "repo", capsys, "status")

    assert code == 3
    assert payload["command"] == "maintain status"
    assert set(payload) == {"schemaVersion", "toolVersion", "command", "status", "exitCode", "result",
                            "findings", "diagnostics", "nextActions"}
    assert payload["result"]["jobs"][0]["status"] == "pending"
    assert payload["result"]["policyRevision"]
    assert stderr == ""


def test_setup_refusal_preflights_before_auto_grant_or_policy_write(tmp_path: Path, capsys) -> None:
    from repo2skill.maintenance_approval import read_auto_grant

    root = tmp_path / "repo"
    ledger, maint = _repo(root)
    hook = root / ".git" / "hooks" / "post-commit"
    hook.write_bytes(b"human hook bytes\n")
    before = maint.read_state()

    code, _payload, _stderr = _invoke(root, capsys, "setup", "--auto-approve")

    assert code == 1
    assert maint.read_state() == before
    assert read_auto_grant(ledger) is None
    assert hook.read_bytes() == b"human hook bytes\n"


def test_plain_setup_refusal_preserves_current_auto_authority_bytes(tmp_path: Path, capsys) -> None:
    from repo2skill.maintenance_approval import configure_auto_approval, read_auto_grant

    root = tmp_path / "repo"
    ledger, maint = _repo(root)
    configure_auto_approval(ledger)
    grant_path = root / ".git" / "repo2skill" / "maintenance" / "auto-grant.json"
    grant_before = grant_path.read_bytes()
    state_before = maint.read_state()
    hook = root / ".git" / "hooks" / "post-commit"
    hook.write_bytes(b"human hook bytes\n")

    code, _payload, _stderr = _invoke(root, capsys, "setup")

    assert code == 1
    assert maint.read_state() == state_before
    assert grant_path.read_bytes() == grant_before
    assert read_auto_grant(ledger) is not None
    assert hook.read_bytes() == b"human hook bytes\n"


@pytest.mark.parametrize("cap_kind", ["file", "total"])
def test_catch_up_defers_required_context_caps_and_retries_same_event(
    tmp_path: Path, capsys, monkeypatch, cap_kind: str,
) -> None:
    from repo2skill import maintenance_git

    root = tmp_path / "repo"
    ledger, maint = _repo(root)
    (root / ".git" / "info" / "exclude").write_text(".repo2skill/\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("123\n", encoding="utf-8")
    if cap_kind == "total":
        (root / "src" / "b.py").write_text("456\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "-m", "source"],
                   check=True, capture_output=True)
    with ledger.mutation():
        maint.replace_state(dataclasses.replace(maint.read_state(), enabled=True))

    if cap_kind == "file":
        monkeypatch.setattr(maintenance_git, "MAX_CONTEXT_FILE_BYTES", 2)
    else:
        monkeypatch.setattr(maintenance_git, "MAX_CONTEXT_TOTAL_BYTES", 4)

    code, payload, _stderr = _invoke(root, capsys, "catch-up", "--rebuild")
    assert code == 2
    assert payload["result"]["coverageFailure"] in {
        "maintenance.context-file-cap", "maintenance.context-byte-cap",
    }
    assert maint.read_state().jobs == {}
    before_retry = maint.read_state()
    assert len(before_retry.deferredEvents) == 1
    assert before_retry.appliedBaseline is None

    monkeypatch.setattr(maintenance_git, "MAX_CONTEXT_FILE_BYTES", 1024 * 1024)
    monkeypatch.setattr(maintenance_git, "MAX_CONTEXT_TOTAL_BYTES", 64 * 1024 * 1024)
    code, payload, _stderr = _invoke(root, capsys, "catch-up", "--rebuild")
    assert code == 3
    assert payload["result"]["jobs"]
    assert maint.read_state().deferredEvents == ()
    assert maint.read_state().appliedBaseline is None


def test_status_reports_deferred_event_without_jobs(tmp_path: Path, capsys) -> None:
    from repo2skill.maintenance_models import GitEvent

    root = tmp_path / "repo"
    ledger, maint = _repo(root)
    event = GitEvent("git-event_111111111111111111111111", "manual-catch-up", "deferred-cursor",
                     "a" * 40, "maintenance.git-timeout")
    with ledger.mutation():
        maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline="b" * 40,
                                                deferredEvents=(event,)))

    code, payload, _stderr = _invoke(root, capsys, "status")

    assert code == 2
    assert payload["result"]["deferredEvents"][0]["reasonCode"] == "maintenance.git-timeout"
    assert payload["findings"][0]["code"] == "maintenance.coverage-required"


def test_status_reports_missing_baseline_after_skipped_bootstrap(tmp_path: Path, capsys) -> None:
    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    with ledger.mutation():
        state = maint.read_state()
        jobs = dict(state.jobs)
        jobs[JOB_ID] = dataclasses.replace(jobs[JOB_ID], status="skipped")
        maint.replace_state(dataclasses.replace(state, jobs=jobs))

    code, payload, _stderr = _invoke(tmp_path / "repo", capsys, "status")

    assert code == 4
    assert any(item["code"] == "maintenance.rebuild-required" for item in payload["findings"])


def test_status_does_not_request_user_approval_for_current_auto_grant(tmp_path: Path, capsys) -> None:
    from repo2skill.maintenance_approval import auto_approval_binding, configure_auto_approval

    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    auto_state = configure_auto_approval(ledger)
    with ledger.mutation():
        state = maint.read_state()
        jobs = dict(state.jobs)
        jobs[JOB_ID] = dataclasses.replace(jobs[JOB_ID], queuedGrantRevision=auto_state.policy.grantRevision)
        maint.replace_state(dataclasses.replace(state, jobs=jobs))
    state = maint.read_state()
    assert auto_approval_binding(ledger, maint.read_job(JOB_ID), state.jobs[JOB_ID]) is not None

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "status")

    assert code == 0
    assert payload["findings"] == []
    assert payload["result"]["policy"]["autoGrantActive"] is True
    assert payload["result"]["jobs"][0]["status"] == "pending"


def test_status_requires_user_approval_for_job_queued_before_auto_grant(tmp_path: Path, capsys) -> None:
    from repo2skill.maintenance_approval import auto_approval_binding, configure_auto_approval

    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    configure_auto_approval(ledger)
    state = maint.read_state()
    assert state.jobs[JOB_ID].queuedGrantRevision < state.policy.grantRevision
    assert auto_approval_binding(ledger, maint.read_job(JOB_ID), state.jobs[JOB_ID]) is None

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "status")

    assert code == 3
    assert payload["findings"][0]["code"] == "maintenance.approval-required"


def test_status_requires_user_approval_when_private_auto_grant_is_invalid(tmp_path: Path, capsys) -> None:
    from repo2skill.maintenance_approval import configure_auto_approval

    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    auto_state = configure_auto_approval(ledger)
    with ledger.mutation():
        state = maint.read_state()
        jobs = dict(state.jobs)
        jobs[JOB_ID] = dataclasses.replace(jobs[JOB_ID], queuedGrantRevision=auto_state.policy.grantRevision)
        maint.replace_state(dataclasses.replace(state, jobs=jobs))
    grant_path = ledger.root / ".git" / "repo2skill" / "maintenance" / "auto-grant.json"
    grant_path.write_text("{invalid", encoding="utf-8")

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "status")

    assert code == 3
    assert payload["findings"][0]["code"] == "maintenance.approval-required"
    assert payload["result"]["policy"]["autoGrantActive"] is False


def test_corrupt_maintenance_state_exits_seven(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    _repo(root)
    path = root / ".repo2skill" / "maintenance" / "state.json"
    path.parent.mkdir()
    path.write_text("{broken", encoding="utf-8")

    code, payload, _ = _invoke(root, capsys, "status")

    assert code == 7
    assert payload["command"] == "maintain status"
    assert payload["findings"][0]["code"] == "ledger.corrupt"


def test_skip_does_not_advance_baseline_or_repeat_same_event(tmp_path: Path, capsys) -> None:
    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    initial = maint.read_state()

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "skip", JOB_ID)

    after = maint.read_state()
    assert code == 0
    assert payload["command"] == f"maintain skip {JOB_ID}"
    assert payload["result"]["appliedBaseline"] == initial.appliedBaseline
    assert after.jobs[JOB_ID].status == "skipped"
    assert after.jobs[JOB_ID].skippedTargetCommit == "b" * 40


def test_status_reports_agent_failure_and_conflict(tmp_path: Path, capsys) -> None:
    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    first = maint.read_job(JOB_ID)
    second_id = "maintenance-job_1123456789abcdef01234567"
    second = dataclasses.replace(first, jobId=second_id,
                                 eventIds=("git-event_1123456789abcdef01234567",), targetCommit="c" * 40)
    with ledger.mutation():
        maint.write_job(second)
        state = maint.read_state()
        jobs = dict(state.jobs)
        jobs[JOB_ID] = MaintenanceLifecycle("failed", "user", "sha256:" + "3" * 64,
                                            "reported", None, None, "maintenance.agent-failed")
        jobs[second_id] = MaintenanceLifecycle("failed", "user", "sha256:" + "4" * 64,
                                               "reported", None, None, "maintenance.source-conflict")
        maint.replace_state(dataclasses.replace(state, jobs=jobs))

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "status")

    assert code == 5
    assert payload["command"] == "maintain status"
    assert {item["code"] for item in payload["findings"]} == {
        "maintenance.agent-failed", "maintenance.source-conflict",
    }


def test_stale_approval_reports_finding_without_running_worker(tmp_path: Path, capsys, monkeypatch) -> None:
    from repo2skill import maintenance_worker

    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint, stale=True)
    monkeypatch.setattr(maintenance_worker, "run_worker", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker called")))

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "approve", JOB_ID)

    assert code == 4
    assert payload["command"] == f"maintain approve {JOB_ID}"
    assert payload["findings"][0]["code"] == "maintenance.stale-binding"


def test_owned_output_edit_reports_conflict_before_worker(tmp_path: Path, capsys, monkeypatch) -> None:
    from maintenance.test_approval import _active_output
    from repo2skill import maintenance_worker

    store, maint = _repo(tmp_path / "repo")
    job_id, path, live, _block = _active_output(store, maint, kind="agents-md")
    (store.root / path).write_bytes(live.replace(b"Owned guidance", b"Human edit"))
    monkeypatch.setattr(maintenance_worker, "run_worker",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("worker called")))

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "approve", job_id)

    assert code == 5
    assert payload["command"] == f"maintain approve {job_id}"
    assert payload["findings"][0]["code"] == "maintenance.source-conflict"
    assert maint.read_state().jobs[job_id].status == "pending"


def test_approve_runs_exact_job_in_foreground_and_reports_unavailable_agent(tmp_path: Path, capsys, monkeypatch) -> None:
    from repo2skill.maintenance_worker import WorkerResult
    from repo2skill import maintenance_worker
    calls = []
    monkeypatch.setattr(maintenance_worker, "run_worker",
                        lambda store, **kwargs: calls.append(kwargs) or WorkerResult("agent-unavailable", kwargs["job_id"], "runner unavailable"))
    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "approve", JOB_ID)

    assert code == 6
    assert payload["command"] == f"maintain approve {JOB_ID}"
    assert payload["result"]["status"] == "agent-unavailable"
    assert calls == [{"foreground": True, "job_id": JOB_ID, "explicit_retry": False}]
    assert maint.read_state().jobs[JOB_ID].status == "approved"


def test_busy_first_approval_reuses_binding_and_second_approve_runs_once(tmp_path: Path, capsys, monkeypatch) -> None:
    from repo2skill.maintenance_worker import WorkerResult
    from repo2skill import maintenance_worker

    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    invocation_count = 0
    worker_calls = 0

    def fake_worker(store, **kwargs):
        nonlocal invocation_count, worker_calls
        worker_calls += 1
        lifecycle = MaintenanceStore(store.root, store).read_state().jobs[JOB_ID]
        assert kwargs == {"foreground": True, "job_id": JOB_ID, "explicit_retry": False}
        if worker_calls == 1:
            assert lifecycle.status == "approved" and lifecycle.activeAttemptId is None
            return WorkerResult("busy", JOB_ID, "lease busy")
        assert lifecycle.status == "approved" and lifecycle.activeAttemptId is None
        invocation_count += 1
        return WorkerResult("applied", JOB_ID, "applied")

    from repo2skill.maintenance_store import MaintenanceStore
    monkeypatch.setattr(maintenance_worker, "run_worker", fake_worker)
    code, _payload, _ = _invoke(tmp_path / "repo", capsys, "approve", JOB_ID)
    assert code == 4
    first_binding = maint.read_state().jobs[JOB_ID].approvalBindingHash

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "approve", JOB_ID)

    assert code == 0
    assert payload["result"]["status"] == "applied"
    assert worker_calls == 2
    assert invocation_count == 1
    assert maint.read_state().jobs[JOB_ID].approvalBindingHash == first_binding


def test_new_target_after_skip_can_create_a_new_pending_job(tmp_path: Path, capsys) -> None:
    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    _invoke(tmp_path / "repo", capsys, "skip", JOB_ID)
    previous = maint.read_job(JOB_ID)
    new_id = "maintenance-job_1123456789abcdef01234567"
    next_job = dataclasses.replace(previous, jobId=new_id,
                                   eventIds=("git-event_1123456789abcdef01234567",), targetCommit="c" * 40)
    next_lifecycle = MaintenanceLifecycle("pending", None, None, "pending", None, None, None,
                                          queuedGrantRevision=0)
    with ledger.mutation():
        state = maint.read_state()
        next_jobs = dict(state.jobs)
        next_jobs[new_id] = next_lifecycle
        maint.publish_job(next_job, dataclasses.replace(state, jobs=next_jobs))

    code, payload, _ = _invoke(tmp_path / "repo", capsys, "status")

    assert code == 3
    assert payload["result"]["jobs"][-1]["jobId"] == new_id
    assert maint.read_state().jobs[JOB_ID].status == "skipped"


def test_status_resolves_applied_supersession_but_keeps_unrelated_stale(tmp_path: Path, capsys) -> None:
    ledger, maint = _repo(tmp_path / "repo")
    _pending(ledger, maint)
    predecessor = maint.read_job(JOB_ID)
    assert predecessor is not None
    successor_id = "maintenance-job_1123456789abcdef01234567"
    unrelated_id = "maintenance-job_2123456789abcdef01234567"
    successor = dataclasses.replace(
        predecessor, jobId=successor_id, eventIds=("git-event_1123456789abcdef01234567",),
        targetCommit="c" * 40,
    )
    unrelated = dataclasses.replace(
        predecessor, jobId=unrelated_id, eventIds=("git-event_2123456789abcdef01234567",),
        targetCommit="d" * 40,
    )
    with ledger.mutation():
        maint.write_job(successor)
        maint.write_job(unrelated)
        state = maint.read_state()
        jobs = dict(state.jobs)
        jobs[JOB_ID] = dataclasses.replace(
            jobs[JOB_ID], status="stale", supersededByJobId=successor_id,
        )
        jobs[successor_id] = MaintenanceLifecycle("applied", None, None, "reported", None, None, None)
        jobs[unrelated_id] = MaintenanceLifecycle("stale", None, None, "reported", None, None, None)
        maint.replace_state(dataclasses.replace(state, appliedBaseline="c" * 40, jobs=jobs))

    code, payload, _stderr = _invoke(tmp_path / "repo", capsys, "status")

    by_id = {item["jobId"]: item for item in payload["result"]["jobs"]}
    assert by_id[JOB_ID]["resolved"] is True
    assert by_id[unrelated_id]["resolved"] is False
    assert code == 4
    assert [item["details"]["jobId"] for item in payload["findings"]] == [unrelated_id]


@pytest.mark.parametrize(("job_status", "exit_code"), [("approved", 4), ("failed", 6)])
def test_catch_up_keeps_approved_or_failed_work_visible(
    tmp_path: Path, capsys, job_status: str, exit_code: int,
) -> None:
    root = tmp_path / "repo"
    ledger, maint = _repo(root)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "baseline"],
                   check=True, capture_output=True)
    baseline = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    _pending(ledger, maint)
    with ledger.mutation():
        state = maint.read_state()
        jobs = dict(state.jobs)
        jobs[JOB_ID] = dataclasses.replace(
            jobs[JOB_ID], status=job_status,
            failureCode="maintenance.agent-failed" if job_status == "failed" else None,
        )
        maint.replace_state(dataclasses.replace(state, appliedBaseline=baseline, jobs=jobs))

    code, payload, _stderr = _invoke(root, capsys, "catch-up")

    assert code == exit_code
    assert payload["result"]["jobs"][0]["jobId"] == JOB_ID
    assert payload["result"]["jobs"][0]["status"] == job_status


def test_real_enqueue_deduplicates_skipped_event_and_reports_later_commit(tmp_path: Path, capsys) -> None:
    from repo2skill.maintenance_git import compare_commits, enqueue_event
    from repo2skill.maintenance_models import GitEvent

    root = tmp_path / "repo"
    store, maint = _repo(root)
    (root / ".git" / "info" / "exclude").write_text(".repo2skill/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "--allow-empty", "-m", "baseline"], check=True, capture_output=True)
    baseline = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
    with store.mutation():
        maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline=baseline))
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("first = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-m", "first source change"], check=True, capture_output=True)
    first_target = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
    first_event = GitEvent("git-event_222222222222222222222222", "post-commit", "cursor-skip-first", first_target)
    first_job = enqueue_event(store, first_event, compare_commits(root, baseline, first_target))
    assert first_job is not None

    code, payload, _ = _invoke(root, capsys, "skip", first_job.jobId)
    assert code == 0 and payload["result"]["status"] == "skipped"
    assert enqueue_event(store, first_event, compare_commits(root, baseline, first_target)) is None

    (root / "src" / "app.py").write_text("first = True\nsecond = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-m", "second source change"], check=True, capture_output=True)
    second_target = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                                   capture_output=True, text=True).stdout.strip()
    second_event = GitEvent("git-event_333333333333333333333333", "post-commit", "cursor-skip-second", second_target)
    second_job = enqueue_event(store, second_event, compare_commits(root, baseline, second_target))

    code, status, _ = _invoke(root, capsys, "status")
    assert second_job is not None and second_job.jobId != first_job.jobId
    assert code == 3
    jobs = {job["jobId"]: job["status"] for job in status["result"]["jobs"]}
    assert jobs == {first_job.jobId: "skipped", second_job.jobId: "pending"}
