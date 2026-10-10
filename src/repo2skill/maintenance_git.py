"""Committed-tree maintenance impact analysis and bounded snapshot reads."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import re
import tempfile
import time
from types import MappingProxyType
import unicodedata

from pathspec import GitIgnoreSpec

from .git_read import GitReadError, run_git_bounded
from .ids import canonical_json_bytes, canonical_record_id
from .ledger import LedgerStore
from .maintenance_models import (
    GitEvent, MaintenanceJob, MaintenanceLifecycle, MaintenanceState,
)
from .maintenance_store import MaintenanceStore
from .models import RunLedger


MAX_DIFF_BYTES = 8 * 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
MAX_CONTEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_CONTEXT_TOTAL_BYTES = 16 * 1024 * 1024
MAX_CONTEXT_FILES = 256
DEFAULT_BUDGET_SECONDS = 10.0
DEFAULT_BUDGET_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_HOOK_STDIN_BYTES = 1024 * 1024
MAX_POST_REWRITE_RECORDS = 4096
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_HASH = lambda raw: "sha256:" + hashlib.sha256(raw).hexdigest()


class MaintenanceGitError(ValueError):
    """Stable, actionable failure while reading a committed Git tree."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(slots=True)
class GitReadBudget:
    """One shared deadline/output ceiling for a multi-command Git operation."""

    timeout_seconds: float = DEFAULT_BUDGET_SECONDS
    max_output_bytes: int = DEFAULT_BUDGET_OUTPUT_BYTES
    _deadline: float = 0.0
    _bytes_read: int = 0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.max_output_bytes < 1:
            raise ValueError("Git read budget bounds must be positive")
        self._deadline = time.monotonic() + self.timeout_seconds

    def limits(self, command_limit: int) -> tuple[float, int]:
        seconds = self._deadline - time.monotonic()
        remaining_bytes = self.max_output_bytes - self._bytes_read
        if seconds <= 0:
            raise MaintenanceGitError("maintenance.git-timeout", "shared committed-tree read deadline expired")
        if remaining_bytes < 1:
            raise MaintenanceGitError("maintenance.git-output-cap", "shared committed-tree read output cap exceeded")
        return min(seconds, 20.0), min(command_limit, remaining_bytes)

    def account(self, output_bytes: int) -> None:
        self._bytes_read += output_bytes
        if self._bytes_read > self.max_output_bytes:
            raise MaintenanceGitError("maintenance.git-output-cap", "shared committed-tree read output cap exceeded")


@dataclass(frozen=True, slots=True)
class CommitImpact:
    baselineCommit: str | None
    targetCommit: str
    paths: tuple[str, ...]
    renames: tuple[tuple[str, str], ...]
    diffHash: str
    scopes: tuple[str, ...]
    requiredFullScan: bool
    diagnostics: tuple[str, ...]
    changeStatuses: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    blobId: str
    rawHash: str
    size: int


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    commit: str
    files: tuple[SnapshotFile, ...]
    omittedPaths: tuple[str, ...]
    contextHash: str
    policyRevision: str = ""
    extractorRevisions: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "extractorRevisions", MappingProxyType(dict(self.extractorRevisions or {})))


def parse_hook_event(
    hook_name: str, stdin: bytes, argv: Sequence[str], *, root: Path,
    budget: GitReadBudget | None = None, defer_reason_code: str | None = None,
) -> GitEvent:
    """Parse a supported post-event hook using only its arguments and committed refs."""
    supported = {"post-commit", "post-merge", "post-checkout", "post-rewrite"}
    if hook_name not in supported:
        raise MaintenanceGitError("maintenance.hook-unsupported", "unsupported Git hook name")
    root = Path(root).absolute()
    current = _git(root, ("rev-parse", "--verify", "HEAD^{commit}"), 256, budget).strip().decode("ascii", "strict")
    def deferred(reason_code: str) -> GitEvent:
        data = {
            "hook": hook_name, "argv": list(argv), "stdinHash": _HASH(stdin[:MAX_HOOK_STDIN_BYTES]),
            "targetCommit": current, "deferredReason": reason_code,
        }
        cursor = "sha256:" + hashlib.sha256(canonical_json_bytes(data)).hexdigest()
        return GitEvent(canonical_record_id("git-event", data), hook_name, cursor, current, reason_code)

    if defer_reason_code:
        return deferred(defer_reason_code)
    if len(stdin) > MAX_HOOK_STDIN_BYTES:
        return deferred("maintenance.hook-input-cap")
    if hook_name == "post-commit":
        if argv or stdin:
            raise MaintenanceGitError("maintenance.hook-arguments-invalid", "post-commit does not accept arguments or stdin")
        target = current
    elif hook_name == "post-merge":
        if stdin or len(argv) > 1 or (argv and argv[0] not in {"0", "1"}):
            raise MaintenanceGitError("maintenance.hook-arguments-invalid", "post-merge accepts only its optional squash flag")
        target = current
    elif hook_name == "post-checkout":
        if stdin or len(argv) != 3:
            raise MaintenanceGitError("maintenance.hook-arguments-invalid", "post-checkout requires old, new, and branch arguments")
        zero_id = "0" * len(argv[0])
        if argv[0] != zero_id or len(argv[0]) not in {40, 64}:
            _resolve_commit(root, argv[0], budget)
        target = _resolve_commit(root, argv[1], budget)
        if argv[2] not in {"0", "1"} or target != current:
            raise MaintenanceGitError("maintenance.hook-target-mismatch", "post-checkout target does not match committed HEAD")
    else:
        if len(argv) != 1 or argv[0] not in {"rebase", "am"} or not stdin:
            raise MaintenanceGitError("maintenance.hook-arguments-invalid", "post-rewrite requires the rebase/am command and stdin")
        record_count = stdin.count(b"\n") + (1 if stdin and not stdin.endswith(b"\n") else 0)
        if record_count > MAX_POST_REWRITE_RECORDS:
            return deferred("maintenance.hook-record-cap")
        records = [line.split() for line in stdin.splitlines() if line.strip()]
        if not records or any(len(record) != 2 for record in records):
            raise MaintenanceGitError("maintenance.hook-arguments-invalid", "post-rewrite stdin is malformed")
        for record in records:
            old, new = (item.decode("ascii", "strict") for item in record)
            _resolve_commit(root, old, budget)
            _resolve_commit(root, new, budget)
        target = _resolve_commit(root, records[-1][1].decode("ascii", "strict"), budget)
        if target != current:
            raise MaintenanceGitError("maintenance.hook-target-mismatch", "post-rewrite target does not match committed HEAD")
    cursor_data = {"hook": hook_name, "argv": list(argv), "stdinHash": _HASH(stdin), "targetCommit": target}
    cursor = "sha256:" + hashlib.sha256(canonical_json_bytes(cursor_data)).hexdigest()
    event_id = canonical_record_id("git-event", cursor_data)
    return GitEvent(event_id, hook_name, cursor, target)


def _git(root: Path, args: Sequence[str], limit: int, budget: GitReadBudget | None = None) -> bytes:
    timeout, output_limit = budget.limits(limit) if budget is not None else (20.0, limit)
    try:
        result = run_git_bounded(
            root,
            ("-c", "core.pager=cat", "-c", "diff.external=", "--no-pager", *args),
            timeout_seconds=timeout,
            max_output_bytes=output_limit,
        )
        if budget is not None:
            budget.account(len(result.stdout) + len(result.stderr))
        return result.stdout
    except GitReadError as error:
        code = {
            "output-cap": "maintenance.git-output-cap",
            "timeout": "maintenance.git-timeout",
            "unavailable": "maintenance.git-unavailable",
            "return-code": "maintenance.git-object-missing",
        }[error.reason]
        detail = "requested Git object is missing; rebuild or catch up from an available baseline" if error.reason == "return-code" else f"bounded committed-tree read failed ({error.reason})"
        raise MaintenanceGitError(code, detail) from error


def _resolve_commit(root: Path, commit: str, budget: GitReadBudget | None = None) -> str:
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        raise MaintenanceGitError("maintenance.git-invalid-commit", "commit must be a full lowercase SHA-1 or SHA-256 ID")
    resolved = _git(root, ("rev-parse", "--verify", f"{commit}^{{commit}}"), 256, budget).strip()
    try:
        result = resolved.decode("ascii")
    except UnicodeDecodeError as error:
        raise MaintenanceGitError("maintenance.git-invalid-commit", "Git returned a malformed commit ID") from error
    if result != commit:
        raise MaintenanceGitError("maintenance.git-object-missing", "requested commit object is missing or did not resolve exactly")
    return result


def _deferred_state(
    state: MaintenanceState, event: GitEvent, reason_code: str,
) -> MaintenanceState:
    deferred_event = GitEvent(
        event.eventId, event.hookName, event.cursor, event.targetCommit,
        reason_code or event.reasonCode or "maintenance.impact-unavailable",
    )
    deferred = tuple(item for item in state.deferredEvents if item.eventId != event.eventId) + (deferred_event,)
    return replace(state, observedEventCursor=event.cursor, deferredEvents=deferred)


def _valid_path(raw: bytes) -> str:
    try:
        path = os.fsdecode(raw)
    except UnicodeError as error:
        raise MaintenanceGitError("maintenance.git-path-invalid", "committed path cannot be decoded on this platform") from error
    if not path or path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        raise MaintenanceGitError("maintenance.git-path-invalid", "committed tree contains an unsupported repository path")
    if unicodedata.normalize("NFC", path) != path:
        raise MaintenanceGitError("maintenance.git-path-invalid", "committed path is not canonical NFC")
    return path


def _validate_materializable_path(path: str) -> None:
    if os.name != "nt":
        return
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for part in path.split("/"):
        stem = part.split(".", 1)[0].upper()
        if (
            any(char in part for char in '<>:"|?*')
            or part.endswith((" ", "."))
            or stem in reserved
        ):
            raise MaintenanceGitError(
                "maintenance.git-path-unsupported",
                "committed path cannot be safely materialized on Windows",
            )


def _parse_diff(raw: bytes) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    records = raw.split(b"\0")
    if records and records[-1] == b"":
        records.pop()
    paths: set[str] = set()
    renames: list[tuple[str, str]] = []
    statuses: list[tuple[str, str]] = []
    index = 0
    while index < len(records):
        try:
            status = records[index].decode("ascii")
        except UnicodeDecodeError as error:
            raise MaintenanceGitError("maintenance.git-diff-malformed", "Git emitted malformed NUL-framed diff status") from error
        index += 1
        if not status or status[0] not in "ACDMRTUXB":
            raise MaintenanceGitError("maintenance.git-diff-malformed", "Git emitted an unsupported diff status")
        count = 2 if status[0] in "RC" else 1
        if index + count > len(records):
            raise MaintenanceGitError("maintenance.git-diff-malformed", "Git emitted a partial NUL-framed diff record")
        changed = tuple(_valid_path(item) for item in records[index:index + count])
        index += count
        paths.update(changed)
        if status[0] in "RC":
            renames.append((changed[0], changed[1]))
        statuses.extend((status[0], path) for path in changed)
    return tuple(sorted(paths)), tuple(sorted(renames)), tuple(statuses)


def _classify(paths: Sequence[str], statuses: Sequence[tuple[str, str]]) -> tuple[tuple[str, ...], bool, tuple[str, ...]]:
    scopes: set[str] = set()
    full = False
    diagnostics: list[str] = []
    control_names = {".gitignore", ".gitattributes", ".gitmodules", ".repo2skill.yaml", "repo2skill.yaml"}
    for status, path in statuses:
        lower = path.lower()
        name = path.rsplit("/", 1)[-1]
        if path == ".repo2skill" or path.startswith(".repo2skill/"):
            continue
        if status == "D" or name.lower() in control_names or path.startswith(".github/workflows/"):
            full = True
            diagnostics.append("maintenance.impact-conservative")
        scope = None
        if lower.endswith((".py", ".pyi")) or name in {"pyproject.toml", "requirements.txt", "Pipfile", "poetry.lock", "uv.lock", "setup.py", "setup.cfg"}:
            scope = "python"
        elif lower.endswith((".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")) or name in {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}:
            scope = "javascript"
        elif lower.endswith(".go") or name in {"go.mod", "go.sum"}:
            scope = "go"
        elif lower.endswith(".rs") or name in {"Cargo.toml", "Cargo.lock"}:
            scope = "rust"
        elif name in {"Dockerfile", "docker-compose.yml", "compose.yaml", "Makefile", "Taskfile.yml"}:
            scope = "generic"
        elif lower.endswith(".md") or path.startswith("docs/"):
            scope = "docs"
        elif lower.endswith((".yaml", ".yml", ".toml", ".json")):
            scope = "generic"
        else:
            # Include unknown files conservatively. Excluded state/output handled above.
            full = True
            diagnostics.append("maintenance.impact-unknown-path")
            scope = "full"
        scopes.add(scope)
    return tuple(sorted(scopes)), full, tuple(sorted(set(diagnostics)))


def compare_commits(
    root: Path, baseline: str, target: str, *, budget: GitReadBudget | None = None,
) -> CommitImpact:
    root = Path(root).absolute()
    budget = budget or GitReadBudget()
    base = _resolve_commit(root, baseline, budget)
    head = _resolve_commit(root, target, budget)
    raw = _git(root, ("diff-tree", "--no-commit-id", "--name-status", "-r", "-M", "-z", "--no-ext-diff", "--no-textconv", base, head), MAX_DIFF_BYTES, budget)
    raw_paths, raw_renames, statuses = _parse_diff(raw)
    paths = tuple(path for path in raw_paths if not _excluded_path(path))
    renames = tuple(pair for pair in raw_renames if not any(_excluded_path(path) for path in pair))
    statuses = tuple((status, path) for status, path in statuses if not _excluded_path(path))
    scopes, full, diagnostics = _classify(paths, statuses)
    # Hash exact NUL-framed Git status/path records, independent of worktree state.
    return CommitImpact(base, head, paths, renames, _HASH(raw), scopes, full, diagnostics, statuses)


def _excluded_path(path: str) -> bool:
    return path == ".repo2skill" or path.startswith(".repo2skill/")


def bootstrap_commit(
    root: Path, target: str, *, budget: GitReadBudget | None = None,
) -> CommitImpact:
    root = Path(root).absolute()
    budget = budget or GitReadBudget()
    head = _resolve_commit(root, target, budget)
    raw = _git(root, ("ls-tree", "-r", "-z", "--full-tree", head), MAX_TREE_BYTES, budget)
    paths: list[str] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        try:
            metadata, raw_path = item.split(b"\t", 1)
            mode, kind, _oid = metadata.split(b" ")
        except ValueError as error:
            raise MaintenanceGitError("maintenance.git-tree-malformed", "Git emitted a malformed recursive tree entry") from error
        if kind != b"blob" or mode not in {b"100644", b"100755"}:
            raise MaintenanceGitError("maintenance.git-mode-unsupported", "committed tree contains an unsupported object or mode")
        path = _valid_path(raw_path)
        if not _excluded_path(path):
            paths.append(path)
    scopes, _full, diagnostics = _classify(paths, tuple(("A", path) for path in paths))
    statuses = tuple(("A", path) for path in sorted(paths))
    return CommitImpact(None, head, tuple(sorted(paths)), (), _HASH(raw), scopes, True, diagnostics, statuses)


def read_commit_blob(
    root: Path, commit: str, path: str, max_bytes: int, *,
    budget: GitReadBudget | None = None,
) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a nonnegative integer")
    budget = budget or GitReadBudget()
    head = _resolve_commit(Path(root).absolute(), commit, budget)
    canonical = _valid_path(os.fsencode(path))
    listing = _git(Path(root).absolute(), ("ls-tree", "-z", head, "--", f":(literal){canonical}"), 4096, budget)
    records = [entry for entry in listing.split(b"\0") if entry]
    exact = []
    for record in records:
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ")
        except ValueError as error:
            raise MaintenanceGitError("maintenance.git-tree-malformed", "Git emitted a malformed tree entry") from error
        if _valid_path(raw_path) == canonical:
            exact.append((mode, kind, oid))
    if len(exact) == 1 and exact[0][1] == b"blob" and exact[0][0] == b"120000":
        raise MaintenanceGitError("maintenance.git-mode-unsupported", "unsupported symlink tree mode cannot be materialized as a regular file")
    if len(exact) != 1 or exact[0][1] != b"blob" or exact[0][0] not in {b"100644", b"100755"}:
        raise MaintenanceGitError("maintenance.git-object-missing", "requested committed file is missing or has an unsupported mode")
    blob_id = exact[0][2].decode("ascii")
    try:
        size = int(_git(Path(root).absolute(), ("cat-file", "-s", blob_id), 256, budget).strip())
    except ValueError as error:
        raise MaintenanceGitError("maintenance.git-object-malformed", "Git returned a malformed blob size") from error
    if size > max_bytes:
        raise MaintenanceGitError("maintenance.git-blob-cap", "committed blob exceeds the configured byte cap")
    payload = _git(Path(root).absolute(), ("cat-file", "blob", blob_id), max_bytes + 1, budget)
    return payload


def materialize_commit(
    root: Path, commit: str, paths: Sequence[str], destination: Path, *,
    policy_revision: str = "", extractor_revisions: Mapping[str, str] | None = None,
    budget: GitReadBudget | None = None,
) -> SnapshotManifest:
    root = Path(root).absolute()
    budget = budget or GitReadBudget()
    head = _resolve_commit(root, commit, budget)
    if len(paths) > MAX_CONTEXT_FILES:
        raise MaintenanceGitError("maintenance.context-file-cap", "committed context exceeds file-count cap")
    destination = Path(destination).absolute()
    destination.mkdir(parents=True, exist_ok=True)
    files: list[SnapshotFile] = []
    omitted: list[str] = []
    total = 0
    ordered_paths = sorted(set(paths))
    if len(ordered_paths) > MAX_CONTEXT_FILES:
        omitted.extend(ordered_paths[MAX_CONTEXT_FILES:])
        ordered_paths = ordered_paths[:MAX_CONTEXT_FILES]
    for path in ordered_paths:
        canonical = _valid_path(os.fsencode(path))
        _validate_materializable_path(canonical)
        try:
            data = read_commit_blob(root, head, canonical, MAX_CONTEXT_FILE_BYTES, budget=budget)
        except MaintenanceGitError as error:
            if error.code in {"maintenance.git-object-missing", "maintenance.git-blob-cap"}:
                omitted.append(canonical)
                continue
            raise
        if total + len(data) > MAX_CONTEXT_TOTAL_BYTES:
            omitted.append(canonical)
            continue
        # Resolve and retain the raw Git blob ID separately from its content hash.
        raw = _git(root, ("ls-tree", "-z", head, "--", f":(literal){canonical}"), 4096, budget)
        entry = next(item for item in raw.split(b"\0") if item)
        blob_id = entry.split(b"\t", 1)[0].split(b" ")[2].decode("ascii")
        target = destination.joinpath(*canonical.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            raise MaintenanceGitError("maintenance.context-destination-conflict", "materialization destination already contains a selected path")
        target.write_bytes(data)
        total += len(data)
        files.append(SnapshotFile(canonical, blob_id, _HASH(data), len(data)))
    projection = {
        "commit": head,
        "files": [{"path": item.path, "blobId": item.blobId, "rawHash": item.rawHash, "size": item.size} for item in files],
        "omittedPaths": sorted(omitted),
        "policyRevision": policy_revision,
        "extractorRevisions": dict(sorted((extractor_revisions or {}).items())),
    }
    digest = _HASH(canonical_json_bytes(projection))
    return SnapshotManifest(
        head, tuple(files), tuple(sorted(omitted)), digest, policy_revision,
        dict(sorted((extractor_revisions or {}).items())),
    )


def _run_for_store(store: LedgerStore) -> RunLedger | None:
    state = store.read_state()
    if state is None or state.currentRunId is None:
        return None
    return store.read_run(state.currentRunId)


def _expected_output_hashes(store: LedgerStore, *, target_kinds: Sequence[str]) -> dict[str, str]:
    project_state = store.read_state()
    project = store.read_project()
    if project_state is None or project is None:
        raise ValueError("initialized project documents are required to bind generated outputs")
    if project_state.activeExportId is None and project_state.activeManifestSetId is None:
        return {}
    if project_state.activeExportId is None or project_state.activeManifestSetId is None:
        raise ValueError("active output manifest binding is incomplete")
    manifest_set = store.read_manifest_set(
        f"manifests/{project_state.activeExportId}/{project_state.activeManifestSetId}.json"
    )
    if (
        manifest_set is None
        or manifest_set.exportId != project_state.activeExportId
        or manifest_set.manifestSetId != project_state.activeManifestSetId
    ):
        raise ValueError("active output manifest set is missing or mismatched")
    kinds = {target.targetId: target.kind for target in project.targets}
    result: dict[str, str] = {}
    for target_id, reference in manifest_set.targets.items():
        if kinds.get(target_id) not in target_kinds:
            continue
        manifest = store.read_target_manifest(reference.path)
        if (
            manifest is None
            or manifest.manifestId != reference.manifestId
            or manifest.exportId != reference.exportId
            or manifest.targetId != target_id
        ):
            raise ValueError("active target output manifest is missing or mismatched")
        for file in manifest.files:
            previous = result.setdefault(file.path, file.exactHash)
            if previous != file.exactHash:
                raise ValueError("active target manifests disagree about an output path")
    return dict(sorted(result.items()))


def _project_output_paths(
    store: LedgerStore, project, project_state,
) -> tuple[set[str], set[str]]:
    """Resolve outputs from configured targets and the active ownership manifests."""
    exact = {target.livePath for target in project.targets if target.kind == "agents-md"}
    prefixes = {target.livePath.rstrip("/") for target in project.targets if target.kind == "skill"}
    if project_state.activeExportId is None and project_state.activeManifestSetId is None:
        return exact, prefixes
    if project_state.activeExportId is None or project_state.activeManifestSetId is None:
        raise ValueError("active output manifest binding is incomplete")
    manifest_set = store.read_manifest_set(
        f"manifests/{project_state.activeExportId}/{project_state.activeManifestSetId}.json"
    )
    if (
        manifest_set is None
        or manifest_set.exportId != project_state.activeExportId
        or manifest_set.manifestSetId != project_state.activeManifestSetId
    ):
        raise ValueError("active output manifest set is missing or mismatched")
    for target_id, reference in manifest_set.targets.items():
        manifest = store.read_target_manifest(reference.path)
        if (
            manifest is None
            or manifest.manifestId != reference.manifestId
            or manifest.exportId != reference.exportId
            or manifest.targetId != target_id
        ):
            raise ValueError("active target output manifest is missing or mismatched")
        exact.update(file.path for file in manifest.files)
    return exact, prefixes


def _is_project_output(path: str, exact: set[str], prefixes: set[str]) -> bool:
    return path in exact or any(
        path == prefix or path.startswith(prefix + "/") for prefix in prefixes
    )


def _filter_project_outputs(
    impact: CommitImpact, exact: set[str], prefixes: set[str],
) -> CommitImpact:
    statuses = impact.changeStatuses or tuple(("M", path) for path in impact.paths)
    filtered_statuses = tuple(
        (status, path) for status, path in statuses
        if not _is_project_output(path, exact, prefixes)
    )
    paths = tuple(sorted({path for _status, path in filtered_statuses}))
    renames = tuple(
        pair for pair in impact.renames
        if all(path in paths for path in pair)
    )
    scopes, full, diagnostics = _classify(paths, filtered_statuses)
    return replace(
        impact,
        paths=paths,
        renames=renames,
        scopes=scopes,
        requiredFullScan=impact.baselineCommit is None or full,
        diagnostics=diagnostics,
        changeStatuses=filtered_statuses,
    )


def _affected_scopes(impact: CommitImpact, run: RunLedger | None) -> tuple[str, ...]:
    if impact.requiredFullScan or run is None:
        return impact.scopes or ("full",)
    paths = set(impact.paths)
    scopes: set[str] = set()
    for read in run.reads:
        selector = read.selector
        matches = False
        if selector.type == "file":
            matches = selector.value in paths
        elif selector.type == "glob":
            matches = any(GitIgnoreSpec.from_lines([selector.value], backend="simple").match_file(path) for path in paths)
            # Matching a newly added path is a freshness change even when the prior match set was empty.
            matches = matches or any(path not in read.matchPaths and GitIgnoreSpec.from_lines([selector.value], backend="simple").match_file(path) for path in paths)
        elif selector.type == "negative":
            name = selector.value.split(":", 1)[-1]
            matches = any(path.rsplit("/", 1)[-1] == name for path in paths)
        if matches:
            scopes.add(selector.scope if selector.scope != "." else read.extractorId)
    return tuple(sorted(scopes))


def enqueue_event(
    store: LedgerStore, event: GitEvent, impact: CommitImpact | None,
    reason_code: str | None = None, *, budget: GitReadBudget | None = None,
) -> MaintenanceJob | None:
    """Atomically persist the committed context, immutable job, and event cursor."""
    budget = budget or GitReadBudget()
    maint = MaintenanceStore(store.root, store)
    with store.mutation():
        state = maint.read_state()
        if state.observedEventCursor == event.cursor:
            return None
        if impact is None:
            maint.replace_state(_deferred_state(
                state, event, reason_code or event.reasonCode or "maintenance.impact-unavailable",
            ))
            return None
        if impact.targetCommit != event.targetCommit:
            reason_code = "maintenance.event-impact-mismatch"
        if impact.baselineCommit != state.appliedBaseline:
            reason_code = "maintenance.baseline-mismatch"
        if reason_code:
            maint.replace_state(_deferred_state(state, event, reason_code))
            return None
        project = store.read_project()
        project_state = store.read_state()
        if project is None or project_state is None:
            raise ValueError("initialized P0 project is required to enqueue committed impact")
        run = _run_for_store(store)
        exact_outputs, output_prefixes = _project_output_paths(store, project, project_state)
        impact = _filter_project_outputs(impact, exact_outputs, output_prefixes)
        if not impact.paths and impact.baselineCommit is not None:
            maint.replace_state(replace(state, observedEventCursor=event.cursor))
            return None
        scopes = _affected_scopes(impact, run)
        if not scopes:
            # A previously unobserved input or selector gap cannot prove that
            # changed committed source is irrelevant. Preserve it for review.
            scopes = impact.scopes or ("full",)
        extractor_revisions = dict(run.snapshot.extractorVersions) if run is not None else {}
        from .ids import effective_policy_revision
        policy_revision = effective_policy_revision(
            project.scanPolicy, project.claimPolicy, project.targets, generation_options={},
        )
        if impact.requiredFullScan or "full" in scopes:
            selected_paths = set(bootstrap_commit(store.root, event.targetCommit, budget=budget).paths)
        else:
            selected_paths = set(impact.paths)
            if run is not None:
                # Preserve the complete previous dependency support so retained
                # claims can be rebound to the target commit's saved context.
                for read in run.reads:
                    if read.selector.type == "file":
                        selected_paths.add(read.selector.value)
                    selected_paths.update(read.matchPaths)
                    if read.selector.type == "negative":
                        absent_path = read.selector.value.split(":", 1)[-1]
                        if absent_path in impact.paths:
                            selected_paths.add(absent_path)
        selected_paths = {
            path for path in selected_paths
            if not _is_project_output(path, exact_outputs, output_prefixes)
        }
        try:
            with tempfile.TemporaryDirectory(prefix="repo2skill-context-") as temp_dir:
                temporary = Path(temp_dir)
                manifest = materialize_commit(
                    store.root, event.targetCommit, tuple(sorted(selected_paths)), temporary,
                    policy_revision=policy_revision, extractor_revisions=extractor_revisions,
                    budget=budget,
                )
                manifest_bytes = canonical_json_bytes({
                    "schemaVersion": "1.0", "manifest": manifest,
                }) + b"\n"
                source_hash = manifest.contextHash
                manifest_id = canonical_record_id("source-context", {"contextHash": source_hash})
                store.write_immutable_bytes(f"maintenance/contexts/{manifest_id}.json", manifest_bytes)
        except MaintenanceGitError as error:
            maint.replace_state(_deferred_state(state, event, error.code))
            return None
        changed_paths = impact.paths
        job_id = canonical_record_id("maintenance-job", {
            "baselineCommit": state.appliedBaseline, "targetCommit": event.targetCommit,
            "eventIds": [event.eventId], "changedPaths": changed_paths,
            "impactScopes": scopes, "diffHash": impact.diffHash,
            "sourceContextHash": source_hash,
        })
        job = MaintenanceJob(
            job_id, state.appliedBaseline, event.targetCommit, (event.eventId,),
            changed_paths, scopes, impact.diffHash, source_hash, policy_revision,
            state.policy.targetKinds, project_state.activeManifestSetId,
            _expected_output_hashes(store, target_kinds=state.policy.targetKinds),
        )
        jobs = dict(state.jobs)
        jobs[job_id] = MaintenanceLifecycle(
            "pending", None, None, "pending", None, None, None,
            queuedGrantRevision=state.policy.grantRevision,
        )
        next_state = replace(
            state,
            observedEventCursor=event.cursor,
            deferredEvents=tuple(item for item in state.deferredEvents if item.eventId != event.eventId),
            jobs=jobs,
        )
        maint.publish_job(job, next_state)
        return job
