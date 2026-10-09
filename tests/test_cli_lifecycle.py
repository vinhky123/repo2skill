from __future__ import annotations

import json
from pathlib import Path

from repo2skill.cli import main
from repo2skill.ledger import LedgerStore
from repo2skill.cli import _exit_code, _finding
from repo2skill.claims import apply_review_dispositions
from repo2skill.validation import validate_claims
from repo2skill.ids import canonical_record_id
from repo2skill.models import ExportJournal, ExportResult
from repo2skill.models import Finding
from datetime import datetime, timezone


def _invoke_json(root: Path, *args: str, config: str | None = None, capsys):
    argv = ["--root", str(root), "--format", "json"]
    if config is not None:
        argv.extend(["--config", config])
    argv.extend(args)
    code = main(argv)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    expected_diagnostics = "".join(
        f"{item['code']}: {item['path'] + ': ' if item.get('path') else ''}{item['message']}\n"
        for item in payload["diagnostics"]
    )
    assert captured.err == expected_diagnostics
    return code, payload


def test_scan_activates_scan_only_without_stage_and_custom_state_config(tmp_path: Path, capsys) -> None:
    root = tmp_path
    code, initialized = _invoke_json(root, "init", "--state-dir", ".cache/r2s", "--git-mode", "no-git", capsys=capsys)
    assert code == 0
    assert initialized["result"]["stateDir"] == ".cache/r2s"

    code, scanned = _invoke_json(
        root, "scan", "--config", ".cache/r2s/project.json", "--git-mode", "no-git", capsys=capsys,
    )
    assert code == 0
    assert scanned["command"] == "scan"
    state = LedgerStore(root, root / ".cache/r2s").read_state()
    assert state is not None and state.currentRunId == scanned["result"]["runId"]
    assert state.activeStageRevision is None
    assert not (root / ".cache/r2s" / "staging" / state.currentRunId).exists()


def test_strict_scan_failure_keeps_current_pointer(tmp_path: Path, capsys) -> None:
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    first_code, first = _invoke_json(tmp_path, "scan", "--git-mode", "no-git", capsys=capsys)
    assert first_code == 0
    code, scanned = _invoke_json(tmp_path, "scan", "--strict", "--git-mode", "no-git", capsys=capsys)
    assert code == 2
    assert scanned["status"] == "incomplete"
    state = LedgerStore(tmp_path, tmp_path / ".repo2skill").read_state()
    assert state is not None and state.currentRunId == first["result"]["runId"] and state.activeStageRevision is None


def test_nonstrict_global_cap_fails_scan_without_advancing_pointer(tmp_path: Path, capsys, monkeypatch) -> None:
    from dataclasses import replace

    import repo2skill.cli as cli
    from repo2skill.models import CoverageEntry, InputSelector
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    initial_code, initial = _invoke_json(tmp_path, "scan", "--git-mode", "no-git", capsys=capsys)
    assert initial_code == 0

    original_collect_scan = cli.collect_scan

    def capped_scan(*args, **kwargs):
        products = original_collect_scan(*args, **kwargs)
        coverage = (*products.run.coverage, CoverageEntry(
            1, InputSelector("file", "optional.txt", ".", False), "generic", "capped", "open-budget", (),
        ))
        return replace(products, run=replace(products.run, coverage=coverage))

    monkeypatch.setattr(cli, "collect_scan", capped_scan)
    code, result = _invoke_json(tmp_path, "scan", "--git-mode", "no-git", capsys=capsys)
    assert code == result["exitCode"] == 2
    assert result["status"] == "incomplete"
    state = LedgerStore(tmp_path, tmp_path / ".repo2skill").read_state()
    assert state is not None and state.currentRunId == initial["result"]["runId"]


def test_generate_from_scan_uses_a_fresh_run_id(tmp_path: Path, capsys) -> None:
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    scan_code, scanned = _invoke_json(tmp_path, "scan", "--git-mode", "no-git", capsys=capsys)
    assert scan_code == 0
    generate_code, generated = _invoke_json(tmp_path, "generate", "--from-run", scanned["result"]["runId"], capsys=capsys)
    assert generate_code == 0
    assert generated["result"]["runId"] != scanned["result"]["runId"]
    state = LedgerStore(tmp_path, tmp_path / ".repo2skill").read_state()
    assert state is not None and state.currentRunId == generated["result"]["runId"]
    assert state.activeStageRevision == 1


def test_from_run_staleness_is_checked_before_claim_derivation(tmp_path: Path, capsys, monkeypatch) -> None:
    from repo2skill import pipeline

    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    _scan_code, scanned = _invoke_json(tmp_path, "scan", "--git-mode", "no-git", capsys=capsys)
    (tmp_path / "README.md").write_text("new documentation\n", encoding="utf-8")

    def unexpected_derivation(*_args, **_kwargs):
        raise AssertionError("claim derivation ran before from-run freshness")

    monkeypatch.setattr(pipeline, "derive_claims", unexpected_derivation)
    code, result = _invoke_json(tmp_path, "generate", "--from-run", scanned["result"]["runId"], capsys=capsys)
    assert code == 4
    assert result["status"] == "stale"
    state = LedgerStore(tmp_path, tmp_path / ".repo2skill").read_state()
    assert state is not None and state.currentRunId == scanned["result"]["runId"]
    assert state.activeStageRevision is None


def test_corrupt_enumerated_history_fails_closed_with_exit_seven(tmp_path: Path, capsys) -> None:
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    with store.mutation():
        store.write_immutable_bytes("runs/not-a-run.json", b"{}\n")
    code, result = _invoke_json(tmp_path, "scan", "--git-mode", "no-git", capsys=capsys)
    assert code == result["exitCode"] == 7
    assert result["findings"][0]["code"] == "ledger.corrupt"
    assert store.read_state().currentRunId is None


def test_exit_priority_is_independent_of_finding_order() -> None:
    findings = (
        _finding("pending", "pending", "pending"),
        _finding("stale", "stale", "stale"),
        _finding("conflict", "conflict", "conflict"),
        _finding("recovery", "incomplete", "recovery", exit_code=7),
    )
    assert _exit_code(findings) == _exit_code(tuple(reversed(findings))) == 7


def test_review_exact_claim_accepts_without_changing_kind_or_origin(tmp_path: Path, capsys) -> None:
    (tmp_path / "package.json").write_text(
        '{"name":"demo","version":"1.0.0","scripts":{"test":"pytest"}}', encoding="utf-8",
    )
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    code, generated = _invoke_json(tmp_path, "generate", capsys=capsys)
    assert code == 3
    run_id = generated["result"]["runId"]
    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    run = store.read_run(run_id)
    pending = next(item for item in run.claims if item.proposalStatus == "pending")

    code, reviewed = _invoke_json(tmp_path, "review", "accept", "--claim", pending.claimId, capsys=capsys)
    assert code == 0
    assert reviewed["result"]["acceptedClaimRevisions"][pending.claimId] == pending.claimRevision
    state = store.read_state()
    assert state is not None and state.activeStageRevision == 2
    effective = next(item for item in apply_review_dispositions(validate_claims(run.claims, run).validClaims, state)
                     if item.claimId == pending.claimId)
    assert effective.proposalStatus == "accepted"
    assert (effective.kind, effective.origin) == (pending.kind, pending.origin)

    next_code, next_generated = _invoke_json(tmp_path, "generate", capsys=capsys)
    assert next_code == 0
    assert next_generated["result"]["claimCounts"]["status"]["accepted"] == len(run.claims)


def test_export_keeps_pending_proposals_while_exporting_accepted_claims(tmp_path: Path, capsys) -> None:
    (tmp_path / "package.json").write_text(
        '{"name":"demo","version":"1.0.0","scripts":{"test":"pytest"}}', encoding="utf-8",
    )
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    code, generated = _invoke_json(tmp_path, "generate", capsys=capsys)
    assert code == 3
    code, exported = _invoke_json(tmp_path, "export", capsys=capsys)
    assert code == 0
    assert exported["result"]["remainingPendingCount"] == 1
    assert (tmp_path / "AGENTS.md").exists()


def test_read_only_commands_and_selected_resume_guard(tmp_path: Path, capsys, monkeypatch) -> None:
    import repo2skill.cli as cli

    (tmp_path / "package.json").write_text('{"name":"demo","version":"1.0.0"}', encoding="utf-8")
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    code, generated = _invoke_json(tmp_path, "generate", capsys=capsys)
    assert code == 0
    run_id = generated["result"]["runId"]
    run = LedgerStore(tmp_path, tmp_path / ".repo2skill").read_run(run_id)
    claim_id = run.claims[0].claimId

    check_code, checked = _invoke_json(tmp_path, "check", capsys=capsys)
    assert check_code == 0
    assert checked["result"]["sourceStatus"] == "fresh"
    explain_code, explained = _invoke_json(tmp_path, "explain", claim_id, capsys=capsys)
    assert explain_code == 0
    assert explained["result"]["runId"] == run_id
    report_code, report = _invoke_json(tmp_path, "report", "--section", "claims", capsys=capsys)
    assert report_code == 0 and "claims" in report["result"]["sections"]
    doctor_code, doctor = _invoke_json(tmp_path, "doctor", capsys=capsys)
    assert doctor_code == 0 and doctor["result"]["codexRequested"] is False

    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    export_id = canonical_record_id("export", {"test": "incomplete"})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    with store.mutation():
        store.write_export_journal(ExportJournal(
            "1.0", "0.1.0", export_id, run_id, 1, (), (), {}, None,
            "incomplete", now, now,
        ))
    blocked_code, blocked = _invoke_json(tmp_path, "scan", "--git-mode", "no-git", capsys=capsys)
    assert blocked_code == 7
    assert blocked["findings"][0]["code"] == "export.recovery-required"
    check_code, checked_with_journal = _invoke_json(tmp_path, "check", capsys=capsys)
    assert check_code == 7
    assert any(item["code"] == "export.recovery-required" for item in checked_with_journal["findings"])

    observed = {}
    def selected_resume(export_id_arg, ownership, **kwargs):
        observed["id"] = export_id_arg
        return ExportResult(export_id_arg, "complete", (), (), (), {})

    monkeypatch.setattr(cli, "resume_export", selected_resume)
    resume_code, resumed = _invoke_json(tmp_path, "export", "--resume", export_id, capsys=capsys)
    assert resume_code == 0
    assert observed["id"] == export_id
    assert resumed["command"] == "export-resume"


def test_final_activation_stale_result_maps_to_exit_four(tmp_path: Path, capsys, monkeypatch) -> None:
    import repo2skill.stage as stage
    from repo2skill.dependencies import DependencyRecheckResult

    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    stale = DependencyRecheckResult(
        "stale", (), (Finding(1, "source.changed", "stale", "source input changed", None, None, (), {}),),
    )
    monkeypatch.setattr(stage, "recheck_dependencies", lambda *_args, **_kwargs: stale)
    code, result = _invoke_json(tmp_path, "generate", capsys=capsys)
    assert code == 4
    assert result["findings"][0]["code"] == "source.changed"
    state = LedgerStore(tmp_path, tmp_path / ".repo2skill").read_state()
    assert state is not None and state.currentRunId is None and state.activeStageRevision is None


def test_busy_lock_maps_to_exit_one_without_pointer_change(tmp_path: Path, capsys, monkeypatch) -> None:
    import repo2skill.cli as cli
    from repo2skill.ledger import LockBusyError

    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    before = store.read_state()

    class BusyStore(LedgerStore):
        def mutation(self):
            raise LockBusyError("busy")

    monkeypatch.setattr(cli, "LedgerStore", BusyStore)
    code, result = _invoke_json(tmp_path, "scan", "--git-mode", "no-git", capsys=capsys)
    assert code == 1
    assert result["findings"][0]["code"] == "state.lock-busy"
    assert store.read_state() == before


def test_explicit_init_config_mismatch_fails_before_state_creation(tmp_path: Path, capsys) -> None:
    code, result = _invoke_json(
        tmp_path, "init", "--state-dir", ".cache/state", "--git-mode", "no-git",
        config=".other/project.json", capsys=capsys,
    )
    assert code == 1
    assert result["findings"][0]["code"] == "state.invalid"
    assert not (tmp_path / ".cache/state").exists()


def test_internal_read_corruption_uses_exit_seven(tmp_path: Path, capsys) -> None:
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    state_path = tmp_path / ".repo2skill" / "state.json"
    state_path.write_text("{broken", encoding="utf-8")
    code, checked = _invoke_json(tmp_path, "check", capsys=capsys)
    assert code == checked["exitCode"] == 7
    assert checked["findings"][0]["code"] == "ledger.corrupt"


def test_corrupt_selected_run_uses_exit_seven(tmp_path: Path, capsys) -> None:
    (tmp_path / "package.json").write_text('{"name":"demo","version":"1.0.0"}', encoding="utf-8")
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    _code, generated = _invoke_json(tmp_path, "generate", capsys=capsys)
    run_id = generated["result"]["runId"]
    (tmp_path / ".repo2skill" / "runs" / f"{run_id}.json").write_text("{broken", encoding="utf-8")
    code, reported = _invoke_json(tmp_path, "report", "--run", run_id, capsys=capsys)
    assert code == reported["exitCode"] == 7
    assert reported["findings"][0]["code"] == "ledger.corrupt"


def test_corrupt_project_document_uses_exit_seven(tmp_path: Path, capsys) -> None:
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    (tmp_path / ".repo2skill" / "project.json").write_text("{broken", encoding="utf-8")
    code, result = _invoke_json(tmp_path, "scan", capsys=capsys)
    assert code == result["exitCode"] == 7
    assert result["findings"][0]["code"] == "ledger.corrupt"


def test_review_mutation_acquires_lock_before_reading_state_or_run(tmp_path: Path, capsys, monkeypatch) -> None:
    import repo2skill.cli as cli
    from repo2skill.ledger import LockBusyError

    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)

    class BusyStore(LedgerStore):
        def mutation(self):
            raise LockBusyError("busy")

        def read_state(self):
            raise AssertionError("mutation read state before acquiring lock")

        def read_run(self, _run_id):
            raise AssertionError("mutation read selected run before acquiring lock")

    monkeypatch.setattr(cli, "LedgerStore", BusyStore)
    code, result = _invoke_json(tmp_path, "review", "accept", "--claim", "claim_missing", "--run", "run_missing", capsys=capsys)
    assert code == 1
    assert result["findings"][0]["code"] == "state.lock-busy"


def test_export_incomplete_result_requires_exit_seven(tmp_path: Path, capsys, monkeypatch) -> None:
    import repo2skill.cli as cli

    (tmp_path / "package.json").write_text('{"name":"demo","version":"1.0.0"}', encoding="utf-8")
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    _code, generated = _invoke_json(tmp_path, "generate", capsys=capsys)
    export_id = canonical_record_id("export", {"incomplete": generated["result"]["runId"]})
    monkeypatch.setattr(cli, "export_staged", lambda *_args, **_kwargs: ExportResult(export_id, "incomplete", (), (), (), {}))
    code, result = _invoke_json(tmp_path, "export", capsys=capsys)
    assert code == result["exitCode"] == 7
    assert result["findings"][0]["code"] == "export.incomplete"


def test_doctor_reports_read_only_environment_checks(tmp_path: Path, capsys) -> None:
    import os

    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    state_path = tmp_path / ".repo2skill" / "state.json"
    state_before = state_path.read_bytes()
    files_before = sorted(item.relative_to(tmp_path).as_posix() for item in tmp_path.rglob("*"))
    code, result = _invoke_json(tmp_path, "doctor", capsys=capsys)
    files_after = sorted(item.relative_to(tmp_path).as_posix() for item in tmp_path.rglob("*"))
    checks = result["result"]["checks"]
    assert code == 0
    assert checks["statePathConfined"] is True
    assert checks["outputPathsConfined"] is True
    assert checks["schemaReadable"] is True
    assert isinstance(checks["gitAvailable"], bool)
    assert isinstance(checks["filesystemPermissions"]["stateReadable"], bool)
    assert isinstance(checks["filesystemPermissions"]["outputWritable"], bool)
    assert files_after == files_before
    assert state_path.read_bytes() == state_before
    assert os.path.exists(tmp_path / ".repo2skill")


def test_rejected_valid_claim_remains_in_dedup_provenance(tmp_path: Path, capsys) -> None:
    (tmp_path / "package.json").write_text(
        '{"name":"demo","version":"1.0.0","scripts":{"test":"pytest"}}', encoding="utf-8",
    )
    _invoke_json(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    first_code, first = _invoke_json(tmp_path, "generate", capsys=capsys)
    assert first_code == 3
    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    first_run = store.read_run(first["result"]["runId"])
    rejected = next(item for item in first_run.claims if item.proposalStatus == "pending")
    reject_code, _ = _invoke_json(
        tmp_path, "review", "reject", "--claim", rejected.claimId, "--reason", "not applicable", capsys=capsys,
    )
    assert reject_code in {0, 3}

    _next_code, generated = _invoke_json(tmp_path, "generate", capsys=capsys)
    next_run = store.read_run(generated["result"]["runId"])
    effective = next(item for item in next_run.claims if item.claimId == rejected.claimId)
    assert effective.proposalStatus == "rejected"
    assert any(rejected.claimId in decision.candidateClaimIds for decision in next_run.dedupDecisions)
