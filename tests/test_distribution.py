from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tarfile
import zipfile


PROJECT_ROOT = Path(__file__).parents[1]


def _source_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source = str(PROJECT_ROOT / "src")
    environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
    return environment


def test_module_entry_point_exposes_commands_help_and_version() -> None:
    help_result = subprocess.run(
        [sys.executable, "-m", "repo2skill", "--help"],
        cwd=PROJECT_ROOT,
        env=_source_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    version_result = subprocess.run(
        [sys.executable, "-m", "repo2skill", "--version"],
        cwd=PROJECT_ROOT,
        env=_source_environment(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert help_result.returncode == 0
    assert "repository knowledge generator" in help_result.stdout
    for command in ("init", "scan", "generate", "review", "check", "explain", "report", "export", "doctor"):
        assert command in help_result.stdout
    assert version_result.returncode == 0
    assert version_result.stdout.strip() == "repo2skill 0.1.0"


def test_wheel_and_sdist_contain_upstream_notice(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    wheel = next(tmp_path.glob("*.whl"))
    sdist = next(tmp_path.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_notices = [name for name in archive.namelist() if name.endswith("THIRD_PARTY_NOTICES")]
        assert len(wheel_notices) == 1
        wheel_notice = archive.read(wheel_notices[0]).decode("utf-8")
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = set(archive.getnames())
        sdist_notices = [name for name in sdist_names if name.endswith("THIRD_PARTY_NOTICES")]
        assert len(sdist_notices) == 1
        member = archive.extractfile(sdist_notices[0])
        assert member is not None
        sdist_notice = member.read().decode("utf-8")

    sdist_root = "repo2skill-0.1.0/"
    assert {
        sdist_root + "tests/extractors/conftest.py",
        sdist_root + "tests/fixtures/alpha-mixed/package.json",
        sdist_root + "tests/fixtures/alpha-mixed/src/client.ts",
        sdist_root + "tests/fixtures/ecosystems/go/go.mod",
        sdist_root + "tests/integration/test_no_ai_alpha.py",
    } <= sdist_names
    assert not any("/__pycache__/" in name or name.endswith((".pyc", ".pyo")) for name in sdist_names)

    expected = "Copyright (c) 2026 Osmane B."
    assert expected in wheel_notice
    assert expected in sdist_notice
    assert wheel_notice == sdist_notice
