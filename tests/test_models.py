from __future__ import annotations

import dataclasses
import math

import pytest

from repo2skill.models import (
    Claim,
    EvidenceLocator,
    ProjectState,
    ReviewEvent,
)


def _claim(**changes: object) -> Claim:
    values: dict[str, object] = {
        "recordRevision": 1,
        "claimId": "claim_0123456789abcdef01234567",
        "claimRevision": 99,
        "semanticKey": "python.test",
        "scope": "repository",
        "payload": {"command": "pytest"},
        "renderedText": "Run pytest.",
        "templateKey": None,
        "kind": "observed",
        "origin": "deterministic",
        "factIds": ("fact_0123456789abcdef01234567",),
        "evidenceIds": ("evidence_0123456789abcdef01234567",),
        "proposalStatus": "accepted",
        "supersedes": (),
        "derivedFrom": (),
        "lastVerifiedSnapshotId": "snapshot_0123456789abcdef01234567",
    }
    values.update(changes)
    return Claim(**values)  # type: ignore[arg-type]


def test_provenance_records_are_frozen_and_keep_the_exact_contract_fields() -> None:
    assert tuple(field.name for field in dataclasses.fields(Claim)) == (
        "recordRevision", "claimId", "claimRevision", "semanticKey", "scope",
        "payload", "renderedText", "templateKey", "kind", "origin", "factIds",
        "evidenceIds", "proposalStatus", "supersedes", "derivedFrom",
        "lastVerifiedSnapshotId",
    )
    assert tuple(field.name for field in dataclasses.fields(ProjectState)) == (
        "schemaVersion", "toolVersion", "projectId", "currentRunId",
        "activeStageRevision", "reviewEvents", "acceptedClaimRevisions",
        "activeExportId", "activeManifestSetId",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        _claim().claimRevision = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"recordRevision": 0}, "recordRevision"),
        ({"claimId": "claim_NOT_HEX"}, "claimId"),
        ({"claimRevision": 0}, "claimRevision"),
        ({"kind": "guess"}, "kind"),
        ({"origin": "robot"}, "origin"),
        ({"proposalStatus": "stale"}, "proposalStatus"),
        ({"renderedText": None, "templateKey": None}, "exactly one"),
        ({"renderedText": "x", "templateKey": "y"}, "exactly one"),
        ({"payload": {"bad": math.inf}}, "finite"),
        ({"supersedes": ("claim_0123456789abcdef01234567@0",)}, "revision reference"),
    ],
)
def test_claim_rejects_malformed_persisted_values(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _claim(**changes)


def test_locator_and_review_event_enforce_enums_and_reason_rules() -> None:
    with pytest.raises(ValueError, match="locator kind"):
        EvidenceLocator("regex", "x", None)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="lineHint"):
        EvidenceLocator("line-range", [1, 2], 0)

    base = {
        "recordRevision": 1,
        "eventId": "event_0123456789abcdef01234567",
        "claimId": "claim_0123456789abcdef01234567",
        "claimRevision": 1,
        "createdAt": "2026-10-07T12:00:00Z",
    }
    with pytest.raises(ValueError, match="reason"):
        ReviewEvent(action="reject", reason=" ", **base)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reason"):
        ReviewEvent(action="accept", reason="not allowed", **base)  # type: ignore[arg-type]


def test_review_event_rejects_impossible_rfc3339_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="RFC 3339"):
        ReviewEvent(
            1,
            "event_0123456789abcdef01234567",
            "claim_0123456789abcdef01234567",
            1,
            "accept",
            None,
            "2026-99-07T12:00:00Z",
        )
