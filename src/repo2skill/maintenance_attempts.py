"""Append-only maintenance agent attempts and crash recovery."""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import re
import uuid

from .ids import canonical_record_id
from .ledger import LedgerStore
from .maintenance_store import MaintenanceStore


_ID = re.compile(r"[a-z][a-z0-9-]*_[0-9a-f]{24}\Z")


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attemptId: str
    jobId: str
    requestHash: str
    approvalBindingHash: str
    retryOfAttemptId: str | None
    phase: str


def _base_path(attempt_id: str) -> str:
    return f"maintenance/attempts/{attempt_id}"


def _phase(store: LedgerStore, attempt_id: str, name: str) -> dict[str, object] | None:
    raw = store.read_immutable_bytes(f"{_base_path(attempt_id)}/{name}.json")
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"attempt {name} record is corrupt") from error
    if not isinstance(value, dict) or value.get("attemptId") != attempt_id:
        raise ValueError(f"attempt {name} record binding is corrupt")
    return value


def read_attempt(store: LedgerStore, attempt_id: str) -> AttemptRecord:
    phases = {name: _phase(store, attempt_id, name) for name in ("prepared", "invoking", "responded", "unknown")}
    prepared = phases["prepared"]
    if prepared is None or (phases["unknown"] is not None and phases["responded"] is not None):
        raise ValueError("attempt has missing or conflicting phase records")
    _validate_attempt_record(prepared, "prepared", attempt_id)
    for name in ("invoking", "responded", "unknown"):
        document = phases[name]
        if document is not None:
            _validate_attempt_record(document, name, attempt_id, prepared)
    if phases["responded"] is not None and phases["invoking"] is None:
        raise ValueError("responded attempt has no invoking record")
    if phases["unknown"] is not None and phases["invoking"] is None:
        raise ValueError("unknown attempt has no invoking record")
    phase = "unknown" if phases["unknown"] is not None else "responded" if phases["responded"] is not None else "invoking" if phases["invoking"] is not None else "prepared"
    record = phases[phase]
    assert record is not None
    for required in ("jobId", "requestHash", "approvalBindingHash", "retryOfAttemptId"):
        if required not in record:
            raise ValueError("attempt record is missing a binding")
    return AttemptRecord(attempt_id, str(record["jobId"]), str(record["requestHash"]),
                         str(record["approvalBindingHash"]), record["retryOfAttemptId"], phase)


def _validate_attempt_record(document: dict[str, object], phase: str, attempt_id: str,
                             prepared: dict[str, object] | None = None) -> None:
    binding_fields = ("schemaVersion", "attemptId", "jobId", "requestHash", "approvalBindingHash", "retryOfAttemptId")
    if any(field not in document for field in binding_fields):
        raise ValueError(f"attempt {phase} record is missing immutable bindings")
    retry_of = document.get("retryOfAttemptId")
    if (document.get("schemaVersion") != "1.0" or document.get("attemptId") != attempt_id or not _ID.fullmatch(attempt_id)
            or not isinstance(document.get("jobId"), str) or not _ID.fullmatch(document["jobId"])
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(document.get("requestHash")))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(document.get("approvalBindingHash")))
            or (retry_of is not None and (not isinstance(retry_of, str) or not _ID.fullmatch(retry_of)))):
        raise ValueError(f"attempt {phase} record has invalid immutable bindings")
    expected_keys = set(binding_fields)
    if phase == "responded":
        expected_keys.update(("status", "exitCode", "outputHash", "outputLength", "diagnostic"))
    if set(document) != expected_keys:
        raise ValueError(f"attempt {phase} record has unexpected or missing fields")
    if prepared is not None and any(document.get(field) != prepared.get(field) for field in binding_fields):
        raise ValueError(f"attempt {phase} bindings differ from prepared record")
    if phase == "responded":
        allowed_statuses = {"ok", "unavailable", "invalid-request", "input-error", "input-cap", "timeout", "output-cap",
                            "nonzero-exit", "pipe-cleanup", "missing-output", "output-invalid"}
        exit_code = document["exitCode"]
        output_length = document["outputLength"]
        diagnostic = document["diagnostic"]
        if not isinstance(document["status"], str) or document["status"] not in allowed_statuses:
            raise ValueError("attempt response status is invalid")
        if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
            raise ValueError("attempt response exit code is invalid")
        if (isinstance(output_length, bool) or not isinstance(output_length, int)
                or output_length < 0 or output_length > 1024 * 1024):
            raise ValueError("attempt response length is invalid")
        if diagnostic is not None:
            if not isinstance(diagnostic, str):
                raise ValueError("attempt response diagnostic is invalid")
            try:
                if len(diagnostic.encode("utf-8")) > 8192:
                    raise ValueError("attempt response diagnostic is invalid")
            except UnicodeEncodeError as error:
                raise ValueError("attempt response diagnostic is invalid") from error
        if not isinstance(document["outputHash"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", document["outputHash"]):
            raise ValueError("attempt response hash is invalid")


def prepare_attempt(store: LedgerStore, job_id: str, request_hash: str, *, explicit_retry: bool = False) -> AttemptRecord:
    """Publish a prepared attempt and authoritative activeAttemptId pointer."""
    maintenance = MaintenanceStore(store.root, store)
    state = maintenance.read_state()
    lifecycle = state.jobs.get(job_id)
    job = maintenance.read_job(job_id)
    if job is None or lifecycle is None:
        raise ValueError("attempt job does not exist")
    if not isinstance(request_hash, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", request_hash):
        raise ValueError("request hash must be a canonical SHA-256 digest")
    parent_id = lifecycle.activeAttemptId
    if parent_id is not None:
        parent = read_attempt(store, parent_id)
        if not explicit_retry:
            raise ValueError("existing attempt requires explicit retry")
        if parent.phase not in {"unknown", "responded", "prepared"}:
            raise ValueError("existing attempt must be resolved before explicit retry")
        if parent.phase == "responded":
            _verify_response(store, parent_id)
        if parent.jobId != job_id:
            raise ValueError("retry parent belongs to a different job")
    elif explicit_retry:
        raise ValueError("explicit retry requires a prior attempt")
    if lifecycle.status != "approved" or not lifecycle.approvalBindingHash:
        raise ValueError("attempt requires an approved job with a current approval binding")
    attempt_id = canonical_record_id("maintenance-attempt", {"jobId": job_id, "nonce": uuid.uuid4().hex})
    record = {"schemaVersion": "1.0", "attemptId": attempt_id, "jobId": job_id,
              "requestHash": request_hash, "approvalBindingHash": lifecycle.approvalBindingHash,
              "retryOfAttemptId": parent_id}
    with store.mutation():
        current = maintenance.read_state()
        current_lifecycle = current.jobs.get(job_id)
        if current_lifecycle != lifecycle:
            raise ValueError("job approval or attempt pointer changed before preparation")
        store.write_immutable(f"{_base_path(attempt_id)}/prepared.json", record)
        jobs = dict(current.jobs)
        jobs[job_id] = replace(current_lifecycle, activeAttemptId=attempt_id, failureCode=None)
        maintenance.replace_state(replace(current, jobs=jobs))
    return AttemptRecord(attempt_id, job_id, request_hash, lifecycle.approvalBindingHash, parent_id, "prepared")


def begin_invocation(store: LedgerStore, job_id: str, attempt_id: str) -> AttemptRecord:
    maintenance = MaintenanceStore(store.root, store)
    prepared = _phase(store, attempt_id, "prepared")
    if prepared is None or prepared.get("jobId") != job_id:
        raise ValueError("attempt is not a prepared attempt for this job")
    _validate_attempt_record(prepared, "prepared", attempt_id)
    with store.mutation():
        state = maintenance.read_state()
        lifecycle = state.jobs.get(job_id)
        job = maintenance.read_job(job_id)
        if lifecycle is None or lifecycle.activeAttemptId != attempt_id or lifecycle.status != "approved" or job is None:
            raise ValueError("attempt pointer or approval changed before invocation")
        if lifecycle.approvalBindingHash != prepared.get("approvalBindingHash"):
            raise ValueError("attempt approval binding changed before invocation")
        store.write_immutable(f"{_base_path(attempt_id)}/invoking.json", prepared)
        jobs = dict(state.jobs)
        jobs[job_id] = replace(lifecycle, status="running", failureCode=None)
        maintenance.replace_state(replace(state, jobs=jobs, runningJobId=job_id))
    return read_attempt(store, attempt_id)


def persist_response(store: LedgerStore, job_id: str, attempt_id: str, *, status: str,
                     output_bytes: bytes, diagnostic: str | None = None,
                     exit_code: int | None = None) -> AttemptRecord:
    """Durably publish exact response bytes before any caller validates them."""
    if not isinstance(output_bytes, bytes) or len(output_bytes) > 1024 * 1024:
        raise ValueError("response bytes exceed the configured output limit")
    allowed_statuses = {"ok", "unavailable", "invalid-request", "input-error", "input-cap", "timeout", "output-cap",
                        "nonzero-exit", "pipe-cleanup", "missing-output", "output-invalid"}
    if (not isinstance(status, str) or status not in allowed_statuses or (diagnostic is not None and
            (not isinstance(diagnostic, str) or len(diagnostic.encode("utf-8")) > 8192))
            or (exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)))):
        raise ValueError("response status or exit code is invalid")
    maintenance = MaintenanceStore(store.root, store)
    invoking = _phase(store, attempt_id, "invoking")
    if invoking is None or invoking.get("jobId") != job_id:
        raise ValueError("response requires a durably invoking attempt")
    if read_attempt(store, attempt_id).phase != "invoking":
        raise ValueError("response attempt phase is not invoking")
    digest = "sha256:" + hashlib.sha256(output_bytes).hexdigest()
    metadata = {**invoking, "status": status, "exitCode": exit_code, "outputHash": digest, "outputLength": len(output_bytes),
                "diagnostic": diagnostic}
    with store.mutation():
        state = maintenance.read_state()
        lifecycle = state.jobs.get(job_id)
        if lifecycle is None or lifecycle.activeAttemptId != attempt_id or lifecycle.status != "running":
            raise ValueError("response attempt is no longer active")
        store.write_immutable_bytes(f"{_base_path(attempt_id)}/response.bin", output_bytes)
        store.write_immutable(f"{_base_path(attempt_id)}/responded.json", metadata)
    return read_attempt(store, attempt_id)


def fail_attempt(store: LedgerStore, job_id: str, attempt_id: str, failure_code: str) -> None:
    """Finish a responded or known failed attempt without changing P0 output pointers."""
    if not isinstance(failure_code, str) or not failure_code.strip() or len(failure_code) > 128:
        raise ValueError("failure code must be a short nonempty string")
    maintenance = MaintenanceStore(store.root, store)
    attempt = read_attempt(store, attempt_id)
    if attempt.jobId != job_id or attempt.phase not in {"responded", "invoking", "unknown", "prepared"}:
        raise ValueError("attempt cannot be failed in its current phase")
    if attempt.phase == "responded":
        _verify_response(store, attempt_id)
    with store.mutation():
        state = maintenance.read_state()
        lifecycle = state.jobs.get(job_id)
        if lifecycle is None or lifecycle.activeAttemptId != attempt_id:
            raise ValueError("attempt is no longer active for this job")
        jobs = dict(state.jobs)
        jobs[job_id] = replace(lifecycle, status="failed", failureCode=failure_code)
        maintenance.replace_state(replace(state, jobs=jobs,
            runningJobId=None if state.runningJobId == job_id else state.runningJobId))


def _verify_response(store: LedgerStore, attempt_id: str) -> tuple[dict[str, object], bytes]:
    prepared = _phase(store, attempt_id, "prepared")
    invoking = _phase(store, attempt_id, "invoking")
    metadata = _phase(store, attempt_id, "responded")
    unknown = _phase(store, attempt_id, "unknown")
    if prepared is None or invoking is None or metadata is None:
        raise ValueError("attempt has no durable response metadata")
    _validate_attempt_record(prepared, "prepared", attempt_id)
    _validate_attempt_record(invoking, "invoking", attempt_id, prepared)
    _validate_attempt_record(metadata, "responded", attempt_id, prepared)
    if unknown is not None:
        _validate_attempt_record(unknown, "unknown", attempt_id, prepared)
        raise ValueError("attempt has conflicting responded and unknown phase records")
    output = store.read_immutable_bytes(f"{_base_path(attempt_id)}/response.bin", max_bytes=1024 * 1024)
    if output is None or len(output) > 1024 * 1024 or len(output) != metadata.get("outputLength"):
        raise ValueError("attempt response blob is missing or has a different length")
    digest = "sha256:" + hashlib.sha256(output).hexdigest()
    if digest != metadata.get("outputHash"):
        raise ValueError("attempt response blob hash does not match metadata")
    return metadata, output


def read_response(store: LedgerStore, attempt_id: str):
    """Read a verified durable response as an AgentResponse."""
    from .maintenance_agent import AgentResponse
    metadata, output = _verify_response(store, attempt_id)
    return AgentResponse(str(metadata["status"]), output, metadata.get("diagnostic"), metadata.get("exitCode"))


def recover_attempt(store: LedgerStore, job_id: str) -> AttemptRecord | None:
    """Resolve interrupted work without ever restarting a process automatically."""
    maintenance = MaintenanceStore(store.root, store)
    state = maintenance.read_state()
    lifecycle = state.jobs.get(job_id)
    if lifecycle is None or lifecycle.activeAttemptId is None:
        return None
    attempt_id = lifecycle.activeAttemptId
    phases = {name: _phase(store, attempt_id, name) for name in ("prepared", "invoking", "responded", "unknown")}
    if phases["responded"] is not None:
        if phases["invoking"] is None or phases["unknown"] is not None:
            raise ValueError("responded attempt has conflicting phase records")
        _verify_response(store, attempt_id)
        return read_attempt(store, attempt_id)
    if phases["unknown"] is not None:
        attempt = read_attempt(store, attempt_id)
        with store.mutation():
            current = maintenance.read_state()
            current_lifecycle = current.jobs.get(job_id)
            if current_lifecycle is None or current_lifecycle.activeAttemptId != attempt_id:
                raise ValueError("active attempt changed during recovery")
            if current_lifecycle.status != "failed" or current.runningJobId == job_id:
                jobs = dict(current.jobs)
                jobs[job_id] = replace(current_lifecycle, status="failed", failureCode="maintenance.attempt-unknown")
                maintenance.replace_state(replace(current, jobs=jobs,
                    runningJobId=None if current.runningJobId == job_id else current.runningJobId))
        return attempt
    if phases["prepared"] is None:
        raise ValueError("active attempt is missing its prepared record")
    if phases["invoking"] is not None and phases["prepared"] != phases["invoking"]:
        raise ValueError("invoking attempt binding differs from prepared record")
    phase = "unknown" if phases["invoking"] is not None else "prepared"
    with store.mutation():
        current = maintenance.read_state()
        current_lifecycle = current.jobs.get(job_id)
        if current_lifecycle is None or current_lifecycle.activeAttemptId != attempt_id:
            raise ValueError("active attempt changed during recovery")
        if phase == "unknown":
            store.write_immutable(f"{_base_path(attempt_id)}/unknown.json", phases["prepared"])
        jobs = dict(current.jobs)
        jobs[job_id] = replace(current_lifecycle, status="failed", failureCode=(
            "maintenance.attempt-unknown" if phase == "unknown" else "maintenance.attempt-not-started"))
        maintenance.replace_state(replace(current, jobs=jobs,
            runningJobId=None if current.runningJobId == job_id else current.runningJobId))
    base = phases["prepared"]
    return AttemptRecord(attempt_id, job_id, str(base["requestHash"]), str(base["approvalBindingHash"]),
                         base.get("retryOfAttemptId"), phase)
