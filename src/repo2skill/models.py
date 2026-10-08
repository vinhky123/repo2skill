from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Callable, Literal, Mapping, TypeAlias
import unicodedata


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
RepositoryPath: TypeAlias = str
SnapshotId: TypeAlias = str
RunId: TypeAlias = str
FactId: TypeAlias = str
ClaimId: TypeAlias = str
TargetId: TypeAlias = str
ExportId: TypeAlias = str
Sha256: TypeAlias = str
Timestamp: TypeAlias = str
RecordRevision: TypeAlias = int
LineSpan: TypeAlias = tuple[int, int]
ClaimRevisionRef: TypeAlias = str

_CANONICAL_ID = re.compile(r"[a-z][a-z0-9-]*_[0-9a-f]{24}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CLAIM_REF = re.compile(r"[a-z][a-z0-9-]*_[0-9a-f]{24}@[1-9][0-9]*\Z")


def validate_json_value(value: object, *, field: str = "JSON value") -> None:
    if value is None or isinstance(value, (bool, str, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for item in value:
            validate_json_value(item, field=field)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field} object keys must be strings")
            validate_json_value(item, field=field)
        return
    raise ValueError(f"{field} is not a JSON value")


def _positive(value: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")


def _identifier(value: str, field: str, *, allow_uuid: bool = False) -> None:
    if not isinstance(value, str) or not (_CANONICAL_ID.fullmatch(value) or (allow_uuid and _UUID.fullmatch(value))):
        raise ValueError(f"{field} must be a canonical lowercase ID")


def _sha256(value: str | None, field: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be sha256: followed by 64 lowercase hex characters")


def _one_of(value: object, allowed: set[str], field: str) -> None:
    if value not in allowed:
        raise ValueError(f"unsupported {field}: {value!r}")


def _repository_path(value: str, field: str = "path") -> None:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError(f"{field} must be a normalized repository-relative path")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must use NFC Unicode normalization")
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError(f"{field} must be repository-relative")
    if any(part in {"", ".", ".."} for part in path.parts) or path.as_posix() != value:
        raise ValueError(f"{field} must be normalized without dot segments")


def _timestamp(value: str) -> None:
    match = (
        re.fullmatch(
            r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z",
            value,
        )
        if isinstance(value, str)
        else None
    )
    if match is None:
        raise ValueError("createdAt/updatedAt/completedAt must be an RFC 3339 UTC timestamp")
    try:
        datetime(*(int(component) for component in match.groups()))
    except ValueError as error:
        raise ValueError(
            "createdAt/updatedAt/completedAt must be an RFC 3339 UTC timestamp"
        ) from error


def _schema(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*\.[0-9]+", value):
        raise ValueError("schemaVersion must contain major.minor")
    if value.split(".", 1)[0] != "1":
        raise ValueError(f"unknown schema major: {value}")


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    schemaVersion: str
    revision: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    followSymlinks: bool
    generatedMode: Literal["exclude"]
    maxInventoryEntries: int
    maxOpenedFiles: int
    maxFileBytes: int
    maxDecodedBytes: int
    maxSnippetBytes: int
    encodings: tuple[str, ...]

    def __post_init__(self) -> None:
        _schema(self.schemaVersion)
        if not self.revision:
            raise ValueError("revision is required")
        if self.followSymlinks:
            raise ValueError("followSymlinks must be false in P0")
        if self.generatedMode != "exclude":
            raise ValueError("generatedMode must be 'exclude' in P0")
        for name in ("maxInventoryEntries", "maxOpenedFiles", "maxFileBytes", "maxDecodedBytes", "maxSnippetBytes"):
            _positive(getattr(self, name), name)
        if not self.encodings:
            raise ValueError("encodings must not be empty")


@dataclass(frozen=True, slots=True)
class ClaimPolicy:
    schemaVersion: str
    revision: str
    autoAcceptObserved: bool
    renderBudget: int

    def __post_init__(self) -> None:
        _schema(self.schemaVersion)
        if not self.revision:
            raise ValueError("revision is required")
        _positive(self.renderBudget, "renderBudget")


@dataclass(frozen=True, slots=True)
class TargetSpec:
    targetId: TargetId
    kind: Literal["agents-md", "skill"]
    livePath: RepositoryPath
    ownershipMode: Literal["managed-blocks", "whole-files"]
    emptyBlockPolicy: Literal["keep"]

    def __post_init__(self) -> None:
        if not self.targetId:
            raise ValueError("targetId is required")
        _one_of(self.kind, {"agents-md", "skill"}, "target kind")
        _one_of(self.ownershipMode, {"managed-blocks", "whole-files"}, "ownershipMode")
        _one_of(self.emptyBlockPolicy, {"keep"}, "emptyBlockPolicy")
        _repository_path(self.livePath, "livePath")


@dataclass(frozen=True, slots=True)
class EvidenceLocator:
    kind: Literal["json-pointer", "key-path", "line-range", "file-presence", "inventory-predicate"]
    value: JsonValue
    lineHint: int | None

    def __post_init__(self) -> None:
        _one_of(self.kind, {"json-pointer", "key-path", "line-range", "file-presence", "inventory-predicate"}, "locator kind")
        validate_json_value(self.value, field="locator value")
        if self.lineHint is not None:
            _positive(self.lineHint, "lineHint")


@dataclass(frozen=True, slots=True)
class Evidence:
    recordRevision: RecordRevision
    evidenceId: str
    revision: int
    snapshotId: SnapshotId
    path: RepositoryPath
    contentHash: Sha256
    semanticKey: str
    mediaType: str
    encoding: str | None
    locator: EvidenceLocator
    redactedPreview: str | None
    availability: Literal["available", "missing", "unreadable", "capped", "unsupported"]

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _positive(self.revision, "revision")
        _identifier(self.evidenceId, "evidenceId"); _identifier(self.snapshotId, "snapshotId")
        _repository_path(self.path); _sha256(self.contentHash, "contentHash")
        _one_of(self.availability, {"available", "missing", "unreadable", "capped", "unsupported"}, "availability")


@dataclass(frozen=True, slots=True)
class Fact:
    recordRevision: RecordRevision
    factId: FactId
    factRevision: int
    snapshotId: SnapshotId
    factType: str
    semanticKey: str
    scope: str
    value: JsonValue
    derivation: Literal["observed", "deterministic-derived", "heuristic", "template"]
    ruleId: str
    ruleVersion: str
    evidenceIds: tuple[str, ...]

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _positive(self.factRevision, "factRevision")
        _identifier(self.factId, "factId"); _identifier(self.snapshotId, "snapshotId")
        validate_json_value(self.value, field="fact value")
        _one_of(self.derivation, {"observed", "deterministic-derived", "heuristic", "template"}, "derivation")
        for value in self.evidenceIds: _identifier(value, "evidenceId")


@dataclass(frozen=True, slots=True)
class Claim:
    recordRevision: RecordRevision
    claimId: ClaimId
    claimRevision: int
    semanticKey: str
    scope: str
    payload: JsonValue
    renderedText: str | None
    templateKey: str | None
    kind: Literal["observed", "inferred", "human_policy"]
    origin: Literal["deterministic", "llm", "human"]
    factIds: tuple[FactId, ...]
    evidenceIds: tuple[str, ...]
    proposalStatus: Literal["pending", "accepted", "rejected", "contested"]
    supersedes: tuple[ClaimRevisionRef, ...]
    derivedFrom: tuple[ClaimRevisionRef, ...]
    lastVerifiedSnapshotId: SnapshotId

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _positive(self.claimRevision, "claimRevision")
        _identifier(self.claimId, "claimId"); _identifier(self.lastVerifiedSnapshotId, "lastVerifiedSnapshotId")
        validate_json_value(self.payload, field="claim payload")
        if (self.renderedText is None) == (self.templateKey is None):
            raise ValueError("exactly one of renderedText and templateKey must be non-null")
        _one_of(self.kind, {"observed", "inferred", "human_policy"}, "kind")
        _one_of(self.origin, {"deterministic", "llm", "human"}, "origin")
        _one_of(self.proposalStatus, {"pending", "accepted", "rejected", "contested"}, "proposalStatus")
        for value in self.factIds: _identifier(value, "factId")
        for value in self.evidenceIds: _identifier(value, "evidenceId")
        for value in self.supersedes + self.derivedFrom:
            if not _CLAIM_REF.fullmatch(value): raise ValueError("invalid claim revision reference")


@dataclass(frozen=True, slots=True)
class InputSelector:
    type: Literal["file", "glob", "negative"]
    value: str
    scope: str
    required: bool

    def __post_init__(self) -> None:
        _one_of(self.type, {"file", "glob", "negative"}, "selector type")
        if not self.value or not self.scope: raise ValueError("selector value and scope are required")


@dataclass(frozen=True, slots=True)
class ExtractionRead:
    recordRevision: RecordRevision
    readId: str
    snapshotId: SnapshotId
    extractorId: str
    extractorVersion: str
    ruleId: str
    ruleVersion: str
    dependencyType: Literal["file", "glob", "negative"]
    selector: InputSelector
    required: bool
    outcome: Literal["present", "missing", "matched", "no-match", "capped", "decode-error", "io-error"]
    contentHash: Sha256 | None
    matchPaths: tuple[RepositoryPath, ...]
    matchSetHash: Sha256 | None
    diagnosticIds: tuple[str, ...]

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _identifier(self.readId, "readId"); _identifier(self.snapshotId, "snapshotId")
        _one_of(self.dependencyType, {"file", "glob", "negative"}, "dependencyType")
        _one_of(self.outcome, {"present", "missing", "matched", "no-match", "capped", "decode-error", "io-error"}, "outcome")
        if self.dependencyType != self.selector.type: raise ValueError("dependencyType must match selector.type")
        _sha256(self.contentHash, "contentHash", optional=True); _sha256(self.matchSetHash, "matchSetHash", optional=True)
        for path in self.matchPaths: _repository_path(path)


@dataclass(frozen=True, slots=True)
class DedupDecision:
    recordRevision: RecordRevision
    decisionId: str
    snapshotId: SnapshotId
    candidateClaimIds: tuple[ClaimId, ...]
    candidateFactIds: tuple[FactId, ...]
    documentationEvidenceIds: tuple[str, ...]
    ruleId: str
    ruleVersion: str
    result: Literal["render", "suppress", "merge"]
    reasonCode: str

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _identifier(self.decisionId, "decisionId"); _identifier(self.snapshotId, "snapshotId")
        _one_of(self.result, {"render", "suppress", "merge"}, "dedup result")
        for value in self.candidateClaimIds: _identifier(value, "claimId")
        for value in self.candidateFactIds: _identifier(value, "factId")
        for value in self.documentationEvidenceIds: _identifier(value, "evidenceId")


@dataclass(frozen=True, slots=True)
class Diagnostic:
    recordRevision: RecordRevision
    code: str
    severity: Literal["info", "warning", "error"]
    message: str
    path: RepositoryPath | None
    extractorId: str | None
    details: JsonValue

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _one_of(self.severity, {"info", "warning", "error"}, "severity")
        if self.path is not None: _repository_path(self.path)
        validate_json_value(self.details, field="diagnostic details")


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    recordRevision: RecordRevision
    selector: InputSelector
    collector: str
    status: Literal["supported", "unsupported", "skipped", "capped", "generated", "invalid", "failed"]
    reasonCode: str
    diagnosticIds: tuple[str, ...]

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision")
        _one_of(self.status, {"supported", "unsupported", "skipped", "capped", "generated", "invalid", "failed"}, "coverage status")


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    recordRevision: RecordRevision
    eventId: str
    claimId: ClaimId
    claimRevision: int
    action: Literal["accept", "reject", "contest"]
    reason: str | None
    createdAt: Timestamp

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _identifier(self.eventId, "eventId"); _identifier(self.claimId, "claimId"); _positive(self.claimRevision, "claimRevision")
        _one_of(self.action, {"accept", "reject", "contest"}, "review action"); _timestamp(self.createdAt)
        if self.action == "accept" and self.reason is not None: raise ValueError("accept reason must be null")
        if self.action in {"reject", "contest"} and (self.reason is None or not self.reason.strip()): raise ValueError("reject and contest require a nonblank reason")


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    path: RepositoryPath
    kind: str
    sizeBytes: int
    contentHash: Sha256 | None
    ignored: bool
    generated: bool
    symlinkTarget: str | None

    def __post_init__(self) -> None:
        _repository_path(self.path)
        if isinstance(self.sizeBytes, bool) or not isinstance(self.sizeBytes, int) or self.sizeBytes < 0:
            raise ValueError("sizeBytes must be a nonnegative integer")
        _sha256(self.contentHash, "contentHash", optional=True)


@dataclass(frozen=True, slots=True)
class InventoryResult:
    root: Path
    entries: tuple[InventoryEntry, ...]
    inventoryHash: Sha256
    diagnostics: tuple[Diagnostic, ...]
    coverage: tuple[CoverageEntry, ...]

    def __post_init__(self) -> None:
        _sha256(self.inventoryHash, "inventoryHash")


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    repositoryId: str
    root: Path
    gitMode: Literal["auto", "head", "worktree", "no-git"]
    commitId: str | None
    branch: str | None
    dirty: bool
    dirtySummary: JsonValue

    def __post_init__(self) -> None:
        _identifier(self.repositoryId, "repositoryId", allow_uuid=True)
        _one_of(self.gitMode, {"auto", "head", "worktree", "no-git"}, "gitMode")
        validate_json_value(self.dirtySummary, field="dirtySummary")


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    snapshotId: SnapshotId
    repositoryId: str
    rootLabel: str
    gitMode: Literal["auto", "head", "worktree", "no-git"]
    commitId: str | None
    branch: str | None
    dirty: bool
    dirtySummary: JsonValue
    inventoryHash: Sha256
    inputDigest: Sha256
    policyRevision: Sha256
    extractorVersions: Mapping[str, str]
    completedAt: Timestamp

    def __post_init__(self) -> None:
        _identifier(self.snapshotId, "snapshotId"); _identifier(self.repositoryId, "repositoryId", allow_uuid=True)
        _one_of(self.gitMode, {"auto", "head", "worktree", "no-git"}, "gitMode")
        _sha256(self.inventoryHash, "inventoryHash"); _sha256(self.inputDigest, "inputDigest"); _sha256(self.policyRevision, "policyRevision"); _timestamp(self.completedAt)
        validate_json_value(self.dirtySummary, field="dirtySummary")


@dataclass(frozen=True, slots=True)
class ReadResult:
    selector: InputSelector
    dependency: ExtractionRead
    bytes: bytes | None
    encoding: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ExtractionContext:
    root: Path
    snapshotId: SnapshotId
    inventory: InventoryResult
    policy: ScanPolicy
    read: Callable[[InputSelector], ReadResult]


@dataclass(frozen=True, slots=True)
class ExtractionBatch:
    reads: tuple[ExtractionRead, ...]
    evidence: tuple[Evidence, ...]
    facts: tuple[Fact, ...]
    diagnostics: tuple[Diagnostic, ...]
    coverage: tuple[CoverageEntry, ...]


@dataclass(frozen=True, slots=True)
class ClaimSet:
    claims: tuple[Claim, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class ProjectDocument:
    schemaVersion: str
    toolVersion: str
    projectId: str
    rootLabel: str
    stateDir: RepositoryPath
    gitMode: Literal["auto", "head", "worktree", "no-git"]
    scanPolicy: ScanPolicy
    claimPolicy: ClaimPolicy
    targets: tuple[TargetSpec, ...]

    def __post_init__(self) -> None:
        _schema(self.schemaVersion); _identifier(self.projectId, "projectId", allow_uuid=True)
        _repository_path(self.stateDir, "stateDir")
        _one_of(self.gitMode, {"auto", "head", "worktree", "no-git"}, "gitMode")
        if not self.toolVersion or not self.rootLabel: raise ValueError("toolVersion and rootLabel are required")


@dataclass(frozen=True, slots=True)
class RunLedger:
    schemaVersion: str
    toolVersion: str
    runId: RunId
    snapshot: SourceSnapshot
    effectiveScanPolicy: ScanPolicy
    effectiveClaimPolicy: ClaimPolicy
    configuredTargets: tuple[TargetSpec, ...]
    reads: tuple[ExtractionRead, ...]
    evidence: tuple[Evidence, ...]
    facts: tuple[Fact, ...]
    claims: tuple[Claim, ...]
    dedupDecisions: tuple[DedupDecision, ...]
    diagnostics: tuple[Diagnostic, ...]
    coverage: tuple[CoverageEntry, ...]
    stagedTargets: Mapping[TargetId, Mapping[Literal["accepted", "preview"], Mapping[RepositoryPath, Sha256]]]
    completed: bool

    def __post_init__(self) -> None:
        _schema(self.schemaVersion); _identifier(self.runId, "runId")
        if not self.toolVersion: raise ValueError("toolVersion is required")
        for modes in self.stagedTargets.values():
            if set(modes) - {"accepted", "preview"}: raise ValueError("invalid staged target mode")
            for files in modes.values():
                for path, digest in files.items(): _repository_path(path); _sha256(digest, "staged target hash")


@dataclass(frozen=True, slots=True)
class ProjectState:
    schemaVersion: str
    toolVersion: str
    projectId: str
    currentRunId: RunId | None
    activeStageRevision: int | None
    reviewEvents: tuple[ReviewEvent, ...]
    acceptedClaimRevisions: Mapping[ClaimId, int]
    activeExportId: ExportId | None
    activeManifestSetId: str | None

    def __post_init__(self) -> None:
        _schema(self.schemaVersion); _identifier(self.projectId, "projectId", allow_uuid=True)
        if self.currentRunId is not None: _identifier(self.currentRunId, "currentRunId")
        if self.activeStageRevision is not None: _positive(self.activeStageRevision, "activeStageRevision")
        if self.activeExportId is not None: _identifier(self.activeExportId, "activeExportId")
        if self.activeManifestSetId is not None: _identifier(self.activeManifestSetId, "activeManifestSetId")
        for key, revision in self.acceptedClaimRevisions.items(): _identifier(key, "accepted claimId"); _positive(revision, "accepted claim revision")


@dataclass(frozen=True, slots=True)
class ValidationResult:
    validClaims: tuple[Claim, ...]
    rejectedClaims: tuple[Claim, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class DedupResult:
    renderableClaims: tuple[Claim, ...]
    decisions: tuple[DedupDecision, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class RenderedTarget:
    targetId: TargetId
    relativeFiles: Mapping[RepositoryPath, bytes]
    exactHashes: Mapping[RepositoryPath, Sha256]
    claimSpans: Mapping[RepositoryPath, Mapping[ClaimRevisionRef, tuple[LineSpan, ...]]]
    diagnostics: tuple[Diagnostic, ...]
    previewFiles: Mapping[RepositoryPath, bytes]
    previewHashes: Mapping[RepositoryPath, Sha256]
    previewClaimSpans: Mapping[RepositoryPath, Mapping[ClaimRevisionRef, tuple[LineSpan, ...]]]


@dataclass(frozen=True, slots=True)
class RenderBundle:
    targets: tuple[RenderedTarget, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class StagedRun:
    runId: RunId
    stageRoot: Path
    targetHashes: Mapping[TargetId, Mapping[RepositoryPath, Sha256]]
    stageRevision: int
    previewTargetHashes: Mapping[TargetId, Mapping[RepositoryPath, Sha256]]


@dataclass(frozen=True, slots=True)
class ManifestBlock:
    recordRevision: RecordRevision
    blockId: str
    exactHash: Sha256
    claimIds: tuple[ClaimId, ...]

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _identifier(self.blockId, "blockId"); _sha256(self.exactHash, "exactHash")
        for claim_id in self.claimIds: _identifier(claim_id, "claimId")


@dataclass(frozen=True, slots=True)
class ManifestFile:
    recordRevision: RecordRevision
    path: RepositoryPath
    ownershipMode: Literal["managed-blocks", "whole-files"]
    exactHash: Sha256
    eol: Literal["lf", "crlf"]
    blocks: tuple[ManifestBlock, ...]
    claimSpans: Mapping[ClaimRevisionRef, tuple[LineSpan, ...]]

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _repository_path(self.path); _sha256(self.exactHash, "exactHash")
        _one_of(self.ownershipMode, {"managed-blocks", "whole-files"}, "ownershipMode"); _one_of(self.eol, {"lf", "crlf"}, "eol")
        _validate_claim_spans(self.claimSpans)


@dataclass(frozen=True, slots=True)
class TargetManifest:
    schemaVersion: str
    toolVersion: str
    manifestId: str
    exportId: ExportId
    targetId: TargetId
    runId: RunId
    stageRevision: int
    files: tuple[ManifestFile, ...]
    createdDirectories: tuple[RepositoryPath, ...]
    createdAt: Timestamp

    def __post_init__(self) -> None:
        _schema(self.schemaVersion); _identifier(self.manifestId, "manifestId"); _identifier(self.exportId, "exportId"); _identifier(self.runId, "runId")
        if not self.targetId: raise ValueError("targetId is required")
        _positive(self.stageRevision, "stageRevision"); _timestamp(self.createdAt)
        for path in self.createdDirectories: _repository_path(path)


@dataclass(frozen=True, slots=True)
class ManifestReference:
    recordRevision: RecordRevision
    targetId: TargetId
    manifestId: str
    exportId: ExportId
    path: RepositoryPath

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _identifier(self.manifestId, "manifestId"); _identifier(self.exportId, "exportId"); _repository_path(self.path)
        if not self.targetId: raise ValueError("targetId is required")


@dataclass(frozen=True, slots=True)
class ManifestSet:
    schemaVersion: str
    toolVersion: str
    manifestSetId: str
    exportId: ExportId
    targets: Mapping[TargetId, ManifestReference]
    createdAt: Timestamp

    def __post_init__(self) -> None:
        _schema(self.schemaVersion); _identifier(self.manifestSetId, "manifestSetId"); _identifier(self.exportId, "exportId"); _timestamp(self.createdAt)


@dataclass(frozen=True, slots=True)
class OwnershipState:
    targetId: TargetId
    manifestId: str | None
    expectedHashes: Mapping[RepositoryPath, Sha256]
    ownedBlocks: Mapping[RepositoryPath, tuple[str, ...]]
    wholeFiles: tuple[RepositoryPath, ...]
    createdDirectories: tuple[RepositoryPath, ...]


@dataclass(frozen=True, slots=True)
class Finding:
    recordRevision: RecordRevision
    code: str
    status: Literal["info", "pending", "incomplete", "stale", "unknown", "conflict", "error"]
    message: str
    path: RepositoryPath | None
    targetId: TargetId | None
    claimIds: tuple[ClaimId, ...]
    details: JsonValue

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision")
        _one_of(self.status, {"info", "pending", "incomplete", "stale", "unknown", "conflict", "error"}, "finding status")
        if self.path is not None: _repository_path(self.path)
        for claim_id in self.claimIds: _identifier(claim_id, "claimId")
        validate_json_value(self.details, field="finding details")


@dataclass(frozen=True, slots=True)
class ExportResult:
    exportId: ExportId
    status: Literal["complete", "incomplete", "conflict"]
    completedTargets: tuple[TargetId, ...]
    remainingTargets: tuple[TargetId, ...]
    conflicts: tuple[Finding, ...]
    manifestIds: Mapping[TargetId, str]


@dataclass(frozen=True, slots=True)
class JournalOperation:
    recordRevision: RecordRevision
    operationId: str
    targetId: TargetId
    path: RepositoryPath
    action: Literal["replace", "delete", "mkdir"]
    expectedOldHash: Sha256 | None
    stagedNewHash: Sha256 | None
    backupPath: RepositoryPath | None
    backupHash: Sha256 | None
    proposedPatchPath: RepositoryPath | None
    status: Literal["planned", "complete", "conflict"]

    def __post_init__(self) -> None:
        _positive(self.recordRevision, "recordRevision"); _identifier(self.operationId, "operationId"); _repository_path(self.path)
        _one_of(self.action, {"replace", "delete", "mkdir"}, "journal action"); _one_of(self.status, {"planned", "complete", "conflict"}, "journal status")
        for name in ("expectedOldHash", "stagedNewHash", "backupHash"): _sha256(getattr(self, name), name, optional=True)
        for name in ("backupPath", "proposedPatchPath"):
            value = getattr(self, name)
            if value is not None: _repository_path(value, name)
        if self.action == "replace" and self.stagedNewHash is None: raise ValueError("replace requires stagedNewHash")


@dataclass(frozen=True, slots=True)
class ExportJournal:
    schemaVersion: str
    toolVersion: str
    exportId: ExportId
    runId: RunId
    stageRevision: int
    targetIds: tuple[TargetId, ...]
    operations: tuple[JournalOperation, ...]
    createdDirectories: Mapping[TargetId, tuple[RepositoryPath, ...]]
    manifestSetId: str | None
    status: Literal["prepared", "incomplete", "conflict", "committed"]
    createdAt: Timestamp
    updatedAt: Timestamp

    def __post_init__(self) -> None:
        _schema(self.schemaVersion); _identifier(self.exportId, "exportId"); _identifier(self.runId, "runId"); _positive(self.stageRevision, "stageRevision")
        _one_of(self.status, {"prepared", "incomplete", "conflict", "committed"}, "export journal status")
        if self.manifestSetId is not None: _identifier(self.manifestSetId, "manifestSetId")
        _timestamp(self.createdAt); _timestamp(self.updatedAt)
        for paths in self.createdDirectories.values():
            for path in paths: _repository_path(path)


@dataclass(frozen=True, slots=True)
class CheckResult:
    sourceStatus: Literal["fresh", "stale", "unknown"]
    reviewStatus: Literal["clear", "pending"]
    coverageStatus: Literal["complete", "informational-gaps", "required-gap"]
    affectedClaimIds: tuple[ClaimId, ...]
    findings: tuple[Finding, ...]


@dataclass(frozen=True, slots=True)
class Explanation:
    claim: Claim
    facts: tuple[Fact, ...]
    evidence: tuple[Evidence, ...]
    snapshot: SourceSnapshot
    dedupDecision: DedupDecision | None
    stagedLocations: tuple[RepositoryPath, ...]
    liveLocations: tuple[RepositoryPath, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class LedgerStoreSnapshot:
    project: ProjectDocument
    state: ProjectState
    runs: Mapping[RunId, RunLedger]
    manifestSets: Mapping[ExportId, ManifestSet]
    stageLocations: Mapping[tuple[RunId, int], RepositoryPath]


def _validate_claim_spans(value: Mapping[ClaimRevisionRef, tuple[LineSpan, ...]]) -> None:
    for reference, spans in value.items():
        if not _CLAIM_REF.fullmatch(reference): raise ValueError("invalid claim revision reference")
        for start, end in spans:
            _positive(start, "line span start"); _positive(end, "line span end")
            if end < start: raise ValueError("line span end must not precede start")


_validate_repository_path = _repository_path
