from __future__ import annotations

import dataclasses
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

from repo2skill.config import default_scan_policy
from repo2skill.ids import canonical_json_bytes
from repo2skill.inventory import (
    IgnoreControlObservation,
    InventoryError,
    _CanonicalPathIndex,
    collect_inventory,
)


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
