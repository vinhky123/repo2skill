"""OS-leased foreground and hidden maintenance worker orchestration."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import Callable, Iterator

from .ledger import LedgerStore, LockBusyError
from .maintenance_approval import (
    OwnershipConflictError, StaleApprovalError, auto_approval_binding,
    verify_approval_binding, verify_job_fingerprints,
)
from .maintenance_store import MaintenanceStore


@dataclass(frozen=True, slots=True)
class WorkerResult:
    status: str
    jobId: str | None = None
    message: str = ""


@contextmanager
def _worker_lease(root: Path, *, state_dir: Path | None = None) -> Iterator[None]:
    """Hold a lock on one confined, retained state-file handle."""
    from .safe_fs import open_directory

    root = Path(root).absolute()
    state_dir = Path(state_dir).absolute() if state_dir is not None else root / ".repo2skill"
    try:
        state_parts = state_dir.relative_to(root).parts
    except ValueError as error:
        raise ValueError("worker state directory must be inside project root") from error
    store = LedgerStore(root, state_dir)
    with store.mutation():
        existing = store.read_immutable_bytes("maintenance/worker.lock")
        if existing is None:
            store.write_immutable_bytes("maintenance/worker.lock", b"\0")
        elif existing != b"\0":
            raise ValueError("maintenance worker lease file has invalid contents")
    guards = []
    opened = None
    try:
        guard = open_directory(root)
        guards.append(guard)
        for component in state_parts:
            guard = guard.open_directory(component)
            guards.append(guard)
        guard = guard.open_directory("maintenance")
        guards.append(guard)
        opened = guard.open_file("worker.lock")
        if opened.stat_before.st_size != 1:
            raise ValueError("maintenance worker lease file has invalid size")
        descriptor = opened._descriptor
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise LockBusyError("maintenance worker lease is held") from error
        else:
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise LockBusyError("maintenance worker lease is held") from error
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        if opened is not None:
            opened.close()
        for guard in reversed(guards):
            guard.close()


def _source_guard(store: LedgerStore, job, authorization_id: str):
    from .maintenance_models import CommittedSourceGuard
    return CommittedSourceGuard(job.jobId, job.targetCommit, job.sourceContextHash,
                                job.policyRevision, authorization_id)


def _build_context_and_request(store: LedgerStore, job):
    from .maintenance_context import _context_manifest, build_agent_request, build_committed_context
    from .maintenance_git import GitReadBudget, materialize_commit
    from .maintenance_source_fragment import maintenance_extractor_versions

    context = build_committed_context(store, job)
    manifest = _context_manifest(store, job)
    files = manifest.get("files")
    omitted = manifest.get("omittedPaths", ())
    if not isinstance(files, list) or not isinstance(omitted, list):
        raise ValueError("saved maintenance context manifest is malformed")
    paths = [item["path"] for item in files if isinstance(item, dict) and isinstance(item.get("path"), str)]
    paths.extend(item for item in omitted if isinstance(item, str))
    directory = tempfile.TemporaryDirectory(prefix="repo2skill-maintenance-request-")
    snapshot_root = Path(directory.name) / "snapshot"
    try:
        materialize_commit(store.root, job.targetCommit, paths, snapshot_root,
                           policy_revision=job.policyRevision,
                           extractor_revisions=maintenance_extractor_versions(),
                           budget=GitReadBudget(timeout_seconds=30.0))
        request = build_agent_request(context, snapshot_root, source_root=store.root)
        return context, request, directory
    except BaseException:
        directory.cleanup()
        raise


def _set_failure(store: LedgerStore, job_id: str, code: str) -> None:
    maintenance = MaintenanceStore(store.root, store)
    with store.mutation():
        state = maintenance.read_state()
        lifecycle = state.jobs.get(job_id)
        if lifecycle is not None:
            jobs = dict(state.jobs)
            jobs[job_id] = replace(lifecycle, status="failed", failureCode=code)
            maintenance.replace_state(replace(state, jobs=jobs,
                runningJobId=None if state.runningJobId == job_id else state.runningJobId))


def _recover_apply_first(store: LedgerStore, job_id: str) -> WorkerResult | None:
    from .maintenance_apply import _read_apply_journal, recover_apply

    intent = _read_apply_journal(store, job_id)
    if intent is None or intent.status == "applied":
        return None
    project_state = store.read_state()
    if (intent.status == "intent" and project_state is not None
            and project_state.currentRunId != intent.intendedRunId):
        # The intent predates any P0 pointer change; resume the exact responded attempt below.
        return None
    run = store.read_run(intent.intendedRunId)
    if run is None or run.applicationAuthorizationId is None:
        return WorkerResult("rebuild-required", job_id, "durable apply intent has no matching immutable run authorization")
    job = MaintenanceStore(store.root, store).read_job(job_id)
    if job is None:
        return WorkerResult("stale", job_id, "durable apply intent references a missing job")
    try:
        result = recover_apply(store, job_id, _source_guard(store, job, run.applicationAuthorizationId))
    except OwnershipConflictError as error:
        return WorkerResult("conflict", job_id, str(error))
    except (OSError, ValueError, RuntimeError) as error:
        return WorkerResult("rebuild-required", job_id, str(error))
    if result.status == "applied":
        return WorkerResult("applied", job_id, "recovered and verified the durable maintenance apply")
    return WorkerResult(result.status, job_id, "durable export remains unresolved; run maintain catch-up for repair")


def _recover_after_applied_pointer(store: LedgerStore, job_id: str) -> WorkerResult | None:
    """Finish a journal phase if an error followed the applied maintenance pointer."""
    maintenance = MaintenanceStore(store.root, store)
    state = maintenance.read_state()
    lifecycle = state.jobs.get(job_id)
    if lifecycle is None or lifecycle.status != "applied":
        return None
    from .maintenance_apply import _read_apply_journal, recover_apply
    intent = _read_apply_journal(store, job_id)
    run = store.read_run(intent.intendedRunId) if intent is not None else None
    if intent is None or run is None or run.applicationAuthorizationId is None:
        return WorkerResult("rebuild-required", job_id,
                            "applied lifecycle has no verifiable durable apply record")
    job = maintenance.read_job(job_id)
    if job is None:
        return WorkerResult("rebuild-required", job_id, "applied lifecycle references a missing job")
    try:
        result = recover_apply(store, job_id, _source_guard(store, job, run.applicationAuthorizationId))
    except (OSError, ValueError, RuntimeError) as error:
        return WorkerResult("rebuild-required", job_id, str(error))
    return WorkerResult(result.status, job_id, "recovered apply after its maintenance pointer committed")


def _recover_apply_after_error(store: LedgerStore, job_id: str) -> WorkerResult | None:
    """Prefer durable apply recovery over failing a responded attempt."""
    result = _recover_apply_first(store, job_id)
    if result is None:
        return _recover_after_applied_pointer(store, job_id)
    return result


def _run_one(
    store: LedgerStore, job_id: str, *, explicit_retry: bool,
    probe: Callable | None = None, invoke: Callable | None = None,
) -> WorkerResult:
    from .maintenance_agent import invoke_agent as default_invoke, probe_agent as default_probe, validate_operations
    from .maintenance_apply import apply_job
    from .maintenance_attempts import (
        begin_invocation, fail_attempt, persist_response, prepare_attempt,
        read_response, recover_attempt, read_attempt,
    )
    from .maintenance_context import (
        authorize_claim_revisions, build_maintenance_run, build_prose_claim,
        retained_accepted_prose_claims,
    )

    maintenance = MaintenanceStore(store.root, store)
    state = maintenance.read_state()
    lifecycle = state.jobs.get(job_id)
    job = maintenance.read_job(job_id)
    if lifecycle is None or job is None:
        return WorkerResult("not-found", job_id, "maintenance job does not exist")
    recovery = _recover_apply_first(store, job_id)
    if recovery is not None:
        return recovery
    if not state.enabled:
        return WorkerResult("disabled", job_id, "maintenance is disabled; run maintain setup first")

    # Resolve an interrupted invocation before deciding whether an explicit retry is needed.
    recovered_attempt = None
    if lifecycle.activeAttemptId is not None:
        try:
            recovered_attempt = recover_attempt(store, job_id)
        except (OSError, ValueError, RuntimeError) as error:
            return WorkerResult("rebuild-required", job_id, f"attempt recovery needs repair: {error}")
        state = maintenance.read_state()
        lifecycle = state.jobs[job_id]
        if recovered_attempt is not None and recovered_attempt.phase == "responded" and lifecycle.status == "running":
            pass
        elif lifecycle.status == "failed" and not explicit_retry:
            return WorkerResult("agent-failed", job_id,
                                "the prior attempt failed or is unknown; run maintain retry for an explicit new call")
    if lifecycle.status == "failed" and explicit_retry:
        # The explicit CLI retry is the only authority to append a child attempt.
        from .maintenance_approval import approve_job
        try:
            current = maintenance.read_state()
            lifecycle = approve_job(store, job_id, grant_revision=current.policy.grantRevision)
        except (OwnershipConflictError, StaleApprovalError) as error:
            return WorkerResult("conflict" if isinstance(error, OwnershipConflictError) else "stale", job_id, str(error))

    if lifecycle.status == "pending":
        binding = auto_approval_binding(store, job, lifecycle)
        if binding is None:
            return WorkerResult("approval-required", job_id, "approve this exact job before running the worker")
        with store.mutation():
            current = maintenance.read_state()
            current_lifecycle = current.jobs.get(job_id)
            if current_lifecycle != lifecycle or not current.enabled:
                return WorkerResult("stale", job_id, "automatic approval changed before execution")
            jobs = dict(current.jobs)
            jobs[job_id] = replace(current_lifecycle, status="approved", approvalSource="auto",
                                   approvalBindingHash=binding)
            maintenance.replace_state(replace(current, jobs=jobs))
        lifecycle = maintenance.read_state().jobs[job_id]
    if lifecycle.status != "approved" and not (lifecycle.status == "running" and recovered_attempt is not None
                                                 and recovered_attempt.phase == "responded"):
        return WorkerResult("approval-required", job_id, "approve or explicitly retry this exact job")

    try:
        lifecycle = verify_approval_binding(store, job, lifecycle)
        verify_job_fingerprints(store, job)
        context, request, snapshot_directory = _build_context_and_request(store, job)
    except OwnershipConflictError as error:
        return WorkerResult("conflict", job_id, str(error))
    except StaleApprovalError as error:
        return WorkerResult("stale", job_id, str(error))
    except (OSError, ValueError, RuntimeError) as error:
        _set_failure(store, job_id, "maintenance.rebuild-required")
        return WorkerResult("rebuild-required", job_id, str(error))

    # A response can be reused only if a deterministic rebuild reproduces its original request.
    response = None
    attempt = None
    if (recovered_attempt is not None and recovered_attempt.phase == "responded"
            and not (explicit_retry and lifecycle.status == "approved")):
        if recovered_attempt.requestHash != request.requestHash or recovered_attempt.approvalBindingHash != lifecycle.approvalBindingHash:
            return WorkerResult("stale", job_id, "stored response request or approval binding changed; explicit retry is required")
        try:
            response = read_response(store, recovered_attempt.attemptId)
        except (OSError, ValueError) as error:
            return WorkerResult("rebuild-required", job_id, str(error))
        attempt = recovered_attempt

    if response is None:
        capability = (probe or default_probe)(None, source_root=store.root)
        if not capability.supported:
            snapshot_directory.cleanup()
            return WorkerResult("agent-unavailable", job_id,
                                "installed headless agent does not provide the required isolated structured-output surface")
        if lifecycle.status == "approved":
            try:
                attempt = prepare_attempt(store, job_id, request.requestHash, explicit_retry=explicit_retry)
            except (OSError, ValueError, RuntimeError) as error:
                return WorkerResult("stale", job_id, str(error))
        else:
            attempt = recovered_attempt
            if attempt is None or attempt.phase != "prepared":
                return WorkerResult("agent-failed", job_id, "no safely retryable prepared attempt exists")
        try:
            verify_approval_binding(store, job)
            verify_job_fingerprints(store, job)
            begin_invocation(store, job_id, attempt.attemptId)
            response = (invoke or default_invoke)(capability, request, 180.0)
            persist_response(store, job_id, attempt.attemptId, status=response.status,
                             output_bytes=response.outputBytes, diagnostic=response.diagnostic,
                             exit_code=response.exitCode)
        except OwnershipConflictError as error:
            fail_attempt(store, job_id, attempt.attemptId, "maintenance.source-conflict")
            return WorkerResult("conflict", job_id, str(error))
        except StaleApprovalError as error:
            fail_attempt(store, job_id, attempt.attemptId, "maintenance.stale-binding")
            return WorkerResult("stale", job_id, str(error))
        except (OSError, ValueError, RuntimeError) as error:
            try:
                fail_attempt(store, job_id, attempt.attemptId, "maintenance.agent-failed")
            except (OSError, ValueError, RuntimeError):
                pass
            return WorkerResult("agent-failed", job_id, str(error))
        finally:
            snapshot_directory.cleanup()
        attempt = read_attempt(store, attempt.attemptId)

    snapshot_directory.cleanup()

    if response.status != "ok":
        fail_attempt(store, job_id, attempt.attemptId, "maintenance.agent-failed")
        return WorkerResult("agent-unavailable" if response.status == "unavailable" else "agent-failed",
                            job_id, response.diagnostic or f"agent failed with status {response.status}")
    try:
        operations = validate_operations(response, context)
        response_hash = "sha256:" + hashlib.sha256(response.outputBytes).hexdigest()
        claims = tuple(build_prose_claim(operation, context) for operation in operations
                       if operation.op in {"add", "update"})
        changed_claim_ids = {
            operation.claimRef.rsplit("@", 1)[0]
            for operation in operations if operation.op in {"update", "delete"}
            and operation.claimRef is not None
        }
        retained = tuple(claim for claim in retained_accepted_prose_claims(context)
                         if claim.claimId not in changed_claim_ids)
        claims = (*claims, *retained)
        authorization = authorize_claim_revisions(job, attempt.approvalBindingHash, response_hash,
                                                  claims)
        bundle = build_maintenance_run(store, job, context, operations, authorization)
        guard = _source_guard(store, job, authorization.authorizationId)
        result = apply_job(store, job, bundle, guard)
        if result.status in {"applied", "noop"}:
            return WorkerResult(result.status, job_id, "maintenance apply was verified")
        return WorkerResult(result.status, job_id, "maintenance export remains incomplete; run maintain catch-up")
    except OwnershipConflictError as error:
        recovered = _recover_apply_after_error(store, job_id)
        if recovered is not None:
            return recovered
        fail_attempt(store, job_id, attempt.attemptId, "maintenance.source-conflict")
        return WorkerResult("conflict", job_id, str(error))
    except StaleApprovalError as error:
        recovered = _recover_apply_after_error(store, job_id)
        if recovered is not None:
            return recovered
        fail_attempt(store, job_id, attempt.attemptId, "maintenance.stale-binding")
        return WorkerResult("stale", job_id, str(error))
    except (OSError, ValueError, RuntimeError) as error:
        recovered = _recover_apply_after_error(store, job_id)
        if recovered is not None:
            return recovered
        fail_attempt(store, job_id, attempt.attemptId, "maintenance.agent-failed")
        return WorkerResult("agent-failed", job_id, str(error))


def run_worker(
    store: LedgerStore, *, foreground: bool = False, job_id: str | None = None,
    explicit_retry: bool = False, auto_drain: bool = False,
    wait_budget_seconds: float = 300.0, sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic, retry_delay: Callable[[int], float] | None = None,
    probe: Callable | None = None, invoke: Callable | None = None,
) -> WorkerResult:
    """Run an exact foreground job or drain current auto-eligible work under one lease."""
    root = Path(store.root).resolve()
    state_dir = Path(store.state_dir).resolve()
    if not state_dir.is_relative_to(root):
        return WorkerResult("unsupported", job_id, "configured maintenance state directory is outside the project root")

    deadline = monotonic() + max(0.0, wait_budget_seconds) if auto_drain else monotonic()
    attempt_number = 0
    while True:
        try:
            project = store.read_project()
            if project is None or Path(project.stateDir).as_posix() != store.state_dir.relative_to(store.root).as_posix():
                return WorkerResult("unsupported", job_id, "configured state directory does not match the project")
            with _worker_lease(store.root, state_dir=store.state_dir):
                maintenance = MaintenanceStore(store.root, store)
                state = maintenance.read_state()
                if job_id is not None:
                    result = _run_one(store, job_id, explicit_retry=explicit_retry, probe=probe, invoke=invoke)
                    return result
                if not auto_drain:
                    selected = next((candidate for candidate, lifecycle in state.jobs.items()
                                     if lifecycle.status == "approved" and lifecycle.activeAttemptId is None), None)
                    if selected is not None:
                        return _run_one(store, selected, explicit_retry=False, probe=probe, invoke=invoke)
                    if state.policy.approval == "auto":
                        selected = next((candidate for candidate, lifecycle in state.jobs.items()
                                         if lifecycle.status == "pending" and lifecycle.activeAttemptId is None
                                         and (job := maintenance.read_job(candidate)) is not None
                                         and auto_approval_binding(store, job, lifecycle) is not None), None)
                    if selected is None:
                        return WorkerResult("idle", message="no approved maintenance job is eligible")
                    return _run_one(store, selected, explicit_retry=False, probe=probe, invoke=invoke)
                if not state.enabled:
                    return WorkerResult("disabled", message="maintenance is disabled")
                selected = None
                for candidate, lifecycle in state.jobs.items():
                    if lifecycle.status == "approved" and lifecycle.approvalSource == "auto":
                        selected = candidate
                        break
                    if (lifecycle.status == "pending" and state.policy.approval == "auto"
                            and lifecycle.activeAttemptId is None):
                        job = maintenance.read_job(candidate)
                        if job is not None and auto_approval_binding(store, job, lifecycle) is not None:
                            selected = candidate
                            break
                if selected is None:
                    return WorkerResult("idle", message="no auto-approved maintenance job is eligible")
                result = _run_one(store, selected, explicit_retry=False, probe=probe, invoke=invoke)
                if result.status not in {"applied", "noop"}:
                    return result
                from .maintenance_git import refresh_queued_successor
                refresh_queued_successor(store)
        except LockBusyError:
            if not auto_drain:
                return WorkerResult("busy", job_id, "another maintenance worker currently holds the OS lease")
            remaining = deadline - monotonic()
            if remaining <= 0:
                return WorkerResult("busy-expired", job_id,
                                    "maintenance worker lease remained busy; run maintain catch-up and maintain status")
            attempt_number += 1
            delay = retry_delay(attempt_number) if retry_delay else min(0.25 * (2 ** min(attempt_number - 1, 5)), 5.0)
            sleep(min(max(0.0, delay), remaining))


def _entrypoint() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--state-dir")
    args = parser.parse_args()
    root = Path(args.root).absolute()
    state_dir = Path(args.state_dir).absolute() if args.state_dir else root / ".repo2skill"
    store = LedgerStore(root, state_dir)
    result = run_worker(store, foreground=False, auto_drain=True)
    sys.stderr.write(f"{result.status}: {result.message}\n")
    return 0 if result.status == "idle" else 6


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
