from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess

import pytest

from repo2skill.output_fs import (
    OutputConflictError,
    create_live_directory,
    read_live_file,
    replace_live_file,
    unlink_owned_file,
)


def _hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def test_guarded_output_operations_enforce_expected_state(tmp_path: Path) -> None:
    assert create_live_directory(tmp_path, "docs") is True
    assert create_live_directory(tmp_path, "docs") is False
    assert read_live_file(tmp_path, "docs/guide.md", limit=20) is None

    replace_live_file(tmp_path, "docs/guide.md", b"first\n", expected_old_hash=None)
    assert read_live_file(tmp_path, "docs/guide.md", limit=20) == b"first\n"
    with pytest.raises(OutputConflictError):
        replace_live_file(tmp_path, "docs/guide.md", b"second\n", expected_old_hash=None)
    replace_live_file(
        tmp_path, "docs/guide.md", b"second\n", expected_old_hash=_hash(b"first\n")
    )
    with pytest.raises(OutputConflictError):
        unlink_owned_file(tmp_path, "docs/guide.md", expected_old_hash=_hash(b"first\n"))
    unlink_owned_file(tmp_path, "docs/guide.md", expected_old_hash=_hash(b"second\n"))
    assert read_live_file(tmp_path, "docs/guide.md", limit=20) is None


def test_guarded_output_operations_reject_noncanonical_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_live_file(tmp_path, "docs/../guide.md", limit=100)
    with pytest.raises(ValueError):
        read_live_file(tmp_path, "docs\\guide.md", limit=100)
    with pytest.raises(ValueError):
        read_live_file(tmp_path, "e\u0301.md", limit=100)


def test_guarded_output_operations_reject_redirected_ancestors(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "guide.md").write_bytes(b"outside")
    try:
        (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if __import__("os").name != "nt":
            pytest.skip(f"directory symlink unavailable: {error}")
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(tmp_path / "linked"), str(outside)],
            capture_output=True, text=True, check=False,
        )
        if completed.returncode:
            pytest.skip(f"directory junction unavailable: {completed.stderr.strip()}")
    with pytest.raises(OSError):
        replace_live_file(tmp_path, "linked/guide.md", b"owned", expected_old_hash=None)
    assert (outside / "guide.md").read_bytes() == b"outside"
