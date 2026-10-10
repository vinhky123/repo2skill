from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import dataclasses

import pytest

import repo2skill.maintenance_git as maintenance_git
from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore
from repo2skill.ids import canonical_record_id
from repo2skill.maintenance_git import (
    _parse_diff, bootstrap_commit, compare_commits, enqueue_event,
    materialize_commit, parse_hook_event, read_commit_blob,
)
from repo2skill.maintenance_models import GitEvent
from repo2skill.models import ProjectDocument, ProjectState


def _git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True,
    )
    return result.stdout


def _repo(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    return root


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").decode().strip()


def test_diff_uses_commit_blobs_when_worktree_is_dirty(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    target = _commit(root, "target")
    clean_impact = compare_commits(root, baseline, target)
    (root / "src" / "app.py").write_text("uncommitted secret\n", encoding="utf-8")
    dirty_impact = compare_commits(root, baseline, target)

    assert dirty_impact.diffHash == clean_impact.diffHash
    assert dirty_impact.paths == clean_impact.paths == ("src/app.py",)


def test_nul_paths_preserve_renames_and_newlines(tmp_path: Path) -> None:
    # Newline-containing filenames are legal on POSIX; exercise the exact
    # NUL-framed parser payload on Windows, where the filesystem rejects them.
    if os.name == "nt":
        paths, renames, statuses = _parse_diff(
            b"R100\0before\nname.txt\0after\nname.txt\0A\0new\nfile.txt\0"
        )
        assert paths == ("after\nname.txt", "before\nname.txt", "new\nfile.txt")
        assert renames == (("before\nname.txt", "after\nname.txt"),)
        assert statuses
        return
    root = _repo(tmp_path / "repo")
    (root / "before\nname.txt").write_text("same\n", encoding="utf-8")
    baseline = _commit(root, "base")
    _git(root, "mv", "before\nname.txt", "after\nname.txt")
    (root / "new\nfile.txt").write_text("new\n", encoding="utf-8")
    target = _commit(root, "rename")

    impact = compare_commits(root, baseline, target)

    assert impact.paths == ("after\nname.txt", "before\nname.txt", "new\nfile.txt")
    assert impact.renames == (("before\nname.txt", "after\nname.txt"),)


def test_diff_disables_external_diff_and_textconv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path / "repo")
    (root / "source.txt").write_text("one\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / "source.txt").write_text("two\n", encoding="utf-8")
    target = _commit(root, "target")
    marker = tmp_path / "external-diff-ran"
    script = tmp_path / "external-diff.cmd"
    script.write_text(f"@echo off\r\necho ran>{marker}\r\n", encoding="utf-8")
    _git(root, "config", "diff.external", str(script))
    _git(root, "config", "diff.source.textconv", str(script))
    _git(root, "config", "diff.source.binary", "false")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(script))

    compare_commits(root, baseline, target)

    assert not marker.exists()


def test_added_glob_match_and_negative_invalidation_queue_scope(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    target = _commit(root, "add required input")

    impact = compare_commits(root, baseline, target)

    assert impact.paths == ("pyproject.toml",)
    assert impact.scopes


def test_deleted_and_control_file_changes_are_conservative(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / "README.md").unlink()
    (root / ".gitignore").write_text("ignored\n*.tmp\n", encoding="utf-8")
    target = _commit(root, "delete and control")

    impact = compare_commits(root, baseline, target)

    assert impact.requiredFullScan is True
    assert {"README.md", ".gitignore"}.issubset(impact.paths)


def test_state_and_output_only_change_has_no_impact(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / ".repo2skill").mkdir()
    (root / ".repo2skill" / "state.json").write_text("{}\n", encoding="utf-8")
    (root / ".agents").mkdir()
    (root / ".agents" / "AGENTS.md").write_text("managed\n", encoding="utf-8")
    target = _commit(root, "state and output")

    impact = compare_commits(root, baseline, target)

    assert impact.paths == ()
    assert impact.scopes == ()


def test_missing_baseline_requires_rebuild(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    target = _commit(root, "target")

    with pytest.raises(ValueError, match="missing|rebuild"):
        compare_commits(root, "f" * 40, target)


def test_bootstrap_has_explicit_full_tree_impact(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    target = _commit(root, "target")

    impact = bootstrap_commit(root, target)

    assert impact.requiredFullScan is True
    assert impact.baselineCommit is None
    assert impact.paths == ("src/main.py",)


def test_hook_event_resolves_current_commit_for_post_commit_and_merge(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("one\n", encoding="utf-8")
    target = _commit(root, "target")

    post_commit = parse_hook_event("post-commit", b"", (), root=root)
    post_merge = parse_hook_event("post-merge", b"", ("1",), root=root)

    assert post_commit.targetCommit == target
    assert post_merge.targetCommit == target
    assert post_commit.hookName == "post-commit"


def test_materialize_and_blob_reads_use_committed_bytes(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_bytes(b"committed\x00bytes")
    target = _commit(root, "target")
    (root / "src" / "main.py").write_bytes(b"dirty worktree")

    assert read_commit_blob(root, target, "src/main.py", 1024) == b"committed\x00bytes"
    manifest = materialize_commit(root, target, ("src/main.py",), tmp_path / "snapshot")

    assert manifest.files[0].path == "src/main.py"
    assert (tmp_path / "snapshot" / "src" / "main.py").read_bytes() == b"committed\x00bytes"
    assert manifest.contextHash.startswith("sha256:")


def test_materialize_records_size_cap_as_an_explicit_omission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path / "repo")
    (root / "large.txt").write_text("too large for this test cap", encoding="utf-8")
    target = _commit(root, "target")
    monkeypatch.setattr(maintenance_git, "MAX_CONTEXT_FILE_BYTES", 4)

    manifest = materialize_commit(root, target, ("large.txt",), tmp_path / "snapshot")

    assert manifest.files == ()
    assert manifest.omittedPaths == ("large.txt",)


def test_enqueue_persists_context_before_publishing_job_and_cursor(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    target = _commit(root, "target")
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project = ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture",
        ".repo2skill", "worktree", default_scan_policy(), default_claim_policy(),
        default_targets(root),
    )
    state = ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    store = LedgerStore(root, state_dir)
    event = GitEvent("git-event_0123456789abcdef01234567", "post-commit", "cursor-1", target)
    impact = bootstrap_commit(root, target)

    job = enqueue_event(store, event, impact)

    assert job is not None
    manifest_id = canonical_record_id("source-context", {"contextHash": job.sourceContextHash})
    saved = store.read_immutable_bytes(f"maintenance/contexts/{manifest_id}.json")
    assert saved is not None
    committed = store.read_maintenance_state_document()
    assert committed is not None
    assert committed["observedEventCursor"] == event.cursor
    assert job.jobId in committed["jobs"]
