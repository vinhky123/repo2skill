from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import venv


def main() -> int:
    parser = argparse.ArgumentParser(description="Install and smoke-test a repo2skill wheel.")
    parser.add_argument("wheel", nargs="?", type=Path)
    arguments = parser.parse_args()

    repository = Path(__file__).resolve().parents[1]
    wheel = arguments.wheel or next((repository / "dist").glob("repo2skill-*.whl"))
    wheel = wheel.resolve()
    if not wheel.is_file():
        parser.error(f"wheel does not exist: {wheel}")

    with tempfile.TemporaryDirectory(prefix="repo2skill-wheel-smoke-") as temporary:
        temporary_root = Path(temporary)
        environment_path = temporary_root / "venv"
        venv.create(environment_path, with_pip=True)
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        if os.name == "nt":
            python = environment_path / "Scripts" / "python.exe"
            executable = environment_path / "Scripts" / "repo2skill.exe"
        else:
            python = environment_path / "bin" / "python"
            executable = environment_path / "bin" / "repo2skill"

        subprocess.run(
            [str(python), "-m", "pip", "install", str(wheel)],
            cwd=temporary_root,
            env=environment,
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-c",
                "import pathlib, repo2skill, sys; "
                "pathlib.Path(repo2skill.__file__).resolve().relative_to("
                "pathlib.Path(sys.prefix).resolve())",
            ],
            cwd=temporary_root,
            env=environment,
            check=True,
        )
        subprocess.run([str(executable), "--help"], cwd=temporary_root, env=environment, check=True)
        subprocess.run([str(executable), "--version"], cwd=temporary_root, env=environment, check=True)
        subprocess.run(
            [
                str(python),
                "-c",
                "from importlib.resources import files; "
                "assert files('repo2skill').joinpath('templates/agents.md.tmpl').is_file(); "
                "assert files('repo2skill').joinpath('templates/skill.md.tmpl').is_file()",
            ],
            cwd=temporary_root,
            env=environment,
            check=True,
        )
        subprocess.run(
            [
                str(python),
                "-c",
                "from importlib.metadata import distribution; "
                "from pathlib import Path; "
                "dist = distribution('repo2skill'); "
                "notice = next(dist.locate_file(item) for item in dist.files "
                "if str(item).endswith('.dist-info/licenses/THIRD_PARTY_NOTICES')); "
                "assert Path(notice).read_bytes() == Path(r'"
                + str(repository / "THIRD_PARTY_NOTICES")
                + "').read_bytes()",
            ],
            cwd=temporary_root,
            env=environment,
            check=True,
        )

    print(f"Wheel smoke passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
