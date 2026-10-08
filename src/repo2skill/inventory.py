from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat
import subprocess
from typing import Literal
import unicodedata

from pathspec import GitIgnoreSpec

from .config import _is_link_or_reparse, _validated_root
from .ids import canonical_json_bytes
from .models import (
    CoverageEntry,
    Diagnostic,
    InputSelector,
    InventoryEntry,
    InventoryResult,
    RepositoryPath,
    ScanPolicy,
    Sha256,
)


INVENTORY_VERSION = "1.0"
_GIT_TIMEOUT_SECONDS = 10.0
_GIT_OUTPUT_LIMIT = 16 * 1024 * 1024
_GENERATED_DIRECTORIES = frozenset(
    {
        "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
        ".mypy_cache", ".ruff_cache", ".tox", ".nox", "dist", "build",
        "target", "vendor", ".next", ".nuxt", "coverage",
    }
)
_GENERATED_SUFFIXES = (".pyc", ".pyo", ".min.js", ".min.css", ".map")
_SECURITY_DIRECTORIES = frozenset({".ssh", ".aws", ".gnupg"})
_SECURITY_FILES = frozenset(
    {".git-credentials", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519"}
)
_SECURITY_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_INTRINSIC_DIRECTORIES = frozenset({".git", ".repo2skill", ".superpowers"})


@dataclass(frozen=True, slots=True)
class IgnoreControlObservation:
    selector: InputSelector
    outcome: Literal[
        "present", "missing", "matched", "no-match", "capped", "decode-error", "io-error"
    ]
    contentHash: Sha256 | None
    matchPaths: tuple[RepositoryPath, ...]
    matchSetHash: Sha256 | None
    openedFileCount: int
    decodedByteCount: int


class InventoryError(ValueError):
    """A fatal inventory invariant failed before a safe result could be built."""

    def __init__(self, diagnostic: Diagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic.message)


class _CanonicalPathIndex:
    """Reject names that collapse under NFC or case-insensitive comparison."""

    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, str]] = {}

    def add(self, path: str, *, original: str | None = None) -> None:
        original_value = path if original is None else original
        normalized = _nfc_path(path)
        key = normalized.casefold()
        previous = self._seen.get(key)
        current = (normalized, original_value)
        if previous is not None and previous != current:
            diagnostic = _diagnostic(
                "inventory.path-collision",
                "error",
                f"repository path collision after canonical normalization: {previous[1]!r} and {original_value!r}",
                normalized,
                {"first": previous[1], "second": original_value},
            )
            raise InventoryError(diagnostic)
        self._seen[key] = current


@dataclass(frozen=True, slots=True)
class _IgnoreSource:
    directory: str
    spec: GitIgnoreSpec


@dataclass(slots=True)
class _ControlBudget:
    opened: int = 0
    decoded: int = 0
    incomplete: Literal["capped", "io-error"] | None = None


def collect_inventory(
    root: Path,
    policy: ScanPolicy,
    *,
    excluded_paths: Sequence[RepositoryPath] = (),
    control_sink: Callable[[IgnoreControlObservation], None] | None = None,
) -> InventoryResult:
    """Discover repository metadata without following redirects or reading source bytes."""
    root_path = _validated_root(root)
    root_stat = root_path.lstat()
    normalized_excluded = tuple(_normalize_configured_path(value, allow_pattern=False) for value in excluded_paths)
    includes = tuple(_normalize_configured_path(value, allow_pattern=True) for value in policy.include)
    exact_includes = frozenset(value for value in includes if not _has_glob_magic(value))
    exclude_lines = tuple(_normalize_exclude(value) for value in policy.exclude)
    exclude_spec = GitIgnoreSpec.from_lines(exclude_lines, backend="simple")
    tracked, gitlinks = _git_index_paths(root_path)

    entries: list[InventoryEntry] = []
    diagnostics: list[Diagnostic] = []
    coverage_counts: dict[tuple[str, str], int] = {}
    collision_index = _CanonicalPathIndex()
    controls: list[str] = []
    control_budget = _ControlBudget()
    traversal_incomplete = False

    def record_skip(status: str, reason: str) -> None:
        key = (status, reason)
        coverage_counts[key] = coverage_counts.get(key, 0) + 1

    def visit(directory: Path, relative_directory: str, sources: tuple[_IgnoreSource, ...], forced: bool) -> None:
        nonlocal traversal_incomplete
        try:
            with os.scandir(directory) as iterator:
                children = sorted(
                    list(iterator),
                    key=lambda item: unicodedata.normalize("NFC", item.name).casefold(),
                )
        except OSError as error:
            traversal_incomplete = True
            control_budget.incomplete = control_budget.incomplete or "io-error"
            diagnostics.append(
                _diagnostic(
                    "inventory.directory-io",
                    "error",
                    "directory could not be enumerated safely",
                    relative_directory or None,
                    {"errorType": type(error).__name__},
                )
            )
            record_skip("failed", "inventory.directory-io")
            return

        local_sources = sources
        control_entry = next((child for child in children if child.name == ".gitignore"), None)
        control_ok = True
        control_hash: str | None = None
        if control_entry is not None:
            control_path = _join(relative_directory, ".gitignore")
            controls.append(control_path)
            observation, decoded = _capture_ignore_control(
                control_entry,
                control_path,
                policy,
                control_budget,
            )
            control_hash = observation.contentHash
            if control_sink is not None:
                control_sink(observation)
            if observation.outcome == "present":
                assert decoded is not None
                local_sources = sources + (
                    _IgnoreSource(
                        relative_directory,
                        GitIgnoreSpec.from_lines(decoded.splitlines(), backend="simple"),
                    ),
                )
            else:
                control_ok = False
                reason = f"inventory.gitignore-control-{observation.outcome}"
                diagnostics.append(
                    _diagnostic(
                        reason,
                        "error",
                        "ignore control could not be captured completely",
                        control_path,
                        {"outcome": observation.outcome},
                    )
                )
                record_skip("capped" if observation.outcome == "capped" else "failed", reason)

        for child in children:
            original_name = child.name
            name = unicodedata.normalize("NFC", original_name)
            relative = _join(relative_directory, name)
            try:
                # DirEntry.stat() reports zero device/inode fields on some
                # supported Windows Python builds. os.stat() supplies the
                # stable metadata needed for boundary checks.
                child_stat = os.stat(child.path, follow_symlinks=False)
            except OSError as error:
                diagnostics.append(
                    _diagnostic(
                        "inventory.entry-io",
                        "warning",
                        "entry metadata could not be read",
                        relative,
                        {"errorType": type(error).__name__},
                    )
                )
                record_skip("failed", "inventory.entry-io")
                continue

            collision_index.add(relative, original=_join(relative_directory, original_name))
            if not control_ok and child is not control_entry:
                traversal_incomplete = True
                record_skip("failed", "inventory.gitignore-control-unavailable")
                continue

            if _is_link_or_reparse(Path(child.path), child_stat):
                reason = _final_exclusion(relative, False, normalized_excluded, exclude_spec)
                if reason is not None:
                    record_skip(*reason)
                    continue
                generated_reason = _generated_or_security(relative, False)
                if generated_reason is not None:
                    record_skip(*generated_reason)
                    continue
                target = _safe_link_target(root_path, Path(child.path))
                entries.append(
                    InventoryEntry(relative, "symlink", child_stat.st_size, None, False, False, target)
                )
                record_skip("skipped", "inventory.redirect-not-followed")
                continue

            is_directory = stat.S_ISDIR(child_stat.st_mode)
            reason = _final_exclusion(relative, is_directory, normalized_excluded, exclude_spec)
            if reason is not None:
                record_skip(*reason)
                continue

            generated_reason = _generated_or_security(relative, is_directory)
            if generated_reason is not None:
                record_skip(*generated_reason)
                continue

            if is_directory:
                if relative in gitlinks or _nested_repository(Path(child.path)):
                    diagnostics.append(
                        _diagnostic(
                            "inventory.repository-boundary",
                            "warning",
                            "nested repository or submodule was not traversed",
                            relative,
                            {},
                        )
                    )
                    record_skip("skipped", "inventory.boundary-excluded")
                    continue
                if child_stat.st_dev != root_stat.st_dev or _is_mount(Path(child.path)):
                    diagnostics.append(
                        _diagnostic(
                            "inventory.mount-boundary",
                            "warning",
                            "filesystem mount boundary was not traversed",
                            relative,
                            {},
                        )
                    )
                    record_skip("skipped", "inventory.boundary-excluded")
                    continue
                ignored = _is_ignored(relative, True, local_sources)
                tracked_chain = _is_ancestor_of_any(relative, tracked)
                exact_chain = _is_ancestor_of_any(relative, exact_includes)
                if ignored and not tracked_chain and not exact_chain:
                    record_skip("skipped", "inventory.gitignored")
                    continue
                if control_ok:
                    visit(Path(child.path), relative, local_sources, forced or ignored)
                else:
                    traversal_incomplete = True
                continue

            if not stat.S_ISREG(child_stat.st_mode):
                record_skip("skipped", "inventory.special-file")
                continue

            ignored = forced or _is_ignored(relative, False, local_sources)
            tracked_path = relative in tracked
            exact = relative in exact_includes
            is_control = child is control_entry
            if ignored and not tracked_path and not exact and not is_control:
                record_skip("skipped", "inventory.gitignored")
                continue
            if forced and not tracked_path and not exact and not is_control:
                record_skip("skipped", "inventory.gitignored")
                continue
            entries.append(
                InventoryEntry(
                    relative,
                    "file",
                    child_stat.st_size,
                    control_hash if control_entry is child and control_ok else None,
                    bool(ignored and (exact or is_control) and not tracked_path),
                    False,
                    None,
                )
            )

    visit(root_path, "", (), False)
    entries.sort(key=lambda entry: entry.path)
    if len(entries) > policy.maxInventoryEntries:
        omitted = len(entries) - policy.maxInventoryEntries
        entries = entries[: policy.maxInventoryEntries]
        traversal_incomplete = True
        control_budget.incomplete = "capped"
        diagnostics.append(
            _diagnostic(
                "inventory.entry-cap",
                "error",
                "inventory entry cap was reached",
                None,
                {"limit": policy.maxInventoryEntries, "omittedCount": omitted},
            )
        )
        record_skip("capped", "inventory.entry-cap")

    if traversal_incomplete and control_budget.incomplete is None:
        control_budget.incomplete = "io-error"
    control_paths = tuple(sorted(controls))
    if control_budget.incomplete is not None:
        aggregate_outcome: Literal["matched", "no-match", "capped", "io-error"] = control_budget.incomplete
        match_hash = None
    else:
        aggregate_outcome = "matched" if control_paths else "no-match"
        match_hash = _sha256(canonical_json_bytes(list(control_paths)))
    if control_sink is not None:
        control_sink(
            IgnoreControlObservation(
                InputSelector("glob", "**/.gitignore", ".", True),
                aggregate_outcome,
                None,
                control_paths,
                match_hash,
                0,
                0,
            )
        )

    coverage = tuple(
        CoverageEntry(
            1,
            InputSelector("negative", reason, ".", status in {"capped", "failed"}),
            "inventory",
            status,  # type: ignore[arg-type]
            reason,
            (reason,) if status in {"capped", "failed"} else (),
        )
        for (status, reason), _count in sorted(coverage_counts.items())
    )
    inventory_hash = _inventory_hash(entries)
    return InventoryResult(root_path, tuple(entries), inventory_hash, tuple(diagnostics), coverage)


def _capture_ignore_control(
    entry: os.DirEntry[str],
    relative: str,
    policy: ScanPolicy,
    budget: _ControlBudget,
) -> tuple[IgnoreControlObservation, str | None]:
    selector = InputSelector("file", relative, ".", True)
    if budget.opened >= policy.maxOpenedFiles:
        budget.incomplete = "capped"
        return IgnoreControlObservation(selector, "capped", None, (), None, 0, 0), None
    try:
        before = os.stat(entry.path, follow_symlinks=False)
        if _is_link_or_reparse(Path(entry.path), before) or not stat.S_ISREG(before.st_mode):
            budget.incomplete = budget.incomplete or "io-error"
            return IgnoreControlObservation(selector, "io-error", None, (), None, 0, 0), None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(entry.path, flags)
    except OSError:
        budget.incomplete = budget.incomplete or "io-error"
        return IgnoreControlObservation(selector, "io-error", None, (), None, 0, 0), None
    budget.opened += 1
    try:
        opened = os.fstat(descriptor)
        raw = _read_bounded(descriptor, policy.maxFileBytes)
        after = os.stat(entry.path, follow_symlinks=False)
    except OSError:
        budget.incomplete = budget.incomplete or "io-error"
        return IgnoreControlObservation(selector, "io-error", None, (), None, 1, 0), None
    finally:
        os.close(descriptor)
    if len(raw) > policy.maxFileBytes:
        budget.incomplete = "capped"
        return IgnoreControlObservation(selector, "capped", None, (), None, 1, 0), None
    if not _same_file(before, opened) or not _same_file(before, after) or len(raw) != before.st_size:
        budget.incomplete = budget.incomplete or "io-error"
        return IgnoreControlObservation(selector, "io-error", None, (), None, 1, 0), None
    decoded: str | None = None
    for encoding in policy.encodings:
        try:
            decoded = raw.decode(encoding)
            break
        except (LookupError, UnicodeDecodeError):
            continue
    if decoded is None:
        budget.incomplete = budget.incomplete or "io-error"
        return IgnoreControlObservation(selector, "decode-error", None, (), None, 1, 0), None
    if budget.decoded + len(raw) > policy.maxDecodedBytes:
        budget.incomplete = "capped"
        return IgnoreControlObservation(selector, "capped", None, (), None, 1, 0), None
    budget.decoded += len(raw)
    content_hash = _sha256(raw)
    return IgnoreControlObservation(selector, "present", content_hash, (), None, 1, len(raw)), decoded


def _read_bounded(descriptor: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(64 * 1024, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _same_file(first: os.stat_result, second: os.stat_result) -> bool:
    keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    return all(getattr(first, key, None) == getattr(second, key, None) for key in keys)


def _git_index_paths(root: Path) -> tuple[frozenset[str], frozenset[str]]:
    prefix = ["git", "-c", "core.fsmonitor=false", "--no-pager", "-C", str(root)]
    environment = os.environ.copy()
    environment.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    try:
        inside = subprocess.run(
            [*prefix, "rev-parse", "--is-inside-work-tree"],
            shell=False,
            check=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=environment,
        )
        if inside.returncode or inside.stdout.strip() != b"true":
            return frozenset(), frozenset()
        top = subprocess.run(
            [*prefix, "rev-parse", "--path-format=absolute", "--show-toplevel"],
            shell=False,
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=environment,
        ).stdout
        staged = subprocess.run(
            [*prefix, "ls-files", "--stage", "-z"],
            shell=False,
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=environment,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset(), frozenset()
    if len(top) > _GIT_OUTPUT_LIMIT or len(staged) > _GIT_OUTPUT_LIMIT:
        return frozenset(), frozenset()
    try:
        top_path = Path(os.fsdecode(top.strip()))
        scan_prefix = root.relative_to(top_path).as_posix()
    except (ValueError, UnicodeError):
        return frozenset(), frozenset()
    tracked: set[str] = set()
    gitlinks: set[str] = set()
    for record in staged.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode = metadata.split(b" ", 1)[0]
            path = _from_git_path(os.fsdecode(raw_path), scan_prefix)
        except (ValueError, UnicodeError):
            continue
        if path is None:
            continue
        tracked.add(path)
        if mode == b"160000":
            gitlinks.add(path)
    return frozenset(tracked), frozenset(gitlinks)


def _from_git_path(path: str, scan_prefix: str) -> str | None:
    canonical = _nfc_path(path)
    if scan_prefix in {"", "."}:
        return canonical
    prefix = _nfc_path(scan_prefix).rstrip("/") + "/"
    return canonical[len(prefix):] if canonical.startswith(prefix) else None


def _normalize_configured_path(value: str, *, allow_pattern: bool) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("configured path must be nonempty")
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    windows = PureWindowsPath(normalized)
    parts = PurePosixPath(normalized).parts
    if windows.drive or normalized.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"configured path must be repository-relative: {value}")
    if not allow_pattern and _has_glob_magic(normalized):
        raise ValueError(f"excluded path must be literal: {value}")
    return normalized.rstrip("/")


def _normalize_exclude(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if normalized.startswith("!"):
        raise ValueError("exclude patterns cannot use gitignore negation")
    if "\x00" in normalized or any(part == ".." for part in PurePosixPath(normalized).parts):
        raise ValueError(f"unsafe exclude pattern: {value}")
    return normalized


def _has_glob_magic(path: str) -> bool:
    return any(character in path for character in "*?[")


def _final_exclusion(
    path: str,
    is_directory: bool,
    excluded: tuple[str, ...],
    exclude_spec: GitIgnoreSpec,
) -> tuple[str, str] | None:
    parts = PurePosixPath(path).parts
    if any(part in _INTRINSIC_DIRECTORIES for part in parts):
        return "skipped", "inventory.boundary-excluded"
    if any(path == item or path.startswith(item + "/") for item in excluded):
        return "skipped", "inventory.boundary-excluded"
    candidate = path + "/" if is_directory else path
    if exclude_spec.check_file(candidate, separators=()).include is True:
        return "skipped", "inventory.explicit-excluded"
    return None


def _generated_or_security(path: str, is_directory: bool) -> tuple[str, str] | None:
    name = PurePosixPath(path).name
    parts = PurePosixPath(path).parts
    if any(part in _GENERATED_DIRECTORIES for part in parts):
        return "generated", "inventory.generated-excluded"
    if not is_directory and name.endswith(_GENERATED_SUFFIXES):
        return "generated", "inventory.generated-excluded"
    if any(part in _SECURITY_DIRECTORIES for part in parts):
        return "skipped", "inventory.security-excluded"
    if not is_directory:
        if name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"}):
            return "skipped", "inventory.security-excluded"
        if name in _SECURITY_FILES or name.endswith(_SECURITY_SUFFIXES):
            return "skipped", "inventory.security-excluded"
    return None


def _is_ignored(path: str, is_directory: bool, sources: tuple[_IgnoreSource, ...]) -> bool:
    ignored = False
    for source in sources:
        relative = path
        if source.directory:
            prefix = source.directory + "/"
            if not path.startswith(prefix):
                continue
            relative = path[len(prefix):]
        candidate = relative + "/" if is_directory else relative
        result = source.spec.check_file(candidate, separators=())
        if result.include is not None:
            ignored = bool(result.include)
    return ignored


def _is_ancestor_of_any(directory: str, paths: frozenset[str]) -> bool:
    prefix = directory.rstrip("/") + "/"
    return any(path.startswith(prefix) for path in paths)


def _nested_repository(directory: Path) -> bool:
    marker = directory / ".git"
    try:
        marker.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _is_mount(path: Path) -> bool:
    try:
        return os.path.ismount(path)
    except OSError:
        return True


def _safe_link_target(root: Path, link: Path) -> str | None:
    try:
        target = os.readlink(link)
    except OSError:
        return None
    target_path = Path(target)
    if target_path.is_absolute():
        return "<external-target>"
    lexical = Path(os.path.abspath(link.parent / target_path))
    if not lexical.is_relative_to(root):
        return "<external-target>"
    return unicodedata.normalize("NFC", target.replace("\\", "/"))


def _inventory_hash(entries: Sequence[InventoryEntry]) -> str:
    projection = [
        {
            "path": entry.path,
            "kind": entry.kind,
            "sizeBytes": entry.sizeBytes,
            "contentHash": entry.contentHash,
            "ignored": entry.ignored,
            "generated": entry.generated,
            "symlinkTarget": entry.symlinkTarget,
        }
        for entry in sorted(entries, key=lambda item: item.path)
    ]
    return _sha256(canonical_json_bytes(projection))


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _nfc_path(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\\", "/"))


def _join(parent: str, child: str) -> str:
    return f"{parent}/{child}" if parent else child


def _diagnostic(
    code: str,
    severity: Literal["info", "warning", "error"],
    message: str,
    path: str | None,
    details: dict[str, object],
) -> Diagnostic:
    return Diagnostic(1, code, severity, message, path, "inventory", details)  # type: ignore[arg-type]
