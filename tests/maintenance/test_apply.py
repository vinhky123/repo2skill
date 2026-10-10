from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import subprocess

import pytest

def test_apply_module_exposes_recoverable_apply_api() -> None:
    from repo2skill.maintenance_apply import ApplyJournal, apply_job, recover_apply

    assert ApplyJournal is not None
    assert callable(apply_job)
    assert callable(recover_apply)


@pytest.mark.parametrize("scenario", ["responded-recovery", "post-baseline-crash", "explicit-retry"])
def test_responded_attempt_recovery_and_explicit_retry(
    tmp_path: Path, monkeypatch, scenario: str,
) -> None:
    from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
    from repo2skill.ids import canonical_json_bytes
    from repo2skill.ledger import LedgerStore, encode_record
    from repo2skill.maintenance_agent import AgentResponse
    from repo2skill.maintenance_approval import approve_job
    from repo2skill.maintenance_attempts import (
        begin_invocation, fail_attempt, persist_response, prepare_attempt, read_attempt,
    )
    from repo2skill.maintenance_context import build_agent_request, build_committed_context
    from repo2skill.maintenance_git import bootstrap_commit, enqueue_event
    from repo2skill.maintenance_models import GitEvent, MaintenancePolicy
    from repo2skill.maintenance_store import MaintenanceStore
    from repo2skill.maintenance_worker import run_worker
    from repo2skill.models import ProjectDocument, ProjectState
    from repo2skill.pipeline import collect_scan

    root = tmp_path / "repo"
    root.mkdir()
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (root / ".gitignore").write_text(".repo2skill/\n.agents/\nAGENTS.md\n", encoding="utf-8")
    (root / "src.py").write_text("print('base')\n", encoding="utf-8")
    git("add", ".gitignore", "src.py")
    git("commit", "-qm", "base")

    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project = ProjectDocument("1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture",
                               ".repo2skill", "worktree", default_scan_policy(),
                               default_claim_policy(), default_targets(root))
    state = ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    store = LedgerStore(root, state_dir)
    initial = collect_scan(root, project)
    with store.mutation():
        store.write_immutable(f"runs/{initial.run.runId}.json", encode_record(initial.run))
        store.replace_state(dataclasses.replace(store.read_state(), currentRunId=initial.run.runId))
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), enabled=True,
                                                policy=MaintenancePolicy("ask", 1, ("skill",))))

    (root / "src.py").write_text("print('target')\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "-qm", "target")
    target = git("rev-parse", "HEAD")
    event = GitEvent("git-event_0123456789abcdef01234567", "post-commit", "cursor-target", target)
    job = enqueue_event(store, event, bootstrap_commit(root, target))
    assert job is not None
    approve_job(store, job.jobId)
    context = build_committed_context(store, job)
    request = build_agent_request(context, tmp_path / "prepared-snapshot", source_root=root)
    attempt = prepare_attempt(store, job.jobId, request.requestHash)
    begin_invocation(store, job.jobId, attempt.attemptId)
    response_bytes = canonical_json_bytes({"jobId": job.jobId, "snapshotId": context.snapshot.snapshotId,
                                           "operations": []})
    persist_response(store, job.jobId, attempt.attemptId, status="ok", output_bytes=response_bytes)
    if scenario == "post-baseline-crash":
        from repo2skill import maintenance_apply
        original_persist = maintenance_apply._persist_apply_journal
        crash_once = [True]

        def fail_first_applied_phase(ledger, journal):
            if journal.status == "applied" and crash_once[0]:
                crash_once[0] = False
                raise OSError("simulated interruption after baseline pointer")
            return original_persist(ledger, journal)

        monkeypatch.setattr(maintenance_apply, "_persist_apply_journal", fail_first_applied_phase)
    if scenario == "explicit-retry":
        fail_attempt(store, job.jobId, attempt.attemptId, "maintenance.agent-failed")
    calls = []

    result = run_worker(
        store, foreground=True, job_id=job.jobId,
        explicit_retry=scenario == "explicit-retry",
        probe=lambda *_args, **_kwargs: calls.append("probe") or type("Capability", (), {"supported": True})(),
        invoke=lambda *_args, **_kwargs: calls.append("invoke") or AgentResponse("ok", response_bytes, None),
    )

    assert result.status == "applied", result.message
    if scenario == "explicit-retry":
        assert calls == ["probe", "invoke"]
        retry = read_attempt(store, MaintenanceStore(root, store).read_state().jobs[job.jobId].activeAttemptId)
        assert retry.phase == "responded"
        assert retry.retryOfAttemptId == attempt.attemptId
    else:
        assert calls == []
    assert MaintenanceStore(root, store).read_state().appliedBaseline == target
    assert MaintenanceStore(root, store).read_state().jobs[job.jobId].status == "applied"
