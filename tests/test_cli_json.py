from __future__ import annotations

import json
from pathlib import Path

from repo2skill.cli import main


def test_json_init_emits_one_versioned_envelope(tmp_path: Path, capsys) -> None:
    code = main(["--root", str(tmp_path), "--format", "json", "init", "--git-mode", "no-git"])
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    assert code == payload["exitCode"] == 0
    assert payload["schemaVersion"] == "1.0"
    assert payload["command"] == "init"
    assert payload["status"] == "ok"
    assert payload["result"]["stateDir"] == ".repo2skill"
    assert payload["findings"] == []
    assert payload["diagnostics"] == []
    assert payload["nextActions"] == []


def test_recognized_codex_and_measure_options_have_p0_exit_codes(tmp_path: Path, capsys) -> None:
    code = main(["--root", str(tmp_path), "--format", "json", "generate", "--ai", "codex"])
    payload = json.loads(capsys.readouterr().out)
    assert code == payload["exitCode"] == 6
    assert payload["findings"][0]["code"] == "codex.unavailable"
    assert not (tmp_path / ".repo2skill").exists()

    code = main(["--root", str(tmp_path), "--format", "json", "generate", "--measure"])
    payload = json.loads(capsys.readouterr().out)
    assert code == payload["exitCode"] == 1
    assert not (tmp_path / ".repo2skill").exists()


def test_json_diagnostic_is_not_written_to_stdout(tmp_path: Path, capsys) -> None:
    code = main(["--root", str(tmp_path), "--format", "json", "generate", "--yes-ai"])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["findings"][0]["code"] == "invocation.invalid"
    assert "Traceback" not in captured.out


def test_json_errors_do_not_expose_absolute_runtime_paths(tmp_path: Path, capsys) -> None:
    missing_config = tmp_path / "private" / "project.json"
    code = main([
        "--root", str(tmp_path), "--config", str(missing_config), "--format", "json", "scan",
    ])
    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == ""
    assert str(tmp_path) not in captured.out
    payload = json.loads(captured.out)
    assert payload["findings"][0]["code"] == "lookup.not-found"


def test_unexpected_internal_error_uses_exit_seven(tmp_path: Path, capsys, monkeypatch) -> None:
    import repo2skill.cli as cli

    def fail(_args):
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr(cli, "_dispatch", fail)
    code = main(["--root", str(tmp_path), "--format", "json", "doctor"])
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert code == payload["exitCode"] == 7
    assert payload["findings"][0]["code"] == "internal.error"
    assert "private implementation detail" not in captured.out


def test_text_and_json_diagnostics_always_reach_stderr_even_when_quiet(capsys) -> None:
    from repo2skill.cli import _print_envelope

    envelope = {
        "command": "scan", "exitCode": 0, "findings": [],
        "diagnostics": [{"code": "coverage.optional-gap", "severity": "warning", "message": "optional input skipped"}],
    }
    assert _print_envelope(envelope, "text", quiet=True) == 0
    text_output = capsys.readouterr()
    assert text_output.out == ""
    assert "coverage.optional-gap: optional input skipped" in text_output.err

    assert _print_envelope(envelope, "json", quiet=True) == 0
    json_output = capsys.readouterr()
    assert json.loads(json_output.out) == envelope
    assert json_output.out.endswith("\n") and json_output.out.count("\n") == 1
    assert "coverage.optional-gap: optional input skipped" in json_output.err
