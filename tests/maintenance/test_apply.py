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


@pytest.mark.parametrize("scenario", [
    "responded-recovery", "post-baseline-crash", "explicit-retry",
    "disable-mid-call", "finalization-p0-race", "accepted-claim",
    "failed-agent", "move-head-queue", "move-head-after-stage",
    "export-before-baseline-crash",
])
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
    job = enqueue_event(store, event, bootstrap_commit(root, target), allow_baseline_bootstrap=True)
    assert job is not None
    approve_job(store, job.jobId)
    context = build_committed_context(store, job)
    request = build_agent_request(context, tmp_path / "prepared-snapshot", source_root=root)
    attempt = None
    if scenario not in {"disable-mid-call", "failed-agent", "move-head-queue"}:
        attempt = prepare_attempt(store, job.jobId, request.requestHash)
        begin_invocation(store, job.jobId, attempt.attemptId)
    operation = None
    if scenario == "accepted-claim":
        fact = next(item for item in context.facts if item.factType == "source.fragment.v1")
        operation = {
            "op": "add", "semanticKey": "maintenance:source-guidance:python",
            "scope": fact.scope, "renderedText": "The committed Python source prints a message.",
            "factIds": [fact.factId], "evidenceIds": sorted(fact.evidenceIds), "claimRef": None,
        }
    response_bytes = canonical_json_bytes({
        "jobId": job.jobId, "snapshotId": context.snapshot.snapshotId,
        "operations": [operation] if operation is not None else [],
    })
    if attempt is not None:
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
    if scenario == "export-before-baseline-crash":
        from repo2skill import maintenance_apply
        original_persist = maintenance_apply._persist_apply_journal
        crash_once = [True]

        def fail_exported_phase(ledger, journal):
            if journal.status == "exported" and crash_once[0]:
                crash_once[0] = False
                raise OSError("simulated interruption after export pointer")
            return original_persist(ledger, journal)

        monkeypatch.setattr(maintenance_apply, "_persist_apply_journal", fail_exported_phase)
    if scenario == "move-head-after-stage":
        from repo2skill import maintenance_context
        original_guard = maintenance_context.recheck_committed_source
        guard_calls = [0]

        def move_after_stage(ledger, guard):
            guard_calls[0] += 1
            # The stage pointer has been published before export's second guard.
            if ledger.read_state().activeStageRevision is not None and guard_calls[0] >= 2:
                git("commit", "--allow-empty", "-qm", "later source commit")
            return original_guard(ledger, guard)

        monkeypatch.setattr(maintenance_context, "recheck_committed_source", move_after_stage)
    if scenario == "explicit-retry":
        assert attempt is not None
        fail_attempt(store, job.jobId, attempt.attemptId, "maintenance.agent-failed")
    if scenario == "finalization-p0-race":
        original_mutation = store.mutation
        initial_run_id = store.read_state().currentRunId
        interposed = [False]

        from contextlib import contextmanager

        @contextmanager
        def mutate_pointer_before_final_lock():
            state = store.read_state()
            if (not interposed[0] and state.currentRunId != initial_run_id
                    and state.activeExportId is not None):
                interposed[0] = True
                with original_mutation():
                    current = store.read_state()
                    store.replace_state(dataclasses.replace(
                        current, activeManifestSetId="manifestset_ffffffffffffffffffffffff",
                    ))
            with original_mutation():
                yield

        monkeypatch.setattr(store, "mutation", mutate_pointer_before_final_lock)
    calls = []

    def fake_invoke(*_args, **_kwargs):
        calls.append("invoke")
        if scenario == "disable-mid-call":
            with store.mutation():
                maint.replace_state(dataclasses.replace(maint.read_state(), enabled=False))
        if scenario == "move-head-queue":
            git("commit", "--allow-empty", "-qm", "later source commit")
            later_commit = git("rev-parse", "HEAD")
            later_event = GitEvent("git-event_1123456789abcdef01234567", "post-commit",
                                   "cursor-later", later_commit)
            later_job = enqueue_event(store, later_event, bootstrap_commit(root, later_commit),
                                      allow_baseline_bootstrap=True)
            assert later_job is not None
            queued.append(later_job.jobId)
        if scenario == "failed-agent":
            return AgentResponse("nonzero-exit", b"", "fake CLI failed", 17)
        return AgentResponse("ok", response_bytes, None)

    initial_p0 = store.read_state()
    queued = []

    result = run_worker(
        store, foreground=True, job_id=job.jobId,
        explicit_retry=scenario == "explicit-retry",
        probe=lambda *_args, **_kwargs: calls.append("probe") or type("Capability", (), {"supported": True})(),
        invoke=fake_invoke,
    )

    if scenario in {"disable-mid-call", "finalization-p0-race", "move-head-queue", "failed-agent", "move-head-after-stage"}:
        assert result.status != "applied", result.message
        after = store.read_state()
        assert MaintenanceStore(root, store).read_state().appliedBaseline is None
        if scenario == "disable-mid-call":
            assert calls == ["probe", "invoke"]
            assert MaintenanceStore(root, store).read_state().enabled is False
            assert after.currentRunId == initial_p0.currentRunId
            assert after.activeExportId == initial_p0.activeExportId
            assert after.activeManifestSetId == initial_p0.activeManifestSetId
            assert not (root / "AGENTS.md").exists()
            assert not (root / ".agents").exists()
        elif scenario == "finalization-p0-race":
            assert interposed == [True]
            assert after.currentRunId != initial_p0.currentRunId
            assert after.activeExportId is not None
            assert after.activeManifestSetId == "manifestset_ffffffffffffffffffffffff"
        elif scenario == "move-head-after-stage":
            assert guard_calls[0] >= 2
            assert after.currentRunId != initial_p0.currentRunId
            assert after.activeExportId == initial_p0.activeExportId
            assert after.activeManifestSetId == initial_p0.activeManifestSetId
            assert not (root / "AGENTS.md").exists()
            assert not (root / ".agents").exists()
            assert calls == []
        else:
            assert after.currentRunId == initial_p0.currentRunId
            assert after.activeExportId == initial_p0.activeExportId
            assert after.activeManifestSetId == initial_p0.activeManifestSetId
            assert not (root / "AGENTS.md").exists()
            assert not (root / ".agents").exists()
            if scenario == "move-head-queue":
                assert calls == ["probe", "invoke"]
                assert len(queued) == 1
                queued_state = MaintenanceStore(root, store).read_state()
                assert queued_state.jobs[queued[0]].status == "pending"
                assert queued_state.runningJobId is None
            else:
                assert result.status == "agent-failed"
                assert calls == ["probe", "invoke"]
                assert store.read_state().currentRunId == initial_p0.currentRunId
        return

    assert result.status == "applied", result.message
    if scenario == "explicit-retry":
        assert attempt is not None
        assert calls == ["probe", "invoke"]
        retry = read_attempt(store, MaintenanceStore(root, store).read_state().jobs[job.jobId].activeAttemptId)
        assert retry.phase == "responded"
        assert retry.retryOfAttemptId == attempt.attemptId
    else:
        assert calls == []
    assert MaintenanceStore(root, store).read_state().appliedBaseline == target
    assert MaintenanceStore(root, store).read_state().jobs[job.jobId].status == "applied"
    if scenario == "accepted-claim":
        applied_state = store.read_state()
        applied_run = store.read_run(applied_state.currentRunId)
        accepted = [claim for claim in applied_run.claims if claim.origin == "llm"]
        assert len(accepted) == 1 and accepted[0].proposalStatus == "accepted"
        assert applied_state.acceptedClaimRevisions[accepted[0].claimId] == accepted[0].claimRevision
        assert applied_state.reviewEvents == initial_p0.reviewEvents
