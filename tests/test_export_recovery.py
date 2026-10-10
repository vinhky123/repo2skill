from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.dependencies import build_snapshot
from repo2skill.identity import capture_repository
from repo2skill.ids import canonical_json_bytes, effective_policy_revision
from repo2skill.inventory import collect_inventory
from repo2skill.ledger import LedgerStore
from repo2skill.models import ExportJournal, OwnershipState, ProjectDocument, ProjectState, RenderBundle, RunLedger
from repo2skill.claims import review_claims
from repo2skill.ownership import ExportFreshnessError, export_staged, resume_export
from repo2skill.output_fs import OutputConflictError
from repo2skill.render import render_targets
from repo2skill.stage import publish_stage_revision, stage_run


PROJECT_ID = "project_0123456789abcdef01234567"
RUN_ID = "run_0123456789abcdef01234567"


def _scenario(root: Path):
    state_dir = ".repo2skill"
    targets = default_targets(root)
    scan = default_scan_policy()
    claim = default_claim_policy()
    exclusions = (state_dir, *(target.livePath for target in targets))
    inventory = collect_inventory(root, scan, excluded_paths=exclusions)
    identity = capture_repository(root, inventory, "no-git", project_id=PROJECT_ID,
                                  policy=scan, excluded_paths=exclusions)
    policy_revision = effective_policy_revision(scan, claim, targets, generation_options={})
    snapshot = build_snapshot("snapshot_0123456789abcdef01234567", identity,
                              inventory, {}, (), policy_revision=policy_revision,
                              root_label="repo")
    project = ProjectDocument("1.0", "0.1.0", PROJECT_ID, "repo", state_dir,
                              "no-git", scan, claim, targets)
    bundle = render_targets((), targets, policy=claim, project_id=PROJECT_ID)
    staged_targets = {target.targetId: {"accepted": dict(target.exactHashes),
                                       "preview": dict(target.previewHashes)}
                      for target in bundle.targets}
    run = RunLedger("1.0", "0.1.0", RUN_ID, snapshot, scan, claim, targets,
                    (), (), (), (), (), (), (), staged_targets, True)
    store = LedgerStore(root, root / state_dir)
    with store.mutation():
        store.write_immutable("project.json", json.loads(canonical_json_bytes(project)))
        store.read_state()
        store.replace_state(ProjectState("1.0", "0.1.0", PROJECT_ID, None, None, (), {}, None, None))
        stage_run(bundle, run, store=store, current_identity=identity,
                  current_root_label="repo", current_policy_revision=policy_revision,
                  current_extractor_versions={}, excluded_paths=exclusions,
                  selected_specs=targets)
    return store, project, identity, policy_revision, exclusions, targets, run, bundle


def _active_ownership(store: LedgerStore) -> dict[str, OwnershipState]:
    state = store.read_state()
    assert state is not None
    manifest_set = store.read_manifest_set(
        f"manifests/{state.activeExportId}/{state.activeManifestSetId}.json"
    )
    assert manifest_set is not None
    result: dict[str, OwnershipState] = {}
    for target_id, ref in manifest_set.targets.items():
        manifest = store.read_target_manifest(ref.path)
        assert manifest is not None
        result[target_id] = OwnershipState(
            target_id, manifest.manifestId,
            {item.path: item.exactHash for item in manifest.files},
            {item.path: tuple(block.blockId for block in item.blocks)
             for item in manifest.files if item.blocks},
            tuple(item.path for item in manifest.files if item.ownershipMode == "whole-files"),
            manifest.createdDirectories,
        )
    return result


def test_export_publishes_one_selected_target_and_commits_manifest_pointer(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "complete", result.conflicts
    assert (tmp_path / "AGENTS.md").exists()
    state = store.read_state()
    assert state is not None and state.activeExportId == result.exportId
    assert len(store.list_export_journals()) == 1
    assert store.list_export_journals()[0].status == "committed"


def test_prepared_export_callback_runs_before_journal_and_live_writes(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.maintenance_context as maintenance_context
    from repo2skill.maintenance_models import CommittedSourceGuard
    from repo2skill.ownership import manifest_set_id_for_export

    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    guard_calls = []
    monkeypatch.setattr(maintenance_context, "recheck_committed_source", lambda *_args: guard_calls.append(True))
    calls = []

    def prepared(journal):
        calls.append(journal)
        assert store.read_export_journal(f"exports/{journal.exportId}.json") is None
        assert not (tmp_path / "AGENTS.md").exists()

    guard = CommittedSourceGuard(
        "maintenance-job_0123456789abcdef01234567", "0" * 40, "sha256:" + "1" * 64,
        "policy", "application-auth_0123456789abcdef01234567",
    )
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision, current_extractor_versions={},
            excluded_paths=exclusions, source_guard=guard, on_prepared_export=prepared,
        )
    assert result.status == "complete"
    assert len(calls) == 1
    assert len(guard_calls) >= 2
    assert manifest_set_id_for_export(result.exportId) == store.read_state().activeManifestSetId


def test_unresolved_export_journal_blocks_new_exports(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    # The first export finishes normally; a committed journal does not block a later export.
    with store.mutation():
        first = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert first.status == "complete", first.conflicts


def test_resume_completes_prepared_journal_after_all_live_writes(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.ownership as ownership_module

    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    original_commit = ownership_module._commit_manifests

    def interrupt_before_pointer(*args, **kwargs):
        raise OSError("simulated interruption before pointer commit")

    monkeypatch.setattr(ownership_module, "_commit_manifests", interrupt_before_pointer)
    with store.mutation():
        try:
            export_staged(
                RUN_ID, (targets[0].targetId,), {}, store=store,
                current_identity=identity, current_root_label="repo",
                current_policy_revision=policy_revision,
                current_extractor_versions={}, excluded_paths=exclusions,
            )
        except OSError as error:
            assert str(error).startswith("simulated interruption")
        else:
            raise AssertionError("expected simulated interruption")
    journal = store.list_export_journals()[0]
    assert journal.status == "prepared"
    monkeypatch.setattr(ownership_module, "_commit_manifests", original_commit)
    with store.mutation():
        result = resume_export(
            journal.exportId, {}, store=store, current_identity=identity,
            current_root_label="repo", current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "complete", result.conflicts
    assert store.list_export_journals()[0].status == "committed"


def test_guarded_resume_stops_after_partial_live_write_when_source_moves(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.maintenance_context as maintenance_context
    import repo2skill.ownership as ownership_module

    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    original_replace = ownership_module.replace_live_file
    writes = []

    def write_then_crash(*args, **kwargs):
        result = original_replace(*args, **kwargs)
        writes.append(args[1])
        raise OSError("simulated interruption after one live replacement")

    monkeypatch.setattr(ownership_module, "replace_live_file", write_then_crash)
    guard_failures = []

    def source_guard(_store, _guard):
        if guard_failures:
            raise ValueError("committed HEAD moved")

    monkeypatch.setattr(maintenance_context, "recheck_committed_source", source_guard)
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
            source_guard=object(),
        )
    assert result.status == "incomplete"
    assert len(writes) == 1
    journal = store.list_export_journals()[0]
    assert journal.status == "incomplete"
    written_path = writes[0]
    written_bytes = (tmp_path / written_path).read_bytes()

    guard_failures.append(True)
    with store.mutation(), pytest.raises(ValueError, match="committed HEAD moved"):
        resume_export(
            journal.exportId, {}, store=store, current_identity=identity,
            current_root_label="repo", current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
            source_guard=object(),
        )

    assert (tmp_path / written_path).read_bytes() == written_bytes
    state = store.read_state()
    assert state is not None and state.activeExportId is None and state.activeManifestSetId is None
    assert store.read_export_journal(f"exports/{journal.exportId}.json").status == "incomplete"


def test_resume_reuses_immutable_manifest_written_before_crash(tmp_path: Path, monkeypatch) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    original_write = store.write_immutable
    crashed = False

    def interrupt_after_manifest(relative_path, document):
        nonlocal crashed
        original_write(relative_path, document)
        if relative_path.endswith(f"/{targets[0].targetId}.json") and not crashed:
            crashed = True
            raise OSError("simulated interruption after target manifest")

    store.write_immutable = interrupt_after_manifest  # type: ignore[method-assign]
    with store.mutation():
        try:
            export_staged(
                RUN_ID, (targets[0].targetId,), {}, store=store,
                current_identity=identity, current_root_label="repo",
                current_policy_revision=policy_revision,
                current_extractor_versions={}, excluded_paths=exclusions,
            )
        except OSError as error:
            assert str(error).startswith("simulated interruption")
        else:
            raise AssertionError("expected simulated interruption")
    journal = store.list_export_journals()[0]
    assert journal.status == "prepared"
    store.write_immutable = original_write  # type: ignore[method-assign]
    with store.mutation():
        result = resume_export(
            journal.exportId, {}, store=store, current_identity=identity,
            current_root_label="repo", current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "complete"


def test_resume_finalizes_journal_after_pointer_commit_crash(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.ownership as ownership_module

    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    original_write_journal = ownership_module._write_journal

    def interrupt_after_pointer(store_arg, journal):
        if journal.status == "committed":
            raise OSError("simulated crash after pointer commit")
        return original_write_journal(store_arg, journal)

    monkeypatch.setattr(ownership_module, "_write_journal", interrupt_after_pointer)
    with store.mutation():
        try:
            export_staged(
                RUN_ID, (targets[0].targetId,), {}, store=store,
                current_identity=identity, current_root_label="repo",
                current_policy_revision=policy_revision,
                current_extractor_versions={}, excluded_paths=exclusions,
            )
        except OSError as error:
            assert str(error).startswith("simulated crash")
        else:
            raise AssertionError("expected simulated crash")
    journal = store.list_export_journals()[0]
    assert journal.status != "committed"
    pointer = store.read_state()
    assert pointer is not None and pointer.activeExportId == journal.exportId
    monkeypatch.setattr(ownership_module, "_write_journal", original_write_journal)
    with store.mutation():
        result = resume_export(
            journal.exportId, {}, store=store, current_identity=identity,
            current_root_label="renamed-after-commit", current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "complete", result.conflicts
    assert store.list_export_journals()[0].status == "committed"


def test_resume_pointer_finalization_refuses_other_unresolved_journal(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.ownership as ownership_module
    from repo2skill.ledger import ExportRecoveryRequired

    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    original_write_journal = ownership_module._write_journal

    def interrupt_after_pointer(store_arg, journal):
        if journal.status == "committed":
            raise OSError("simulated crash after pointer commit")
        return original_write_journal(store_arg, journal)

    monkeypatch.setattr(ownership_module, "_write_journal", interrupt_after_pointer)
    with store.mutation():
        try:
            export_staged(
                RUN_ID, (targets[0].targetId,), {}, store=store,
                current_identity=identity, current_root_label="repo",
                current_policy_revision=policy_revision,
                current_extractor_versions={}, excluded_paths=exclusions,
            )
        except OSError as error:
            assert str(error).startswith("simulated crash")
        else:
            raise AssertionError("expected simulated crash")
    monkeypatch.setattr(ownership_module, "_write_journal", original_write_journal)
    selected = store.list_export_journals()[0]
    stamp = "2026-10-09T00:00:00Z"
    other = ExportJournal(
        "1.0", "0.1.0", "export_1123456789abcdef01234567", RUN_ID, 1,
        (targets[1].targetId,), (), {}, None, "incomplete", stamp, stamp,
    )
    with store.mutation():
        store.write_export_journal(other)
    state_before = (store.state_dir / "state.json").read_bytes()
    live_before = (tmp_path / "AGENTS.md").read_bytes()
    selected_path = store.state_dir / "exports" / f"{selected.exportId}.json"
    other_path = store.state_dir / "exports" / f"{other.exportId}.json"
    selected_before = selected_path.read_bytes()
    other_before = other_path.read_bytes()
    with store.mutation():
        with pytest.raises(ExportRecoveryRequired):
            resume_export(
                selected.exportId, {}, store=store, current_identity=identity,
                current_root_label="renamed-after-commit",
                current_policy_revision=policy_revision,
                current_extractor_versions={}, excluded_paths=exclusions,
            )
    assert (store.state_dir / "state.json").read_bytes() == state_before
    assert (tmp_path / "AGENTS.md").read_bytes() == live_before
    assert selected_path.read_bytes() == selected_before
    assert other_path.read_bytes() == other_before


def test_export_preserves_outside_marker_bytes(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    with store.mutation():
        first = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert first.status == "complete", first.conflicts
    live = tmp_path / "AGENTS.md"
    prior = live.read_bytes()
    live.write_bytes(b"Human notes\n\n" + prior)
    outside = b"Human notes\n\n"
    before = live.read_bytes()
    ownership = _active_ownership(store)
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[0].targetId,), ownership, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "complete", result.conflicts
    assert live.read_bytes().startswith(outside)
    assert b"Human notes" in before
    assert b"\n" in live.read_bytes()


def test_export_conflicts_on_changed_managed_block_without_live_write(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    with store.mutation():
        first = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert first.status == "complete", first.conflicts
    live = tmp_path / "AGENTS.md"
    changed = live.read_bytes().replace(b"Repository guide", b"Manual guide")
    live.write_bytes(changed)
    ownership = _active_ownership(store)
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[0].targetId,), ownership, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "conflict"
    assert live.read_bytes() == changed
    assert len(store.list_export_journals()) == 1


def test_subset_export_keeps_unselected_manifest_reference(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    with store.mutation():
        first = export_staged(
            RUN_ID, tuple(target.targetId for target in targets), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert first.status == "complete", first.conflicts
    first_state = store.read_state()
    assert first_state is not None
    first_set = store.read_manifest_set(
        f"manifests/{first_state.activeExportId}/{first_state.activeManifestSetId}.json"
    )
    assert first_set is not None
    unselected = first_set.targets[targets[1].targetId]
    ownership = _active_ownership(store)
    with store.mutation():
        second = export_staged(
            RUN_ID, (targets[0].targetId,), ownership, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert second.status == "complete"
    second_state = store.read_state()
    assert second_state is not None
    second_set = store.read_manifest_set(
        f"manifests/{second_state.activeExportId}/{second_state.activeManifestSetId}.json"
    )
    assert second_set is not None
    assert second_set.targets[targets[1].targetId] == unselected
    with store.mutation():
        third = export_staged(
            RUN_ID, (targets[0].targetId,), _active_ownership(store), store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert third.status == "complete"


def test_third_live_hash_is_preserved_as_recovery_conflict(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.ownership as ownership_module

    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)

    def competing_write(root, path, payload, *, expected_old_hash):
        (root / path).write_bytes(b"manual third version\n")
        raise OutputConflictError("simulated competing editor")

    monkeypatch.setattr(ownership_module, "replace_live_file", competing_write)
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "conflict"
    assert (tmp_path / "AGENTS.md").read_bytes() == b"manual third version\n"


def test_planned_directory_without_journal_creation_proof_conflicts(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.ownership as ownership_module

    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)

    def preexisting_directory(root, path):
        (root / Path(*path.split("/"))).mkdir()
        return False

    monkeypatch.setattr(ownership_module, "create_live_directory", preexisting_directory)
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[1].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "conflict"
    assert (tmp_path / ".agents").is_dir()
    assert (tmp_path / ".repo2skill" / "exports").exists()
    journal = store.list_export_journals()[0]
    assert journal.status == "conflict"
    assert journal.operations[0].path == ".agents"
    assert journal.createdDirectories == {}


def test_explicit_empty_block_deletion_keeps_surrounding_file(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    with store.mutation():
        first = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert first.status == "complete"
    ownership = _active_ownership(store)
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[0].targetId,), ownership, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
            delete_empty_owned_blocks=True,
        )
    assert result.status == "complete"
    live = tmp_path / "AGENTS.md"
    assert live.exists() and b"repo2skill:" not in live.read_bytes()


def test_empty_skill_stage_does_not_delete_without_intent(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, run, bundle = _scenario(tmp_path)
    with store.mutation():
        first = export_staged(
            RUN_ID, (targets[1].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert first.status == "complete"
    ownership = _active_ownership(store)
    skill_files = {
        path: (tmp_path / Path(*path.split("/"))).read_bytes()
        for path in (targets[1].livePath + "/SKILL.md",
                     targets[1].livePath + "/repo2skill-provenance.json")
    }
    skill = bundle.targets[1]
    omitted = replace(skill, relativeFiles={}, exactHashes={}, claimSpans={},
                      previewFiles={}, previewHashes={}, previewClaimSpans={})
    with store.mutation():
        publish_stage_revision(
            RenderBundle((omitted,), ()), run, store=store,
            selected_specs=(targets[1],), stage_revision=2,
        )
        state = store.read_state()
        assert state is not None
        store.replace_state(replace(state, activeStageRevision=2))
        result = export_staged(
            RUN_ID, (targets[1].targetId,), ownership, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "conflict"
    for path, prior_bytes in skill_files.items():
        assert (tmp_path / Path(*path.split("/"))).read_bytes() == prior_bytes
    assert len(store.list_export_journals()) == 1


def test_mixed_eol_is_a_preflight_conflict(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    with store.mutation():
        first = export_staged(
            RUN_ID, (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert first.status == "complete"
    live = tmp_path / "AGENTS.md"
    prior = live.read_bytes()
    live.write_bytes(prior.replace(b"\n", b"\r\n", 1))
    mixed = live.read_bytes()
    with store.mutation():
        result = export_staged(
            RUN_ID, (targets[0].targetId,), _active_ownership(store), store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert result.status == "conflict"
    assert live.read_bytes() == mixed
    assert len(store.list_export_journals()) == 1


def test_freshness_failure_writes_no_journal_or_live_output(tmp_path: Path) -> None:
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    with store.mutation():
        try:
            export_staged(
                RUN_ID, (targets[0].targetId,), {}, store=store,
                current_identity=identity, current_root_label="changed",
                current_policy_revision=policy_revision,
                current_extractor_versions={}, excluded_paths=exclusions,
            )
        except ExportFreshnessError as error:
            assert error.result.status == "stale"
        else:
            raise AssertionError("expected stale source error")
    assert store.list_export_journals() == ()
    assert not (tmp_path / "AGENTS.md").exists()


def test_unresolved_journal_blocks_review_and_stage_publication(tmp_path: Path) -> None:
    from repo2skill.ledger import ExportRecoveryRequired

    store, _project, identity, policy_revision, exclusions, targets, run, bundle = _scenario(tmp_path)
    stamp = "2026-10-09T00:00:00Z"
    journal = ExportJournal(
        "1.0", "0.1.0", "export_0123456789abcdef01234567", RUN_ID, 1,
        (targets[0].targetId,), (), {}, None, "incomplete", stamp, stamp,
    )
    with store.mutation():
        store.write_export_journal(journal)
        state = store.read_state()
        assert state is not None
        with pytest.raises(ExportRecoveryRequired):
            review_claims(
                state, run, (), targets, store=store,
                current_identity=identity, current_root_label="repo",
                current_policy_revision=policy_revision,
                current_extractor_versions={}, excluded_paths=exclusions,
            )
        with pytest.raises(ExportRecoveryRequired):
            publish_stage_revision(
                bundle, run, store=store, selected_specs=targets,
                stage_revision=2,
            )
    assert not store.immutable_path_exists(f"staging/{RUN_ID}/r2")
