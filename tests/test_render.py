from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from repo2skill.config import default_claim_policy
from repo2skill.ids import canonical_record_id
from repo2skill.models import Claim, TargetSpec
from repo2skill.render import parse_managed_markers, render_targets


PROJECT = "project_0123456789abcdef01234567"
SNAPSHOT = "snapshot_0123456789abcdef01234567"


def _claim(name: str, status: str, *, kind: str = "observed") -> Claim:
    claim_id = canonical_record_id("claim", {"name": name})
    fact_id = canonical_record_id("fact", {"name": name})
    evidence_id = canonical_record_id("evidence", {"name": name})
    return Claim(
        1, claim_id, 1, name, ".", {"text": name}, name, None, kind,
        "deterministic", (fact_id,), (evidence_id,), status, (), (), SNAPSHOT,
    )


def _targets() -> tuple[TargetSpec, ...]:
    return (
        TargetSpec("agents-md", "agents-md", "AGENTS.md", "managed-blocks", "keep"),
        TargetSpec("skill", "skill", ".agents/skills/demo", "whole-files", "keep"),
    )


def test_render_outputs_canonical_full_paths_and_separates_pending_preview() -> None:
    claims = (_claim("pending inferred", "pending", kind="inferred"),
              _claim("accepted fact", "accepted"))

    bundle = render_targets(claims, _targets(), policy=default_claim_policy(), project_id=PROJECT)

    agents, skill = bundle.targets
    assert set(agents.relativeFiles) == {"AGENTS.md"}
    assert set(skill.relativeFiles) == {
        ".agents/skills/demo/SKILL.md",
        ".agents/skills/demo/repo2skill-provenance.json",
    }
    assert b"accepted fact" in agents.relativeFiles["AGENTS.md"]
    assert b"pending inferred" not in agents.relativeFiles["AGENTS.md"]
    assert b"Pending inferred" in agents.previewFiles["AGENTS.md"]
    assert b"pending inferred" in agents.previewFiles["AGENTS.md"]
    assert set(skill.claimSpans[".agents/skills/demo/SKILL.md"]) == {
        f"{claim.claimId}@1" for claim in claims if claim.proposalStatus == "accepted"
    }
    accepted_ref = f"{claims[1].claimId}@1"
    first, last = agents.claimSpans["AGENTS.md"][accepted_ref][0]
    assert agents.relativeFiles["AGENTS.md"].decode("utf-8").splitlines()[first - 1:last] == ["- accepted fact"]
    assert all(b"\r" not in data and data.endswith(b"\n") for target in bundle.targets
               for files in (target.relativeFiles, target.previewFiles) for data in files.values())
    assert agents.exactHashes["AGENTS.md"] == "sha256:" + hashlib.sha256(agents.relativeFiles["AGENTS.md"]).hexdigest()
    provenance = json.loads(skill.relativeFiles[".agents/skills/demo/repo2skill-provenance.json"])
    assert "pending inferred" not in json.dumps(provenance)
    assert "proposalStatus" not in json.dumps(provenance)
    assert {item["claimId"] for item in provenance["claims"]} == {
        claim.claimId for claim in claims if claim.proposalStatus == "accepted"
    }


def test_render_order_and_managed_block_id_do_not_depend_on_claim_order_or_prose() -> None:
    claims = (_claim("second", "accepted"), _claim("first", "accepted"))
    first = render_targets(claims, _targets()[:1], policy=default_claim_policy(), project_id=PROJECT)
    reversed_bundle = render_targets(tuple(reversed(claims)), _targets()[:1], policy=default_claim_policy(), project_id=PROJECT)
    edited = tuple(replace(claim, renderedText="changed wording") for claim in claims)
    edited_bundle = render_targets(edited, _targets()[:1], policy=default_claim_policy(), project_id=PROJECT)

    assert first.targets[0].relativeFiles == reversed_bundle.targets[0].relativeFiles
    marker = first.targets[0].relativeFiles["AGENTS.md"].splitlines()[0]
    assert marker.startswith(b"<!-- repo2skill:begin project=" + PROJECT.encode() + b" block=managed-block_")
    assert b"block=" + canonical_record_id("managed-block", {
        "projectId": PROJECT, "targetId": "agents-md", "sectionKey": "repository-guide",
    }).encode() in marker
    assert marker in edited_bundle.targets[0].relativeFiles["AGENTS.md"]


def test_render_counts_skill_companion_toward_target_budget() -> None:
    policy = replace(default_claim_policy(), renderBudget=200)
    claim = _claim("claim that cannot fit with provenance", "accepted")

    bundle = render_targets((claim,), _targets()[1:], policy=policy, project_id=PROJECT)

    target = bundle.targets[0]
    assert sum(map(len, target.relativeFiles.values())) <= policy.renderBudget
    assert claim.claimId not in repr(target.claimSpans)
    assert any(item.code == "render.budget-omitted" for item in target.diagnostics)


def test_render_excludes_rejected_and_contested_claims_and_marker_parser_fails_closed() -> None:
    accepted = _claim("accepted", "accepted")
    rejected = _claim("rejected", "rejected")
    contested = _claim("contested", "contested")
    target = render_targets((accepted, rejected, contested), _targets()[:1],
                            policy=default_claim_policy(), project_id=PROJECT).targets[0]
    rendered = target.relativeFiles["AGENTS.md"].decode("utf-8")

    assert "accepted" in rendered
    assert "rejected" not in rendered
    assert "contested" not in rendered
    project_id, block_id, body = parse_managed_markers(rendered)
    assert project_id == PROJECT and block_id.startswith("managed-block_")
    assert "accepted" in body
    with pytest.raises(ValueError, match="exactly one"):
        parse_managed_markers(rendered + rendered)
