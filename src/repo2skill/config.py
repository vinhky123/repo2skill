from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import re
import stat
import unicodedata
import uuid

from . import SCHEMA_VERSION, __version__
from .ids import canonical_json_bytes
from .models import ClaimPolicy, ProjectDocument, ProjectState, ScanPolicy, TargetSpec


class PathBoundaryError(ValueError):
    """A configured path cannot be accessed safely below its root."""


class ProjectDataCorruptionError(RuntimeError):
    """A persisted project document or state pointer cannot be decoded safely."""


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


def canonical_repository_path(root: Path, candidate: str | Path) -> str:
    """Resolve an existing or future root-relative path without redirect traversal."""
    root_path = _validated_root(root)
    raw = str(candidate)
    if not raw or "\x00" in raw:
        raise PathBoundaryError("path must be a nonempty root-relative path")
    candidate_path = Path(raw)
    windows = PureWindowsPath(raw)
    if windows.drive or windows.is_absolute() or (os.sep != "\\" and "\\" in raw):
        if not candidate_path.is_absolute():
            raise PathBoundaryError("path must use canonical root-relative syntax")
    if candidate_path.is_absolute():
        try:
            relative = candidate_path.relative_to(root_path)
        except ValueError as error:
            raise PathBoundaryError("path must remain inside repository root") from error
    else:
        relative = candidate_path
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise PathBoundaryError("path must not contain empty or dot segments")
    resolved = resolve_under(root_path, relative)
    return resolved.relative_to(root_path).as_posix()


def load_project_document(root: Path, config_path: str | Path) -> tuple[ProjectDocument, Path]:
    """Read the single canonical project document selected by --config."""
    from .ledger import LedgerStore

    root_path = _validated_root(root)
    relative = canonical_repository_path(root_path, config_path)
    selected = Path(*relative.split("/"))
    if selected.name != "project.json":
        raise ValueError("--config must name the canonical project.json document")
    state_relative = selected.parent.as_posix()
    if state_relative in {"", "."}:
        raise ValueError("project state directory must be below repository root")
    store = LedgerStore(root_path, root_path / selected.parent)
    try:
        project = store.read_project()
    except (OSError, ValueError, TypeError) as error:
        raise ProjectDataCorruptionError("project document is unreadable or invalid") from error
    if project is None:
        raise FileNotFoundError("project configuration is not initialized")
    if project.stateDir != state_relative:
        raise ValueError("project stateDir does not match the canonical config parent")
    return project, root_path / selected.parent


def initialize_project_document(
    root: Path,
    *,
    state_dir: str | Path,
    config_path: str | Path,
    config_was_explicit: bool,
    git_mode: str,
    targets: tuple[str, ...],
) -> tuple[ProjectDocument, bool]:
    """Create the authoritative project and initial state under one mutation lock."""
    from dataclasses import replace

    from .ledger import LedgerStore

    root_path = _validated_root(root)
    state_relative = canonical_repository_path(root_path, state_dir)
    if config_was_explicit and canonical_repository_path(root_path, config_path) != f"{state_relative}/project.json":
        raise ValueError("explicit --config must equal <state-dir>/project.json on init")
    if git_mode not in {"auto", "head", "worktree", "no-git"}:
        raise ValueError("invalid Git mode")
    target_map = {target.targetId: target for target in default_targets(root_path)}
    if not targets:
        selected_targets = tuple(target_map.values())
    else:
        if len(set(targets)) != len(targets) or any(target_id not in target_map for target_id in targets):
            raise ValueError("targets must be unique configured target IDs")
        selected_targets = tuple(target_map[target_id] for target_id in targets)
    for target in selected_targets:
        path = target.livePath
        if path == state_relative or path.startswith(state_relative + "/") or state_relative.startswith(path + "/"):
            raise ValueError("state directory and live target paths must not overlap")
    for index, first in enumerate(selected_targets):
        for second in selected_targets[index + 1:]:
            if first.livePath == second.livePath or first.livePath.startswith(second.livePath + "/") or second.livePath.startswith(first.livePath + "/"):
                raise ValueError("configured live target paths must not overlap")
    store = LedgerStore(root_path, root_path / Path(*state_relative.split("/")))
    root_label = root_path.name or "repository"
    with store.mutation():
        store.require_no_unresolved_export()
        try:
            existing = store.read_project()
            state = store.read_state()
        except (OSError, ValueError, TypeError) as error:
            raise ProjectDataCorruptionError("project document or state pointer is unreadable or invalid") from error
        if existing is not None:
            expected = replace(
                existing, rootLabel=root_label, stateDir=state_relative, gitMode=git_mode,
                scanPolicy=default_scan_policy(), claimPolicy=default_claim_policy(),
                targets=selected_targets,
            )
            if existing != expected or state is None or state.projectId != existing.projectId:
                raise ValueError("existing project state is incompatible with init options")
            return existing, False
        if state is not None:
            raise ValueError("state exists without its project document")
        known = {".lock"}
        unknown = {
            item.name for item in store.state_dir.iterdir()
            if item.name not in known
        }
        if unknown:
            raise ValueError("state directory contains unrecognized files")
        project = ProjectDocument(
            SCHEMA_VERSION, __version__, str(uuid.uuid4()), root_label, state_relative,
            git_mode, default_scan_policy(), default_claim_policy(), selected_targets,
        )
        store.write_immutable("project.json", __import__("json").loads(canonical_json_bytes(project)))
        store.read_state()
        store.replace_state(ProjectState(
            SCHEMA_VERSION, __version__, project.projectId, None, None, (), {}, None, None,
        ))
        return project, True


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
