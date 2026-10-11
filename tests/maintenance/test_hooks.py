from __future__ import annotations

import dataclasses
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore
from repo2skill.maintenance_git import MaintenanceGitError
from repo2skill.cli import main
from repo2skill.maintenance_hooks import (
    HOOK_NAMES, install_hooks, process_hook, remove_owned_hooks,
)
from repo2skill.maintenance_models import MaintenancePolicy, MaintenanceState
from repo2skill.maintenance_store import MaintenanceStore
from repo2skill.models import ProjectDocument, ProjectState


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE).stdout


def _repo(root: Path, state_dir_name: str = ".repo2skill") -> tuple[Path, LedgerStore]:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    state_dir = root / state_dir_name
    state_dir.mkdir(parents=True)
    project = ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture", state_dir_name,
        "worktree", default_scan_policy(), default_claim_policy(), default_targets(root),
    )
    state = ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    return root, LedgerStore(root, state_dir)


def _commit(root: Path, name: str = "src.py") -> str:
    (root / name).write_text("source = 1\n", encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-qm", "fixture")
    return _git(root, "rev-parse", "HEAD").decode().strip()


def _hook_dir(root: Path) -> Path:
    return root / ".git" / "hooks"


def test_setup_refuses_foreign_hook_without_modifying_bytes(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "repo")
    hook = _hook_dir(root) / "post-commit"
    before = b"#!/bin/sh\necho user-owned\n"
    hook.write_bytes(before)
    git_config = root / ".git" / "config"
    config_before = git_config.read_bytes()

    result = install_hooks(root, store)

    assert result.installed is False
    assert result.manualIntegrationRequired is True
    assert hook.read_bytes() == before
    assert git_config.read_bytes() == config_before


def test_setup_refuses_configured_or_shared_hooks_directory(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "configured")
    _git(root, "config", "--local", "core.hooksPath", "custom-hooks")
    config = (root / ".git" / "config").read_bytes()
    result = install_hooks(root, store)
    assert result.installed is False
    assert result.manualIntegrationRequired is True
    assert (root / ".git" / "config").read_bytes() == config
    assert not (root / "custom-hooks").exists()

    linked, linked_store = _repo(tmp_path / "linked-source")
    _commit(linked)
    worktree = tmp_path / "linked-worktree"
    _git(linked, "worktree", "add", "-q", str(worktree), "HEAD")
    linked_config = (linked / ".git" / "config").read_bytes()
    result = install_hooks(worktree, LedgerStore(worktree, worktree / ".repo2skill"))
    assert result.installed is False
    assert result.manualIntegrationRequired is True
    assert (linked / ".git" / "config").read_bytes() == linked_config


def test_main_checkout_refuses_hooks_shared_with_linked_worktrees(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "main")
    _commit(root)
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", str(linked), "HEAD")
    config_before = (root / ".git" / "config").read_bytes()
    hooks_before = {item.name: item.read_bytes() for item in _hook_dir(root).iterdir() if item.is_file()}

    result = install_hooks(root, store)

    assert result.installed is False
    assert result.manualIntegrationRequired is True
    assert "linked" in result.message or "shared" in result.message
    assert (root / ".git" / "config").read_bytes() == config_before
    assert {item.name: item.read_bytes() for item in _hook_dir(root).iterdir() if item.is_file()} == hooks_before


def test_disable_removes_owned_hooks_after_linked_worktree_is_added(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "main")
    _commit(root)
    installed = install_hooks(root, store)
    assert installed.installed is True
    linked = tmp_path / "linked"
    _git(root, "worktree", "add", "-q", str(linked), "HEAD")

    result = remove_owned_hooks(root, store)

    assert result.installed is True
    assert result.manualIntegrationRequired is False
    assert set(result.hooks) == set(HOOK_NAMES)
    assert all(not (_hook_dir(root) / name).exists() for name in HOOK_NAMES)
    assert MaintenanceStore(root, store).read_state().enabled is False


def test_setup_installs_only_absent_local_hooks(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "repo")
    result = install_hooks(root, store)

    assert result.installed is True
    assert set(result.hooks) == set(HOOK_NAMES)
    for name in HOOK_NAMES:
        wrapper = _hook_dir(root) / name
        assert wrapper.is_file()
        assert b"-I -m repo2skill.maintenance_hooks" in wrapper.read_bytes()
        if os.name != "nt":
            assert wrapper.stat().st_mode & 0o111
    assert MaintenanceStore(root, store).read_state().enabled is True
    assert b'"$@"' in (_hook_dir(root) / "post-checkout").read_bytes()


def test_posix_wrapper_publication_has_executable_mode(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.maintenance_hooks as hooks

    path = tmp_path / "hook"
    chmod_calls = []
    def record_chmod(self, mode):
        chmod_calls.append((self, mode))
    monkeypatch.setattr(Path, "chmod", record_chmod)
    monkeypatch.setattr(hooks.os, "name", "posix")
    hooks._write_absent(path, hooks._wrapper_bytes("post-commit"))

    assert len(chmod_calls) == 1
    assert chmod_calls[0][1] == 0o755


def test_absent_publication_does_not_replace_concurrent_hook(tmp_path: Path) -> None:
    import repo2skill.maintenance_hooks as hooks

    path = tmp_path / "hook"
    foreign = b"foreign hook\n"
    path.write_bytes(foreign)

    with pytest.raises(FileExistsError):
        hooks._write_absent(path, hooks._wrapper_bytes("post-commit"))

    assert path.read_bytes() == foreign


def test_disable_removes_only_owned_hook(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "repo")
    install_hooks(root, store)
    owned = _hook_dir(root) / "post-commit"
    owned.write_bytes(owned.read_bytes() + b"# user edit\n")
    edited = owned.read_bytes()
    removed = _hook_dir(root) / "post-merge"

    result = remove_owned_hooks(root, store)

    assert result.installed is True
    assert owned.read_bytes() == edited
    assert not removed.exists()
    assert result.manualIntegrationRequired is True
    assert MaintenanceStore(root, store).read_state().enabled is False


def test_disable_state_failure_preserves_owned_hooks(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.maintenance_hooks as hooks

    root, store = _repo(tmp_path / "repo")
    install_hooks(root, store)
    before = {name: (_hook_dir(root) / name).read_bytes() for name in HOOK_NAMES}
    monkeypatch.setattr(hooks, "_set_enabled", lambda *a, **kw: (_ for _ in ()).throw(OSError("ledger unavailable")))

    result = hooks.remove_owned_hooks(root, store)

    assert result.installed is False
    assert result.manualIntegrationRequired is True
    assert {name: (_hook_dir(root) / name).read_bytes() for name in HOOK_NAMES} == before


def test_manual_setup_enables_without_touching_hook_files(tmp_path: Path, capsys) -> None:
    root, store = _repo(tmp_path / "repo")
    before = {path.name: path.read_bytes() for path in _hook_dir(root).iterdir() if path.is_file()}

    code = main(["--root", str(root), "maintain", "setup", "--manual-hooks"])

    assert code == 0
    assert MaintenanceStore(root, store).read_state().enabled is True
    after = {path.name: path.read_bytes() for path in _hook_dir(root).iterdir() if path.is_file()}
    assert after == before


def test_hook_prints_job_targets_and_approve_command(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "repo")
    baseline = _commit(root, "src.py")
    with store.mutation():
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline=baseline, enabled=True))
    (root / "src.py").write_text("source = 2\n", encoding="utf-8")
    _git(root, "add", "src.py")
    _git(root, "commit", "-qm", "change")
    target = _git(root, "rev-parse", "HEAD").decode().strip()
    output = []

    result = process_hook(root, "post-commit", b"", (), store=store, output=output.append)

    lifecycle = MaintenanceStore(root, store).read_state().jobs[result.jobId]
    assert lifecycle.notificationState == "pending"
    assert result.jobId in output[0]
    assert "src.py" in output[0]
    assert baseline in output[0] and target in output[0]
    assert f"maintain approve {result.jobId}" in output[0]


def test_no_baseline_hook_defers_until_explicit_rebuild(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "repo")
    _commit(root)
    with store.mutation():
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), enabled=True))
    output = []

    result = process_hook(root, "post-commit", b"", (), store=store, output=output.append)

    state = MaintenanceStore(root, store).read_state()
    assert result.status == "deferred"
    assert state.deferredEvents[-1].reasonCode == "maintenance.rebuild-required"
    assert not state.jobs
    assert "maintain catch-up --rebuild" in output[0]


@pytest.mark.parametrize("hook_name", ["post-merge", "post-rewrite", "post-checkout"])
def test_completed_git_hook_is_recovered_by_manual_catch_up(tmp_path: Path, capsys, hook_name: str) -> None:
    root, store = _repo(tmp_path / "repo")
    baseline = _commit(root, "src.py")
    with store.mutation():
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline=baseline, enabled=True))
    base_branch = _git(root, "branch", "--show-current").decode().strip()

    if hook_name == "post-merge":
        _git(root, "checkout", "-b", "feature")
        _commit(root, "feature.py")
        feature_tip = _git(root, "rev-parse", "HEAD").decode().strip()
        _git(root, "checkout", base_branch)
        (root / "base.py").write_text("base = 1\n", encoding="utf-8")
        _git(root, "add", "base.py")
        _git(root, "commit", "-qm", "base-side")
        _git(root, "merge", "--no-ff", "feature", "-m", "merge-feature")
        hook_stdin, hook_argv = b"", ("0",)
    elif hook_name == "post-checkout":
        _git(root, "checkout", "-b", "feature")
        _commit(root, "feature.py")
        feature_tip = _git(root, "rev-parse", "HEAD").decode().strip()
        _git(root, "checkout", baseline)
        _git(root, "checkout", "feature")
        hook_stdin, hook_argv = b"", (baseline, feature_tip, "1")
    else:
        _commit(root, "old.py")
        old_tip = _git(root, "rev-parse", "HEAD").decode().strip()
        _git(root, "reset", "--hard", baseline)
        _commit(root, "rewritten.py")
        new_tip = _git(root, "rev-parse", "HEAD").decode().strip()
        hook_stdin, hook_argv = f"{old_tip} {new_tip}\n".encode(), ("rebase",)

    hook_output = []
    result = process_hook(root, hook_name, hook_stdin, hook_argv, store=store, output=hook_output.append)
    assert result.status == "pending", hook_output
    prior_job = MaintenanceStore(root, store).read_job(result.jobId)
    assert prior_job is not None
    (root / "recovered.py").write_text("recovered = True\n", encoding="utf-8")
    _git(root, "add", "recovered.py")
    _git(root, "commit", "-qm", "recovered without hook")
    latest = _git(root, "rev-parse", "HEAD").decode().strip()

    code = main(["--root", str(root), "--format", "json", "maintain", "catch-up"])
    catch_up = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert code == 3
    assert catch_up["result"]["targetCommit"] == latest
    state = MaintenanceStore(root, store).read_state()
    successor_id = next(job_id for job_id, lifecycle in state.jobs.items() if lifecycle.status == "pending")
    successor = MaintenanceStore(root, store).read_job(successor_id)
    assert successor is not None and successor.targetCommit == latest
    assert prior_job.eventIds[0] in successor.eventIds
    assert state.jobs[result.jobId].status == "stale"


def test_hook_budget_failure_prints_catch_up_not_completed_diff(tmp_path: Path, monkeypatch) -> None:
    root, store = _repo(tmp_path / "repo")
    baseline = _commit(root)
    with store.mutation():
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline=baseline, enabled=True))
    _commit(root, "other.py")
    output = []
    monkeypatch.setattr("repo2skill.maintenance_hooks.compare_commits", lambda *a, **kw: (_ for _ in ()).throw(MaintenanceGitError("maintenance.git-timeout", "deadline")))

    result = process_hook(root, "post-commit", b"", (), store=store, output=output.append)

    assert result.jobId is None
    assert "analysis queued" in output[0]
    assert "maintain catch-up" in output[0]
    assert "completed diff" not in output[0]
    assert MaintenanceStore(root, store).read_state().deferredEvents[-1].reasonCode == "maintenance.git-timeout"


def test_late_context_cap_failure_prints_catch_up_notice(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.maintenance_git as git_api

    root, store = _repo(tmp_path / "repo")
    baseline = _commit(root)
    with store.mutation():
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline=baseline, enabled=True))
    _commit(root, "other.py")
    monkeypatch.setattr(
        git_api, "materialize_commit",
        lambda *a, **kw: (_ for _ in ()).throw(MaintenanceGitError("maintenance.git-output-cap", "cap")),
    )
    output = []

    result = process_hook(root, "post-commit", b"", (), store=store, output=output.append)

    assert result.status == "deferred"
    assert "maintain catch-up" in output[0] and "maintain status" in output[0]
    assert MaintenanceStore(root, store).read_state().deferredEvents[-1].reasonCode == "maintenance.git-output-cap"


def test_hook_stdin_read_is_byte_and_time_bounded(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.maintenance_hooks as hooks

    root, store = _repo(tmp_path / "repo")
    _commit(root)
    with store.mutation():
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), enabled=True))
    release = threading.Event()
    observed_sizes = []

    class BlockingInput:
        def read(self, size=-1):
            observed_sizes.append(size)
            release.wait(0.5)
            return b""

    started = time.monotonic()
    payload, reason = hooks._read_hook_stdin(BlockingInput(), timeout_seconds=0.01)
    elapsed = time.monotonic() - started
    release.set()

    assert payload == b""
    assert reason == "maintenance.hook-input-timeout"
    assert observed_sizes and observed_sizes[0] > 0
    assert observed_sizes[0] <= hooks.MAX_HOOK_STDIN_BYTES + 1
    assert elapsed < 0.25
    output = []
    result = process_hook(
        root, "post-rewrite", payload, ("rebase",), store=store, output=output.append,
        defer_reason_code=reason,
    )
    assert result.status == "deferred"
    assert "maintain catch-up" in output[0]
    assert MaintenanceStore(root, store).read_state().deferredEvents[-1].reasonCode == reason


def test_hook_stdin_byte_cap_is_deferred_with_catch_up_notice(tmp_path: Path) -> None:
    import repo2skill.maintenance_hooks as hooks

    root, store = _repo(tmp_path / "repo")
    target = _commit(root)
    with store.mutation():
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), enabled=True))
    output = []

    result = process_hook(
        root, "post-rewrite", b"x" * (hooks.MAX_HOOK_STDIN_BYTES + 1), ("rebase",),
        store=store, output=output.append,
    )

    state = MaintenanceStore(root, store).read_state()
    assert result.status == "deferred"
    assert state.deferredEvents[-1].targetCommit == target
    assert state.deferredEvents[-1].reasonCode == "maintenance.hook-input-cap"
    assert "maintain catch-up" in output[0] and "maintain status" in output[0]


def test_post_rewrite_record_count_is_bounded(tmp_path: Path) -> None:
    from repo2skill.maintenance_git import parse_hook_event

    root, _store = _repo(tmp_path / "repo")
    current = _commit(root)
    records = (b"a" * 40 + b" " + b"b" * 40 + b"\n") * 4097

    event = parse_hook_event("post-rewrite", records, ("rebase",), root=root)

    assert event.reasonCode == "maintenance.hook-record-cap"
    assert event.targetCommit == current


def test_post_checkout_wrapper_returns_success_after_diagnostic(tmp_path: Path, monkeypatch) -> None:
    import types
    import repo2skill.maintenance_hooks as hooks

    root, _store = _repo(tmp_path / "repo")
    _commit(root)
    monkeypatch.chdir(root)
    monkeypatch.setattr(hooks.sys, "argv", ["maintenance_hooks.py", "post-checkout", "bad-args"])
    monkeypatch.setattr(hooks.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO()))
    monkeypatch.setattr(hooks, "process_hook", lambda *a, **kw: hooks.HookProcessResult("diagnostic", message="bounded failure"))

    assert hooks._entrypoint() == 0


def test_wrapper_launch_failure_is_diagnostic_but_returns_success(tmp_path: Path) -> None:
    from repo2skill.maintenance_hooks import _wrapper_bytes

    wrapper = tmp_path / "post-commit"
    wrapper.write_bytes(_wrapper_bytes("post-commit", tmp_path / "missing python.exe"))
    if os.name == "nt":
        shell = Path(r"C:\Program Files\Git\bin\bash.exe")
        command = [str(shell), str(wrapper)]
    else:
        shell = Path("/bin/sh")
        command = [str(shell), str(wrapper)]
    if not shell.is_file():
        pytest.skip(f"portable shell is unavailable at {shell}")

    result = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=5, check=False,
    )

    assert result.returncode == 0
    assert b"maintain catch-up" in result.stderr + result.stdout
    assert b"maintain status" in result.stderr + result.stdout


def test_dangling_foreign_hook_is_reported_during_disable(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.maintenance_hooks as hooks

    root, store = _repo(tmp_path / "repo")
    dangling = _hook_dir(root) / "post-commit"
    original_exists = Path.exists
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "exists", lambda self: False if self == dangling else original_exists(self))
    monkeypatch.setattr(Path, "is_symlink", lambda self: True if self == dangling else original_is_symlink(self))

    result = hooks.remove_owned_hooks(root, store)

    assert result.manualIntegrationRequired is True
    assert "post-commit" in result.message


def test_wrapper_launcher_avoids_repo_shadow_module_and_windows_console(tmp_path: Path, monkeypatch) -> None:
    from repo2skill.maintenance_hooks import _wrapper_bytes, launch_worker

    wrapper = _wrapper_bytes("post-commit", Path("C:/installed/python.exe"))
    assert b"-I -m repo2skill.maintenance_hooks" in wrapper
    assert b"PYTHONPATH" not in wrapper
    shadow = tmp_path / "repo2skill.py"
    shadow.write_text("raise RuntimeError('shadow imported')", encoding="utf-8")
    captured = {}
    monkeypatch.setattr("repo2skill.maintenance_hooks.subprocess.Popen", lambda *a, **kw: captured.update(args=a[0], **kw))
    monkeypatch.setattr("repo2skill.maintenance_hooks.os.name", "nt")
    launch_worker(Path("C:/repo"), python=Path("C:/installed/python.exe"))
    assert captured["creationflags"] != 0
    assert captured["creationflags"] & getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0x08000000)
    assert Path(captured["args"][-1]) == Path("C:/repo/.repo2skill")


def test_hidden_worker_launch_preserves_custom_confined_state_dir(tmp_path: Path, monkeypatch) -> None:
    from repo2skill.maintenance_hooks import launch_worker

    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".local-state" / "nested"
    captured = {}
    monkeypatch.setattr("repo2skill.maintenance_hooks.subprocess.Popen",
                        lambda *args, **kwargs: captured.update(command=args[0], **kwargs))

    launch_worker(root, state_dir=state_dir, python=Path("C:/installed/python.exe"))

    assert captured["command"][-2:] == ["--state-dir", str(state_dir.absolute())]
    with pytest.raises(ValueError, match="inside project root"):
        launch_worker(root, state_dir=tmp_path / "outside", python=Path("C:/installed/python.exe"))


def test_hidden_entrypoint_opens_exact_custom_state_dir(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.maintenance_worker as worker
    root, custom_store = _repo(tmp_path / "repo", ".local-state/ledger")
    captured = []
    monkeypatch.setattr(sys, "argv", ["maintenance_worker", "--root", str(root),
                                       "--state-dir", str(custom_store.state_dir)])
    monkeypatch.setattr(worker, "run_worker", lambda store, **kwargs:
                        captured.append((store.state_dir, kwargs)) or worker.WorkerResult("idle"))

    assert worker._entrypoint() == 0
    assert captured == [(custom_store.state_dir, {"foreground": False, "auto_drain": True})]


def test_isolated_hook_module_ignores_repo_local_shadow_module(tmp_path: Path) -> None:
    root, _store = _repo(tmp_path / "repo")
    trap = root / "repo2skill.py"
    trap.write_text("raise RuntimeError('SHADOW MODULE EXECUTED')\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-I", "-m", "repo2skill.maintenance_hooks", "post-commit"],
        cwd=root, input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=False,
    )

    assert b"SHADOW MODULE EXECUTED" not in result.stdout + result.stderr
    assert b"disabled" in result.stdout


def test_windows_git_bash_runs_installed_wrapper_for_real_commit(tmp_path: Path) -> None:
    root, store = _repo(tmp_path / "repo")
    baseline = _commit(root, "src.py")
    with store.mutation():
        maint = MaintenanceStore(root, store)
        maint.replace_state(dataclasses.replace(maint.read_state(), appliedBaseline=baseline))
    trap = root / "repo2skill.py"
    trap.write_text("raise RuntimeError('SHADOW MODULE EXECUTED')\n", encoding="utf-8")
    install = install_hooks(root, store)
    assert install.installed is True

    (root / "src.py").write_text("source = 2\n", encoding="utf-8")
    _git(root, "add", "src.py")
    result = subprocess.run(
        ["git", "-C", str(root), "commit", "-m", "event through installed hook"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output.decode("utf-8", "replace")
    assert b"SHADOW MODULE EXECUTED" not in output
    assert b"maintain approve maintenance-job_" in output
    state = MaintenanceStore(root, store).read_state()
    assert len(state.jobs) == 1
    lifecycle = next(iter(state.jobs.values()))
    assert lifecycle.status == "pending"
    assert lifecycle.notificationState == "pending"
    assert lifecycle.activeAttemptId is None
