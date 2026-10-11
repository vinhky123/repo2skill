from __future__ import annotations

import os
import json
import hashlib
from pathlib import Path
import subprocess
import dataclasses

import pytest

import repo2skill.maintenance_git as maintenance_git
from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore, encode_record
from repo2skill.ids import canonical_record_id
from repo2skill.maintenance_git import (
    CommitImpact, MaintenanceGitError, _parse_diff, bootstrap_commit,
    compare_commits, enqueue_event, materialize_commit, parse_hook_event,
    read_commit_blob,
)
from repo2skill.maintenance_models import GitEvent
from repo2skill.maintenance_store import MaintenanceStore
from repo2skill.models import (
    ExtractionRead, InputSelector, ManifestFile, ManifestReference, ManifestSet,
    ProjectDocument, ProjectState, RunLedger, SourceSnapshot, TargetManifest, TargetSpec,
)
from repo2skill.ids import effective_policy_revision


def _git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args], input=input, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=True,
    )
    return result.stdout


def _repo(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "user.email", "fixture@example.invalid")
    return root


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").decode().strip()


def _project(root: Path, *, targets: tuple[TargetSpec, ...] | None = None) -> ProjectDocument:
    return ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture",
        ".repo2skill", "worktree", default_scan_policy(), default_claim_policy(),
        targets or default_targets(root),
    )


def _store(
    root: Path, *, targets: tuple[TargetSpec, ...] | None = None,
    applied_baseline: str | None = None, run: RunLedger | None = None,
) -> LedgerStore:
    state_dir = root / ".repo2skill"
    state_dir.mkdir(exist_ok=True)
    project = _project(root, targets=targets)
    state = ProjectState(
        "1.0", "0.1.0", project.projectId,
        None if run is None else run.runId, None, (), {}, None, None,
    )
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    store = LedgerStore(root, state_dir)
    if run is not None:
        with store.mutation():
            store.write_immutable(f"runs/{run.runId}.json", encode_record(run))
    if applied_baseline is not None:
        maintenance = MaintenanceStore(root, store)
        with store.mutation():
            maintenance.replace_state(dataclasses.replace(
                maintenance.read_state(), appliedBaseline=applied_baseline,
            ))
    return store


def _active_run(root: Path) -> RunLedger:
    policy = default_scan_policy()
    selectors = (
        InputSelector("glob", "*.py", ".", False),
        InputSelector("negative", "filename-absent:Cargo.toml", ".", False),
    )
    from repo2skill.extractors.base import ExtractionSession
    from repo2skill.inventory import collect_inventory
    from repo2skill.identity import capture_repository
    from repo2skill.dependencies import build_snapshot

    controls = []
    inventory = collect_inventory(root, policy, control_sink=controls.append)
    identity = capture_repository(
        root, inventory, "worktree", project_id="project_0123456789abcdef01234567", policy=policy,
    )
    snapshot_id = "snapshot_0123456789abcdef01234567"
    with ExtractionSession(
        root=root, snapshot_id=snapshot_id, inventory=inventory, policy=policy,
        repository_id=identity.repositoryId, prior_evidence=(), prior_facts=(),
        excluded_paths=(), control_observations=controls,
    ) as session:
        reads = tuple(
            session.bind("sample", "1.0", f"sample.selector-{index}", "1.0").read(selector).dependency
            for index, selector in enumerate(selectors)
        )
    project = _project(root)
    policy_revision = effective_policy_revision(
        project.scanPolicy, project.claimPolicy, project.targets, generation_options={},
    )
    snapshot = build_snapshot(
        snapshot_id, identity, inventory, {"sample": "1.0"}, reads,
        policy_revision=policy_revision, root_label=project.rootLabel,
    )
    return RunLedger(
        "1.0", "0.1.0", "run_0123456789abcdef01234567", snapshot,
        policy, project.claimPolicy, project.targets, reads, (), (), (), (), (), (), {}, True,
    )


def test_diff_uses_commit_blobs_when_worktree_is_dirty(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
    target = _commit(root, "target")
    clean_impact = compare_commits(root, baseline, target)
    (root / "src" / "app.py").write_text("uncommitted secret\n", encoding="utf-8")
    dirty_impact = compare_commits(root, baseline, target)

    assert dirty_impact.diffHash == clean_impact.diffHash
    assert dirty_impact.paths == clean_impact.paths == ("src/app.py",)


def test_committed_change_projection_preserves_and_verifies_status_records(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "modify.py").write_text("before\n", encoding="utf-8")
    (root / "remove.py").write_text("remove\n", encoding="utf-8")
    (root / "rename-before.py").write_text("rename unchanged\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / "modify.py").write_text("after\n", encoding="utf-8")
    (root / "add.py").write_text("new\n", encoding="utf-8")
    (root / "remove.py").unlink()
    (root / "rename-before.py").rename(root / "rename-after.py")
    target = _commit(root, "status changes")
    impact = compare_commits(root, baseline, target)

    statuses = maintenance_git.committed_change_statuses(
        root, baseline, target, impact.diffHash, impact.paths,
    )

    assert set(statuses) == {
        ("M", "modify.py"), ("A", "add.py"), ("D", "remove.py"),
        ("R", "rename-before.py"), ("R", "rename-after.py"),
    }
    with pytest.raises(MaintenanceGitError, match="diff hash"):
        maintenance_git.committed_change_statuses(
            root, baseline, target, "sha256:" + "f" * 64, impact.paths,
        )


def test_bootstrap_change_projection_verifies_inventory_hash_not_projection_hash(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "a.py").write_text("a\n", encoding="utf-8")
    (root / "b.py").write_text("b\n", encoding="utf-8")
    target = _commit(root, "bootstrap")
    impact = bootstrap_commit(root, target)

    statuses = maintenance_git.committed_change_statuses(
        root, None, target, impact.diffHash, impact.paths,
    )

    assert statuses == (("A", "a.py"), ("A", "b.py"))


def test_nul_paths_preserve_renames_and_newlines(tmp_path: Path) -> None:
    # Newline-containing filenames are legal on POSIX; exercise the exact
    # NUL-framed parser payload on Windows, where the filesystem rejects them.
    if os.name == "nt":
        paths, renames, statuses = _parse_diff(
            b"R100\0before\nname.txt\0after\nname.txt\0A\0new\nfile.txt\0"
        )
        assert paths == ("after\nname.txt", "before\nname.txt", "new\nfile.txt")
        assert renames == (("before\nname.txt", "after\nname.txt"),)
        assert statuses
        return
    root = _repo(tmp_path / "repo")
    (root / "before\nname.txt").write_text("same\n", encoding="utf-8")
    baseline = _commit(root, "base")
    _git(root, "mv", "before\nname.txt", "after\nname.txt")
    (root / "new\nfile.txt").write_text("new\n", encoding="utf-8")
    target = _commit(root, "rename")

    impact = compare_commits(root, baseline, target)

    assert impact.paths == ("after\nname.txt", "before\nname.txt", "new\nfile.txt")
    assert impact.renames == (("before\nname.txt", "after\nname.txt"),)


def test_diff_disables_external_diff_and_textconv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path / "repo")
    (root / "source.txt").write_text("one\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / "source.txt").write_text("two\n", encoding="utf-8")
    target = _commit(root, "target")
    marker = tmp_path / "external-diff-ran"
    script = tmp_path / "external-diff.cmd"
    script.write_text(f"@echo off\r\necho ran>{marker}\r\n", encoding="utf-8")
    _git(root, "config", "diff.external", str(script))
    _git(root, "config", "diff.source.textconv", str(script))
    _git(root, "config", "diff.source.binary", "false")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(script))

    compare_commits(root, baseline, target)

    assert not marker.exists()


def test_added_glob_match_and_negative_invalidation_queue_scope(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "old.py").write_text("pass\n", encoding="utf-8")
    baseline = _commit(root, "base")
    run = _active_run(root)
    store = _store(root, applied_baseline=baseline, run=run)
    (root / "new.py").write_text("pass\n", encoding="utf-8")
    glob_target = _commit(root, "add glob match")
    glob_event = GitEvent("git-event_111111111111111111111111", "post-commit", "cursor-1", glob_target)

    glob_impact = compare_commits(root, baseline, glob_target)
    glob_job = enqueue_event(store, glob_event, glob_impact)

    (root / "nested").mkdir()
    (root / "nested" / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    negative_target = _commit(root, "add nested negative match")
    maintenance = MaintenanceStore(root, store)
    with store.mutation():
        maintenance.replace_state(dataclasses.replace(
            maintenance.read_state(), appliedBaseline=glob_target,
        ))
    negative_event = GitEvent("git-event_111111111111111111111112", "post-commit", "cursor-2", negative_target)
    negative_impact = compare_commits(root, glob_target, negative_target)
    negative_job = enqueue_event(store, negative_event, negative_impact)

    assert glob_job is not None
    assert glob_job.changedPaths == ("new.py",)
    assert glob_job.impactScopes == ("sample",)
    assert negative_job is not None
    assert negative_job.changedPaths == ("nested/Cargo.toml",)
    assert negative_job.impactScopes == ("sample",)


def test_deleted_and_control_file_changes_are_conservative(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / "README.md").unlink()
    (root / ".gitignore").write_text("ignored\n*.tmp\n", encoding="utf-8")
    target = _commit(root, "delete and control")

    impact = compare_commits(root, baseline, target)

    assert impact.requiredFullScan is True
    assert {"README.md", ".gitignore"}.issubset(impact.paths)


def test_state_and_output_only_change_has_no_impact(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    baseline = _commit(root, "base")
    (root / ".repo2skill").mkdir()
    (root / ".repo2skill" / "state.json").write_text("{}\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("managed\n", encoding="utf-8")
    target = _commit(root, "state and output")

    impact = compare_commits(root, baseline, target)
    store = _store(root, applied_baseline=baseline)
    event = GitEvent("git-event_222222222222222222222222", "post-commit", "cursor-2", target)
    job = enqueue_event(store, event, impact)
    state = MaintenanceStore(root, store).read_state()

    assert impact.paths == ("AGENTS.md",)
    assert job is None
    assert state.observedEventCursor == event.cursor
    assert state.jobs == {}


def test_enqueue_filters_custom_output_but_keeps_unrelated_agents_source(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    baseline = _commit(root, "base")
    targets = (
        TargetSpec("agents-md", "agents-md", "docs/generated.md", "whole-files", "keep"),
        TargetSpec("skill", "skill", ".agents/skills/fixture", "whole-files", "keep"),
    )
    store = _store(root, targets=targets, applied_baseline=baseline)
    (root / "docs").mkdir()
    (root / "docs" / "generated.md").write_text("generated output\n", encoding="utf-8")
    (root / ".agents").mkdir()
    (root / ".agents" / "unrelated.py").write_text("pass\n", encoding="utf-8")
    target = _commit(root, "output plus source")
    event = GitEvent("git-event_333333333333333333333333", "post-commit", "cursor-3", target)

    job = enqueue_event(store, event, compare_commits(root, baseline, target))

    assert job is not None
    assert job.changedPaths == (".agents/unrelated.py",)


def test_enqueue_reads_actual_exported_manifest_reference_path(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    output = root / "AGENTS.md"
    output.write_text("generated\n", encoding="utf-8")
    baseline = _commit(root, "base with generated output")
    store = _store(root, applied_baseline=baseline)
    project = store.read_project()
    state = store.read_state()
    assert project is not None and state is not None
    target = next(item for item in project.targets if item.kind == "agents-md")
    export_id = "export_0123456789abcdef01234567"
    manifest_id = "manifest_0123456789abcdef01234567"
    manifest_set_id = "manifestset_0123456789abcdef01234567"
    relative_path = f"manifests/{export_id}/{target.targetId}.json"
    exact_hash = "sha256:" + hashlib.sha256(output.read_bytes()).hexdigest()
    manifest = TargetManifest(
        "1.0", "0.1.0", manifest_id, export_id, target.targetId,
        "run_0123456789abcdef01234567", 1,
        (ManifestFile(1, "AGENTS.md", "whole-files", exact_hash, "lf", (), {}),),
        (), "2026-10-09T00:00:00Z",
    )
    manifest_set = ManifestSet(
        "1.0", "0.1.0", manifest_set_id, export_id,
        {target.targetId: ManifestReference(1, target.targetId, manifest_id, export_id, relative_path)},
        "2026-10-09T00:00:00Z",
    )
    with store.mutation():
        store.write_immutable(relative_path, encode_record(manifest))
        store.write_immutable(f"manifests/{export_id}/{manifest_set_id}.json", encode_record(manifest_set))
        store.replace_state(dataclasses.replace(
            store.read_state(), activeExportId=export_id, activeManifestSetId=manifest_set_id,
        ))
    (root / "src" / "main.py").write_text("changed\n", encoding="utf-8")
    target_commit = _commit(root, "source change")
    event = GitEvent("git-event_555555555555555555555555", "post-commit", "cursor-manifest", target_commit)

    job = enqueue_event(store, event, compare_commits(root, baseline, target_commit))

    assert job is not None
    assert job.changedPaths == ("src/main.py",)
    assert job.expectedOutputHashes["AGENTS.md"] == exact_hash


def test_mismatched_empty_impact_is_deferred_before_no_impact_fast_path(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    applied = _commit(root, "applied")
    (root / "README.md").write_text("target\n", encoding="utf-8")
    target = _commit(root, "target")
    store = _store(root, applied_baseline=applied)
    event = GitEvent("git-event_444444444444444444444444", "post-commit", "cursor-4", target)
    wrong = CommitImpact("f" * 40, target, (), (), "sha256:" + "a" * 64, (), False, ())

    job = enqueue_event(store, event, wrong)
    state = MaintenanceStore(root, store).read_state()

    assert job is None
    assert state.observedEventCursor == event.cursor
    assert len(state.deferredEvents) == 1
    assert state.deferredEvents[0].reasonCode == "maintenance.baseline-mismatch"


def test_missing_baseline_requires_rebuild(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("readme\n", encoding="utf-8")
    target = _commit(root, "target")

    with pytest.raises(ValueError, match="missing|rebuild"):
        compare_commits(root, "f" * 40, target)


def test_deferred_event_retries_same_cursor_into_a_published_job(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src.py").write_text("base\n", encoding="utf-8")
    baseline = _commit(root, "baseline")
    store = _store(root, applied_baseline=baseline)
    (root / "src.py").write_text("changed\n", encoding="utf-8")
    target = _commit(root, "target")
    event = GitEvent("git-event_333333333333333333333333", "manual-catch-up", "deferred-cursor", target)

    assert enqueue_event(store, event, None, "maintenance.git-timeout") is None
    job = enqueue_event(store, event, compare_commits(root, baseline, target))

    assert job is not None
    state = MaintenanceStore(root, store).read_state()
    assert state.observedEventCursor == event.cursor
    assert all(item.eventId != event.eventId for item in state.deferredEvents)
    assert job.jobId in state.jobs


def test_deferred_event_retries_after_intervening_cursor_with_real_publication(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src.py").write_text("base\n", encoding="utf-8")
    baseline = _commit(root, "baseline")
    store = _store(root, applied_baseline=baseline)
    (root / "src.py").write_text("changed\n", encoding="utf-8")
    target = _commit(root, "target")
    deferred = GitEvent("git-event_333333333333333333333333", "manual-catch-up", "cursor-deferred", target)
    intervening = GitEvent("git-event_444444444444444444444444", "manual-catch-up", "cursor-later", target)

    assert enqueue_event(store, deferred, None, "maintenance.git-timeout") is None
    intervening_job = enqueue_event(store, intervening, compare_commits(root, baseline, target))
    assert intervening_job is not None
    retried_job = enqueue_event(store, deferred, compare_commits(root, baseline, target))

    assert retried_job is not None
    state = MaintenanceStore(root, store).read_state()
    assert state.observedEventCursor == deferred.cursor
    assert retried_job.jobId in state.jobs
    assert all(item.eventId != deferred.eventId for item in state.deferredEvents)
    published = MaintenanceStore(root, store).read_job(retried_job.jobId)
    assert published is not None and deferred.eventId in published.eventIds


def test_bootstrap_has_explicit_full_tree_impact(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    target = _commit(root, "target")

    impact = bootstrap_commit(root, target)

    assert impact.requiredFullScan is True
    assert impact.baselineCommit is None
    assert impact.paths == ("src/main.py",)


def test_hook_event_resolves_current_commit_for_post_commit_and_merge(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "README.md").write_text("one\n", encoding="utf-8")
    target = _commit(root, "target")

    post_commit = parse_hook_event("post-commit", b"", (), root=root)
    post_merge = parse_hook_event("post-merge", b"", ("1",), root=root)

    assert post_commit.targetCommit == target
    assert post_merge.targetCommit == target
    assert post_commit.hookName == "post-commit"


def test_materialize_and_blob_reads_use_committed_bytes(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_bytes(b"committed\x00bytes")
    target = _commit(root, "target")
    (root / "src" / "main.py").write_bytes(b"dirty worktree")

    assert read_commit_blob(root, target, "src/main.py", 1024) == b"committed\x00bytes"
    manifest = materialize_commit(root, target, ("src/main.py",), tmp_path / "snapshot")

    assert manifest.files[0].path == "src/main.py"
    assert (tmp_path / "snapshot" / "src" / "main.py").read_bytes() == b"committed\x00bytes"
    assert manifest.contextHash.startswith("sha256:")


def test_optional_bootstrap_projection_preserves_legacy_default_hash_and_record(tmp_path: Path) -> None:
    from repo2skill.ids import canonical_json_bytes

    root = _repo(tmp_path / "repo")
    (root / "source.py").write_text("source = True\n", encoding="utf-8")
    target = _commit(root, "target")
    default = materialize_commit(root, target, ("source.py",), tmp_path / "default")
    bootstrap = materialize_commit(
        root, target, ("source.py",), tmp_path / "bootstrap", impact_projection="tree-bootstrap",
    )
    expected_projection = {
        "commit": target,
        "files": [{"path": item.path, "blobId": item.blobId, "rawHash": item.rawHash, "size": item.size}
                  for item in default.files],
        "omittedPaths": [], "policyRevision": "", "extractorRevisions": {},
    }
    expected_hash = "sha256:" + hashlib.sha256(canonical_json_bytes(expected_projection)).hexdigest()
    default_record = maintenance_git.snapshot_manifest_record(default)

    assert default.impactProjection is None
    assert default.contextHash == expected_hash
    assert "impactProjection" not in default_record
    assert bootstrap.impactProjection == "tree-bootstrap"
    assert bootstrap.contextHash != default.contextHash
    assert maintenance_git.snapshot_manifest_record(bootstrap)["impactProjection"] == "tree-bootstrap"


def test_materialize_records_size_cap_as_an_explicit_omission(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repo(tmp_path / "repo")
    (root / "large.txt").write_text("too large for this test cap", encoding="utf-8")
    target = _commit(root, "target")
    monkeypatch.setattr(maintenance_git, "MAX_CONTEXT_FILE_BYTES", 4)

    manifest = materialize_commit(root, target, ("large.txt",), tmp_path / "snapshot")

    assert manifest.files == ()
    assert manifest.omittedPaths == ("large.txt",)


def test_materialize_rejects_symlink_mode_on_every_platform(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    _commit(root, "empty baseline")
    blob = _git(root, "hash-object", "-w", "--stdin", input=b"target.txt").decode().strip()
    _git(root, "update-index", "--add", "--cacheinfo", "120000", blob, "link")
    _git(root, "commit", "-qm", "add symlink mode")
    target = _git(root, "rev-parse", "HEAD").decode().strip()

    with pytest.raises(MaintenanceGitError, match="unsupported") as error:
        materialize_commit(root, target, ("link",), tmp_path / "snapshot")

    assert error.value.code == "maintenance.git-mode-unsupported"


def test_enqueue_persists_context_before_publishing_job_and_cursor(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    target = _commit(root, "target")
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project = ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "fixture",
        ".repo2skill", "worktree", default_scan_policy(), default_claim_policy(),
        default_targets(root),
    )
    state = ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    store = LedgerStore(root, state_dir)
    event = GitEvent("git-event_0123456789abcdef01234567", "post-commit", "cursor-1", target)
    impact = bootstrap_commit(root, target)

    job = enqueue_event(store, event, impact, allow_baseline_bootstrap=True)

    assert job is not None
    manifest_id = canonical_record_id("source-context", {"contextHash": job.sourceContextHash})
    saved = store.read_immutable_bytes(f"maintenance/contexts/{manifest_id}.json")
    assert saved is not None
    committed = store.read_maintenance_state_document()
    assert committed is not None
    assert committed["observedEventCursor"] == event.cursor
    assert job.jobId in committed["jobs"]

def test_full_scan_context_excludes_owned_outputs_but_keeps_agents_source(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("generated instructions\n", encoding="utf-8")
    skill_dir = root / ".agents" / "skills" / root.name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("generated skill\n", encoding="utf-8")
    (root / ".agents" / "unrelated.py").write_text("source\n", encoding="utf-8")
    target = _commit(root, "bootstrap source and outputs")
    store = _store(root)
    event = GitEvent("git-event_555555555555555555555555", "post-commit", "cursor-full", target)

    job = enqueue_event(store, event, bootstrap_commit(root, target), allow_baseline_bootstrap=True)

    assert job is not None
    manifest_id = canonical_record_id("source-context", {"contextHash": job.sourceContextHash})
    saved = store.read_immutable_bytes(f"maintenance/contexts/{manifest_id}.json")
    assert saved is not None
    manifest = json.loads(saved)["manifest"]
    paths = {entry["path"] for entry in manifest["files"]}
    assert paths == {"src/main.py", ".agents/unrelated.py"}
    assert set(manifest["omittedPaths"]) == set()
