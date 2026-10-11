from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Mapping

from .ids import canonical_json_bytes
from .ledger import LedgerStore, decode_root_document, encode_record
from .maintenance_models import MaintenanceJob, MaintenancePolicy, MaintenanceState
from .models import ProjectDocument, ProjectState, validate_json_value


_ID = re.compile(r"[a-z][a-z0-9-]*_[0-9a-f]{24}\Z")


@dataclass(frozen=True, slots=True)
class _MaintenanceJobEnvelope:
    schemaVersion: str
    job: MaintenanceJob


class MaintenanceStore:
    """Durable maintenance records kept beside, but separate from, P0 state."""

    def __init__(self, root: Path, ledger: LedgerStore) -> None:
        self.root = Path(root).absolute()
        if self.root != ledger.root:
            raise ValueError("maintenance root must match LedgerStore root")
        self.ledger = ledger

    def _require_initialized_project(self) -> tuple[ProjectDocument, ProjectState]:
        project = self.ledger.read_project()
        state = self.ledger.read_state()
        if project is None or state is None:
            raise ValueError("initialized P0 project documents are required")
        relative_state_dir = self.ledger.state_dir.relative_to(self.ledger.root).as_posix()
        if project.projectId != state.projectId:
            raise ValueError("project and state project IDs do not match")
        if project.stateDir != relative_state_dir:
            raise ValueError("project state directory does not match LedgerStore")
        if project.schemaVersion.split(".", 1)[0] != "1" or state.schemaVersion.split(".", 1)[0] != "1":
            raise ValueError("initialized P0 schema major is unsupported")
        return project, state

    def read_state(self) -> MaintenanceState:
        """Read the authoritative pointer, or a read-only default for old projects."""
        project, _project_state = self._require_initialized_project()
        raw = self.ledger.read_maintenance_state_document()
        if raw is None:
            kinds = tuple(dict.fromkeys(target.kind for target in project.targets))
            if not kinds:
                kinds = ("agents-md", "skill")
            return MaintenanceState(
                "1.0", None, None, MaintenancePolicy("ask", 0, kinds), (), {}, None, False
            )
        maintenance = decode_root_document(raw, MaintenanceState)
        for job_id in maintenance.jobs:
            if self.read_job(job_id) is None:
                raise ValueError(f"maintenance state references missing job: {job_id}")
        return maintenance

    def read_job(self, job_id: str) -> MaintenanceJob | None:
        self._validate_job_id(job_id)
        payload = self.ledger.read_immutable_bytes(f"maintenance/jobs/{job_id}.json")
        if payload is None:
            return None
        try:
            raw = json.loads(
                payload.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"non-finite JSON number: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("maintenance job is not valid UTF-8 JSON") from error
        validate_json_value(raw)
        if not isinstance(raw, dict):
            raise ValueError("maintenance job envelope must be an object")
        envelope = decode_root_document(raw, _MaintenanceJobEnvelope)
        if envelope.schemaVersion != "1.0":
            # Minor versions in the current major remain forward-readable.
            if not re.fullmatch(r"1\.[0-9]+", envelope.schemaVersion):
                raise ValueError("invalid maintenance job schemaVersion")
        if envelope.job.jobId != job_id:
            raise ValueError("maintenance job ID does not match its filename")
        return envelope.job

    def write_job(self, job: MaintenanceJob) -> None:
        self._require_initialized_project()
        self._validate_job_id(job.jobId)
        payload = canonical_json_bytes({"schemaVersion": "1.0", "job": encode_record(job)}) + b"\n"
        self.ledger.write_maintenance_job_bytes(job.jobId, payload)

    def read_apply_phase(self, job_id: str, phase: str) -> bytes | None:
        self._validate_job_id(job_id)
        if phase not in {"intent", "staged", "export-prepared", "exported", "applied", "failed"}:
            raise ValueError("unsupported apply journal phase")
        return self.ledger.read_immutable_bytes(f"maintenance/apply/{job_id}/{phase}.json")

    def write_apply_phase(self, job_id: str, phase: str, payload: bytes) -> None:
        self._validate_job_id(job_id)
        if phase not in {"intent", "staged", "export-prepared", "exported", "applied", "failed"}:
            raise ValueError("unsupported apply journal phase")
        if not isinstance(payload, bytes):
            raise TypeError("apply journal payload must be bytes")
        existing = self.read_apply_phase(job_id, phase)
        if existing is not None:
            if existing != payload:
                raise FileExistsError(f"maintenance/apply/{job_id}/{phase}.json")
            return
        self.ledger.write_immutable_bytes(f"maintenance/apply/{job_id}/{phase}.json", payload)

    def replace_state(self, state: MaintenanceState) -> None:
        """Replace the sole maintenance pointer under LedgerStore's lock."""
        self._require_initialized_project()
        if not self.ledger._mutation_active:
            raise RuntimeError("maintenance state write requires an active mutation scope")
        self.read_state()  # Require an authoritative current read before replacing the pointer.
        for job_id in state.jobs:
            if self.read_job(job_id) is None:
                raise ValueError(f"maintenance state references missing job: {job_id}")
        encoded = encode_record(state)
        if not isinstance(encoded, dict):
            raise ValueError("maintenance state must encode as a JSON object")
        self.ledger.replace_maintenance_state_document(encoded)

    def publish_job(
        self, job: MaintenanceJob, state: MaintenanceState, *, supersedes: tuple[str, ...] = (),
        resolves_deferred_event_id: str | None = None,
    ) -> MaintenanceState:
        """Publish an immutable job then commit its visibility with one pointer write."""
        if not self.ledger._mutation_active:
            raise RuntimeError("maintenance job publication requires an active mutation scope")
        current = self.read_state()
        superseded = set(supersedes)
        if len(superseded) != len(supersedes):
            raise ValueError("superseded job IDs must be unique")
        if not superseded.issubset(current.jobs):
            raise ValueError("superseded jobs must be visible in the current maintenance state")
        carried_ids: set[str] = set()
        for predecessor_id in superseded:
            lifecycle = current.jobs[predecessor_id]
            predecessor = self.read_job(predecessor_id)
            if lifecycle.status not in {"pending", "approved"} or lifecycle.activeAttemptId is not None or predecessor is None:
                raise ValueError("only never-attempted pending or approved jobs may be superseded")
            carried_ids.update(predecessor.eventIds)
            if state.jobs.get(predecessor_id) is None or state.jobs[predecessor_id].status != "stale":
                raise ValueError("superseded jobs must become stale in the published state")
        if not set(job.eventIds).issuperset(carried_ids):
            raise ValueError("replacement job must retain predecessor event provenance")
        deferred_ids = {event.eventId for event in current.deferredEvents}
        if resolves_deferred_event_id is not None and resolves_deferred_event_id not in deferred_ids:
            raise ValueError("resolved deferred event must be visible in the current state")
        prior_event_ids = deferred_ids - ({resolves_deferred_event_id} if resolves_deferred_event_id else set())
        for job_id in current.jobs:
            prior = self.read_job(job_id)
            if prior is not None:
                overlap = set(prior.eventIds).intersection(job.eventIds)
                if not overlap:
                    continue
                if job_id in superseded:
                    continue
                # Old stale copies are history, and can overlap only with provenance
                # explicitly carried through a current eligible predecessor.
                if current.jobs[job_id].status == "stale" and overlap.issubset(carried_ids):
                    continue
                prior_event_ids.update(overlap)
        if prior_event_ids.intersection(job.eventIds):
            return current
        lifecycle = state.jobs.get(job.jobId)
        if lifecycle is None:
            raise ValueError("published job must have a lifecycle in the new state")
        self.write_job(job)
        self.replace_state(state)
        return state

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if not isinstance(job_id, str) or not _ID.fullmatch(job_id):
            raise ValueError("job ID must be a canonical lowercase ID")

