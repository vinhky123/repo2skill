from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from repo2skill.claims import derive_claims, review_claims
from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.dependencies import build_snapshot
from repo2skill.ids import canonical_json_bytes, canonical_record_id, effective_policy_revision
from repo2skill.ledger import LedgerStore
from repo2skill.models import (
    Evidence, EvidenceLocator, ExtractionRead, Fact, InputSelector, ProjectDocument,
    ProjectState, RepositoryIdentity, ReviewEvent, RunLedger,
)
from repo2skill.render import render_targets
from repo2skill.stage import publish_stage_revision, read_stage_descriptor


PROJECT_ID = "project_0123456789abcdef01234567"
SNAPSHOT_ID = "snapshot_0123456789abcdef01234567"
REPO_ID = "repo_0123456789abcdef01234567"
RUN_ID = "run_0123456789abcdef01234567"
FACT_ID = "fact_0123456789abcdef01234567"
EVIDENCE_ID = "evidence_0123456789abcdef01234567"
HASH = "sha256:" + "a" * 64


def _scenario(root: Path, *, inferred: bool = True):
    scan = default_scan_policy()
    policy = replace(default_claim_policy(), autoAcceptObserved=True)
    targets = default_targets(root)
    read_selector = InputSelector("file", "package.json", ".", False)
    read = ExtractionRead(
        1, canonical_record_id("read", {"snapshot": SNAPSHOT_ID}), SNAPSHOT_ID,
        "generic", "1.0", "generic.structural", "generic-allowlist-v1", "file",
        read_selector, False, "present", HASH, (), None, (),
    )
    fact = Fact(
        1, FACT_ID, 1, SNAPSHOT_ID, "artifact.presence", "artifact.presence:package.v1:package.json",
        ".", {"category": "package.v1", "name": "demo@1.0", "path": "package.json"},
        "template" if inferred else "observed", "generic.structural", "generic-allowlist-v1", (EVIDENCE_ID,),
    )
    evidence = Evidence(
        1, EVIDENCE_ID, 1, SNAPSHOT_ID, "package.json", HASH, "config:json", "application/json",
        "utf-8", EvidenceLocator("json-pointer", "/name", None), None, "available",
    )
    policy_revision = effective_policy_revision(scan, policy, targets, generation_options={})
    snapshot = build_snapshot(
        SNAPSHOT_ID,
        RepositoryIdentity(REPO_ID, root, "no-git", None, None, False, {}),
        # build_snapshot needs only these identity fields and a stable inventory object.
        _inventory_stub(HASH, root), {"generic": "1.0"}, (read,),
        policy_revision=policy_revision, root_label="repo",
    )
    run = RunLedger(
        "1.0", "0.1.0", RUN_ID, snapshot, scan, policy, targets,
        (read,), (evidence,), (fact,), (), (), (), (), {}, True,
    )
    claim = derive_claims((fact,), policy, repository_id=REPO_ID).claims[0]
    run = replace(run, claims=(claim,))
    store = LedgerStore(root, root / ".repo2skill")
    project = ProjectDocument("1.0", "0.1.0", PROJECT_ID, "repo", ".repo2skill", "no-git", scan, policy, targets)
    state = ProjectState("1.0", "0.1.0", PROJECT_ID, RUN_ID, 1, (), {}, None, None)
    identity = RepositoryIdentity(REPO_ID, root, "no-git", None, None, False, {})
    exclusions = (".repo2skill", *(target.livePath for target in targets))
    return store, project, state, run, claim, identity, policy_revision, exclusions, targets


def _inventory_stub(digest: str, root: Path):
    from types import SimpleNamespace

    return SimpleNamespace(inventoryHash=digest, root=root)


def _initialize(store: LedgerStore, project: ProjectDocument, state: ProjectState, run: RunLedger,
                identity: RepositoryIdentity, policy_revision: str, exclusions: tuple[str, ...], targets):
    with store.mutation():
        store.write_immutable("project.json", json.loads(canonical_json_bytes(project)))
        store.read_state()
        store.replace_state(replace(state, activeStageRevision=None))
        store.write_immutable_bytes(f"runs/{run.runId}.json", canonical_json_bytes(run) + b"\n")
        initial_claim = run.claims[0]
        bundle = render_targets((initial_claim,), targets, policy=run.effectiveClaimPolicy, project_id=project.projectId)
        publish_stage_revision(bundle, run, store=store, selected_specs=targets, stage_revision=1)
        effective = "accepted" if initial_claim.kind == "observed" and run.effectiveClaimPolicy.autoAcceptObserved else "pending"
        accepted = {initial_claim.claimId: initial_claim.claimRevision} if effective == "accepted" else {}
        store.replace_state(replace(state, activeStageRevision=1, acceptedClaimRevisions=accepted))
    return store.read_state()


def _event(claim, action: str, *, reason: str | None = None, suffix: str = "1") -> ReviewEvent:
    return ReviewEvent(1, f"event_{suffix * 24}", claim.claimId, claim.claimRevision,
                       action, reason, "2026-10-09T00:00:00Z")


def _review(store, state, run, events, targets, identity, policy_revision, exclusions):
    with store.mutation():
        return review_claims(
            state, run, events, targets, store=store, current_identity=identity,
            current_root_label="repo", current_policy_revision=policy_revision,
            current_extractor_versions={"generic": "1.0"}, excluded_paths=exclusions,
        )


def test_review_accepts_pending_claim_and_publishes_next_immutable_revision(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.claims as claims_module

    monkeypatch.setattr(claims_module, "recheck_dependencies", lambda *args, **kwargs: _fresh())
    store, project, state, run, claim, identity, revision, exclusions, targets = _scenario(tmp_path)
    state = _initialize(store, project, state, run, identity, revision, exclusions, targets)
    with store.mutation():
        store.write_immutable_bytes(f"staging/{RUN_ID}/r2/orphan", b"inactive")
        store.write_immutable_bytes(f"staging/{RUN_ID}/r3/orphan", b"inactive")
    supplied_state = state
    supplied_run = run

    updated, staged = _review(store, state, run, (_event(claim, "accept"),), targets,
                              identity, revision, exclusions)

    assert updated.reviewEvents == (_event(claim, "accept"),)
    assert updated.acceptedClaimRevisions == {claim.claimId: claim.claimRevision}
    assert updated.activeStageRevision == staged.stageRevision == 4
    assert store.read_state() == updated
    assert store.read_run(RUN_ID) == supplied_run
    descriptor = read_stage_descriptor(store, RUN_ID, 4)
    assert descriptor["stageRevision"] == 4
    accepted_spans = descriptor["targets"][targets[0].targetId]["accepted"][targets[0].livePath]["claimSpans"]
    assert f"{claim.claimId}@{claim.claimRevision}" in accepted_spans
    assert store.read_state() != supplied_state


def test_review_validates_entire_batch_and_transition_reasons_before_publication(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.claims as claims_module

    monkeypatch.setattr(claims_module, "recheck_dependencies", lambda *args, **kwargs: _fresh())
    store, project, state, run, claim, identity, revision, exclusions, targets = _scenario(tmp_path)
    state = _initialize(store, project, state, run, identity, revision, exclusions, targets)
    valid_then_unknown = (_event(claim, "accept", suffix="1"),
                          ReviewEvent(1, "event_" + "2" * 24, "claim_" + "f" * 24, 1,
                                      "reject", "reason", "2026-10-09T00:00:01Z"))

    with pytest.raises(ValueError, match="unknown claim revision"):
        _review(store, state, run, valid_then_unknown, targets, identity, revision, exclusions)
    assert store.read_state() == state
    assert not store.immutable_path_exists(f"staging/{RUN_ID}/r2")

    with pytest.raises(ValueError, match="reject and contest require"):
        _event(claim, "reject", reason=" ", suffix="3")
    accept_then_reject = (_event(claim, "accept", suffix="4"),
                          _event(claim, "reject", reason="no", suffix="5"))
    with pytest.raises(ValueError, match="pending"):
        _review(store, state, run, accept_then_reject, targets, identity, revision, exclusions)
    assert store.read_state() == state
    with pytest.raises(ValueError, match="unsupported review action"):
        ReviewEvent(1, "event_" + "6" * 24, claim.claimId, claim.claimRevision,
                    "all", None, "2026-10-09T00:00:00Z")  # type: ignore[arg-type]


def test_review_contests_pending_or_accepted_and_rejects_only_pending(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.claims as claims_module

    monkeypatch.setattr(claims_module, "recheck_dependencies", lambda *args, **kwargs: _fresh())
    store, project, state, run, claim, identity, revision, exclusions, targets = _scenario(tmp_path, inferred=False)
    state = _initialize(store, project, state, run, identity, revision, exclusions, targets)
    contested, staged = _review(store, state, run, (_event(claim, "contest", reason="needs check"),),
                                targets, identity, revision, exclusions)
    assert contested.acceptedClaimRevisions == {}
    assert contested.activeStageRevision == staged.stageRevision == 2
    assert not read_stage_descriptor(store, RUN_ID, 2)["targets"][targets[0].targetId]["accepted"][targets[0].livePath]["claimSpans"]
    assert store.read_state() == contested
    with pytest.raises(ValueError, match="pending"):
        _review(store, contested, run, (_event(claim, "reject", reason="still rejected", suffix="2"),),
                targets, identity, revision, exclusions)


def test_review_can_reject_pending_and_contest_pending(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.claims as claims_module

    monkeypatch.setattr(claims_module, "recheck_dependencies", lambda *args, **kwargs: _fresh())
    store, project, state, run, claim, identity, revision, exclusions, targets = _scenario(tmp_path)
    state = _initialize(store, project, state, run, identity, revision, exclusions, targets)
    rejected, staged = _review(store, state, run, (_event(claim, "reject", reason="out of scope"),),
                               targets, identity, revision, exclusions)
    assert rejected.acceptedClaimRevisions == {}
    assert rejected.activeStageRevision == staged.stageRevision == 2
    accepted_files = read_stage_descriptor(store, RUN_ID, 2)["targets"][targets[0].targetId]["accepted"]
    assert not accepted_files[targets[0].livePath]["claimSpans"]

    second_root = tmp_path / "second"
    second_root.mkdir()
    store2, project2, state2, run2, claim2, identity2, revision2, exclusions2, targets2 = _scenario(second_root)
    state2 = _initialize(store2, project2, state2, run2, identity2, revision2, exclusions2, targets2)
    contested, staged2 = _review(store2, state2, run2, (_event(claim2, "contest", reason="verify"),),
                                 targets2, identity2, revision2, exclusions2)
    assert contested.acceptedClaimRevisions == {}
    assert contested.activeStageRevision == staged2.stageRevision == 2


def test_render_failure_and_stale_or_unknown_freshness_never_activate_review(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.claims as claims_module

    store, project, state, run, claim, identity, revision, exclusions, targets = _scenario(tmp_path)
    state = _initialize(store, project, state, run, identity, revision, exclusions, targets)
    before = state
    monkeypatch.setattr(claims_module, "render_targets", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("render failed")))
    with pytest.raises(RuntimeError, match="render failed"):
        _review(store, state, run, (_event(claim, "accept"),), targets, identity, revision, exclusions)
    assert store.read_state() == before
    assert not store.immutable_path_exists(f"staging/{RUN_ID}/r2")

    monkeypatch.undo()
    for outcome in ("stale", "unknown"):
        monkeypatch.setattr(claims_module, "recheck_dependencies", lambda *args, _outcome=outcome, **kwargs: _fresh(_outcome))
        with pytest.raises(ValueError, match=outcome):
            _review(store, before, run, (_event(claim, "accept", suffix="5" if outcome == "stale" else "6"),),
                    targets, identity, revision, exclusions)
        assert store.read_state() == before


def test_review_rejects_stale_supplied_state_run_and_invalid_claim_accept(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.claims as claims_module

    monkeypatch.setattr(claims_module, "recheck_dependencies", lambda *args, **kwargs: _fresh())
    store, project, state, run, claim, identity, revision, exclusions, targets = _scenario(tmp_path)
    state = _initialize(store, project, state, run, identity, revision, exclusions, targets)
    with pytest.raises(ValueError, match="state is stale"):
        _review(store, replace(state, toolVersion="0.2.0"), run, (_event(claim, "accept"),),
                targets, identity, revision, exclusions)
    with pytest.raises(ValueError, match="run is stale"):
        _review(store, state, replace(run, completed=False), (_event(claim, "accept", suffix="2"),),
                targets, identity, revision, exclusions)
    assert store.read_state() == state

    invalid_claim = replace(claim, factIds=("fact_" + "f" * 24,))
    invalid_root = tmp_path / "invalid"
    invalid_root.mkdir()
    invalid_store, invalid_project, invalid_state, _original, _claim, invalid_identity, invalid_revision, invalid_exclusions, invalid_targets = _scenario(invalid_root)
    invalid_run = replace(run, claims=(invalid_claim,), configuredTargets=invalid_targets)
    invalid_state = _initialize(invalid_store, invalid_project, invalid_state, invalid_run,
                                invalid_identity, invalid_revision, invalid_exclusions, invalid_targets)
    with pytest.raises(ValueError, match="invalid for this run"):
        _review(invalid_store, invalid_state, invalid_run, (_event(invalid_claim, "accept", suffix="3"),),
                invalid_targets, invalid_identity, invalid_revision, invalid_exclusions)
    assert invalid_store.read_state() == invalid_state


def _fresh(status: str = "fresh"):
    from repo2skill.dependencies import DependencyRecheckResult

    return DependencyRecheckResult(status, (), ())
