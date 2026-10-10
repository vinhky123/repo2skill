from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path

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
    policy_revision = "old-policy"
    if not stale and project is not None:
        from repo2skill.ids import effective_policy_revision
        policy_revision = effective_policy_revision(project.scanPolicy, project.claimPolicy, project.targets,
                                                    generation_options={})
    job = MaintenanceJob(JOB_ID, None, "b" * 40, ("git-event_0123456789abcdef01234567",),
                         ("src/app.py",), ("python",), "sha256:" + "1" * 64,
                         "sha256:" + "2" * 64, policy_revision, ("skill",), None, {})
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
    assert calls == [{"foreground": True, "job_id": JOB_ID}]
    assert maint.read_state().jobs[JOB_ID].status == "approved"


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
