from __future__ import annotations

import json

import pytest

from repo2skill.cli import build_parser


def test_parser_exposes_all_canonical_commands_and_codex_grammar() -> None:
    parser = build_parser()
    parsed = parser.parse_args(["generate", "--ai", "codex", "--codex-config", "local"])
    assert parsed.command == "generate"
    assert parsed.ai == "codex"
    assert parsed.codex_config == "local"


def test_parser_supports_custom_config_after_subcommand() -> None:
    parsed = build_parser().parse_args(["scan", "--config", ".cache/repo2skill/project.json", "--strict"])
    assert parsed.config == ".cache/repo2skill/project.json"
    assert parsed.strict is True


def test_parser_enforces_review_and_export_selection_grammar() -> None:
    parser = build_parser()
    assert parser.parse_args(["review", "accept", "--all-observed"]).all_observed is True
    assert parser.parse_args(["review", "reject", "--claim", "claim_0123456789abcdef01234567", "--reason", "unsupported"]).reason == "unsupported"
    assert parser.parse_args(["export", "--resume", "export_0123456789abcdef01234567"]).resume == "export_0123456789abcdef01234567"


def test_report_fails_closed_on_denied_state_read_and_recovers(
    tmp_path, capsys, monkeypatch,
) -> None:
    from repo2skill.cli import main
    from repo2skill.ledger import LedgerStore
    from repo2skill.safe_fs import GuardedDirectory

    def deny_worker(*_args, **_kwargs):
        raise AssertionError("read-only report must never enter the maintenance worker")

    monkeypatch.setattr("repo2skill.maintenance_worker.run_worker", deny_worker)

    def invoke(*args: str) -> tuple[int, dict, str]:
        code = main(["--root", str(tmp_path), "--format", "json", *args])
        captured = capsys.readouterr()
        return code, json.loads(captured.out), captured.err

    code, _, _ = invoke("init", "--git-mode", "no-git")
    assert code == 0
    code, scanned, _ = invoke("scan", "--git-mode", "no-git")
    assert code == 0
    state_path = tmp_path / ".repo2skill" / "state.json"
    original_state_bytes = state_path.read_bytes()
    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    authority_before = store.read_state()
    assert authority_before is not None and authority_before.currentRunId == scanned["result"]["runId"]

    original_open_file = GuardedDirectory.open_file
    denied: list[str] = []

    def deny_actual_state_open(guard, name: str):
        if guard.path == state_path.parent and name == state_path.name:
            denied.append(name)
            raise PermissionError("injected state read denial")
        return original_open_file(guard, name)

    with monkeypatch.context() as denied_access:
        denied_access.setattr(GuardedDirectory, "open_file", deny_actual_state_open)
        code, result, error_text = invoke("report", "--section", "summary")

    assert denied == ["state.json"]
    assert code == result["exitCode"] == 7
    assert result["findings"][0]["code"] == "ledger.corrupt"
    assert "injected state read denial" not in error_text
    assert state_path.read_bytes() == original_state_bytes
    assert store.read_state() == authority_before

    recovered_code, recovered, _ = invoke("report", "--section", "summary")
    assert recovered_code == 0
    assert recovered["result"]["runId"] == authority_before.currentRunId
    assert state_path.read_bytes() == original_state_bytes
