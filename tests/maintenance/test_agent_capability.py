from __future__ import annotations

import json
from pathlib import Path
import pytest

from repo2skill.maintenance_agent import probe_agent


FIXTURES = Path(__file__).parents[1] / "fixtures" / "maintenance" / "codex"


@pytest.mark.parametrize(
    ("fixture_name", "capability"),
    [
        ("missing-stdin.txt", "stdin-prompt"),
        ("missing-schema.txt", "output-schema"),
        ("missing-output-file.txt", "output-file"),
        ("missing-read-only.txt", "read-only-sandbox"),
        ("missing-user-config.txt", "user-config"),
        ("missing-working-directory.txt", "working-directory"),
    ],
)
def test_probe_fails_closed_when_a_required_cli_surface_is_missing(
    tmp_path: Path, fixture_name: str, capability: str
) -> None:
    executable, log_path = _fake_codex(tmp_path, FIXTURES / fixture_name)

    result = probe_agent(executable)

    assert result.supported is False
    assert capability in result.missingCapabilities
    assert result.version == "codex-cli 0.162.0-alpha.2"
    calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [["--version"], ["exec", "--help"]]
    assert all(call["stdin"] == "" for call in calls)
    assert all(not (Path(call["cwd"]) / "AGENTS.md").exists() for call in calls)
    assert all(Path(call["cwd"]).resolve() != Path(__file__).parents[2].resolve() for call in calls)
    assert not (Path(__file__).parents[2] / "probe-write.txt").exists()


def test_probe_accepts_the_required_local_cli_surface_and_uses_bounded_temp_cwd(
    tmp_path: Path,
) -> None:
    executable, log_path = _fake_codex(tmp_path, FIXTURES / "complete-help.txt")

    result = probe_agent(executable)

    assert result.supported is True
    assert result.missingCapabilities == ()
    assert result.executable == executable
    assert result.version == "codex-cli 0.162.0-alpha.2"
    calls = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [["--version"], ["exec", "--help"]]
    assert all(call["stdin"] == "" for call in calls)
    assert all(call["cwd"] for call in calls)
    assert all(not (Path(call["cwd"]) / "AGENTS.md").exists() for call in calls)
    assert all(Path(call["cwd"]).resolve() != Path(__file__).parents[2].resolve() for call in calls)
    assert not (Path(__file__).parents[2] / "probe-write.txt").exists()


def test_probe_discovers_codex_on_path_or_reports_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repo2skill import maintenance_agent

    executable, _ = _fake_codex(tmp_path, FIXTURES / "complete-help.txt")
    monkeypatch.setattr(maintenance_agent.shutil, "which", lambda _name: str(executable))
    discovered = probe_agent(None)
    assert discovered.executable == executable
    assert discovered.supported is True

    monkeypatch.setattr(maintenance_agent.shutil, "which", lambda _name: None)
    unavailable = probe_agent(None)
    assert unavailable.supported is False
    assert unavailable.executable is None
    assert "executable" in unavailable.missingCapabilities


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "output-cap"])
def test_probe_fails_closed_for_bounded_inspection_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    from repo2skill import maintenance_agent

    executable, _ = _fake_codex(tmp_path, FIXTURES / "complete-help.txt", failure=failure)
    if failure == "timeout":
        monkeypatch.setattr(maintenance_agent, "PROBE_TIMEOUT_SECONDS", 0.05)
    if failure == "output-cap":
        monkeypatch.setattr(maintenance_agent, "PROBE_MAX_OUTPUT_BYTES", 16)

    result = probe_agent(executable)

    assert result.supported is False
    assert result.missingCapabilities
    assert any(failure in reason for reason in result.missingCapabilities)


def _fake_codex(
    tmp_path: Path, help_fixture: Path, *, failure: str | None = None
) -> tuple[Path, Path]:
    executable = tmp_path / "codex.py"
    log_path = tmp_path / "calls.jsonl"
    executable.write_text(
        "from __future__ import annotations\n"
        "import json, os, pathlib, sys, time\n"
        "args = sys.argv[1:]\n"
        "record = {'argv': args, 'cwd': os.getcwd(), 'stdin': sys.stdin.read()}\n"
        f"pathlib.Path({str(log_path)!r}).open('a', encoding='utf-8').write(json.dumps(record) + '\\n')\n"
        f"failure = {failure!r}\n"
        "if failure == 'timeout': time.sleep(1)\n"
        "if failure == 'output-cap': print('x' * 4096)\n"
        "elif args == ['--version']:\n"
        "    print('codex-cli 0.162.0-alpha.2')\n"
        "elif args == ['exec', '--help']:\n"
        f"    print(pathlib.Path({str(help_fixture)!r}).read_text(encoding='utf-8'))\n"
        "if failure == 'nonzero': sys.exit(9)\n",
        encoding="utf-8",
    )
    return executable, log_path
