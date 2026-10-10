from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore, encode_record
from repo2skill.maintenance_models import MaintenanceJob, MaintenanceLifecycle, MaintenancePolicy, MaintenanceState
from repo2skill.maintenance_store import MaintenanceStore
from repo2skill.models import (
    ManifestBlock, ManifestFile, ManifestReference, ManifestSet, ProjectDocument,
    ProjectState, TargetManifest,
)


PROJECT_ID = "project_0123456789abcdef01234567"
JOB_ID = "maintenance-job_0123456789abcdef01234567"


def _store(root: Path) -> tuple[LedgerStore, MaintenanceStore]:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    state_dir = root / ".repo2skill"
    state_dir.mkdir()
    project = ProjectDocument("1.0", "0.1.0", PROJECT_ID, "fixture", ".repo2skill", "worktree",
                              default_scan_policy(), default_claim_policy(), default_targets(root))
    state = ProjectState("1.0", "0.1.0", PROJECT_ID, None, None, (), {}, None, None)
    (state_dir / "project.json").write_text(json.dumps(dataclasses.asdict(project)), encoding="utf-8")
    (state_dir / "state.json").write_text(json.dumps(dataclasses.asdict(state)), encoding="utf-8")
    ledger = LedgerStore(root, state_dir)
    return ledger, MaintenanceStore(root, ledger)


def _job(store: LedgerStore) -> MaintenanceJob:
    from repo2skill.maintenance_approval import _policy_revision
    return MaintenanceJob(JOB_ID, "a" * 40, "b" * 40, ("git-event_0123456789abcdef01234567",),
                          ("src/app.py",), ("python",), "sha256:" + "1" * 64,
                          "sha256:" + "2" * 64, _policy_revision(store), ("skill",), None, {})


def _publish(store: LedgerStore, maint: MaintenanceStore, lifecycle: MaintenanceLifecycle | None = None) -> None:
    job = _job(store)
    with store.mutation():
        maint.write_job(job)
        state = maint.read_state()
        maint.replace_state(dataclasses.replace(
            state, enabled=True, policy=MaintenancePolicy("ask", 1, ("skill",)),
            jobs={job.jobId: lifecycle or MaintenanceLifecycle("pending", None, None, "pending", None, None, None,
                                                               queuedGrantRevision=0)},
        ))


def _active_output(store: LedgerStore, maint: MaintenanceStore, *, kind: str) -> tuple[str, str, bytes, bytes]:
    project = store.read_project()
    assert project is not None
    target = next(item for item in project.targets if item.kind == kind)
    export_id = "export_0123456789abcdef01234567"
    set_id = "manifest-set_0123456789abcdef01234567"
    manifest_id = "target-manifest_0123456789abcdef01234567"
    if kind == "agents-md":
        from repo2skill.render import format_managed_markers
        begin, end = format_managed_markers(project.projectId, "block_0123456789abcdef01234567")
        block_bytes = f"{begin}\nOwned guidance\n{end}\n".encode()
        live_bytes = b"Human preface\n" + block_bytes + b"Human suffix\n"
        block = ManifestBlock(1, "block_0123456789abcdef01234567",
                              "sha256:" + hashlib.sha256(block_bytes).hexdigest(), ())
        files = (ManifestFile(1, "AGENTS.md", "managed-blocks",
                              "sha256:" + hashlib.sha256(live_bytes).hexdigest(), "lf", (block,), {}),)
    else:
        block_bytes = b"whole skill bytes\n"
        live_bytes = block_bytes
        files = (ManifestFile(1, f"{target.livePath}/SKILL.md", "whole-files",
                              "sha256:" + hashlib.sha256(live_bytes).hexdigest(), "lf", (), {}),)
    manifest = TargetManifest("1.0", "0.1.0", manifest_id, export_id, target.targetId,
                              "run_0123456789abcdef01234567", 1, files, (), "2026-10-10T00:00:00Z")
    reference = ManifestReference(1, target.targetId, manifest_id, export_id,
                                  f"manifests/{export_id}/{manifest_id}.json")
    manifest_set = ManifestSet("1.0", "0.1.0", set_id, export_id,
                               {target.targetId: reference}, "2026-10-10T00:00:00Z")
    output_path = store.root / files[0].path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(live_bytes)
    with store.mutation():
        store.write_immutable(f"manifests/{export_id}/{manifest_id}.json", encode_record(manifest))
        store.write_immutable(f"manifests/{export_id}/{set_id}.json", encode_record(manifest_set))
        project_state = store.read_state()
        store.replace_state(dataclasses.replace(project_state, activeExportId=export_id, activeManifestSetId=set_id))
        job = _job(store)
        job = dataclasses.replace(job, targetKinds=(kind,), expectedManifestSetId=set_id,
                                  expectedOutputHashes={files[0].path: files[0].exactHash})
        store_state = maint.read_state()
        maint.write_job(job)
        maint.replace_state(dataclasses.replace(
            store_state, policy=MaintenancePolicy("ask", 1, (kind,)),
            jobs={job.jobId: MaintenanceLifecycle("pending", None, None, "pending", None, None, None,
                                                  queuedGrantRevision=0)},
        ))
    return job.jobId, files[0].path, live_bytes, block_bytes


def test_approve_binds_job_fingerprints(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import approve_job

    store, maint = _store(tmp_path / "repo")
    _publish(store, maint)

    approved = approve_job(store, JOB_ID)

    assert approved.status == "approved"
    assert approved.approvalSource == "user"
    assert approved.approvalBindingHash.startswith("sha256:")
    assert maint.read_state().jobs[JOB_ID] == approved


def test_approval_allows_human_bytes_outside_agents_owned_block(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import approve_job

    store, maint = _store(tmp_path / "repo")
    job_id, path, live, block = _active_output(store, maint, kind="agents-md")
    (store.root / path).write_bytes(b"Human edited preface\n" + block + b"Human suffix\n")

    approved = approve_job(store, job_id)

    assert approved.status == "approved"
    assert (store.root / path).read_bytes().startswith(b"Human edited preface\n")


def test_binding_validation_is_separate_from_owned_output_recheck(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import (
        OwnershipConflictError, approve_job, verify_approval_binding, verify_job_fingerprints,
    )

    store, maint = _store(tmp_path / "repo")
    job_id, path, live, _block = _active_output(store, maint, kind="agents-md")
    approved = approve_job(store, job_id)
    job = maint.read_job(job_id)
    (store.root / path).write_bytes(live.replace(b"Owned guidance", b"Human edit"))

    assert verify_approval_binding(store, job, approved) == approved
    with pytest.raises(OwnershipConflictError):
        verify_job_fingerprints(store, job)


def test_approval_rejects_changed_agents_owned_block_before_runner(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import OwnershipConflictError, approve_job

    store, maint = _store(tmp_path / "repo")
    job_id, path, live, _block = _active_output(store, maint, kind="agents-md")
    changed = live.replace(b"Owned guidance", b"Human edit")
    (store.root / path).write_bytes(changed)

    with pytest.raises(OwnershipConflictError, match="owned output content changed"):
        approve_job(store, job_id)

    assert maint.read_state().jobs[job_id].status == "pending"
    assert (store.root / path).read_bytes() == changed


def test_approval_rejects_changed_whole_file_skill_before_runner(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import OwnershipConflictError, approve_job

    store, _maint = _store(tmp_path / "repo")
    job_id, path, _live, _block = _active_output(store, _maint, kind="skill")
    changed = b"human skill edit\n"
    (store.root / path).write_bytes(changed)

    with pytest.raises(OwnershipConflictError, match="owned output content changed"):
        approve_job(store, job_id)

    assert (store.root / path).read_bytes() == changed


def test_policy_change_does_not_approve_pending_job(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import configure_auto_approval
    from repo2skill.maintenance_worker import run_worker

    store, maint = _store(tmp_path / "repo")
    _publish(store, maint)
    configure_auto_approval(store)

    result = run_worker(store, foreground=True, job_id=JOB_ID)

    assert result.status == "approval-required"
    lifecycle = maint.read_state().jobs[JOB_ID]
    assert lifecycle.status == "pending"
    assert lifecycle.queuedGrantRevision == 0


def test_auto_grant_only_dispatches_job_observed_at_current_revision(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import _matches_current, _policy_revision, _read_grant, auto_approval_binding, configure_auto_approval
    from repo2skill.maintenance_worker import run_worker

    store, maint = _store(tmp_path / "repo")
    _publish(store, maint)
    state = configure_auto_approval(store)
    with store.mutation():
        current = maint.read_state()
        lifecycle = current.jobs[JOB_ID]
        jobs = dict(current.jobs)
        jobs[JOB_ID] = dataclasses.replace(lifecycle, queuedGrantRevision=state.policy.grantRevision)
        maint.replace_state(dataclasses.replace(current, jobs=jobs))

    current = maint.read_state()
    assert _matches_current(store, current, _read_grant(store))
    current_job = maint.read_job(JOB_ID)
    assert current_job.policyRevision == _policy_revision(store)
    assert tuple(sorted(current_job.targetKinds)) == tuple(sorted(current.policy.targetKinds))
    assert current.jobs[JOB_ID].queuedGrantRevision == current.policy.grantRevision
    assert auto_approval_binding(store, current_job, current.jobs[JOB_ID]) is not None

    result = run_worker(store, foreground=True, job_id=JOB_ID)

    assert result.status == "agent-unavailable"
    lifecycle = maint.read_state().jobs[JOB_ID]
    assert lifecycle.status == "approved"
    assert lifecycle.approvalSource == "auto"
    assert lifecycle.queuedGrantRevision == state.policy.grantRevision


def test_auto_grant_is_versioned_and_revocable(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import configure_auto_approval, read_auto_grant, revoke_auto_approval

    store, maint = _store(tmp_path / "repo")
    first = configure_auto_approval(store)
    grant = read_auto_grant(store)
    assert first.policy.approval == "auto"
    assert grant is not None and grant["grantRevision"] == first.policy.grantRevision

    second = configure_auto_approval(store)
    assert second.policy.grantRevision == first.policy.grantRevision + 1
    revoked = revoke_auto_approval(store)
    assert revoked.policy.approval == "ask"
    assert read_auto_grant(store)["status"] == "revoked"


def test_disable_revokes_grant_after_linked_worktree_is_added(tmp_path: Path) -> None:
    from repo2skill.maintenance_approval import configure_auto_approval, read_auto_grant
    from repo2skill.maintenance_hooks import remove_owned_hooks

    store, maint = _store(tmp_path / "repo")
    (store.root / "tracked.txt").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(store.root), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(store.root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                    "commit", "-m", "fixture"], check=True, stdout=subprocess.DEVNULL)
    configure_auto_approval(store)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(store.root), "worktree", "add", "-q", "-b", "linked", str(linked)], check=True)

    result = remove_owned_hooks(store.root, store)
    state = maint.read_state()

    assert result.installed
    assert not state.enabled
    assert state.policy.approval == "ask"
    assert read_auto_grant(store)["status"] == "revoked"


def test_disable_reports_private_grant_revocation_failure(tmp_path: Path, monkeypatch) -> None:
    from repo2skill import maintenance_approval
    from repo2skill.maintenance_approval import configure_auto_approval
    from repo2skill.maintenance_hooks import remove_owned_hooks

    store, maint = _store(tmp_path / "repo")
    configure_auto_approval(store)
    monkeypatch.setattr(maintenance_approval, "_write_grant",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fixture failure")))

    result = remove_owned_hooks(store.root, store)

    assert not result.installed
    assert "private auto grant revocation failed" in result.message
    state = maint.read_state()
    assert not state.enabled
    assert state.policy.approval == "ask"


def test_disable_reports_orphan_active_grant_failure_when_state_is_ask(tmp_path: Path, monkeypatch) -> None:
    from repo2skill import maintenance_approval
    from repo2skill.maintenance_approval import configure_auto_approval, read_auto_grant
    from repo2skill.maintenance_hooks import remove_owned_hooks

    store, maint = _store(tmp_path / "repo")
    configure_auto_approval(store)
    active_grant = read_auto_grant(store)
    with store.mutation():
        state = maint.read_state()
        maint.replace_state(dataclasses.replace(
            state, enabled=True,
            policy=MaintenancePolicy("ask", state.policy.grantRevision, state.policy.targetKinds),
        ))
    assert read_auto_grant(store) == active_grant and active_grant["status"] == "active"
    monkeypatch.setattr(maintenance_approval, "_write_grant",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("fixture failure")))

    result = remove_owned_hooks(store.root, store)

    assert not result.installed
    assert "private auto grant revocation failed" in result.message
    state = maint.read_state()
    assert state.enabled is False
    assert state.policy.approval == "ask"
    assert read_auto_grant(store)["status"] == "active"


def test_state_only_auto_does_not_authorize_worker(tmp_path: Path) -> None:
    from repo2skill.maintenance_worker import run_worker

    store, maint = _store(tmp_path / "repo")
    _publish(store, maint, MaintenanceLifecycle("pending", None, None, "pending", None, None, None,
                                                queuedGrantRevision=1))
    with store.mutation():
        state = maint.read_state()
        maint.replace_state(dataclasses.replace(state, policy=MaintenancePolicy("auto", 1, ("skill",))))

    assert run_worker(store, foreground=True, job_id=JOB_ID).status == "approval-required"


def test_lifecycle_missing_queued_revision_is_backward_compatible() -> None:
    lifecycle = MaintenanceLifecycle("pending", None, None, "pending", None, None, None)
    assert lifecycle.queuedGrantRevision is None
