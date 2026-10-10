from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Literal, Mapping

from .models import _repository_path


_ID = re.compile(r"[a-z][a-z0-9-]*_[0-9a-f]{24}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")


def _identifier(value: str, field: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{field} must be a canonical lowercase ID")


def _commit(value: str | None, field: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not _COMMIT.fullmatch(value):
        raise ValueError(f"{field} must be a full lowercase Git commit ID")


def _hash(value: str, field: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be sha256: followed by 64 lowercase hex characters")


def _nonempty(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


@dataclass(frozen=True, slots=True)
class GitEvent:
    eventId: str
    hookName: str
    cursor: str
    targetCommit: str
    reasonCode: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.eventId, "eventId")
        _nonempty(self.hookName, "hookName")
        _nonempty(self.cursor, "cursor")
        _commit(self.targetCommit, "targetCommit")
        if self.reasonCode is not None:
            _nonempty(self.reasonCode, "reasonCode")


@dataclass(frozen=True, slots=True)
class MaintenancePolicy:
    approval: Literal["ask", "auto"]
    grantRevision: int
    targetKinds: tuple[Literal["agents-md", "skill"], ...]

    def __post_init__(self) -> None:
        if self.approval not in {"ask", "auto"}:
            raise ValueError("approval must be 'ask' or 'auto'")
        if isinstance(self.grantRevision, bool) or not isinstance(self.grantRevision, int) or self.grantRevision < 0:
            raise ValueError("grantRevision must be a nonnegative integer")
        if not self.targetKinds or len(set(self.targetKinds)) != len(self.targetKinds):
            raise ValueError("targetKinds must be a nonempty unique tuple")
        if any(kind not in {"agents-md", "skill"} for kind in self.targetKinds):
            raise ValueError("unsupported maintenance target kind")


@dataclass(frozen=True, slots=True)
class MaintenanceJob:
    """Immutable binding between a committed event and its proposed maintenance."""

    jobId: str
    baselineCommit: str | None
    targetCommit: str
    eventIds: tuple[str, ...]
    changedPaths: tuple[str, ...]
    impactScopes: tuple[str, ...]
    diffHash: str
    sourceContextHash: str
    policyRevision: str
    targetKinds: tuple[str, ...]
    expectedManifestSetId: str | None
    expectedOutputHashes: Mapping[str, str]

    def __post_init__(self) -> None:
        _identifier(self.jobId, "jobId")
        _commit(self.baselineCommit, "baselineCommit", optional=True)
        _commit(self.targetCommit, "targetCommit")
        if not self.eventIds:
            raise ValueError("eventIds must not be empty")
        for event_id in self.eventIds:
            _identifier(event_id, "eventId")
        if len(set(self.eventIds)) != len(self.eventIds):
            raise ValueError("eventIds must be unique")
        for path in self.changedPaths:
            _repository_path(path, "changedPath")
        if len(set(self.changedPaths)) != len(self.changedPaths):
            raise ValueError("changedPaths must be unique")
        for scope in self.impactScopes:
            _nonempty(scope, "impactScope")
        _hash(self.diffHash, "diffHash")
        _hash(self.sourceContextHash, "sourceContextHash")
        _nonempty(self.policyRevision, "policyRevision")
        if any(kind not in {"agents-md", "skill"} for kind in self.targetKinds):
            raise ValueError("unsupported maintenance target kind")
        if self.expectedManifestSetId is not None:
            _identifier(self.expectedManifestSetId, "expectedManifestSetId")
        output_hashes = dict(self.expectedOutputHashes)
        for path, digest in output_hashes.items():
            _repository_path(path, "expected output path")
            _hash(digest, "expected output hash")
        object.__setattr__(self, "expectedOutputHashes", MappingProxyType(output_hashes))


@dataclass(frozen=True, slots=True)
class MaintenanceLifecycle:
    status: Literal["pending", "approved", "running", "skipped", "applied", "failed", "stale"]
    approvalSource: Literal["user", "auto"] | None
    approvalBindingHash: str | None
    notificationState: Literal["pending", "reported"]
    skippedTargetCommit: str | None
    activeAttemptId: str | None
    failureCode: str | None

    def __post_init__(self) -> None:
        if self.status not in {"pending", "approved", "running", "skipped", "applied", "failed", "stale"}:
            raise ValueError("unsupported maintenance lifecycle status")
        if self.approvalSource not in {None, "user", "auto"}:
            raise ValueError("unsupported approval source")
        if self.approvalBindingHash is not None:
            _hash(self.approvalBindingHash, "approvalBindingHash")
        if self.notificationState not in {"pending", "reported"}:
            raise ValueError("unsupported notification state")
        _commit(self.skippedTargetCommit, "skippedTargetCommit", optional=True)
        if self.activeAttemptId is not None:
            _identifier(self.activeAttemptId, "activeAttemptId")
        if self.failureCode is not None:
            _nonempty(self.failureCode, "failureCode")


@dataclass(frozen=True, slots=True)
class MaintenanceState:
    schemaVersion: str
    observedEventCursor: str | None
    appliedBaseline: str | None
    policy: MaintenancePolicy
    deferredEvents: tuple[GitEvent, ...]
    jobs: Mapping[str, MaintenanceLifecycle]
    runningJobId: str | None
    enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.schemaVersion, str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", self.schemaVersion):
            raise ValueError("schemaVersion must contain major.minor")
        if self.schemaVersion.split(".", 1)[0] != "1":
            raise ValueError(f"unknown schema major: {self.schemaVersion}")
        if self.observedEventCursor is not None:
            _nonempty(self.observedEventCursor, "observedEventCursor")
        _commit(self.appliedBaseline, "appliedBaseline", optional=True)
        seen_events: set[str] = set()
        for event in self.deferredEvents:
            if event.eventId in seen_events:
                raise ValueError("deferred event IDs must be unique")
            seen_events.add(event.eventId)
        for job_id, lifecycle in self.jobs.items():
            _identifier(job_id, "job ID")
            if not isinstance(lifecycle, MaintenanceLifecycle):
                raise ValueError("jobs values must be maintenance lifecycles")
        if self.runningJobId is not None:
            _identifier(self.runningJobId, "runningJobId")
            if self.runningJobId not in self.jobs or self.jobs[self.runningJobId].status != "running":
                raise ValueError("runningJobId must reference the running lifecycle")
        if any(job_id != self.runningJobId and item.status == "running" for job_id, item in self.jobs.items()):
            raise ValueError("only runningJobId may have running status")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")

    @property
    def pendingJobIds(self) -> tuple[str, ...]:
        return tuple(job_id for job_id, lifecycle in self.jobs.items() if lifecycle.status == "pending")

