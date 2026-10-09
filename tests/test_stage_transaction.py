from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.dependencies import build_snapshot
from repo2skill.identity import capture_repository
from repo2skill.ids import canonical_json_bytes, effective_policy_revision
from repo2skill.inventory import collect_inventory
from repo2skill.ledger import LedgerStore
from repo2skill.models import ProjectDocument, ProjectState, RunLedger
from repo2skill.render import render_targets
from repo2skill.stage import publish_stage_revision, read_stage_descriptor, stage_run


PROJECT_ID = "project_0123456789abcdef01234567"
RUN_ID = "run_0123456789abcdef01234567"
OLD_RUN_ID = "run_1123456789abcdef01234567"


def _scenario(root: Path):
    state_path = ".repo2skill"
    targets = default_targets(root)
    scan_policy = default_scan_policy()
    claim_policy = default_claim_policy()
    exclusions = (state_path, *(target.livePath for target in targets))
    inventory = collect_inventory(root, scan_policy, excluded_paths=exclusions)
    identity = capture_repository(root, inventory, "no-git", project_id=PROJECT_ID, policy=scan_policy,
                                  excluded_paths=exclusions)
    policy_revision = effective_policy_revision(scan_policy, claim_policy, targets, generation_options={})
    snapshot = build_snapshot("snapshot_0123456789abcdef01234567", identity, inventory, {}, (),
                              policy_revision=policy_revision, root_label="repo")
    project = ProjectDocument("1.0", "0.1.0", PROJECT_ID, "repo", state_path, "no-git",
                              scan_policy, claim_policy, targets)
    run = RunLedger("1.0", "0.1.0", RUN_ID, snapshot, scan_policy, claim_policy, targets,
                    (), (), (), (), (), (), (), {}, True)
    bundle = render_targets((), targets, policy=claim_policy, project_id=PROJECT_ID)
    projection = {
        target.targetId: {
            "accepted": dict(target.exactHashes),
            "preview": dict(target.previewHashes),
        }
        for target in bundle.targets
    }
    return project, replace(run, stagedTargets=projection), bundle, identity, policy_revision, exclusions, targets


def _initialize(root: Path, project: ProjectDocument, *, current_run: str = OLD_RUN_ID) -> LedgerStore:
    store = LedgerStore(root, root / project.stateDir)
    with store.mutation():
        store.write_immutable("project.json", json.loads(canonical_json_bytes(project)))
        store.read_state()
        store.replace_state(ProjectState("1.0", "0.1.0", project.projectId, current_run, 1, (), {}, None, None))
    return store


def test_stage_run_publishes_hashed_revision_then_activates_one_pointer(tmp_path: Path) -> None:
    project, run, bundle, identity, revision, exclusions, targets = _scenario(tmp_path)
    store = _initialize(tmp_path, project)

    with store.mutation():
        staged = stage_run(bundle, run, store=store, current_identity=identity,
                           current_root_label="repo", current_policy_revision=revision,
                           current_extractor_versions={}, excluded_paths=exclusions,
                           selected_specs=targets)

    state = store.read_state()
    assert state is not None and (state.currentRunId, state.activeStageRevision) == (RUN_ID, 1)
    root_stage = root_path(tmp_path, project.stateDir, RUN_ID)
    assert staged.stageRoot == root_stage
    descriptor = read_stage_descriptor(store, RUN_ID, 1)
    assert descriptor["runId"] == RUN_ID and descriptor["stageRevision"] == 1
    assert set(descriptor["targets"]) == {target.targetId for target in targets}
    for target in bundle.targets:
        for path, data in target.relativeFiles.items():
            staged_file = root_stage / "targets" / target.targetId / "accepted" / Path(*path.split("/"))
            assert staged_file.read_bytes() == data
            assert hashlib.sha256(data).hexdigest() in target.exactHashes[path]
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / ".agents").exists()


def test_spec_mismatch_leaves_the_active_pointer_unchanged(tmp_path: Path) -> None:
    project, run, bundle, identity, revision, exclusions, targets = _scenario(tmp_path)
    store = _initialize(tmp_path, project)
    changed = replace(targets[0], livePath="docs/AGENTS.md")

    with store.mutation():
        with pytest.raises(ValueError, match="configured target"):
            stage_run(bundle, run, store=store, current_identity=identity,
                      current_root_label="repo", current_policy_revision=revision,
                      current_extractor_versions={}, excluded_paths=exclusions,
                      selected_specs=(changed,))

    assert store.read_state().currentRunId == OLD_RUN_ID  # type: ignore[union-attr]
    assert store.read_run(RUN_ID) is None


def test_freshness_failure_never_activates_published_initial_revision(tmp_path: Path) -> None:
    project, run, bundle, identity, revision, exclusions, targets = _scenario(tmp_path)
    store = _initialize(tmp_path, project)

    with store.mutation():
        with pytest.raises(ValueError, match="stale"):
            stage_run(bundle, run, store=store, current_identity=identity,
                      current_root_label="renamed", current_policy_revision=revision,
                      current_extractor_versions={}, excluded_paths=exclusions,
                      selected_specs=targets)

    assert store.read_state().currentRunId == OLD_RUN_ID  # type: ignore[union-attr]
    assert store.read_run(RUN_ID) is not None


def test_unresolved_claim_span_is_rejected_before_any_immutable_write(tmp_path: Path) -> None:
    project, run, bundle, identity, revision, exclusions, targets = _scenario(tmp_path)
    store = _initialize(tmp_path, project)
    target = bundle.targets[0]
    bad_spans = {path: {"claim_aaaaaaaaaaaaaaaaaaaaaaaa@1": ((1, 1),)}
                 for path in target.claimSpans}
    bad_target = replace(target, claimSpans=bad_spans)
    bad_bundle = replace(bundle, targets=(bad_target, *bundle.targets[1:]))

    with store.mutation():
        with pytest.raises(ValueError, match="does not resolve"):
            stage_run(bad_bundle, run, store=store, current_identity=identity,
                      current_root_label="repo", current_policy_revision=revision,
                      current_extractor_versions={}, excluded_paths=exclusions,
                      selected_specs=targets)

    assert store.read_state().currentRunId == OLD_RUN_ID  # type: ignore[union-attr]
    assert store.read_run(RUN_ID) is None


def test_duplicate_configured_target_ids_are_rejected_even_when_paths_differ(tmp_path: Path) -> None:
    project, run, _bundle, _identity, _revision, _exclusions, targets = _scenario(tmp_path)
    alternate_skill = replace(targets[1], livePath=".agents/skills/alternate")
    configured = (*targets, alternate_skill)
    project = replace(project, targets=configured)
    run = replace(run, configuredTargets=configured)
    selected = (targets[0], alternate_skill)
    bundle = render_targets((), selected, policy=project.claimPolicy, project_id=PROJECT_ID)
    store = _initialize(tmp_path, project)

    with store.mutation():
        with pytest.raises(ValueError, match="duplicate configured target ID"):
            publish_stage_revision(bundle, run, store=store, selected_specs=selected, stage_revision=1)

    assert not store.immutable_path_exists(f"staging/{RUN_ID}/r1")


def test_stage_requires_all_target_output_files_before_writing(tmp_path: Path) -> None:
    project, run, bundle, _identity, _revision, _exclusions, targets = _scenario(tmp_path)
    skill = bundle.targets[1]
    skill_files = {path: data for path, data in skill.relativeFiles.items() if path.endswith("SKILL.md")}
    skill_hashes = {path: skill.exactHashes[path] for path in skill_files}
    skill_spans = {path: skill.claimSpans[path] for path in skill_files}
    incomplete = replace(skill, relativeFiles=skill_files, exactHashes=skill_hashes, claimSpans=skill_spans)
    bundle = replace(bundle, targets=(bundle.targets[0], incomplete))
    store = _initialize(tmp_path, project)

    with store.mutation():
        with pytest.raises(ValueError, match="exactly match target output paths"):
            publish_stage_revision(bundle, run, store=store, selected_specs=targets, stage_revision=1)

    assert not store.immutable_path_exists(f"staging/{RUN_ID}/r1")


def test_descriptor_reader_rejects_a_missing_skill_companion(tmp_path: Path) -> None:
    project, run, bundle, identity, revision, exclusions, targets = _scenario(tmp_path)
    store = _initialize(tmp_path, project)
    with store.mutation():
        stage_run(bundle, run, store=store, current_identity=identity,
                  current_root_label="repo", current_policy_revision=revision,
                  current_extractor_versions={}, excluded_paths=exclusions,
                  selected_specs=targets)

    descriptor_path = root_path(tmp_path, project.stateDir, RUN_ID) / "stage.json"
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    skill = descriptor["targets"]["skill"]
    skill["accepted"].pop(".agents/skills/" + Path(project.targets[1].livePath).name + "/repo2skill-provenance.json")
    descriptor_path.write_bytes(canonical_json_bytes(descriptor) + b"\n")

    with pytest.raises(ValueError, match="exactly match target output paths"):
        read_stage_descriptor(store, RUN_ID, 1)


def test_stage_descriptor_allows_empty_mode_maps(tmp_path: Path) -> None:
    project, run, bundle, _identity, _revision, _exclusions, targets = _scenario(tmp_path)
    empty = replace(bundle.targets[0], relativeFiles={}, exactHashes={}, claimSpans={},
                    previewFiles={}, previewHashes={}, previewClaimSpans={})
    bundle = replace(bundle, targets=(empty,))
    store = _initialize(tmp_path, project)

    with store.mutation():
        publish_stage_revision(bundle, run, store=store, selected_specs=targets[:1], stage_revision=1)

    descriptor = read_stage_descriptor(store, RUN_ID, 1)
    assert descriptor["targets"]["agents-md"]["accepted"] == {}
    assert descriptor["targets"]["agents-md"]["preview"] == {}


def root_path(root: Path, state_dir: str, run_id: str) -> Path:
    return root / state_dir / "staging" / run_id / "r1"
