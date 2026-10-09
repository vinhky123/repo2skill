from __future__ import annotations

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
