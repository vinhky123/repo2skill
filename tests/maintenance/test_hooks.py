from __future__ import annotations

import dataclasses
import io
import json
import os
from pathlib import Path
import subprocess
import sys

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


def _repo(root: Path) -> tuple[Path, LedgerStore]:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project = ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture", ".repo2skill",
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


def test_wrapper_launcher_avoids_repo_shadow_module_and_windows_console(tmp_path: Path, monkeypatch) -> None:
    from repo2skill.maintenance_hooks import _wrapper_bytes, launch_worker

    wrapper = _wrapper_bytes("post-commit", Path("C:/installed/python.exe"))
    assert b"-I -m repo2skill.maintenance_hooks" in wrapper
    assert b"PYTHONPATH" not in wrapper
    shadow = tmp_path / "repo2skill.py"
    shadow.write_text("raise RuntimeError('shadow imported')", encoding="utf-8")
    captured = {}
    monkeypatch.setattr("repo2skill.maintenance_hooks.subprocess.Popen", lambda *a, **kw: captured.update(kw))
    monkeypatch.setattr("repo2skill.maintenance_hooks.os.name", "nt")
    launch_worker(Path("C:/repo"), python=Path("C:/installed/python.exe"))
    assert captured["creationflags"] != 0
    assert captured["creationflags"] & getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0x08000000)


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
