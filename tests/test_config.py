from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import subprocess

import pytest

from repo2skill.config import (
    PathBoundaryError,
    default_claim_policy,
    default_scan_policy,
    default_targets,
    resolve_under,
)
from repo2skill.models import ClaimPolicy, ScanPolicy, TargetSpec


def test_policy_models_are_frozen_and_defaults_are_versioned() -> None:
    scan = default_scan_policy()
    claims = default_claim_policy()

    assert scan == ScanPolicy(
        schemaVersion="1.0",
        revision="scan-policy-v1",
        include=(),
        exclude=(),
        followSymlinks=False,
        generatedMode="exclude",
        maxInventoryEntries=100_000,
        maxOpenedFiles=5_000,
        maxFileBytes=2 * 1024 * 1024,
        maxDecodedBytes=64 * 1024 * 1024,
        maxSnippetBytes=200 * 1024,
        encodings=("utf-8-sig",),
    )
    assert claims == ClaimPolicy(
        schemaVersion="1.0",
        revision="claim-policy-v1",
        autoAcceptObserved=True,
        renderBudget=16_384,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        scan.followSymlinks = True  # type: ignore[misc]


def test_default_targets_are_relative_and_use_a_safe_root_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "My Project!"
    root.mkdir()

    targets = default_targets(root)

    assert targets == (
        TargetSpec("agents-md", "agents-md", "AGENTS.md", "managed-blocks", "keep"),
        TargetSpec(
            "skill",
            "skill",
            ".agents/skills/my-project",
            "whole-files",
            "keep",
        ),
    )
    assert {target.targetId for target in targets} == {"agents-md", "skill"}
    assert all(not Path(target.livePath).is_absolute() for target in targets)
    assert all(resolve_under(root, Path(target.livePath)).is_relative_to(root.resolve()) for target in targets)

    monkeypatch.chdir(root)
    assert default_targets(Path("."))[1].livePath == ".agents/skills/my-project"


@pytest.mark.parametrize(
    "candidate",
    [
        Path("../outside"),
        Path("nested/../../outside"),
        Path("C:/outside"),
        Path(r"C:\outside"),
        pytest.param(
            Path(r"nested\outside"),
            marks=pytest.mark.skipif(
                os.name == "nt",
                reason="Windows Path normalizes both separator spellings identically",
            ),
        ),
    ],
)
def test_resolve_under_rejects_escape_and_windows_path_syntax(
    tmp_path: Path, candidate: Path
) -> None:
    with pytest.raises(PathBoundaryError):
        resolve_under(tmp_path, candidate)


def _make_directory_link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as error:
        if os.name != "nt":
            raise
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            pytest.skip(f"cannot create a symlink or junction: {error}")


def test_resolve_under_rejects_missing_destination_below_link(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    _make_directory_link(root / "unsafe", outside)

    with pytest.raises(PathBoundaryError):
        resolve_under(root, Path("unsafe/missing/destination"))


def test_resolve_under_preserves_complete_missing_destination(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    resolved = resolve_under(root, Path("safe/missing/destination"))

    assert resolved == root / "safe" / "missing" / "destination"


def test_root_dot_segments_are_normalized_without_resolving_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "My Project"
    source = root / "src"
    source.mkdir(parents=True)

    assert resolve_under(source / "..", Path("AGENTS.md")) == root / "AGENTS.md"
    assert default_targets(source / "..")[1].livePath == ".agents/skills/my-project"

    monkeypatch.chdir(root)
    assert resolve_under(Path("."), Path("AGENTS.md")) == root / "AGENTS.md"


def test_root_link_before_parent_segment_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "unsafe"
    _make_directory_link(link, outside)

    with pytest.raises(PathBoundaryError, match="symlink or reparse"):
        resolve_under(link / "..", Path("AGENTS.md"))
    with pytest.raises(PathBoundaryError, match="symlink or reparse"):
        resolve_under(link, Path("AGENTS.md"))


def test_model_constructors_validate_policy_and_target_invariants() -> None:
    with pytest.raises(ValueError, match="followSymlinks"):
        dataclasses.replace(default_scan_policy(), followSymlinks=True)
    with pytest.raises(ValueError, match="renderBudget"):
        dataclasses.replace(default_claim_policy(), renderBudget=0)
    with pytest.raises(ValueError, match="livePath"):
        TargetSpec("unsafe", "skill", "../outside", "whole-files", "keep")
    with pytest.raises(ValueError, match="livePath"):
        TargetSpec("unsafe", "skill", r"nested\outside", "whole-files", "keep")
    with pytest.raises(ValueError, match="NFC"):
        TargetSpec("unsafe", "skill", "cafe\u0301", "whole-files", "keep")
