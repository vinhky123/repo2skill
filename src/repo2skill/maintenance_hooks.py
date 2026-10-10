"""Conservative repository-local Git hook installation and observation."""
from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
from typing import Callable, Sequence

from .git_read import GitReadError, run_git_bounded
from .ledger import LedgerStore
from .maintenance_git import (
    GitReadBudget, MaintenanceGitError, bootstrap_commit, compare_commits,
    enqueue_event, parse_hook_event,
)
from .maintenance_models import MaintenancePolicy
from .maintenance_store import MaintenanceStore


HOOK_NAMES = ("post-commit", "post-merge", "post-checkout", "post-rewrite")
_MARKER = b"# repo2skill-managed-hook-v1\n"


@dataclass(frozen=True, slots=True)
class HookInstallResult:
    installed: bool
    manualIntegrationRequired: bool
    hooks: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True, slots=True)
class HookProcessResult:
    status: str
    jobId: str | None = None
    message: str = ""


def _git(root: Path, args: Sequence[str], *, timeout: float = 2.0) -> bytes:
    result = run_git_bounded(root, args, timeout_seconds=timeout, max_output_bytes=4096)
    return result.stdout


def _local_hooks_directory(root: Path) -> tuple[Path | None, str | None]:
    root = Path(root).absolute()
    git_marker = root / ".git"
    if not root.is_dir() or not git_marker.is_dir() or _is_redirected(git_marker):
        return None, "repository is linked, bare, or does not have an ordinary local .git directory"
    try:
        top = Path(os.fsdecode(_git(root, ("rev-parse", "--show-toplevel")).strip())).resolve()
        git_dir = Path(os.fsdecode(_git(root, ("rev-parse", "--absolute-git-dir")).strip())).resolve()
        common_dir_text = _git(root, ("rev-parse", "--git-common-dir")).strip()
        common_dir = Path(os.fsdecode(common_dir_text))
        if not common_dir.is_absolute():
            common_dir = root / common_dir
        common_dir = common_dir.resolve()
        configured = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "core.hooksPath"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, GitReadError, ValueError):
        return None, "could not safely inspect the repository Git configuration"
    if top != root.resolve() or git_dir != (root / ".git").resolve() or common_dir != git_dir:
        return None, "linked worktrees and shared Git directories require manual hook integration"
    if configured.returncode == 0:
        return None, "core.hooksPath is configured; use --manual-hooks or integrate observation manually"
    if configured.returncode != 1:
        return None, "could not safely inspect core.hooksPath; use manual hook integration"
    hooks = git_dir / "hooks"
    try:
        if _is_redirected(hooks) or not hooks.is_dir():
            return None, "the repository-local hooks directory is missing or redirected"
    except OSError:
        return None, "the repository-local hooks directory cannot be inspected"
    return hooks, None


def _is_redirected(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except OSError:
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(path_stat, "st_file_attributes", 0) & reparse)


def _wrapper_bytes(hook_name: str, python: Path | None = None) -> bytes:
    if hook_name not in HOOK_NAMES:
        raise ValueError("unsupported maintenance hook")
    python = python or Path(sys.executable)
    executable = shlex.quote(python.as_posix())
    if os.name == "nt":
        executable = '"$(cygpath -u ' + shlex.quote(str(python).replace("\\", "/")) + ')"'
    script = (
        "#!/bin/sh\n"
        + _MARKER.decode("ascii")
        + "# This wrapper observes committed Git state and always returns success.\n"
        + f"exec {executable} -I -m repo2skill.maintenance_hooks {shlex.quote(hook_name)} \"$@\"\n"
    )
    return script.encode("utf-8")


def _write_absent(path: Path, payload: bytes) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".repo2skill-hook-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o755)
        # A hard link publishes the complete inode atomically and fails if a
        # concurrent process created the destination after preflight.
        os.link(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    temporary.unlink()


def _set_enabled(store: LedgerStore, enabled: bool) -> None:
    maintenance = MaintenanceStore(store.root, store)
    with store.mutation():
        state = maintenance.read_state()
        policy = state.policy
        if not enabled and policy.approval == "auto":
            policy = replace(policy, approval="ask", grantRevision=policy.grantRevision + 1)
        maintenance.replace_state(replace(state, enabled=enabled, policy=policy))


def install_hooks(root: Path, store: LedgerStore) -> HookInstallResult:
    """Install wrappers only when every local hook destination is absent/owned."""
    root = Path(root).absolute()
    if store.root != root:
        return HookInstallResult(False, True, message="store and repository roots differ")
    if store.state_dir != root / ".repo2skill":
        return HookInstallResult(False, True, message="custom state directories require manual hook integration")
    hooks, refusal = _local_hooks_directory(root)
    if refusal or hooks is None:
        return HookInstallResult(False, True, message=refusal or "manual hook integration is required")
    expected = {name: _wrapper_bytes(name) for name in HOOK_NAMES}
    for name, payload in expected.items():
        path = hooks / name
        if path.exists():
            try:
                if path.is_symlink() or not path.is_file():
                    return HookInstallResult(False, True, message=f"existing {name} hook is redirected or not a regular file")
                current = path.read_bytes()
            except OSError:
                return HookInstallResult(False, True, message=f"cannot inspect existing {name} hook")
            if current != payload:
                return HookInstallResult(False, True, message=f"existing {name} hook is not owned by repo2skill")
    created: list[Path] = []
    try:
        for name, payload in expected.items():
            path = hooks / name
            if not path.exists():
                _write_absent(path, payload)
                created.append(path)
        _set_enabled(store, True)
    except (OSError, ValueError, RuntimeError) as error:
        for path in reversed(created):
            try:
                if path.read_bytes() == expected[path.name]:
                    path.unlink()
            except OSError:
                pass
        return HookInstallResult(False, True, message=f"hook installation could not be completed: {type(error).__name__}")
    return HookInstallResult(True, False, HOOK_NAMES, "repository-local maintenance hooks installed")


def remove_owned_hooks(root: Path, store: LedgerStore) -> HookInstallResult:
    """Disable observation and unlink only wrappers whose bytes still match."""
    root = Path(root).absolute()
    if store.root != root:
        return HookInstallResult(False, True, message="store and repository roots differ")
    hooks, refusal = _local_hooks_directory(root)
    if hooks is None:
        _set_enabled(store, False)
        return HookInstallResult(False, True, message=refusal or "manual hook integration is required")
    removed: list[str] = []
    foreign: list[str] = []
    for name in HOOK_NAMES:
        path = hooks / name
        if not path.exists():
            continue
        try:
            if not path.is_symlink() and path.is_file() and path.read_bytes() == _wrapper_bytes(name):
                path.unlink()
                removed.append(name)
            else:
                foreign.append(name)
        except OSError:
            foreign.append(name)
    _set_enabled(store, False)
    message = "owned maintenance hooks removed"
    if foreign:
        message += "; manual integration remains for: " + ", ".join(foreign)
    return HookInstallResult(True, bool(foreign), tuple(removed), message)


def process_hook(
    root: Path, hook_name: str, stdin: bytes, argv: Sequence[str], *, store: LedgerStore,
    output: Callable[[str], object] | None = None,
) -> HookProcessResult:
    """Observe one completed event; never invokes an agent or blocks Git."""
    emit = output or (lambda message: sys.stdout.write(message + "\n"))
    try:
        maintenance = MaintenanceStore(store.root, store)
        state = maintenance.read_state()
        if not state.enabled:
            message = "maintenance is disabled; run repo2skill maintain setup or catch-up manually"
            emit(message)
            return HookProcessResult("disabled", message=message)
        budget = GitReadBudget()
        event = parse_hook_event(hook_name, stdin, argv, root=root, budget=budget)
        baseline = state.appliedBaseline
        try:
            impact = bootstrap_commit(root, event.targetCommit, budget=budget) if baseline is None else compare_commits(
                root, baseline, event.targetCommit, budget=budget,
            )
            job = enqueue_event(store, event, impact, budget=budget)
        except MaintenanceGitError as error:
            enqueue_event(store, event, None, error.code, budget=budget)
            message = f"analysis queued for bounded catch-up ({error.code}); run maintain catch-up, then maintain status"
            emit(message)
            return HookProcessResult("deferred", message=message)
        if job is None:
            message = "maintenance event recorded; no new relevant job was queued"
            emit(message)
            return HookProcessResult("observed", message=message)
        affected = ", ".join(job.targetKinds) or "repository"
        changed = ", ".join(job.changedPaths[:5])
        if len(job.changedPaths) > 5:
            changed += f", and {len(job.changedPaths) - 5} more"
        message = (
            f"maintenance job {job.jobId} queued for {affected} ({changed or 'committed source'}); "
            f"baseline {job.baselineCommit or 'uninitialized'} -> target {job.targetCommit}; "
            f"run maintain approve {job.jobId}"
        )
        emit(message)
        return HookProcessResult("pending", job.jobId, message)
    except Exception as error:
        # Git hooks must not turn a successful user Git operation into failure.
        message = f"repo2skill maintenance observation unavailable ({type(error).__name__}); run maintain catch-up and maintain status"
        try:
            emit(message)
        except Exception:
            pass
        return HookProcessResult("diagnostic", message=message)


def launch_worker(root: Path, *, python: Path | None = None) -> subprocess.Popen:
    """Start the isolated installed module hidden on Windows."""
    interpreter = python or Path(sys.executable)
    command = [str(interpreter), "-I", "-m", "repo2skill.maintenance_worker", "--root", str(root)]
    options: dict[str, object] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return subprocess.Popen(command, **options)


def _entrypoint() -> int:
    hook_name = sys.argv[1] if len(sys.argv) > 1 else ""
    root = Path.cwd().absolute()
    store = LedgerStore(root, root / ".repo2skill")
    # Git hooks run with the repository root as cwd and supply their native argv.
    result = process_hook(root, hook_name, sys.stdin.buffer.read(), sys.argv[2:], store=store)
    return 0 if result.status in {"pending", "observed", "deferred", "disabled", "diagnostic"} else 0


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
