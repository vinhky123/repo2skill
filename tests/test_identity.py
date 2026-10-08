from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import subprocess

import pytest

from repo2skill.config import default_scan_policy
from repo2skill.identity import RepositoryIdentityError, capture_repository
from repo2skill.inventory import collect_inventory


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return completed.stdout.strip()


def _write(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _init_repo(root: Path, *, commit: bool = True) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.invalid")
    if commit:
        _write(root / "tracked.txt", "original\n")
        _git(root, "add", "tracked.txt")
        _git(root, "commit", "-q", "-m", "initial")


def _capture(root: Path, mode: str = "auto", **kwargs: object):
    policy = kwargs.pop("policy", default_scan_policy())
    excluded = kwargs.pop("excluded_paths", ())
    inventory = collect_inventory(root, policy, excluded_paths=excluded)
    return capture_repository(
        root,
        inventory,
        mode,  # type: ignore[arg-type]
        project_id="123e4567-e89b-12d3-a456-426614174000",
        policy=policy,
        excluded_paths=excluded,
    )


def test_no_git_identity_is_stable_and_explicit_no_git_does_not_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path / "file.txt")
    first = _capture(tmp_path)
    second = _capture(tmp_path, "no-git")

    assert first == second
    assert first.gitMode == "no-git"
    assert not first.dirty
    assert first.dirtySummary == {"tracked": [], "untracked": [], "ignoredIncluded": []}
    assert first.repositoryId.startswith("repo_")


def test_auto_inside_git_resolves_to_worktree_and_scan_root_participates_in_id(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    subdir = tmp_path / "src"
    _write(subdir / "app.py")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-q", "-m", "src")

    top = _capture(tmp_path)
    nested = _capture(subdir)

    assert top.gitMode == nested.gitMode == "worktree"
    assert top.commitId == nested.commitId
    assert top.repositoryId != nested.repositoryId
    assert not top.dirty and not nested.dirty


def test_identity_filters_dirty_paths_with_policy_including_deleted_inputs(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "delete-me.txt")
    _write(tmp_path / "state" / "tracked-output.txt")
    _git(tmp_path, "add", "delete-me.txt", "state/tracked-output.txt")
    _git(tmp_path, "commit", "-q", "-m", "more")
    _write(tmp_path / "tracked.txt", "modified\n")
    (tmp_path / "delete-me.txt").unlink()
    _write(tmp_path / "new.txt")
    _write(tmp_path / "state" / "tracked-output.txt", "tool output\n")
    _write(tmp_path / ".gitignore", "ignored.txt\n")
    _write(tmp_path / "ignored.txt")
    policy = dataclasses.replace(default_scan_policy(), include=("ignored.txt",))

    identity = _capture(tmp_path, policy=policy, excluded_paths=("state",))

    assert identity.dirty
    assert identity.dirtySummary == {
        "tracked": ["delete-me.txt", "tracked.txt"],
        "untracked": [".gitignore", "new.txt"],
        "ignoredIncluded": ["ignored.txt"],
    }
    with pytest.raises(RepositoryIdentityError, match="clean") as error:
        _capture(tmp_path, "head", policy=policy, excluded_paths=("state",))
    assert error.value.code == "identity.head-dirty"


def test_head_accepts_clean_repo_and_unborn_worktree_has_no_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    clean = _capture(tmp_path, "head")
    assert clean.gitMode == "head"
    assert clean.commitId == _git(tmp_path, "rev-parse", "HEAD")

    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _init_repo(unborn, commit=False)
    worktree = _capture(unborn, "worktree")
    assert worktree.commitId is None
    with pytest.raises(RepositoryIdentityError, match="commit") as error:
        _capture(unborn, "head")
    assert error.value.code == "identity.head-unborn"


def test_explicit_worktree_outside_git_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(RepositoryIdentityError, match="Git") as error:
        _capture(tmp_path, "worktree")
    assert error.value.code == "identity.not-git"


def test_linked_worktrees_share_repository_id_at_same_scan_root(tmp_path: Path) -> None:
    main = tmp_path / "main"
    linked = tmp_path / "linked"
    main.mkdir()
    _init_repo(main)
    _git(main, "worktree", "add", "-q", "-b", "linked-branch", str(linked))

    main_identity = _capture(main)
    linked_identity = _capture(linked)

    assert main_identity.root != linked_identity.root
    assert main_identity.repositoryId == linked_identity.repositoryId


def test_capture_disables_repository_configured_fsmonitor_hook(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    marker = tmp_path / "hook-ran"
    hook = tmp_path / "fsmonitor-hook.sh"
    hook.write_text("#!/bin/sh\nprintf ran > hook-ran\n", encoding="utf-8")
    if os.name != "nt":
        hook.chmod(0o755)
    _git(tmp_path, "config", "core.fsmonitor", "./fsmonitor-hook.sh")
    _git(tmp_path, "status", "--porcelain")
    assert marker.exists(), "fixture must prove the configured hook is executable"
    marker.unlink()

    identity = _capture(tmp_path)

    assert identity.gitMode == "worktree"
    assert not marker.exists()


def test_git_info_exclude_does_not_remove_an_eligible_untracked_input(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / "eligible.txt")
    git_dir = Path(_git(tmp_path, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = tmp_path / git_dir
    (git_dir / "info" / "exclude").write_text("eligible.txt\n", encoding="utf-8")

    identity = _capture(tmp_path)

    assert identity.dirtySummary["untracked"] == ["eligible.txt"]
    assert identity.dirtySummary["ignoredIncluded"] == []


def test_identity_expands_ignored_directory_status_to_eligible_inputs(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _write(tmp_path / ".gitignore", "private/\n")
    _write(tmp_path / "private" / "keep.txt")
    policy = dataclasses.replace(default_scan_policy(), include=("private/keep.txt",))

    identity = _capture(tmp_path, policy=policy)

    assert identity.dirtySummary["ignoredIncluded"] == ["private/keep.txt"]
