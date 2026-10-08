from __future__ import annotations

from collections.abc import Sequence
import os
from pathlib import Path
import subprocess
from typing import Literal
import unicodedata

from pathspec import GitIgnoreSpec

from .config import _validated_root
from .git_read import GitReadError, run_git_bounded
from .ids import canonical_record_id
from .inventory import (
    _final_exclusion,
    _generated_or_security,
    _normalize_configured_path,
    _normalize_exclude,
)
from .models import InventoryResult, RepositoryIdentity, RepositoryPath, ScanPolicy


class RepositoryIdentityError(ValueError):
    """Repository identity could not be captured under the requested semantics."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def capture_repository(
    root: Path,
    inventory: InventoryResult,
    git_mode: Literal["auto", "head", "worktree", "no-git"],
    *,
    project_id: str,
    policy: ScanPolicy,
    excluded_paths: Sequence[RepositoryPath] = (),
) -> RepositoryIdentity:
    """Capture stable repository identity and eligible worktree changes."""
    root_path = _validated_root(root)
    if inventory.root != root_path:
        raise RepositoryIdentityError(
            "identity.root-mismatch",
            "inventory root does not match the repository identity root",
        )
    if any(
        item.selector.required
        and item.status in {"unsupported", "capped", "invalid", "failed"}
        for item in inventory.coverage
    ):
        raise RepositoryIdentityError(
            "identity.inventory-incomplete",
            "repository identity cannot classify dirtiness from an incomplete inventory",
        )
    if git_mode == "no-git":
        return _no_git_identity(root_path, project_id)

    runner = _GitRunner(root_path)
    inside = runner.call("rev-parse", "--is-inside-work-tree", allowed=(0, 128))
    in_worktree = inside.returncode == 0 and inside.stdout.strip() == b"true"
    if not in_worktree:
        if git_mode == "auto":
            return _no_git_identity(root_path, project_id)
        raise RepositoryIdentityError(
            "identity.not-git",
            "requested Git worktree identity outside a Git repository",
        )

    top = Path(
        os.fsdecode(
            runner.call(
                "rev-parse", "--path-format=absolute", "--show-toplevel"
            ).stdout.strip()
        )
    )
    common_dir = Path(
        os.fsdecode(
            runner.call(
                "rev-parse", "--path-format=absolute", "--git-common-dir"
            ).stdout.strip()
        )
    )
    try:
        scan_root = root_path.relative_to(top).as_posix() or "."
    except ValueError as error:
        raise RepositoryIdentityError(
            "identity.root-outside-worktree",
            "scan root is outside the detected Git worktree",
        ) from error

    commit_id, branch = _read_head_anchor(runner)

    status = runner.call(
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=all",
        "--ignored=matching",
        "--ignore-submodules=none",
        "--no-renames",
        "--no-ahead-behind",
        "--",
        ".",
    ).stdout
    if _read_head_anchor(runner) != (commit_id, branch):
        raise RepositoryIdentityError(
            "identity.repository-changed",
            "Git commit or branch changed while repository identity was captured",
        )
    changed = _classify_status(
        status,
        top=top,
        root=root_path,
        inventory=inventory,
        policy=policy,
        excluded_paths=excluded_paths,
    )
    dirty = any(changed.values())
    effective_mode: Literal["head", "worktree"] = "worktree" if git_mode == "auto" else git_mode
    if effective_mode == "head" and commit_id is None:
        raise RepositoryIdentityError(
            "identity.head-unborn",
            "head mode requires an existing commit",
        )
    if effective_mode == "head" and dirty:
        raise RepositoryIdentityError(
            "identity.head-dirty",
            "head mode requires a clean set of eligible repository inputs",
        )

    repository_id = canonical_record_id(
        "repo",
        {
            "kind": "git",
            "commonDir": _normalized_absolute(common_dir),
            "scanRoot": unicodedata.normalize("NFC", scan_root),
        },
    )
    return RepositoryIdentity(
        repository_id,
        root_path,
        effective_mode,
        commit_id,
        branch,
        dirty,
        changed,
    )


class _GitRunner:
    def __init__(self, root: Path):
        self._root = root

    def call(self, *arguments: str, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[bytes]:
        try:
            return run_git_bounded(
                self._root,
                arguments,
                allowed_returncodes=allowed,
            )
        except GitReadError as error:
            code = {
                "timeout": "identity.git-timeout",
                "output-cap": "identity.git-output-cap",
                "unavailable": "identity.git-unavailable",
                "return-code": "identity.git-read-failed",
            }[error.reason]
            raise RepositoryIdentityError(
                code, f"bounded Git metadata read failed for built-in {arguments[0]}"
            ) from error


def _read_head_anchor(runner: _GitRunner) -> tuple[str | None, str | None]:
    commit_result = runner.call(
        "rev-parse",
        "--quiet",
        "--verify",
        "--end-of-options",
        "HEAD^{commit}",
        allowed=(0, 1, 128),
    )
    if commit_result.returncode == 0:
        commit_id = os.fsdecode(commit_result.stdout.strip())
    elif commit_result.returncode == 1:
        commit_id = None
    else:
        raise RepositoryIdentityError(
            "identity.head-read-failed", "Git HEAD could not be read reliably"
        )
    branch_result = runner.call(
        "symbolic-ref", "--quiet", "--short", "HEAD", allowed=(0, 1, 128)
    )
    if branch_result.returncode == 0:
        branch = os.fsdecode(branch_result.stdout.strip())
    elif branch_result.returncode == 1:
        branch = None
    else:
        raise RepositoryIdentityError(
            "identity.branch-read-failed", "Git branch could not be read reliably"
        )
    return commit_id, branch


def _classify_status(
    raw: bytes,
    *,
    top: Path,
    root: Path,
    inventory: InventoryResult,
    policy: ScanPolicy,
    excluded_paths: Sequence[str],
) -> dict[str, list[str]]:
    tracked: set[str] = set()
    untracked: set[str] = set()
    ignored_included: set[str] = {
        entry.path for entry in inventory.entries if entry.ignored
    }
    inventory_by_path = {entry.path: entry for entry in inventory.entries}
    normalized_excluded = tuple(
        _normalize_configured_path(value, allow_pattern=False) for value in excluded_paths
    )
    exclude_spec = GitIgnoreSpec.from_lines(
        (_normalize_exclude(value) for value in policy.exclude), backend="simple"
    )
    try:
        scan_prefix = root.relative_to(top).as_posix()
    except ValueError as error:
        raise RepositoryIdentityError(
            "identity.root-outside-worktree", "scan root is outside worktree"
        ) from error

    records = raw.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record or record.startswith(b"# "):
            continue
        marker = record[:1]
        raw_path: bytes | None = None
        if marker == b"1":
            fields = record.split(b" ", 8)
            if len(fields) == 9:
                raw_path = fields[8]
        elif marker == b"2":
            fields = record.split(b" ", 9)
            if len(fields) == 10:
                raw_path = fields[9]
            index += 1  # original path is the following NUL field
        elif marker == b"u":
            fields = record.split(b" ", 10)
            if len(fields) == 11:
                raw_path = fields[10]
        elif marker in {b"?", b"!"} and record[1:2] == b" ":
            raw_path = record[2:]
        if raw_path is None:
            raise RepositoryIdentityError(
                "identity.status-parse", "Git status returned an unsupported record"
            )
        try:
            path = _scan_relative(os.fsdecode(raw_path), scan_prefix)
        except UnicodeError as error:
            raise RepositoryIdentityError(
                "identity.status-path", "Git status path could not be decoded"
            ) from error
        if path is None:
            continue
        if marker in {b"1", b"2", b"u"}:
            if _eligible_tracked_path(path, normalized_excluded, exclude_spec):
                tracked.add(path)
        elif marker == b"?":
            if path in inventory_by_path:
                untracked.add(path)
        elif marker == b"!":
            status_path = path.rstrip("/")
            matched_entries = [
                entry
                for entry in inventory.entries
                if entry.path == status_path or entry.path.startswith(status_path + "/")
            ]
            for entry in matched_entries:
                if entry.ignored:
                    ignored_included.add(entry.path)
                else:
                    # Git's ignored stream also contains external/global ignore
                    # matches. P0 does not use those controls, so an eligible
                    # inventory path remains an ordinary untracked input.
                    untracked.add(entry.path)
    return {
        "tracked": sorted(tracked),
        "untracked": sorted(untracked),
        "ignoredIncluded": sorted(ignored_included),
    }


def _eligible_tracked_path(
    path: str,
    excluded: tuple[str, ...],
    exclude_spec: GitIgnoreSpec,
) -> bool:
    if _final_exclusion(path, False, excluded, exclude_spec) is not None:
        return False
    return _generated_or_security(path, False) is None


def _scan_relative(path: str, scan_prefix: str) -> str | None:
    canonical = unicodedata.normalize("NFC", path.replace("\\", "/"))
    if scan_prefix in {"", "."}:
        return canonical
    prefix = unicodedata.normalize("NFC", scan_prefix).rstrip("/") + "/"
    return canonical[len(prefix):] if canonical.startswith(prefix) else None


def _no_git_identity(root: Path, project_id: str) -> RepositoryIdentity:
    repository_id = canonical_record_id(
        "repo",
        {"kind": "no-git", "projectId": project_id, "root": _normalized_absolute(root)},
    )
    return RepositoryIdentity(
        repository_id,
        root,
        "no-git",
        None,
        None,
        False,
        {"tracked": [], "untracked": [], "ignoredIncluded": []},
    )


def _normalized_absolute(path: Path) -> str:
    absolute = os.path.abspath(path)
    normalized = os.path.normcase(absolute) if os.name == "nt" else absolute
    return unicodedata.normalize("NFC", normalized.replace("\\", "/"))
