from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Literal


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    """Versioned scan limits; all byte limits are raw integer byte counts."""

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
        if not self.schemaVersion or not self.revision:
            raise ValueError("schemaVersion and revision are required")
        if self.followSymlinks:
            raise ValueError("followSymlinks must be false in P0")
        if self.generatedMode != "exclude":
            raise ValueError("generatedMode must be 'exclude' in P0")
        limits = (
            self.maxInventoryEntries,
            self.maxOpenedFiles,
            self.maxFileBytes,
            self.maxDecodedBytes,
            self.maxSnippetBytes,
        )
        if any(limit <= 0 for limit in limits):
            raise ValueError("scan limits must be positive")
        if not self.encodings:
            raise ValueError("encodings must not be empty")


@dataclass(frozen=True, slots=True)
class ClaimPolicy:
    """Versioned claim defaults; renderBudget is UTF-8 bytes per target."""

    schemaVersion: str
    revision: str
    autoAcceptObserved: bool
    renderBudget: int

    def __post_init__(self) -> None:
        if not self.schemaVersion or not self.revision:
            raise ValueError("schemaVersion and revision are required")
        if self.renderBudget <= 0:
            raise ValueError("renderBudget must be positive")


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """A logical output whose livePath is normalized and root-relative."""

    targetId: str
    kind: Literal["agents-md", "skill"]
    livePath: str
    ownershipMode: Literal["managed-blocks", "whole-files"]
    emptyBlockPolicy: Literal["keep"]

    def __post_init__(self) -> None:
        if not self.targetId:
            raise ValueError("targetId is required")
        if self.kind not in {"agents-md", "skill"}:
            raise ValueError("unsupported target kind")
        if self.ownershipMode not in {"managed-blocks", "whole-files"}:
            raise ValueError("unsupported ownershipMode")
        if self.emptyBlockPolicy != "keep":
            raise ValueError("emptyBlockPolicy must be 'keep' in P0")
        _validate_repository_path(self.livePath)


def _validate_repository_path(value: str) -> None:
    if not value or "\x00" in value or "\\" in value:
        raise ValueError("livePath must be a normalized repository-relative path")
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("livePath must be repository-relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("livePath must not contain dot segments")
    if path.as_posix() != value:
        raise ValueError("livePath must be normalized with '/' separators")
