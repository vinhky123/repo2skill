from __future__ import annotations

import dataclasses

import pytest

import repo2skill.ids as ids
from repo2skill.models import Claim


def _claim(*, text: str, payload: object, revision: int = 47) -> Claim:
    return Claim(
        1, "claim_0123456789abcdef01234567", revision, "python.test", "repository",
        payload, text, None, "observed", "deterministic",
        ("fact_0123456789abcdef01234567",),
        ("evidence_0123456789abcdef01234567",), "accepted", (), (),
        "snapshot_0123456789abcdef01234567",
    )


def test_canonical_record_id_is_stable_for_object_order_and_url_safe() -> None:
    first = ids.canonical_record_id("claim", {"scope": "repo", "key": [1, True]})
    second = ids.canonical_record_id("claim", {"key": [1, True], "scope": "repo"})

    assert first == second
    assert first.startswith("claim_")
    assert len(first.removeprefix("claim_")) == 24
    assert first == first.lower()


def test_explicit_id_context_rejects_a_truncated_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = ids.CanonicalIdContext()
    monkeypatch.setattr(ids, "_digest_suffix", lambda canonical: "0" * 24)

    assert context.record_id("claim", {"semanticKey": "a"}) == "claim_" + "0" * 24
    with pytest.raises(ids.IdCollisionError, match="collision"):
        context.record_id("claim", {"semanticKey": "b"})


def test_revise_record_ignores_prose_but_increments_for_semantic_payload() -> None:
    previous = _claim(text="Run pytest.", payload={"command": "pytest"}, revision=3)
    prose_change = _claim(text="Use pytest.", payload={"command": "pytest"})
    semantic_change = _claim(text="Run tests.", payload={"command": "pytest -q"})

    same = ids.revise_record(
        previous, prose_change,
        semantic_payload=lambda claim: claim.payload,
        revision_field="claimRevision",
    )
    changed = ids.revise_record(
        previous, semantic_change,
        semantic_payload=lambda claim: claim.payload,
        revision_field="claimRevision",
    )

    assert same.claimId == previous.claimId
    assert same.claimRevision == 3
    assert same.renderedText == "Use pytest."
    assert changed.claimId == previous.claimId
    assert changed.claimRevision == 4


def test_revise_record_starts_at_one_and_rejects_a_different_identity() -> None:
    new = _claim(text="Run pytest.", payload={"command": "pytest"})
    assert ids.revise_record(
        None, new, semantic_payload=lambda claim: claim.payload,
        revision_field="claimRevision",
    ).claimRevision == 1

    other = dataclasses.replace(new, claimId="claim_aaaaaaaaaaaaaaaaaaaaaaaa")
    with pytest.raises(ValueError, match="stable ID"):
        ids.revise_record(
            other, new, semantic_payload=lambda claim: claim.payload,
            revision_field="claimRevision",
        )


def test_latest_by_id_selects_maximum_revision_independent_of_input_order() -> None:
    from repo2skill.ids import latest_by_id

    older = _claim(text="old", payload={"command": "old"}, revision=2)
    latest = _claim(text="new", payload={"command": "new"}, revision=5)
    selected = latest_by_id(
        (latest, older), id_field="claimId", revision_field="claimRevision",
        semantic_payload=lambda claim: {"payload": claim.payload, "kind": claim.kind, "origin": claim.origin},
    )
    assert selected[latest.claimId] == latest


def test_latest_by_id_fails_closed_on_equal_maximum_semantic_conflict() -> None:
    from repo2skill.ids import latest_by_id

    first = _claim(text="first", payload={"command": "first"}, revision=5)
    second = _claim(text="second", payload={"command": "second"}, revision=5)
    with pytest.raises(ValueError, match="conflicting semantic history"):
        latest_by_id(
            (first, second), id_field="claimId", revision_field="claimRevision",
            semantic_payload=lambda claim: {"payload": claim.payload, "kind": claim.kind, "origin": claim.origin},
        )


def test_effective_policy_revision_sorts_targets_and_refuses_unknown_options() -> None:
    from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
    from pathlib import Path

    targets = default_targets(Path("repository"))
    forward = ids.effective_policy_revision(
        default_scan_policy(), default_claim_policy(), targets, generation_options={}
    )
    reverse = ids.effective_policy_revision(
        default_scan_policy(), default_claim_policy(), tuple(reversed(targets)),
        generation_options={},
    )
    assert forward == reverse
    assert forward.startswith("sha256:") and len(forward) == 71
    with pytest.raises(ValueError, match="generation_options"):
        ids.effective_policy_revision(
            default_scan_policy(), default_claim_policy(), targets,
            generation_options={"mystery": True},
        )
