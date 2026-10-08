from __future__ import annotations

import dataclasses
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

import repo2skill.inventory as inventory_module
from repo2skill.config import default_scan_policy
from repo2skill.ids import canonical_json_bytes
from repo2skill.inventory import (
    IgnoreControlObservation,
    InventoryError,
    _CanonicalPathIndex,
    collect_inventory,
)
from repo2skill.safe_fs import GuardedDirectory, GuardedFile, UnsafePathError, open_directory


def _write(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _paths(result: object) -> tuple[str, ...]:
    return tuple(entry.path for entry in result.entries)  # type: ignore[attr-defined]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_inventory_is_sorted_and_exact_include_only_overrides_normal_ignore(
    tmp_path: Path,
) -> None:
    _write(tmp_path / ".gitignore", "ignored*\nprivate/\n")
    _write(tmp_path / "src" / "app.py")
    _write(tmp_path / "ignored.txt")
    _write(tmp_path / "ignored-sibling.txt")
    _write(tmp_path / "private" / "keep.txt")
    policy = dataclasses.replace(
        default_scan_policy(),
        include=(r"ignored.txt", r"private\keep.txt", "ignored*.txt"),
        exclude=(r"private\keep.txt",),
    )

    result = collect_inventory(tmp_path, policy)

    assert _paths(result) == (".gitignore", "ignored.txt", "src/app.py")
    assert next(entry for entry in result.entries if entry.path == "ignored.txt").ignored
    assert all(entry.contentHash is None for entry in result.entries if entry.path != ".gitignore")


def test_literal_include_can_name_ignored_glob_metacharacters(tmp_path: Path) -> None:
    _write(tmp_path / ".gitignore", "ignored/\n")
    _write(tmp_path / "ignored" / "a[b].txt")
    _write(tmp_path / "ignored" / "other.txt")
    policy = dataclasses.replace(
        default_scan_policy(), include=(r"literal:ignored\a[b].txt",)
    )

    result = collect_inventory(tmp_path, policy)

    assert _paths(result) == (".gitignore", "ignored/a[b].txt")
    assert result.entries[-1].ignored


@pytest.mark.parametrize(
    "excluded", ("state//secret", "state/./secret", "state/", ".", "")
)
def test_excluded_paths_reject_noncanonical_raw_components(
    tmp_path: Path, excluded: str
) -> None:
    _write(tmp_path / "state" / "secret" / "output.txt")

    with pytest.raises(ValueError):
        collect_inventory(tmp_path, default_scan_policy(), excluded_paths=(excluded,))


def test_inventory_honors_nested_ignore_precedence_and_parent_pruning(tmp_path: Path) -> None:
    _write(tmp_path / ".gitignore", "blocked/\n*.txt\n")
    _write(tmp_path / "blocked" / ".gitignore", "!keep.txt\n")
    _write(tmp_path / "blocked" / "keep.txt")
    _write(tmp_path / "dir" / ".gitignore", "!keep.txt\n")
    _write(tmp_path / "dir" / "keep.txt")
    _write(tmp_path / "dir" / "drop.txt")

    result = collect_inventory(tmp_path, default_scan_policy())

    assert _paths(result) == (".gitignore", "dir/.gitignore", "dir/keep.txt")


def test_inventory_excludes_generated_security_state_and_nested_repository(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "app.py")
    _write(tmp_path / "node_modules" / "pkg.js")
    _write(tmp_path / "dist" / "bundle.js")
    _write(tmp_path / ".env", "SECRET=value")
    _write(tmp_path / ".env.example", "SECRET=")
    _write(tmp_path / "state" / "ledger.json")
    _write(tmp_path / "nested" / ".git" / "HEAD")
    _write(tmp_path / "nested" / "source.py")

    result = collect_inventory(tmp_path, default_scan_policy(), excluded_paths=("state",))

    assert _paths(result) == (".env.example", "app.py")
    assert {entry.reasonCode for entry in result.coverage} >= {
        "inventory.generated-excluded",
        "inventory.security-excluded",
        "inventory.boundary-excluded",
    }


def test_inventory_records_links_without_following_or_leaking_external_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    _write(outside / "secret.txt", "secret")
    link = tmp_path / "external"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            raise
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            pytest.skip(f"cannot create directory link or junction: {error}")

    result = collect_inventory(tmp_path, default_scan_policy())

    assert _paths(result) == ("external",)
    entry = result.entries[0]
    assert entry.kind == "symlink"
    assert entry.symlinkTarget == "<external-target>"
    assert "secret.txt" not in _paths(result)


def test_guarded_directory_rejects_a_child_swapped_to_redirect(tmp_path: Path) -> None:
    child = tmp_path / "child"
    outside = tmp_path.parent / f"{tmp_path.name}-swap-outside"
    child.mkdir()
    outside.mkdir()
    _write(outside / "secret.txt", "secret")

    with open_directory(tmp_path) as directory:
        assert "child" in directory.names()
        child.rename(tmp_path / "original-child")
        try:
            child.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            if os.name != "nt":
                raise
            completed = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(child), str(outside)],
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode:
                pytest.skip(f"cannot create swapped junction: {error}")

        with pytest.raises(UnsafePathError, match="redirect"):
            with directory.open_directory("child"):
                pytest.fail("redirected directory must never be opened")


def test_guarded_file_reads_only_the_opened_regular_file(tmp_path: Path) -> None:
    raw = b"*.tmp\r\n"
    (tmp_path / ".gitignore").write_bytes(raw)

    with open_directory(tmp_path) as directory:
        with directory.open_file(".gitignore") as opened:
            assert opened.read_bounded(100) == raw
            assert opened.stat_before.st_size == len(raw)
            assert opened.stable_after_read()


@pytest.mark.skipif(os.name != "nt", reason="Windows share modes guard against rename")
def test_guarded_directory_handle_blocks_rename(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()

    with open_directory(tmp_path) as root:
        with root.open_directory("child"):
            with pytest.raises(OSError):
                child.rename(tmp_path / "moved-child")


def test_inventory_does_not_follow_directory_swapped_after_enumeration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = tmp_path / "child"
    outside = tmp_path.parent / f"{tmp_path.name}-inventory-outside"
    _write(child / "inside.txt", "inside\n")
    _write(outside / "secret.txt", "secret\n")
    original_lstat = GuardedDirectory.lstat
    swapped = False

    def lstat_then_swap(directory: GuardedDirectory, name: str) -> os.stat_result:
        nonlocal swapped
        result = original_lstat(directory, name)
        if directory.path == tmp_path and name == "child" and not swapped:
            swapped = True
            child.rename(tmp_path / "saved-child")
            try:
                child.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                if os.name != "nt":
                    raise
                completed = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(child), str(outside)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if completed.returncode:
                    pytest.skip(f"cannot create swapped junction: {error}")
        return result

    monkeypatch.setattr(GuardedDirectory, "lstat", lstat_then_swap)

    result = collect_inventory(tmp_path, default_scan_policy())

    assert "child/secret.txt" not in _paths(result)
    assert "child/inside.txt" not in _paths(result)
    assert swapped
    assert any(item.code == "inventory.redirect-not-followed" for item in result.diagnostics)


def test_inventory_does_not_read_replaced_ignore_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / ".gitignore"
    replacement = tmp_path / "replacement"
    _write(control, "original.txt\n")
    _write(replacement, "secret-external-rule.txt\n")
    _write(tmp_path / "original.txt")
    original_lstat = GuardedDirectory.lstat
    original_read = GuardedFile.read_bounded
    replaced = False
    read_called = False

    def lstat_then_replace(directory: GuardedDirectory, name: str) -> os.stat_result:
        nonlocal replaced
        result = original_lstat(directory, name)
        if directory.path == tmp_path and name == ".gitignore" and not replaced:
            replaced = True
            replacement.replace(control)
        return result

    def record_read(opened: GuardedFile, limit: int) -> bytes:
        nonlocal read_called
        read_called = True
        return original_read(opened, limit)

    monkeypatch.setattr(GuardedDirectory, "lstat", lstat_then_replace)
    monkeypatch.setattr(GuardedFile, "read_bounded", record_read)
    observations: list[IgnoreControlObservation] = []

    result = collect_inventory(
        tmp_path, default_scan_policy(), control_sink=observations.append
    )

    assert replaced
    assert not read_called
    assert _paths(result) == (".gitignore",)
    assert observations[0].outcome == "io-error"
    assert observations[0].contentHash is None


def test_control_open_identity_race_charges_a_successfully_opened_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / ".gitignore"
    replacement = tmp_path / "replacement"
    _write(control, "original.txt\n")
    _write(replacement, "replacement.txt\n")
    original_lstat = GuardedDirectory.lstat
    control_checks = 0

    def lstat_then_replace(directory: GuardedDirectory, name: str) -> os.stat_result:
        nonlocal control_checks
        result = original_lstat(directory, name)
        if directory.path == tmp_path and name == ".gitignore":
            control_checks += 1
            if control_checks == 2:
                replacement.replace(control)
        return result

    monkeypatch.setattr(GuardedDirectory, "lstat", lstat_then_replace)
    observations: list[IgnoreControlObservation] = []

    collect_inventory(tmp_path, default_scan_policy(), control_sink=observations.append)

    assert observations[0].outcome == "io-error"
    assert observations[0].openedFileCount == 1
    assert observations[0].decodedByteCount == 0


def test_control_open_failure_before_handle_does_not_charge_opened_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / ".gitignore"
    _write(control, "rule.txt\n")
    original_lstat = GuardedDirectory.lstat
    control_checks = 0

    def lstat_then_remove(directory: GuardedDirectory, name: str) -> os.stat_result:
        nonlocal control_checks
        result = original_lstat(directory, name)
        if directory.path == tmp_path and name == ".gitignore":
            control_checks += 1
            if control_checks == 2:
                control.unlink()
        return result

    monkeypatch.setattr(GuardedDirectory, "lstat", lstat_then_remove)
    observations: list[IgnoreControlObservation] = []

    collect_inventory(tmp_path, default_scan_policy(), control_sink=observations.append)

    assert observations[0].outcome == "io-error"
    assert observations[0].openedFileCount == 0
    assert observations[0].decodedByteCount == 0


def test_inventory_caps_after_canonical_sort_and_reports_required_gap(tmp_path: Path) -> None:
    for name in ("z.txt", "a.txt", "m.txt"):
        _write(tmp_path / name)
    policy = dataclasses.replace(default_scan_policy(), maxInventoryEntries=2)

    result = collect_inventory(tmp_path, policy)

    assert _paths(result) == ("a.txt", "m.txt")
    assert any(item.code == "inventory.entry-cap" for item in result.diagnostics)
    assert any(
        item.status == "capped" and item.reasonCode == "inventory.entry-cap"
        for item in result.coverage
    )


def test_canonical_path_index_rejects_case_and_unicode_collisions() -> None:
    index = _CanonicalPathIndex()
    index.add("A.py")
    with pytest.raises(InventoryError, match="collision") as case_error:
        index.add("a.py")
    assert case_error.value.diagnostic.code == "inventory.path-collision"

    unicode_index = _CanonicalPathIndex()
    unicode_index.add("caf\u00e9.py", original="caf\u00e9.py")
    with pytest.raises(InventoryError, match="collision"):
        unicode_index.add("caf\u00e9.py", original="cafe\u0301.py")


def test_gitignore_control_observations_use_captured_buffer_and_aggregate(tmp_path: Path) -> None:
    raw = b"ignored.txt\r\n"
    (tmp_path / ".gitignore").write_bytes(raw)
    _write(tmp_path / "kept.txt")
    observations: list[IgnoreControlObservation] = []

    result = collect_inventory(
        tmp_path,
        default_scan_policy(),
        control_sink=observations.append,
    )

    assert len(observations) == 2
    file_observation, aggregate = observations
    assert file_observation.selector.value == ".gitignore"
    assert file_observation.outcome == "present"
    assert file_observation.contentHash == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert file_observation.openedFileCount == 1
    assert file_observation.decodedByteCount == len(raw)
    assert aggregate.selector.value == "**/.gitignore"
    assert aggregate.outcome == "matched"
    assert aggregate.matchPaths == (".gitignore",)
    assert aggregate.matchSetHash == "sha256:" + hashlib.sha256(
        canonical_json_bytes([".gitignore"])
    ).hexdigest()
    assert next(entry for entry in result.entries if entry.path == ".gitignore").contentHash == file_observation.contentHash


def test_control_cap_fails_closed_for_descendants_and_marks_aggregate_incomplete(
    tmp_path: Path,
) -> None:
    _write(tmp_path / ".gitignore", "x" * 20)
    _write(tmp_path / "sibling.py")
    _write(tmp_path / "child" / "source.py")
    observations: list[IgnoreControlObservation] = []
    policy = dataclasses.replace(default_scan_policy(), maxFileBytes=8)

    result = collect_inventory(tmp_path, policy, control_sink=observations.append)

    assert _paths(result) == (".gitignore",)
    assert [item.outcome for item in observations] == ["capped", "capped"]
    assert observations[0].openedFileCount == 1
    assert observations[0].decodedByteCount == 0
    assert observations[-1].matchPaths == (".gitignore",)
    assert observations[-1].matchSetHash is None
    assert any(item.reasonCode == "inventory.gitignore-control-capped" for item in result.coverage)


def test_empty_control_discovery_is_a_complete_stable_selector(tmp_path: Path) -> None:
    _write(tmp_path / "source.py")
    observations: list[IgnoreControlObservation] = []

    collect_inventory(tmp_path, default_scan_policy(), control_sink=observations.append)

    assert len(observations) == 1
    assert observations[0].outcome == "no-match"
    assert observations[0].matchPaths == ()
    assert observations[0].matchSetHash == "sha256:" + hashlib.sha256(
        canonical_json_bytes([])
    ).hexdigest()


def test_gitignore_remains_an_eligible_control_when_its_rules_match_it(tmp_path: Path) -> None:
    _write(tmp_path / ".gitignore", "*\n")
    _write(tmp_path / "hidden.txt")

    result = collect_inventory(tmp_path, default_scan_policy())

    assert _paths(result) == (".gitignore",)
    assert result.entries[0].ignored


def test_tracked_file_remains_eligible_despite_worktree_gitignore(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", "tracked.txt\n")
    _write(tmp_path / "tracked.txt")
    _git(tmp_path, "add", "-f", ".gitignore", "tracked.txt")

    result = collect_inventory(tmp_path, default_scan_policy())

    assert _paths(result) == (".gitignore", "tracked.txt")
    assert not next(entry for entry in result.entries if entry.path == "tracked.txt").ignored


def test_git_index_read_failure_is_required_and_prunes_unknown_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(tmp_path, "init", "-q")
    _write(tmp_path / ".gitignore", "tracked-ignored.txt\n")
    _write(tmp_path / "tracked-ignored.txt")
    _write(tmp_path / "visible.txt")
    _write(tmp_path / "possible-gitlink" / "must-not-open.txt")
    _git(tmp_path, "add", "-f", ".gitignore", "tracked-ignored.txt")
    real_run = inventory_module.run_git_bounded

    def malformed_index(
        root: Path, arguments: tuple[str, ...], **kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        if arguments[0] == "ls-files":
            return subprocess.CompletedProcess(arguments, 0, b"malformed-index-record\0", b"")
        return real_run(root, arguments, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(inventory_module, "run_git_bounded", malformed_index)
    observations: list[IgnoreControlObservation] = []

    result = collect_inventory(
        tmp_path, default_scan_policy(), control_sink=observations.append
    )

    assert _paths(result) == (".gitignore", "visible.txt")
    assert any(item.code == "inventory.git-index-malformed" for item in result.diagnostics)
    assert any(
        item.status == "failed"
        and item.selector.required
        and item.reasonCode == "inventory.git-index-malformed"
        for item in result.coverage
    )
    assert observations[-1].outcome == "io-error"
    assert observations[-1].matchSetHash is None


def test_git_index_paths_are_toplevel_relative_for_subdirectory_scan(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _write(tmp_path / "README.md")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    commit_id = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = tmp_path / "src"
    _write(source / ".gitignore", "tracked-ignored.txt\n")
    _write(source / "tracked-ignored.txt")
    _write(source / "possible-gitlink" / "must-not-open.txt")
    _git(tmp_path, "add", "-f", "src/.gitignore", "src/tracked-ignored.txt")
    _git(
        tmp_path,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{commit_id},src/possible-gitlink",
    )

    result = collect_inventory(source, default_scan_policy())

    assert _paths(result) == (".gitignore", "tracked-ignored.txt")
    assert not any("must-not-open" in entry.path for entry in result.entries)
