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


@pytest.mark.parametrize("second_response", ["empty", "partial"])
def test_commit_to_approved_apply_to_verified_baseline(
    tmp_path: Path, capsys, monkeypatch, second_response: str,
) -> None:
    """Exercise completed hook observation, exact CLI approval, fake agent, and verified export."""
    import hashlib

    from repo2skill.cli import main
    from repo2skill.config import load_project_document
    from repo2skill.ledger import LedgerStore
    from repo2skill.maintenance_agent import AgentResponse
    from repo2skill.maintenance_hooks import process_hook
    from repo2skill.maintenance_models import MaintenancePolicy
    from repo2skill.maintenance_store import MaintenanceStore
    from repo2skill import maintenance_worker
    from repo2skill import maintenance_hooks
    from repo2skill.maintenance_approval import configure_auto_approval

    root = tmp_path / "repo"
    root.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (root / "src.py").write_text("print('baseline')\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "-qm", "baseline")
    baseline = git("rev-parse", "HEAD")

    assert main(["--root", str(root), "init", "--target", "agents-md", "--format", "json"]) == 0
    capsys.readouterr()
    assert main(["--root", str(root), "scan", "--format", "json"]) == 0
    capsys.readouterr()
    assert main(["--root", str(root), "generate", "--no-ai", "--target", "agents-md",
                 "--format", "json"]) == 0
    capsys.readouterr()
    assert main(["--root", str(root), "export", "--target", "agents-md", "--format", "json"]) == 0
    capsys.readouterr()

    project, state_dir = load_project_document(root, ".repo2skill/project.json")
    store = LedgerStore(root, state_dir)
    maintenance = MaintenanceStore(root, store)
    with store.mutation():
        maintenance.replace_state(dataclasses.replace(
            maintenance.read_state(), enabled=True, appliedBaseline=baseline,
            policy=MaintenancePolicy("ask", 1, ("agents-md",)),
        ))

    output_path = root / "AGENTS.md"
    previous_bytes = output_path.read_bytes()
    begin_marker = b"<!-- repo2skill:"
    assert begin_marker in previous_bytes, previous_bytes
    human_prefix = b"Human preface\n"
    human_suffix = b"Human suffix\n"
    output_path.write_bytes(human_prefix + previous_bytes + human_suffix)

    (root / "src.py").write_text("print('committed target')\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "-qm", "target")
    target = git("rev-parse", "HEAD")
    hook_output: list[str] = []
    observed = process_hook(root, "post-commit", b"", (), store=store, output=hook_output.append)
    assert observed.status == "pending", hook_output
    job = maintenance.read_job(observed.jobId)
    assert job is not None and job.targetCommit == target
    assert job.eventIds

    # Replace the adapter boundary before CLI approval can enter the worker.
    calls: list[str] = []
    run_worker = maintenance_worker.run_worker

    def fake_probe(*_args, **_kwargs):
        calls.append("probe")
        return type("FakeCapability", (), {"supported": True})()

    def fake_invoke(_capability, request, _timeout):
        calls.append("invoke")
        prompt = json.loads(request.promptBytes.decode("utf-8"))
        if calls.count("invoke") > 1:
            operations = []
            if second_response == "partial":
                prior = next(item for item in prompt["priorClaims"]
                             if item["semanticKey"] == "maintenance:source-guidance:python")
                fact = next(item for item in prompt["facts"] if item["factId"] == prior["factIds"][0])
                operations.append({
                    "op": "update", "semanticKey": prior["semanticKey"],
                    "scope": prior["scope"],
                    "renderedText": "The updated guidance describes the committed Python source.",
                    "factIds": [fact["factId"]], "evidenceIds": sorted(fact["evidenceIds"]),
                    "claimRef": f"{prior['claimId']}@{prior['claimRevision']}",
                })
            response = {"jobId": prompt["jobId"], "snapshotId": prompt["snapshotId"],
                        "operations": operations}
            return AgentResponse("ok", json.dumps(response, separators=(",", ":")).encode(), None)
        fact = next(item for item in prompt["facts"] if item["factType"] == "source.fragment.v1")
        prior = prompt["priorClaims"]
        operation = {
            "op": "update" if prior else "add",
            "semanticKey": "maintenance:source-guidance:python",
            "scope": fact["scope"],
            "renderedText": "The committed Python source prints a message.",
            "factIds": [fact["factId"]],
            "evidenceIds": sorted(fact["evidenceIds"]),
            "claimRef": (f"{prior[0]['claimId']}@{prior[0]['claimRevision']}" if prior else None),
        }
        operations = [operation]
        if not prior:
            operations.append({
                "op": "add", "semanticKey": "maintenance:source-guidance:python:additional",
                "scope": fact["scope"],
                "renderedText": "The secondary guidance describes the committed source example.",
                "factIds": [fact["factId"]], "evidenceIds": sorted(fact["evidenceIds"]),
                "claimRef": None,
            })
        response = {"jobId": prompt["jobId"], "snapshotId": prompt["snapshotId"],
                    "operations": operations}
        return AgentResponse("ok", json.dumps(response, separators=(",", ":")).encode(), None)

    def isolated_worker(*args, **kwargs):
        return run_worker(*args, probe=fake_probe, invoke=fake_invoke, **kwargs)

    first_baseline_seen: list[str] = []
    late_event_jobs: list[str] = []
    late_pending_authorizations: list[tuple[str | None, str | None]] = []
    launch_requests: list[Path] = []
    real_run_one = maintenance_worker._run_one

    def interposed_run_one(*args, **kwargs):
        result = real_run_one(*args, **kwargs)
        if result.status == "applied" and not late_event_jobs:
                # The first baseline is already verified. Observe a later commit while
                # the same OS lease is held and record the hidden-worker handoff.
            first_baseline_seen.append(maintenance.read_state().appliedBaseline)
            configure_auto_approval(store)
            (root / "other.txt").write_text("unrelated committed change\n", encoding="utf-8")
            git("add", "other.txt")
            git("commit", "-qm", "queued successor")
            successor_target = git("rev-parse", "HEAD")
            late_result = process_hook(root, "post-commit", b"", (), store=store,
                                       output=hook_output.append)
            assert late_result.status == "pending"
            assert maintenance.read_job(late_result.jobId).targetCommit == successor_target
            late_event_jobs.append(late_result.jobId)
        return result

    def hidden_launch(_root, *, state_dir):
        current_state = maintenance.read_state()
        late_id = next(job_id for job_id, lifecycle in current_state.jobs.items()
                       if lifecycle.status == "pending"
                       and maintenance.read_job(job_id).targetCommit == git("rev-parse", "HEAD"))
        lifecycle = current_state.jobs[late_id]
        late_pending_authorizations.append((lifecycle.approvalSource, lifecycle.approvalBindingHash))
        launch_requests.append(Path(state_dir))

    monkeypatch.setattr(maintenance_worker, "_run_one", interposed_run_one)
    monkeypatch.setattr(maintenance_hooks, "launch_worker", hidden_launch)
    monkeypatch.setattr(maintenance_worker, "run_worker", isolated_worker)
    assert main(["--root", str(root), "--format", "json", "maintain", "approve", job.jobId]) == 0
    approval_output = capsys.readouterr().out
    assert json.loads(approval_output.splitlines()[-1])["result"]["status"] == "applied"

    assert first_baseline_seen == [target]
    # The hidden-launch request was issued while the foreground worker held its
    # lease. Drain the durable auto-eligible event after that lease is released.
    assert launch_requests == [state_dir]
    successor_result = run_worker(
        store, auto_drain=True, wait_budget_seconds=0, probe=fake_probe, invoke=fake_invoke,
    )
    assert successor_result.status == "idle", successor_result.message  # auto-drain applied the successor
    final_state = maintenance.read_state()
    successor_id = late_event_jobs[0]
    successor = maintenance.read_job(successor_id)
    assert successor is not None and final_state.jobs[successor_id].status == "applied"
    assert successor.targetCommit == git("rev-parse", "HEAD")
    assert late_pending_authorizations == [(None, None)]
    assert final_state.jobs[successor_id].approvalSource == "auto"
    assert final_state.appliedBaseline == successor.targetCommit
    p0_state = store.read_state()
    assert p0_state is not None and p0_state.activeManifestSetId is not None
    manifest_set = store.read_manifest_set(
        f"manifests/{p0_state.activeExportId}/{p0_state.activeManifestSetId}.json"
    )
    assert manifest_set is not None and manifest_set.targets
    for reference in manifest_set.targets.values():
        manifest = store.read_target_manifest(reference.path)
        assert manifest is not None
        for output_file in manifest.files:
            live = (root / output_file.path).read_bytes()
            assert "sha256:" + hashlib.sha256(live).hexdigest() == output_file.exactHash
    final_bytes = output_path.read_bytes()
    assert final_bytes.startswith(human_prefix)
    assert final_bytes.endswith(human_suffix)
    assert b"The secondary guidance describes the committed source example." in final_bytes
    if second_response == "empty":
        assert b"The committed Python source prints a message." in final_bytes
    else:
        assert b"The updated guidance describes the committed Python source." in final_bytes
    applied_run = store.read_run(p0_state.currentRunId)
    assert applied_run is not None
    accepted = [claim for claim in applied_run.claims
                if claim.origin == "llm" and claim.proposalStatus == "accepted"]
    assert len(accepted) == 2
    assert calls == ["probe", "invoke", "probe", "invoke"]


@pytest.mark.parametrize("scenario", [
    "responded-recovery", "post-baseline-crash", "explicit-retry",
    "disable-mid-call", "finalization-p0-race", "accepted-claim",
    "failed-agent", "move-head-queue", "move-head-after-stage",
    "export-before-baseline-crash", "authorization-response-mismatch",
    "authorization-missing-for-new-model", "authorization-extra-accepted-ref",
    "authorization-export-mismatch", "authorization-resume-mismatch",
    "disable-after-probe", "move-head-after-probe", "grant-revoked-after-probe",
    "deferred-no-baseline", "deferred-interval", "deferred-shared-budget", "deferred-stale-proof",
    "authorization-after-export-crash",
])
def test_responded_attempt_recovery_and_explicit_retry(
    tmp_path: Path, monkeypatch, capsys, scenario: str,
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
    from repo2skill.maintenance_git import bootstrap_commit, compare_commits, enqueue_event
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
    base_commit = git("rev-parse", "HEAD")

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

    if scenario in {"deferred-interval", "deferred-shared-budget", "deferred-stale-proof"}:
        with store.mutation():
            maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline=base_commit))
        (root / "src.py").write_text("print('intermediate')\n", encoding="utf-8")
        git("add", "src.py")
        git("commit", "-qm", "intermediate")
        intermediate = git("rev-parse", "HEAD")
        deferred_event = GitEvent("git-event_2123456789abcdef01234567", "post-commit",
                                  "cursor-intermediate", intermediate)
        from repo2skill import maintenance_git
        monkeypatch.setattr(maintenance_git, "MAX_CONTEXT_FILE_BYTES", 1)
        assert enqueue_event(store, deferred_event, compare_commits(root, base_commit, intermediate)) is None
        assert maint.read_state().deferredEvents[0].targetCommit == intermediate
        if scenario == "deferred-shared-budget":
            (root / "src.py").write_text("print('intermediate two')\n", encoding="utf-8")
            git("add", "src.py")
            git("commit", "-qm", "intermediate two")
            intermediate_two = git("rev-parse", "HEAD")
            second_deferred = GitEvent(
                "git-event_2223456789abcdef01234567", "post-commit", "cursor-intermediate-two",
                intermediate_two,
            )
            assert enqueue_event(
                store, second_deferred, compare_commits(root, base_commit, intermediate_two),
            ) is None
        monkeypatch.setattr(maintenance_git, "MAX_CONTEXT_FILE_BYTES", 1024 * 1024)
    elif scenario == "deferred-no-baseline":
        with store.mutation():
            maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline=None))

    (root / "src.py").write_text("print('target')\n", encoding="utf-8")
    git("add", "src.py")
    git("commit", "-qm", "target")
    target = git("rev-parse", "HEAD")
    event = GitEvent("git-event_0123456789abcdef01234567", "post-commit", "cursor-target", target)
    if scenario == "deferred-no-baseline":
        assert enqueue_event(store, event, bootstrap_commit(root, target)) is None
        rebuild_event = GitEvent("git-event_3123456789abcdef01234567", "manual-catch-up",
                                 "manual:rebuild-target", target)
        job = enqueue_event(store, rebuild_event, bootstrap_commit(root, target),
                            allow_baseline_bootstrap=True, impact_projection="tree-bootstrap")
    elif scenario in {"deferred-interval", "deferred-shared-budget", "deferred-stale-proof"}:
        job = enqueue_event(store, event, compare_commits(root, base_commit, target))
    else:
        job = enqueue_event(store, event, bootstrap_commit(root, target), allow_baseline_bootstrap=True)
    assert job is not None
    if scenario == "grant-revoked-after-probe":
        from repo2skill.maintenance_approval import configure_auto_approval
        auto_state = configure_auto_approval(store)
        with store.mutation():
            maintenance_state = maint.read_state()
            jobs = dict(maintenance_state.jobs)
            jobs[job.jobId] = dataclasses.replace(
                jobs[job.jobId], status="pending", approvalSource=None,
                approvalBindingHash=None, queuedGrantRevision=auto_state.policy.grantRevision,
            )
            maint.replace_state(dataclasses.replace(maintenance_state, jobs=jobs))
    else:
        approve_job(store, job.jobId)
    context = None
    request = None
    if scenario != "grant-revoked-after-probe":
        context = build_committed_context(store, job)
        request = build_agent_request(context, tmp_path / "prepared-snapshot", source_root=root)
    attempt = None
    if scenario not in {"disable-mid-call", "disable-after-probe", "move-head-after-probe",
                        "grant-revoked-after-probe",
                        "failed-agent", "move-head-queue"}:
        assert request is not None
        attempt = prepare_attempt(store, job.jobId, request.requestHash)
        begin_invocation(store, job.jobId, attempt.attemptId)
    operation = None
    if scenario in {"accepted-claim", "authorization-missing-for-new-model",
                    "authorization-extra-accepted-ref"}:
        assert context is not None
        fact = next(item for item in context.facts if item.factType == "source.fragment.v1")
        operation = {
            "op": "add", "semanticKey": "maintenance:source-guidance:python",
            "scope": fact.scope, "renderedText": "The committed Python source prints a message.",
            "factIds": [fact.factId], "evidenceIds": sorted(fact.evidenceIds), "claimRef": None,
        }
    response_bytes = (canonical_json_bytes({
        "jobId": job.jobId, "snapshotId": context.snapshot.snapshotId,
        "operations": [operation] if operation is not None else [],
    }) if context is not None else b"")
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
    if scenario in {"export-before-baseline-crash", "authorization-after-export-crash"}:
        from repo2skill import maintenance_apply
        original_persist = maintenance_apply._persist_apply_journal
        crash_once = [True]

        def fail_exported_phase(ledger, journal):
            if journal.status == "exported" and crash_once[0]:
                crash_once[0] = False
                if scenario == "authorization-after-export-crash":
                    active = maint.read_state().jobs[job.jobId].activeAttemptId
                    assert active is not None
                    response_path = (store.state_dir / "maintenance" / "attempts"
                                     / active / "response.bin")
                    response_path.write_bytes(b"corrupt-after-export")
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
    if scenario in {"authorization-response-mismatch", "authorization-missing-for-new-model",
                    "authorization-extra-accepted-ref"}:
        from repo2skill import stage
        original_stage_run = stage.stage_run

        def corrupt_authorization_before_activation(bundle, run, **kwargs):
            path = store.state_dir / "maintenance" / "authorizations" / f"{run.applicationAuthorizationId}.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["authorization"]["responseHash"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(document), encoding="utf-8")
            return original_stage_run(bundle, run, **kwargs)

        def mismatch_run_authorization(bundle, run, **kwargs):
            if scenario == "authorization-missing-for-new-model":
                run = dataclasses.replace(run, applicationAuthorizationId=None)
            elif scenario == "authorization-extra-accepted-ref":
                accepted_claim = next(claim for claim in run.claims
                                      if claim.kind == "inferred" and claim.origin == "llm"
                                      and claim.proposalStatus == "accepted")
                run = dataclasses.replace(run, claims=(*run.claims,
                    dataclasses.replace(accepted_claim, claimRevision=accepted_claim.claimRevision + 1)))
            return original_stage_run(bundle, run, **kwargs)

        monkeypatch.setattr(stage, "stage_run",
                            corrupt_authorization_before_activation
                            if scenario == "authorization-response-mismatch" else mismatch_run_authorization)
    if scenario == "authorization-export-mismatch":
        from repo2skill import stage
        original_stage_run = stage.stage_run

        def corrupt_after_activation(bundle, run, **kwargs):
            result = original_stage_run(bundle, run, **kwargs)
            path = store.state_dir / "maintenance" / "authorizations" / f"{run.applicationAuthorizationId}.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["authorization"]["responseHash"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(document), encoding="utf-8")
            return result

        monkeypatch.setattr(stage, "stage_run", corrupt_after_activation)
    if scenario == "authorization-resume-mismatch":
        from repo2skill import ownership

        def interrupt_and_corrupt_before_resume(ledger, journal):
            run = ledger.read_run(ledger.read_state().currentRunId)
            path = ledger.state_dir / "maintenance" / "authorizations" / f"{run.applicationAuthorizationId}.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["authorization"]["responseHash"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(document), encoding="utf-8")
            raise OSError("injected interruption before export operations")

        monkeypatch.setattr(ownership, "_perform_journal", interrupt_and_corrupt_before_resume)
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

    def fake_probe(*_args, **_kwargs):
        calls.append("probe")
        if scenario == "disable-after-probe":
            with store.mutation():
                maint.replace_state(dataclasses.replace(maint.read_state(), enabled=False))
        elif scenario == "move-head-after-probe":
            git("commit", "--allow-empty", "-qm", "later source commit")
        elif scenario == "grant-revoked-after-probe":
            from repo2skill.maintenance_approval import revoke_auto_approval
            revoke_auto_approval(store, enabled=True)
        return type("Capability", (), {"supported": True})()

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

    if scenario == "deferred-shared-budget":
        from repo2skill import maintenance_apply, maintenance_context, maintenance_git
        from repo2skill.maintenance_git import MaintenanceGitError
        ancestry_calls = []
        original_proves_ancestor = maintenance_git._proves_ancestor
        original_plan = maintenance_apply._plan_deferred_resolutions
        manifest_reads = []

        def count_manifest_for_plan(ledger, planned_job, events):
            original_manifest = maintenance_context._context_manifest

            def counted_manifest(*args, **kwargs):
                manifest_reads.append(True)
                return original_manifest(*args, **kwargs)

            monkeypatch.setattr(maintenance_context, "_context_manifest", counted_manifest)
            try:
                return original_plan(ledger, planned_job, events)
            finally:
                monkeypatch.setattr(maintenance_context, "_context_manifest", original_manifest)

        def expire_shared_budget(repo_root, ancestor, descendant, budget):
            ancestry_calls.append((store._mutation_active, id(budget)))
            if len(ancestry_calls) == 3:
                raise MaintenanceGitError("maintenance.git-timeout", "injected shared deadline")
            return original_proves_ancestor(repo_root, ancestor, descendant, budget)

        monkeypatch.setattr(maintenance_git, "_proves_ancestor", expire_shared_budget)
        monkeypatch.setattr(maintenance_apply, "_plan_deferred_resolutions", count_manifest_for_plan)
    if scenario == "deferred-stale-proof":
        from repo2skill import maintenance_apply, maintenance_worker
        from repo2skill.maintenance_worker import WorkerResult
        original_plan = maintenance_apply._plan_deferred_resolutions
        mutated = [False]
        proof_baselines = []

        def append_after_proof(ledger, planned_job, events):
            proof_baselines.append(maint.read_state().appliedBaseline)
            proofs = original_plan(ledger, planned_job, events)
            if not mutated[0]:
                mutated[0] = True
                new_event = GitEvent(
                    "git-event_2323456789abcdef01234567", "post-commit", "cursor-arrived-during-proof",
                    target,
                )
                with ledger.mutation():
                    current = maint.read_state()
                    maint.replace_state(dataclasses.replace(
                        current, deferredEvents=(*current.deferredEvents, new_event),
                    ))
                assert len(maint.read_state().deferredEvents) == len(events) + 1
            return proofs

        monkeypatch.setattr(maintenance_apply, "_plan_deferred_resolutions", append_after_proof)
        monkeypatch.setattr(
            maintenance_worker, "_recover_apply_after_error",
            lambda *_args, **_kwargs: WorkerResult("failed", job.jobId, "stale deferred proof"),
        )

    initial_p0 = store.read_state()
    queued = []

    result = run_worker(
        store, foreground=True, job_id=job.jobId,
        explicit_retry=scenario == "explicit-retry",
        probe=fake_probe,
        invoke=fake_invoke,
    )
    if scenario == "deferred-stale-proof":
        assert mutated == [True], (result, maint.read_state().deferredEvents)
        assert proof_baselines == [base_commit], proof_baselines

    if scenario in {"disable-mid-call", "disable-after-probe", "move-head-after-probe",
                    "grant-revoked-after-probe",
                    "finalization-p0-race", "move-head-queue", "failed-agent",
                    "move-head-after-stage", "authorization-response-mismatch",
                    "authorization-missing-for-new-model", "authorization-extra-accepted-ref",
                    "authorization-export-mismatch", "authorization-resume-mismatch",
                    "authorization-after-export-crash", "deferred-stale-proof"}:
        assert result.status != "applied", result.message
        after = store.read_state()
        expected_prior_baseline = base_commit if scenario == "deferred-stale-proof" else None
        assert MaintenanceStore(root, store).read_state().appliedBaseline == expected_prior_baseline
        if scenario == "disable-mid-call":
            assert calls == ["probe", "invoke"]
            assert MaintenanceStore(root, store).read_state().enabled is False
            assert after.currentRunId == initial_p0.currentRunId
            assert after.activeExportId == initial_p0.activeExportId
            assert after.activeManifestSetId == initial_p0.activeManifestSetId
            assert not (root / "AGENTS.md").exists()
            assert not (root / ".agents").exists()
        elif scenario in {"disable-after-probe", "move-head-after-probe", "grant-revoked-after-probe"}:
            assert calls == ["probe"]
            assert after.currentRunId == initial_p0.currentRunId
            assert after.activeExportId == initial_p0.activeExportId
            assert after.activeManifestSetId == initial_p0.activeManifestSetId
            assert not (root / "AGENTS.md").exists()
            assert not (root / ".agents").exists()
            if scenario == "disable-after-probe":
                assert MaintenanceStore(root, store).read_state().enabled is False
            if scenario == "grant-revoked-after-probe":
                assert calls == ["probe"]
                assert MaintenanceStore(root, store).read_state().policy.approval == "ask"
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
        elif scenario in {"authorization-response-mismatch", "authorization-missing-for-new-model",
                          "authorization-extra-accepted-ref"}:
            assert after.currentRunId == initial_p0.currentRunId
            assert after.activeExportId == initial_p0.activeExportId
            assert after.activeManifestSetId == initial_p0.activeManifestSetId
            assert not (root / "AGENTS.md").exists()
            assert not (root / ".agents").exists()
            assert calls == []
        elif scenario == "authorization-export-mismatch":
            assert after.currentRunId != initial_p0.currentRunId
            assert after.activeExportId == initial_p0.activeExportId
            assert after.activeManifestSetId == initial_p0.activeManifestSetId
            assert store.read_state().currentRunId is not None
            assert MaintenanceStore(root, store).read_state().appliedBaseline is None
            assert not (root / "AGENTS.md").exists()
            assert not (root / ".agents").exists()
            assert calls == []
        elif scenario == "authorization-resume-mismatch":
            assert after.currentRunId != initial_p0.currentRunId
            assert after.activeExportId == initial_p0.activeExportId
            assert after.activeManifestSetId == initial_p0.activeManifestSetId
            assert MaintenanceStore(root, store).read_state().appliedBaseline is None
            assert not (root / "AGENTS.md").exists()
            assert not (root / ".agents").exists()
            assert calls == []
        elif scenario == "authorization-after-export-crash":
            assert after.currentRunId != initial_p0.currentRunId
            assert after.activeExportId is not None
            assert after.activeManifestSetId is not None
            assert MaintenanceStore(root, store).read_state().appliedBaseline is None
            assert calls == []
            from repo2skill.maintenance_apply import _read_apply_journal
            from repo2skill.maintenance_models import CommittedSourceGuard
            from repo2skill.models import RepositoryIdentity
            from repo2skill.ownership import resume_export

            intent = _read_apply_journal(store, job.jobId)
            assert intent is not None and intent.exportId is not None
            run = store.read_run(intent.intendedRunId)
            assert run is not None
            recovery_guard = CommittedSourceGuard(
                job.jobId, job.targetCommit, job.sourceContextHash, job.policyRevision,
                run.applicationAuthorizationId,
            )
            identity = RepositoryIdentity(run.snapshot.repositoryId, root, "head", job.targetCommit,
                                          run.snapshot.branch, False, {})
            resume_args = dict(
                store=store, current_identity=identity, current_root_label=project.rootLabel,
                current_policy_revision=job.policyRevision, current_extractor_versions={},
                excluded_paths=(), source_guard=recovery_guard,
            )
            with pytest.raises(ValueError, match="application authorization|durable response|attempt response blob"):
                with store.mutation():
                    resume_export(intent.exportId, {}, **resume_args)

            real_read_journal = store.read_export_journal
            def report_active_incomplete_journal(path):
                journal = real_read_journal(path)
                if path == f"exports/{intent.exportId}.json" and journal is not None:
                    return dataclasses.replace(journal, status="incomplete")
                return journal
            monkeypatch.setattr(store, "read_export_journal", report_active_incomplete_journal)
            with pytest.raises(ValueError, match="application authorization|durable response|attempt response blob"):
                with store.mutation():
                    resume_export(intent.exportId, {}, **resume_args)
        elif scenario == "deferred-stale-proof":
            assert mutated == [True]
            assert len(maint.read_state().deferredEvents) == 2
            assert maint.read_state().appliedBaseline == base_commit
            assert result.status == "failed"
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
    if scenario in {"deferred-no-baseline", "deferred-interval"}:
        applied_maintenance = MaintenanceStore(root, store).read_state()
        assert applied_maintenance.deferredEvents == ()
        assert len(applied_maintenance.resolvedDeferredEvents) == 1
        resolution = applied_maintenance.resolvedDeferredEvents[0]
        assert resolution.resolvedByJobId == job.jobId
        assert resolution.resolutionReason == (
            "covered-by-tree-bootstrap" if scenario == "deferred-no-baseline"
            else "covered-by-applied-interval"
        )
        from repo2skill.cli import main
        code = main(["--root", str(root), "--format", "json", "maintain", "status"])
        status = json.loads(capsys.readouterr().out)
        assert code == 0
        assert status["result"]["deferredEvents"] == []
        assert status["result"]["resolvedDeferredEvents"][0]["resolvedByJobId"] == job.jobId
    if scenario == "deferred-shared-budget":
        assert len(ancestry_calls) == 3
        assert all(not under_mutation for under_mutation, _budget in ancestry_calls)
        assert len({budget for _mutation, budget in ancestry_calls}) == 1
        assert len(manifest_reads) == 1
        pending = maint.read_state().deferredEvents
        assert len(pending) == 1
        assert pending[0].targetCommit != job.targetCommit
    if scenario == "accepted-claim":
        applied_state = store.read_state()
        applied_run = store.read_run(applied_state.currentRunId)
        accepted = [claim for claim in applied_run.claims if claim.origin == "llm"]
        assert len(accepted) == 1 and accepted[0].proposalStatus == "accepted"
        assert applied_state.acceptedClaimRevisions[accepted[0].claimId] == accepted[0].claimRevision
        assert applied_state.reviewEvents == initial_p0.reviewEvents
