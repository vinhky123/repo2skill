from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

import pytest

from repo2skill.maintenance_agent import AgentRequest, invoke_agent, probe_agent, validate_operations
from repo2skill.maintenance_models import ClaimOperation


HELP = Path(__file__).parents[1] / "fixtures" / "maintenance" / "codex" / "complete-help.txt"


def _fake_cli(path: Path, log: Path, *, mode: str = "ok") -> Path:
    path.write_text(
        "import json, os, pathlib, sys\n"
        "args=sys.argv[1:]\n"
        "if args == ['--version']: print('codex-cli 0.162.0-alpha.2'); raise SystemExit()\n"
        "if args == ['exec','--help']: print(pathlib.Path(" + repr(str(HELP)) + ").read_text()); raise SystemExit()\n"
        "prompt=sys.stdin.buffer.read()\n"
        "pathlib.Path(" + repr(str(log)) + ").write_text(json.dumps({'argv':args,'cwd':os.getcwd(),'prompt':prompt.decode(),'schema':pathlib.Path(args[args.index('--output-schema')+1]).read_bytes().decode()}))\n"
        f"mode={mode!r}\n"
        "if mode == 'timeout': import time; time.sleep(3)\n"
        "if mode == 'nonzero': raise SystemExit(9)\n"
        "if mode == 'missing-output': raise SystemExit(0)\n"
        "out=pathlib.Path(args[args.index('--output-last-message')+1])\n"
        "if mode == 'output-cap': out.write_bytes(b'x' * (1024*1024+1)); raise SystemExit(0)\n"
        "out.write_bytes(b'{\"jobId\":\"job\",\"snapshotId\":\"snap\",\"operations\":[]}')\n",
        encoding="utf-8",
    )
    return path


def test_invoke_agent_uses_bounded_structured_stdin_schema_output_and_clean_cwd(tmp_path: Path) -> None:
    log = tmp_path / "invocation.json"
    executable = _fake_cli(tmp_path / "codex.py", log)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    request = AgentRequest("job", snapshot, b"exact prompt\x00", b'{"type":"object"}', "")
    request = AgentRequest(request.jobId, request.snapshotRoot, request.promptBytes, request.schemaBytes,
                           "sha256:" + hashlib.sha256(b"job\0" + request.promptBytes + b"\0" + request.schemaBytes).hexdigest(), tmp_path)

    response = invoke_agent(probe_agent(executable), request, 2)

    record = json.loads(log.read_text())
    assert record["prompt"] == "exact prompt\u0000"
    assert record["schema"] == '{"type":"object"}'
    assert record["argv"][:2] == ["exec", "--ephemeral"]
    assert record["argv"][-1] == "-"
    assert Path(record["cwd"]).resolve() != snapshot.resolve()
    assert not (Path(record["cwd"]) / "AGENTS.md").exists()
    assert response.status == "ok"
    assert response.outputBytes == b'{"jobId":"job","snapshotId":"snap","operations":[]}'


def test_invocation_refuses_temp_base_inside_target_source_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tempfile
    from repo2skill.maintenance_agent import request_hash
    source_root = tmp_path / "target-repo"
    temp_root = source_root / "tmp"
    snapshot = tmp_path / "snapshot"
    temp_root.mkdir(parents=True)
    snapshot.mkdir()
    executable = _fake_cli(tmp_path / "codex.py", tmp_path / "invocation.json")
    capability = probe_agent(executable)
    request = AgentRequest("job", snapshot, b"prompt", b"{}", request_hash("job", b"prompt", b"{}"), sourceRoot=source_root)
    monkeypatch.setenv("TMP", str(temp_root))
    monkeypatch.setenv("TEMP", str(temp_root))
    monkeypatch.setattr(tempfile, "tempdir", None)
    response = invoke_agent(capability, request, 2)
    assert response.status == "unavailable"
    assert "outside" in (response.diagnostic or "")
    assert not (tmp_path / "invocation.json").exists()


def test_invoke_requires_verified_source_root_before_creating_temp_workspace(tmp_path: Path) -> None:
    from repo2skill.maintenance_agent import request_hash
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    executable = _fake_cli(tmp_path / "codex.py", tmp_path / "invocation.json")
    request = AgentRequest("job", snapshot, b"prompt", b"{}", request_hash("job", b"prompt", b"{}"))
    response = invoke_agent(probe_agent(executable), request, 2)
    assert response.status == "invalid-request"
    assert "source root" in (response.diagnostic or "")
    assert not (tmp_path / "invocation.json").exists()


def test_validate_operations_accepts_an_exactly_supported_operation_and_noop() -> None:
    from types import SimpleNamespace

    context = SimpleNamespace(
        job=SimpleNamespace(jobId="job"),
        snapshot=SimpleNamespace(snapshotId="snap"),
        facts=(SimpleNamespace(factId="fact_a", snapshotId="snap", scope="global", factType="source.fragment.v1", evidenceIds=("evidence_a",)),),
        evidence=(SimpleNamespace(evidenceId="evidence_a", snapshotId="snap"),),
        priorClaims=(),
    )
    raw = {"jobId": "job", "snapshotId": "snap", "operations": [{
        "op": "add", "semanticKey": "source.fragment.v1:test", "scope": "global",
        "renderedText": "Keep tests deterministic.", "factIds": ["fact_a"],
        "evidenceIds": ["evidence_a"], "claimRef": None,
    }]}
    response = SimpleNamespace(status="ok", outputBytes=json.dumps(raw).encode(), diagnostic=None)
    operations = validate_operations(response, context)
    assert operations == (ClaimOperation("add", "source.fragment.v1:test", "global", "Keep tests deterministic.", ("fact_a",), ("evidence_a",), None),)
    raw["operations"] = []
    response = SimpleNamespace(status="ok", outputBytes=json.dumps(raw).encode(), diagnostic=None)
    assert validate_operations(response, context) == ()


@pytest.mark.parametrize("mutation", ["wrong-job", "wrong-snapshot", "unknown-fact", "unknown-evidence", "hallucinated-scope", "duplicate", "duplicate-json", "partial-json", "mixed-forgery", "altered-observed-content"])
def test_validate_operations_rejects_entire_invalid_transaction(mutation: str) -> None:
    from types import SimpleNamespace

    context = SimpleNamespace(job=SimpleNamespace(jobId="job"), snapshot=SimpleNamespace(snapshotId="snap"),
        facts=(SimpleNamespace(factId="fact_a", snapshotId="snap", scope="global", factType="source.fragment.v1", evidenceIds=("evidence_a",)),),
        evidence=(SimpleNamespace(evidenceId="evidence_a", snapshotId="snap"),), priorClaims=())
    operation = {"op":"add","semanticKey":"source.fragment.v1:test","scope":"global","renderedText":"Text.","factIds":["fact_a"],"evidenceIds":["evidence_a"],"claimRef":None}
    raw = {"jobId":"job","snapshotId":"snap","operations":[operation]}
    if mutation == "wrong-job": raw["jobId"] = "other"
    if mutation == "wrong-snapshot": raw["snapshotId"] = "other"
    if mutation == "unknown-fact": raw["operations"][0]["factIds"] = ["fact_forged"]
    if mutation == "unknown-evidence": raw["operations"][0]["evidenceIds"] = ["evidence_forged"]
    if mutation == "hallucinated-scope": raw["operations"][0]["scope"] = "src/forged.py"
    if mutation == "duplicate": raw["operations"] = [operation, operation]
    if mutation == "mixed-forgery": raw["operations"] = [operation, {**operation, "evidenceIds": ["evidence_forged"]}]
    if mutation == "altered-observed-content": raw["facts"] = [{"factId": "fact_a", "value": "altered"}]
    output = (b'{"jobId":"job","jobId":"job","snapshotId":"snap","operations":[]}'
              if mutation == "duplicate-json" else json.dumps(raw).encode() + (b" trailing" if mutation == "partial-json" else b""))
    with pytest.raises(ValueError):
        validate_operations(SimpleNamespace(status="ok", outputBytes=output, diagnostic=None), context)


def test_update_and_delete_for_same_semantic_claim_are_one_invalid_transaction() -> None:
    from types import SimpleNamespace
    context = SimpleNamespace(
        job=SimpleNamespace(jobId="job"), snapshot=SimpleNamespace(snapshotId="snap"),
        facts=(SimpleNamespace(factId="fact_a", snapshotId="snap", scope="global", factType="source.fragment.v1", evidenceIds=("evidence_a",)),),
        evidence=(SimpleNamespace(evidenceId="evidence_a", snapshotId="snap"),),
        priorClaims=(SimpleNamespace(claimId="claim_a", claimRevision=1, scope="global", semanticKey="source.fragment.v1:test"),),
    )
    update = {"op":"update", "semanticKey":"source.fragment.v1:test", "scope":"global",
        "renderedText":"Updated wording.", "factIds":["fact_a"], "evidenceIds":["evidence_a"], "claimRef":"claim_a@1"}
    delete = {"op":"delete", "semanticKey":"source.fragment.v1:test", "scope":"global",
        "renderedText":None, "factIds":[], "evidenceIds":[], "claimRef":"claim_a@1"}
    response = SimpleNamespace(status="ok", outputBytes=json.dumps({"jobId":"job", "snapshotId":"snap", "operations":[update, delete]}).encode(), diagnostic=None)
    with pytest.raises(ValueError, match="duplicate"):
        validate_operations(response, context)


@pytest.mark.parametrize(("mode", "expected"), [("timeout", "timeout"), ("nonzero", "nonzero-exit"),
                                                   ("missing-output", "missing-output"), ("output-cap", "output-cap")])
def test_invocation_failures_are_bounded_and_typed(tmp_path: Path, mode: str, expected: str) -> None:
    from repo2skill.maintenance_agent import OUTPUT_MAX_BYTES, request_hash
    executable = tmp_path / "codex.py"
    log = tmp_path / "calls.json"
    _fake_cli(executable, log, mode=mode)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    prompt, schema = b"p", b"{}"
    request = AgentRequest("job", snapshot, prompt, schema, request_hash("job", prompt, schema), tmp_path)
    # Keep the cap test fast while exercising the same bounded output path.
    if mode == "output-cap":
        import repo2skill.maintenance_agent as agent
        agent.OUTPUT_MAX_BYTES = 64
    try:
        response = invoke_agent(probe_agent(executable), request, 0.3 if mode == "timeout" else 2)
    finally:
        if mode == "output-cap":
            agent.OUTPUT_MAX_BYTES = OUTPUT_MAX_BYTES
    assert response.status == expected
    assert response.outputBytes == b""
    assert response.diagnostic


def test_request_hash_mismatch_fails_before_starting_cli(tmp_path: Path) -> None:
    executable = _fake_cli(tmp_path / "codex.py", tmp_path / "calls.json")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    response = invoke_agent(probe_agent(executable), AgentRequest("job", snapshot, b"p", b"{}", "sha256:" + "0" * 64), 1)
    assert response.status == "invalid-request"
    assert not (tmp_path / "calls.json").exists()


def test_over_cap_request_is_rejected_before_cli_invocation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import repo2skill.maintenance_agent as agent
    executable = _fake_cli(tmp_path / "codex.py", tmp_path / "calls.json")
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    monkeypatch.setattr(agent, "PROMPT_MAX_BYTES", 2)
    request = AgentRequest("job", snapshot, b"oversize", b"{}", agent.request_hash("job", b"oversize", b"{}"), tmp_path)
    response = invoke_agent(probe_agent(executable), request, 1)
    assert response.status == "input-cap"
    assert not (tmp_path / "calls.json").exists()


def test_build_agent_request_frames_bounded_committed_context(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from repo2skill.maintenance_context import build_agent_request
    context = SimpleNamespace(
        job=SimpleNamespace(jobId="job"), snapshot=SimpleNamespace(snapshotId="snap"),
        sourceContextHash="sha256:" + "a" * 64, changedDiff=b"diff\x00bytes",
        facts=(), evidence=(), priorClaims=(),
    )
    request = build_agent_request(context, tmp_path, source_root=tmp_path)
    prompt = json.loads(request.promptBytes)
    assert prompt["jobId"] == "job"
    assert prompt["snapshotId"] == "snap"
    assert prompt["changedDiffBase64"] == "ZGlmZgBieXRlcw=="
    assert request.snapshotRoot == tmp_path
    assert request.sourceRoot == tmp_path
    assert len(request.promptBytes) <= 1024 * 1024
    assert len(request.schemaBytes) <= 128 * 1024
    assert request.requestHash.startswith("sha256:")


def test_default_discovery_excludes_relative_path_and_repository_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import repo2skill.maintenance_agent as agent

    source_root = tmp_path / "source"
    source_root.mkdir()
    local_shadow = source_root / "codex.exe"
    local_shadow.write_bytes(b"must not execute")
    original = agent.shutil.which
    seen: list[str] = []
    def record_path(name: str, *, path: str | None = None) -> str | None:
        seen.append(path or "")
        return str(local_shadow) if name == "codex" else original(name, path=path)
    monkeypatch.setattr(agent.shutil, "which", record_path)
    monkeypatch.setenv("PATH", "." + os.pathsep + str(source_root))
    try:
        result = probe_agent(None, source_root=source_root)
    finally:
        local_shadow.unlink(missing_ok=True)
    assert result.supported is False
    assert result.executable is None
    assert seen and all("." not in entry.split(os.pathsep) for entry in seen)


def test_invoking_crash_recovers_as_unknown_and_retry_appends_child(tmp_path: Path) -> None:
    import dataclasses

    from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
    from repo2skill.ledger import LedgerStore
    from repo2skill.maintenance_attempts import begin_invocation, fail_attempt, prepare_attempt, persist_response, read_response, recover_attempt
    from repo2skill.maintenance_models import MaintenanceJob, MaintenanceLifecycle, MaintenancePolicy, MaintenanceState
    from repo2skill.maintenance_store import MaintenanceStore
    from repo2skill.models import ProjectDocument, ProjectState

    root = tmp_path / "repo"
    root.mkdir()
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project_id = "project_0123456789abcdef01234567"
    job_id = "maintenance-job_0123456789abcdef01234567"
    binding = "sha256:" + "a" * 64
    project = ProjectDocument("1.0", "0.1.0", project_id, "fixture", ".repo2skill", "worktree",
                               default_scan_policy(), default_claim_policy(), default_targets(root))
    pstate = ProjectState("1.0", "0.1.0", project_id, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(pstate)), encoding="utf-8")
    ledger = LedgerStore(root, state_dir)
    maintenance = MaintenanceStore(root, ledger)
    job = MaintenanceJob(job_id, "a" * 40, "b" * 40, ("git-event_0123456789abcdef01234567",),
                         ("src/app.py",), ("skill",), "sha256:" + "1" * 64,
                         "sha256:" + "2" * 64, "policy-v1", ("skill",), None, {})
    lifecycle = MaintenanceLifecycle("approved", "user", binding, "pending", None, None, None)
    state = MaintenanceState("1.0", None, "a" * 40, MaintenancePolicy("ask", 1, ("skill",)), (), {job_id: lifecycle}, None)
    with ledger.mutation():
        maintenance.write_job(job)
        maintenance.replace_state(state)

    first = prepare_attempt(ledger, job_id, "sha256:" + "3" * 64)
    begin_invocation(ledger, job_id, first.attemptId)
    with ledger.mutation():
        ledger.write_immutable_bytes(f"maintenance/attempts/{first.attemptId}/response.bin", b"orphaned completed response")
        prepared_bytes = ledger.read_immutable_bytes(f"maintenance/attempts/{first.attemptId}/prepared.json")
        ledger.write_immutable(f"maintenance/attempts/{first.attemptId}/unknown.json", json.loads(prepared_bytes.decode("utf-8")))
    recovered = recover_attempt(ledger, job_id)
    assert recovered.phase == "unknown"
    assert maintenance.read_state().jobs[job_id].status == "failed"
    assert maintenance.read_state().runningJobId is None
    unknown_path = state_dir / "maintenance" / "attempts" / first.attemptId / "unknown.json"
    original_unknown = json.loads(unknown_path.read_text(encoding="utf-8"))
    for field, forged_value in (("jobId", "other-job_0123456789abcdef01234567"), ("requestHash", "sha256:" + "8" * 64),
                               ("approvalBindingHash", "sha256:" + "9" * 64),
                               ("retryOfAttemptId", "maintenance-attempt_0123456789abcdef01234567")):
        unknown_path.write_text(json.dumps({**original_unknown, field: forged_value}), encoding="utf-8")
        with pytest.raises(ValueError, match="attempt"):
            recover_attempt(ledger, job_id)
        unknown_path.write_text(json.dumps(original_unknown), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit retry"):
        prepare_attempt(ledger, job_id, "sha256:" + "4" * 64)

    with ledger.mutation():
        current = maintenance.read_state()
        jobs = dict(current.jobs)
        jobs[job_id] = dataclasses.replace(jobs[job_id], status="approved", failureCode=None)
        maintenance.replace_state(dataclasses.replace(current, jobs=jobs))
    second = prepare_attempt(ledger, job_id, "sha256:" + "4" * 64, explicit_retry=True)
    assert second.retryOfAttemptId == first.attemptId
    assert second.attemptId != first.attemptId
    assert maintenance.read_state().jobs[job_id].activeAttemptId == second.attemptId
    begin_invocation(ledger, job_id, second.attemptId)
    saved = persist_response(ledger, job_id, second.attemptId, status="ok", output_bytes=b"exact response", diagnostic="stderr")
    assert saved.phase == "responded"
    metadata_path = state_dir / "maintenance" / "attempts" / second.attemptId / "responded.json"
    original_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    for field, forged_value in (("jobId", "other-job_0123456789abcdef01234567"), ("requestHash", "sha256:" + "8" * 64),
                                ("approvalBindingHash", "sha256:" + "9" * 64),
                                ("retryOfAttemptId", "maintenance-attempt_0123456789abcdef01234567"), ("status", "invented"), ("status", ["ok"]),
                                ("exitCode", "zero"), ("diagnostic", {"unexpected": True}),
                                ("outputLength", 999), ("outputHash", "sha256:" + "b" * 64)):
        forged_metadata = {**original_metadata, field: forged_value}
        metadata_path.write_text(json.dumps(forged_metadata), encoding="utf-8")
        with pytest.raises(ValueError, match="attempt|response"):
            read_response(ledger, second.attemptId)
        metadata_path.write_text(json.dumps(original_metadata), encoding="utf-8")
    response = read_response(ledger, second.attemptId)
    assert response.status == "ok"
    assert response.outputBytes == b"exact response"
    assert response.diagnostic == "stderr"
    fail_attempt(ledger, job_id, second.attemptId, "maintenance.agent-validation-failed")
    assert maintenance.read_state().jobs[job_id].status == "failed"
    response_path = state_dir / "maintenance" / "attempts" / second.attemptId / "response.bin"
    response_path.unlink()
    response_path.write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds byte limit"):
        read_response(ledger, second.attemptId)
