from __future__ import annotations

from pathlib import Path

import pytest

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore
from repo2skill.models import ProjectDocument, ProjectState
from repo2skill.ownership import inspect_target_ownership


def _store(root: Path) -> tuple[LedgerStore, tuple]:
    targets = default_targets(root)
    project = ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "repo",
        ".repo2skill", "no-git", default_scan_policy(), default_claim_policy(), targets,
    )
    store = LedgerStore(root, root / ".repo2skill")
    with store.mutation():
        import json
        from repo2skill.ids import canonical_json_bytes
        store.write_immutable("project.json", json.loads(canonical_json_bytes(project)))
        store.read_state()
        store.replace_state(ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None))
    return store, targets


def test_inspect_target_ownership_is_clean_and_does_not_create_state(tmp_path: Path) -> None:
    store, targets = _store(tmp_path)
    assert inspect_target_ownership(targets[:1], store=store) == ()


def test_inspect_target_ownership_is_read_only_without_state_directory(tmp_path: Path) -> None:
    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    assert inspect_target_ownership((), store=store) == ()
    assert not store.state_dir.exists()


def test_export_without_committed_source_guard_keeps_live_freshness_check(tmp_path: Path, monkeypatch) -> None:
    import repo2skill.ownership as ownership_module
    from tests.test_export_recovery import _scenario

    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    calls = []

    def reject_stale(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("default live freshness check")

    monkeypatch.setattr(ownership_module, "_recheck", reject_stale)
    with store.mutation(), pytest.raises(RuntimeError, match="default live freshness check"):
        ownership_module.export_staged(
            "run_0123456789abcdef01234567", (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )

    assert calls == [True]
    assert store.list_export_journals() == ()
    assert not (tmp_path / "AGENTS.md").exists()


def test_export_conflicts_on_denied_owned_output_read_without_advancing_authority(
    tmp_path: Path, monkeypatch,
) -> None:
    import repo2skill.maintenance_worker as worker
    from repo2skill.ownership import export_staged
    from repo2skill.safe_fs import GuardedDirectory
    from tests.test_export_recovery import _scenario

    monkeypatch.setattr(
        worker, "run_worker",
        lambda *_args, **_kwargs: pytest.fail("ownership export must not enter the worker"),
    )
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    output = tmp_path / "AGENTS.md"
    protected = b"Human notes remain byte-for-byte\n"
    output.write_bytes(protected)
    authority_before = store.read_state()
    assert authority_before is not None
    original_open_file = GuardedDirectory.open_file
    denied: list[str] = []

    def deny_actual_output_open(guard, name: str):
        if guard.path == tmp_path and name == "AGENTS.md":
            denied.append(name)
            raise PermissionError("injected owned-output read denial")
        return original_open_file(guard, name)

    monkeypatch.setattr(GuardedDirectory, "open_file", deny_actual_output_open)
    with store.mutation():
        result = export_staged(
            "run_0123456789abcdef01234567", (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )

    assert denied == ["AGENTS.md"]
    assert result.status == "conflict"
    assert result.conflicts and result.conflicts[0].code == "export.preflight-conflict"
    assert store.list_export_journals() == ()
    assert store.read_state() == authority_before
    assert output.read_bytes() == protected

    monkeypatch.setattr(GuardedDirectory, "open_file", original_open_file)
    with store.mutation():
        recovered = export_staged(
            "run_0123456789abcdef01234567", (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert recovered.status == "complete"
    assert output.read_bytes().startswith(protected)
    assert store.read_state().activeExportId == recovered.exportId


def test_export_journals_denied_owned_output_write_and_resumes_safely(
    tmp_path: Path, monkeypatch,
) -> None:
    import os
    import repo2skill.maintenance_worker as worker
    from repo2skill.ownership import export_staged, resume_export
    from repo2skill import output_fs
    from tests.test_export_recovery import _active_ownership, _scenario

    monkeypatch.setattr(
        worker, "run_worker",
        lambda *_args, **_kwargs: pytest.fail("ownership export must not enter the worker"),
    )
    store, _project, identity, policy_revision, exclusions, targets, _run, _bundle = _scenario(tmp_path)
    with store.mutation():
        first = export_staged(
            "run_0123456789abcdef01234567", (targets[0].targetId,), {}, store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert first.status == "complete"

    agents = tmp_path / "AGENTS.md"
    human_prefix, human_suffix = b"Human prefix\n", b"Human suffix\n"
    agents.write_bytes(human_prefix + agents.read_bytes() + human_suffix)
    skill_file = tmp_path / targets[1].livePath / "SKILL.md"
    authority_before = store.read_state()
    assert authority_before is not None and authority_before.activeExportId == first.exportId
    original_replace = os.replace
    denied: list[str] = []

    def deny_actual_output_replace(source, destination, *args, **kwargs):
        source_name = Path(str(source)).name
        destination_name = Path(str(destination)).name
        if source_name.startswith(".repo2skill-") and destination_name == skill_file.name:
            denied.append(destination_name)
            raise PermissionError("injected owned-output replace denial")
        return original_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(output_fs.os, "replace", deny_actual_output_replace)
    with store.mutation():
        incomplete = export_staged(
            "run_0123456789abcdef01234567", (targets[1].targetId,), _active_ownership(store),
            store=store, current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )

    assert denied == ["SKILL.md"]
    assert incomplete.status == "incomplete"
    journal = store.read_export_journal(f"exports/{incomplete.exportId}.json")
    assert journal is not None and journal.status == "incomplete"
    assert any(item.path.endswith("/SKILL.md") and item.status == "planned" for item in journal.operations)
    assert store.read_state() == authority_before
    assert not skill_file.exists()
    assert agents.read_bytes().startswith(human_prefix)
    assert agents.read_bytes().endswith(human_suffix)

    monkeypatch.setattr(output_fs.os, "replace", original_replace)
    with store.mutation():
        recovered = resume_export(
            incomplete.exportId, _active_ownership(store), store=store,
            current_identity=identity, current_root_label="repo",
            current_policy_revision=policy_revision,
            current_extractor_versions={}, excluded_paths=exclusions,
        )
    assert recovered.status == "complete"
    assert skill_file.exists()
    assert store.read_state().activeExportId == incomplete.exportId
    assert agents.read_bytes().startswith(human_prefix)
    assert agents.read_bytes().endswith(human_suffix)
