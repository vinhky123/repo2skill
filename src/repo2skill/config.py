from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import re
import stat
import unicodedata

from .models import ClaimPolicy, ScanPolicy, TargetSpec


class PathBoundaryError(ValueError):
    """A configured path cannot be accessed safely below its root."""


def default_scan_policy() -> ScanPolicy:
    """Return the immutable P0 scan policy; all size caps are bytes."""
    return ScanPolicy(
        schemaVersion="1.0",
        revision="scan-policy-v1",
        include=(),
        exclude=(),
        followSymlinks=False,
        generatedMode="exclude",
        maxInventoryEntries=100_000,
        maxOpenedFiles=5_000,
        maxFileBytes=2 * 1024 * 1024,
        maxDecodedBytes=64 * 1024 * 1024,
        maxSnippetBytes=200 * 1024,
        encodings=("utf-8-sig",),
    )


def default_claim_policy() -> ClaimPolicy:
    """Return the immutable P0 claim policy; renderBudget is UTF-8 bytes."""
    return ClaimPolicy(
        schemaVersion="1.0",
        revision="claim-policy-v1",
        autoAcceptObserved=True,
        renderBudget=16_384,
    )


def default_targets(root: Path) -> tuple[TargetSpec, ...]:
    """Return logical targets with normalized paths relative to *root*."""
    slug = _project_slug(root)
    return (
        TargetSpec("agents-md", "agents-md", "AGENTS.md", "managed-blocks", "keep"),
        TargetSpec(
            "skill",
            "skill",
            f".agents/skills/{slug}",
            "whole-files",
            "keep",
        ),
    )


def resolve_under(root: Path, candidate: Path) -> Path:
    """Resolve a relative candidate for I/O without crossing unsafe components."""
    root_path = _validated_root(root)

    candidate_text = str(candidate)
    windows = PureWindowsPath(candidate_text)
    if (
        not candidate_text
        or "\x00" in candidate_text
        or (os.sep != "\\" and "\\" in candidate_text)
        or candidate.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise PathBoundaryError(f"path must be normalized and relative to root: {candidate}")

    parts = candidate.parts
    absolute_candidate = root_path.joinpath(*parts)
    current = root_path
    for index, part in enumerate(parts):
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise PathBoundaryError(f"path component is not accessible: {current}") from error
        if _is_link_or_reparse(current, current_stat):
            raise PathBoundaryError(f"symlink or reparse component is not allowed: {current}")
        if index < len(parts) - 1 and not stat.S_ISDIR(current_stat.st_mode):
            raise PathBoundaryError(f"non-directory ancestor is not allowed: {current}")

    absolute_candidate = Path(os.path.abspath(absolute_candidate))
    if not absolute_candidate.is_relative_to(root_path):
        raise PathBoundaryError(f"path escapes root: {candidate}")
    return absolute_candidate


def _is_link_or_reparse(path: Path, path_stat: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    attributes = getattr(path_stat, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & reparse_flag)


def _validated_root(root: Path) -> Path:
    if root.is_absolute():
        raw_root = root
        current = Path(root.anchor)
        parts = root.parts[1:]
    else:
        current = Path.cwd()
        raw_root = current / root
        parts = root.parts
        _validate_root_component(current)

    for part in parts:
        if part == ".":
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        _validate_root_component(current)

    return Path(os.path.normpath(raw_root))


def _validate_root_component(path: Path) -> None:
    try:
        path_stat = path.lstat()
    except OSError as error:
        raise PathBoundaryError(f"root component is not accessible: {path}") from error
    if _is_link_or_reparse(path, path_stat):
        raise PathBoundaryError(f"symlink or reparse root component is not allowed: {path}")
    if not stat.S_ISDIR(path_stat.st_mode):
        raise PathBoundaryError(f"root component is not a directory: {path}")


def _project_slug(root: Path) -> str:
    absolute_root = root if root.is_absolute() else Path.cwd() / root
    root_name = Path(os.path.normpath(absolute_root)).name
    normalized = unicodedata.normalize("NFKD", root_name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-") or "repository"
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
    if slug in reserved:
        slug = f"repo-{slug}"
    return slug[:63].rstrip("-")
