from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import socket

from repo2skill.cli import main


FIXTURE = Path(__file__).parents[1] / "fixtures" / "alpha-mixed"
TRAP = "ALPHA_FIXTURE_EXECUTED"
MANUAL_PREFIX = b"Maintainer-owned introduction.\r\n\r\n"


def _invoke(root: Path, *args: str, capsys):
    code = main(["--root", str(root), "--format", "json", *args])
    captured = capsys.readouterr()
    assert captured.out.endswith("\n")
    assert captured.out.count("\n") == 1
    payload = json.loads(captured.out)
    expected_diagnostics = "".join(
        f"{item['code']}: {item['path'] + ': ' if item.get('path') else ''}{item['message']}\n"
        for item in payload["diagnostics"]
    )
    assert captured.err == expected_diagnostics
    assert payload["exitCode"] == code
    return code, payload


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _accept_pending(root: Path, run_id: str, *, capsys) -> None:
    code, listed = _invoke(root, "review", "list", "--run", run_id, capsys=capsys)
    assert code in {0, 3}
    pending = [claim["claimId"] for claim in listed["result"]["claims"]
               if claim["proposalStatus"] == "pending"]
    if pending:
        arguments = [part for claim_id in pending for part in ("--claim", claim_id)]
        code, accepted = _invoke(
            root, "review", "accept", *arguments, "--run", run_id, capsys=capsys,
        )
        assert code in {0, 3}
        assert accepted["result"]["remainingPendingCount"] == 0


def _generate_and_export(root: Path, *, capsys) -> tuple[str, dict, dict]:
    code, generated = _invoke(root, "generate", "--no-ai", "--git-mode", "no-git", capsys=capsys)
    assert code in {0, 3}, generated
    run_id = generated["result"]["runId"]
    _accept_pending(root, run_id, capsys=capsys)
    code, exported = _invoke(root, "export", capsys=capsys)
    assert code == 0, exported
    return run_id, generated, exported


def _claim_for_semantic_key(root: Path, run_id: str, prefix: str, *, capsys) -> dict:
    code, listed = _invoke(
        root, "review", "list", "--run", run_id, "--status", "accepted", capsys=capsys,
    )
    assert code == 0
    return next(claim for claim in listed["result"]["claims"]
                if claim["semanticKey"].startswith(prefix))


def _explain(root: Path, run_id: str, claim_id: str, *, capsys) -> dict:
    code, result = _invoke(root, "explain", claim_id, "--run", run_id, capsys=capsys)
    assert code == 0, result
    return {**result["result"], "diagnostics": result["diagnostics"]}


def _assert_staged_and_live_spans(explanation: dict, claim: dict, exported: dict) -> None:
    claim_ref = f"{claim['claimId']}@{claim['claimRevision']}"
    staged = [item for item in explanation["diagnostics"]
              if item["code"] == "explain.stage-location.v1"
              and item["details"]["claimRevisionRef"] == claim_ref
              and item["details"]["mode"] == "accepted"]
    live = [item for item in explanation["diagnostics"]
            if item["code"] == "explain.live-location.v1"
            and item["details"]["claimRevisionRef"] == claim_ref]
    expected_targets = {"agents-md", "skill"}
    assert {item["details"]["targetId"] for item in staged} == expected_targets
    assert {item["details"]["targetId"] for item in live} == expected_targets
    assert "AGENTS.md" in explanation["stagedLocations"]
    assert "AGENTS.md" in explanation["liveLocations"]
    assert any(path.endswith("/SKILL.md") for path in explanation["stagedLocations"])
    assert any(path.endswith("/SKILL.md") for path in explanation["liveLocations"])

    for location in (*staged, *live):
        start, end = location["details"]["lineSpans"][0]
        assert start >= 1 and end >= start
    targets = exported["result"]["targets"]
    assert set(targets) == expected_targets
    for target in targets.values():
        assert any(claim_ref in item["exportedClaimRevisions"]
                   for item in target["paths"].values())


def _content_hash(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _outside_managed_block(data: bytes) -> tuple[bytes, bytes]:
    begin = data.index(b"<!-- repo2skill:begin ")
    end_start = data.index(b"<!-- repo2skill:end ", begin)
    end = data.index(b"\r\n", end_start) + 2
    return data[:begin], data[end:]


def _insert_manual_edit(data: bytes) -> bytes:
    end_start = data.index(b"<!-- repo2skill:end ")
    return data[:end_start] + b"Manual maintainer edit.\r\n" + data[end_start:]


def _edit_package_test(root: Path, body: str) -> None:
    package_path = root / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    package["scripts"]["test"] = body
    package_path.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")


def test_no_ai_alpha_preserves_provenance_and_owned_spans_across_updates(tmp_path: Path, capsys, monkeypatch) -> None:
    def deny_network(*_args, **_kwargs):
        raise AssertionError("no-AI repository operations must not access the network")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)

    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_bytes(MANUAL_PREFIX + b"Maintainer-owned closing note.\r\n")

    code, initialized = _invoke(tmp_path, "init", "--git-mode", "no-git", capsys=capsys)
    assert code == 0, initialized

    run1, _generated1, exported1 = _generate_and_export(tmp_path, capsys=capsys)
    script_claim1 = _claim_for_semantic_key(
        tmp_path, run1, "task.definition:package-script.v1:repo-root:test", capsys=capsys,
    )
    script_explanation1 = _explain(tmp_path, run1, script_claim1["claimId"], capsys=capsys)
    script_fact1 = next(fact for fact in script_explanation1["facts"]
                        if fact["factType"] == "task.definition" and fact["value"].get("name") == "test")
    script_evidence1 = next(evidence for evidence in script_explanation1["evidence"]
                            if evidence["evidenceId"] in script_fact1["evidenceIds"])
    package_bytes1 = (tmp_path / "package.json").read_bytes()
    assert script_evidence1["path"] == "package.json"
    assert script_evidence1["contentHash"] == _content_hash(package_bytes1)
    assert script_evidence1["locator"] == {"kind": "json-pointer", "value": "/scripts/test", "lineHint": None}
    assert script_claim1["factIds"] == [script_fact1["factId"]]
    assert script_evidence1["evidenceId"] in script_claim1["evidenceIds"]
    _assert_staged_and_live_spans(script_explanation1, script_claim1, exported1)

    agents1 = agents_path.read_bytes()
    prefix1, suffix1 = _outside_managed_block(agents1)
    assert prefix1.startswith(MANUAL_PREFIX)
    assert b"Maintainer-owned closing note.\r\n" in prefix1 + suffix1
    assert b"\n" not in agents1.replace(b"\r\n", b"")
    skill_paths1 = [path for target in exported1["result"]["targets"].values()
                    for path in target["paths"] if path.endswith("/SKILL.md")]
    assert len(skill_paths1) == 1
    skill_path = tmp_path.joinpath(*skill_paths1[0].split("/"))
    provenance_path = skill_path.with_name("repo2skill-provenance.json")
    assert skill_path.is_file() and provenance_path.is_file()

    before_readonly = _tree_bytes(tmp_path)
    code, checked = _invoke(tmp_path, "check", capsys=capsys)
    assert code == 0, checked
    assert checked["result"]["sourceStatus"] == "fresh"
    assert _tree_bytes(tmp_path) == before_readonly
    _explain(tmp_path, run1, script_claim1["claimId"], capsys=capsys)
    assert _tree_bytes(tmp_path) == before_readonly

    old_agents = agents_path.read_bytes()
    edited_agents = _insert_manual_edit(old_agents)
    agents_path.write_bytes(edited_agents)
    code, conflicted = _invoke(tmp_path, "export", capsys=capsys)
    assert code == 5, conflicted
    assert agents_path.read_bytes() == edited_agents
    agents_path.write_bytes(old_agents)

    _edit_package_test(
        tmp_path,
        "python -c \"from pathlib import Path; Path('ALPHA_FIXTURE_EXECUTED').write_text('changed')\"",
    )
    run2, _generated2, exported2 = _generate_and_export(tmp_path, capsys=capsys)
    script_claim2 = _claim_for_semantic_key(
        tmp_path, run2, "task.definition:package-script.v1:repo-root:test", capsys=capsys,
    )
    script_explanation2 = _explain(tmp_path, run2, script_claim2["claimId"], capsys=capsys)
    script_fact2 = next(fact for fact in script_explanation2["facts"]
                        if fact["factType"] == "task.definition" and fact["value"].get("name") == "test")
    script_evidence2 = next(evidence for evidence in script_explanation2["evidence"]
                            if evidence["evidenceId"] in script_fact2["evidenceIds"])
    assert script_claim2["claimId"] == script_claim1["claimId"]
    assert script_claim2["claimRevision"] == script_claim1["claimRevision"] + 1
    assert script_fact2["factId"] == script_fact1["factId"]
    assert script_fact2["factRevision"] == script_fact1["factRevision"] + 1
    assert script_evidence2["evidenceId"] == script_evidence1["evidenceId"]
    assert script_evidence2["revision"] == script_evidence1["revision"] + 1
    assert script_evidence2["contentHash"] == _content_hash((tmp_path / "package.json").read_bytes())
    assert script_evidence2["contentHash"] != script_evidence1["contentHash"]
    _assert_staged_and_live_spans(script_explanation2, script_claim2, exported2)
    prefix2, suffix2 = _outside_managed_block(agents_path.read_bytes())
    assert (prefix2, suffix2) == (prefix1, suffix1)

    convention_claim1 = _claim_for_semantic_key(
        tmp_path, run2, "artifact.presence:convention.v1:http-client-wrapper", capsys=capsys,
    )
    convention_explanation1 = _explain(tmp_path, run2, convention_claim1["claimId"], capsys=capsys)
    convention_fact1 = next(fact for fact in convention_explanation1["facts"]
                            if fact["factType"] == "artifact.presence"
                            and fact["value"].get("name") == "http-client-wrapper")
    (tmp_path / "src" / "api_client.ts").write_text(
        'export const apiClient = () => fetch("/synthetic");\n', encoding="utf-8",
    )
    run3, _generated3, exported3 = _generate_and_export(tmp_path, capsys=capsys)
    convention_claim2 = _claim_for_semantic_key(
        tmp_path, run3, "artifact.presence:convention.v1:http-client-wrapper", capsys=capsys,
    )
    convention_explanation2 = _explain(tmp_path, run3, convention_claim2["claimId"], capsys=capsys)
    convention_fact2 = next(fact for fact in convention_explanation2["facts"]
                            if fact["factType"] == "artifact.presence"
                            and fact["value"].get("name") == "http-client-wrapper")
    convention_evidence2 = next(evidence for evidence in convention_explanation2["evidence"]
                                if evidence["evidenceId"] in convention_fact2["evidenceIds"])
    assert convention_claim2["claimId"] == convention_claim1["claimId"]
    assert convention_claim2["claimRevision"] == convention_claim1["claimRevision"] + 1
    assert convention_fact2["factRevision"] == convention_fact1["factRevision"] + 1
    assert convention_evidence2["path"] == "src/api_client.ts"
    _assert_staged_and_live_spans(convention_explanation2, convention_claim2, exported3)

    (tmp_path / "src" / "api_client.ts").unlink()
    (tmp_path / "src" / "client.ts").unlink()
    run4, _generated4, exported4 = _generate_and_export(tmp_path, capsys=capsys)
    run4_claims_code, run4_claims = _invoke(
        tmp_path, "review", "list", "--run", run4, capsys=capsys,
    )
    assert run4_claims_code == 0
    assert all(claim["claimId"] != convention_claim2["claimId"]
               for claim in run4_claims["result"]["claims"])
    for target in exported4["result"]["targets"].values():
        assert all(not reference.startswith(convention_claim2["claimId"] + "@")
                   for item in target["paths"].values()
                   for reference in item["exportedClaimRevisions"])
    assert skill_path.is_file() and provenance_path.is_file()
    deleted_source_path = convention_claim2["payload"]["path"]
    assert deleted_source_path.encode("utf-8") not in agents_path.read_bytes()
    assert deleted_source_path.encode("utf-8") not in skill_path.read_bytes()
    current_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert convention_claim2["claimId"] not in {
        item["claimId"] for item in current_provenance["claims"]
    }
    assert current_provenance["claims"]
    remaining_refs = [reference for target in exported4["result"]["targets"].values()
                      for item in target["paths"].values()
                      for reference in item["exportedClaimRevisions"]]
    assert remaining_refs
    assert all(not reference.startswith(convention_claim2["claimId"] + "@")
               for reference in remaining_refs)
    assert not (tmp_path / TRAP).exists()
