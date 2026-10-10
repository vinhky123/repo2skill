"""Exact-job approvals and private, revocable maintenance auto grants."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from .git_read import GitReadError, run_git_bounded
from .ids import canonical_json_bytes, effective_policy_revision
from .ledger import LedgerStore
from .maintenance_models import MaintenanceJob, MaintenanceLifecycle, MaintenancePolicy
from .maintenance_store import MaintenanceStore


GRANT_SCHEMA = "1.0"


class StaleApprovalError(ValueError):
    """The requested approval no longer matches the current job or policy."""


class OwnershipConflictError(ValueError):
    """A currently owned output region was changed by a human."""


class AutoGrantRevocationError(OSError):
    """State was disabled, but private grant revocation could not be confirmed."""


class AutoGrantUnavailableError(ValueError):
    """Private metadata is unsafe for auto authority; ask approval remains enabled."""


def _policy_revision(store: LedgerStore) -> str:
    project = store.read_project()
    if project is None:
        raise ValueError("initialized project is required")
    return effective_policy_revision(project.scanPolicy, project.claimPolicy, project.targets, generation_options={})


def _ordinary_git_dir(store: LedgerStore, *, require_exclusive: bool = True) -> Path:
    root = store.root.resolve()
    marker = root / ".git"
    if _redirected(marker) or not marker.is_dir():
        raise ValueError("automatic approval requires an ordinary local .git directory")
    try:
        top = Path(os.fsdecode(run_git_bounded(root, ("rev-parse", "--show-toplevel"), timeout_seconds=2, max_output_bytes=4096).stdout.strip())).resolve()
        git_dir = Path(os.fsdecode(run_git_bounded(root, ("rev-parse", "--absolute-git-dir"), timeout_seconds=2, max_output_bytes=4096).stdout.strip())).resolve()
        common_raw = os.fsdecode(run_git_bounded(root, ("rev-parse", "--git-common-dir"), timeout_seconds=2, max_output_bytes=4096).stdout.strip())
        common = Path(common_raw)
        if not common.is_absolute():
            common = root / common
        common = common.resolve()
        worktrees = run_git_bounded(root, ("worktree", "list", "--porcelain", "-z"), timeout_seconds=2, max_output_bytes=32768).stdout
    except (OSError, ValueError, GitReadError) as error:
        raise ValueError("could not safely resolve private Git metadata") from error
    count = sum(record.startswith(b"worktree ") for record in worktrees.split(b"\0"))
    if top != root or git_dir != marker.resolve() or common != git_dir or (require_exclusive and count != 1):
        raise ValueError("automatic approval is unavailable for linked or shared Git metadata")
    return git_dir


def _redirected(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return True
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & reparse)


def _grant_path(store: LedgerStore, *, create: bool = False, allow_shared: bool = False) -> Path:
    git_dir = _ordinary_git_dir(store, require_exclusive=not allow_shared)
    directory = git_dir / "repo2skill"
    maintenance = directory / "maintenance"
    for candidate in (directory, maintenance):
        if candidate.exists() or candidate.is_symlink():
            if _redirected(candidate) or not candidate.is_dir():
                raise ValueError("private grant directory is redirected or not a directory")
        elif create:
            candidate.mkdir(mode=0o700)
        else:
            return maintenance / "auto-grant.json"
    return maintenance / "auto-grant.json"


def _read_grant(store: LedgerStore, *, allow_shared: bool = True) -> dict[str, Any] | None:
    try:
        path = _grant_path(store, allow_shared=allow_shared)
        if not path.exists():
            return None
        if _redirected(path) or not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schemaVersion") != GRANT_SCHEMA:
            return None
        return raw
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def read_auto_grant(store: LedgerStore) -> dict[str, Any] | None:
    """Read the private grant, including revoked records, when metadata is safe."""
    return _read_grant(store)


def _write_grant(store: LedgerStore, document: dict[str, Any], *, allow_shared: bool = False) -> None:
    path = _grant_path(store, create=True, allow_shared=allow_shared)
    payload = canonical_json_bytes(document) + b"\n"
    fd, name = tempfile.mkstemp(prefix=".auto-grant-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _matches_current(store: LedgerStore, state, grant: dict[str, Any] | None = None) -> bool:
    grant = grant if grant is not None else _read_grant(store)
    project = store.read_project()
    if not grant or project is None:
        return False
    try:
        _ordinary_git_dir(store, require_exclusive=True)
    except ValueError:
        return False
    return (
        state.enabled and state.policy.approval == "auto"
        and grant.get("schemaVersion") == GRANT_SCHEMA
        and grant.get("status") == "active"
        and grant.get("projectId") == project.projectId
        and grant.get("root") == str(store.root.resolve())
        and grant.get("policyRevision") == _policy_revision(store)
        and grant.get("targetKinds") == sorted(state.policy.targetKinds)
        and grant.get("grantRevision") == state.policy.grantRevision
    )


def auto_approval_binding(store: LedgerStore, job: MaintenanceJob, lifecycle: MaintenanceLifecycle) -> str | None:
    maint = MaintenanceStore(store.root, store)
    state = maint.read_state()
    grant = _read_grant(store)
    if not _matches_current(store, state, grant):
        return None
    if lifecycle.queuedGrantRevision is None or lifecycle.queuedGrantRevision != state.policy.grantRevision:
        return None
    if job.policyRevision != _policy_revision(store) or tuple(sorted(job.targetKinds)) != tuple(sorted(state.policy.targetKinds)):
        return None
    return _binding_hash(job, state.policy.grantRevision, "auto")


def verify_approval_binding(
    store: LedgerStore, job: MaintenanceJob,
    lifecycle: MaintenanceLifecycle | None = None,
) -> MaintenanceLifecycle:
    """Validate the exact approval and current grant revision without checking outputs."""
    maint = MaintenanceStore(store.root, store)
    state = maint.read_state()
    current = state.jobs.get(job.jobId) if lifecycle is None else lifecycle
    persisted_job = maint.read_job(job.jobId)
    if persisted_job is None or persisted_job != job:
        raise StaleApprovalError("immutable job changed since approval")
    if current is None or current.status not in {"approved", "running"}:
        raise StaleApprovalError("job does not have an active approval")
    if state.jobs.get(job.jobId) != current:
        raise StaleApprovalError("job lifecycle changed since approval")
    if job.policyRevision != _policy_revision(store):
        raise StaleApprovalError("project policy changed after approval")
    if current.approvalSource == "auto":
        expected = auto_approval_binding(store, job, current)
    elif current.approvalSource == "user":
        expected = _binding_hash(job, state.policy.grantRevision, "user")
    else:
        expected = None
    if expected is None or current.approvalBindingHash != expected:
        raise StaleApprovalError("approval binding no longer matches the exact job and policy")
    return current


def _binding_hash(job: MaintenanceJob, revision: int, source: str) -> str:
    from .ledger import encode_record
    raw = canonical_json_bytes({"job": encode_record(job), "grantRevision": revision, "source": source})
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def verify_job_fingerprints(store: LedgerStore, job: MaintenanceJob) -> None:
    """Recheck the active manifest set, recorded fingerprints, and owned regions."""
    project_state = store.read_state()
    if project_state is None:
        raise StaleApprovalError("project state is missing")
    if project_state.activeManifestSetId != job.expectedManifestSetId:
        raise StaleApprovalError("active output manifest changed since the job was queued")
    if job.expectedOutputHashes:
        from .ownership import _active_manifests
        manifest_set, manifests = _active_manifests(store, project_state)
        if (manifest_set.manifestSetId if manifest_set else None) != job.expectedManifestSetId:
            raise StaleApprovalError("active output manifest changed since the job was queued")
        actual = {file.path: file.exactHash for manifest in manifests.values() for file in manifest.files}
        if any(actual.get(path) != digest for path, digest in job.expectedOutputHashes.items()):
            raise StaleApprovalError("owned output manifest fingerprints changed since the job was queued")
        project = store.read_project()
        assert project is not None
        specs = tuple(spec for spec in project.targets if spec.kind in job.targetKinds)
        from .ownership import inspect_target_ownership
        conflicts = inspect_target_ownership(specs, store=store)
        if conflicts:
            raise OwnershipConflictError("owned output content changed since the job was queued")


def approve_job(
    store: LedgerStore, job_id: str, *, grant_revision: int | None = None,
) -> MaintenanceLifecycle:
    maint = MaintenanceStore(store.root, store)
    with store.mutation():
        state = maint.read_state()
        lifecycle = state.jobs.get(job_id)
        job = maint.read_job(job_id)
        if lifecycle is None or job is None:
            raise LookupError("maintenance job does not exist")
        if lifecycle.status not in {"pending", "failed"}:
            raise StaleApprovalError("job is no longer awaiting approval or retry")
        revision = state.policy.grantRevision
        if grant_revision is not None and grant_revision != revision:
            raise StaleApprovalError("approval policy revision changed")
        if job.policyRevision != _policy_revision(store):
            raise StaleApprovalError("project policy changed since the job was queued")
        verify_job_fingerprints(store, job)
        approved = replace(lifecycle, status="approved", approvalSource="user",
                           approvalBindingHash=_binding_hash(job, revision, "user"), failureCode=None)
        jobs = dict(state.jobs)
        jobs[job_id] = approved
        maint.replace_state(replace(state, jobs=jobs))
        return approved


def configure_auto_approval(store: LedgerStore):
    """Explicitly create or rotate private auto authority before mirroring policy."""
    maint = MaintenanceStore(store.root, store)
    with store.mutation():
        state = maint.read_state()
        revision = state.policy.grantRevision + 1
        policy = replace(state.policy, approval="auto", grantRevision=revision)
        # Write authority first; a failed state mirror leaves a mismatched grant.
        try:
            _write_grant(store, _grant_document_for(store, state, revision, "active"))
        except (OSError, ValueError) as error:
            current_grant = _read_grant(store)
            if current_grant is not None and current_grant.get("status") == "active":
                revoked = dict(current_grant)
                revoked.update({"status": "revoked", "grantRevision": revision})
                try:
                    _write_grant(store, revoked, allow_shared=True)
                except (OSError, ValueError):
                    pass
            fallback = replace(
                state, enabled=True,
                policy=MaintenancePolicy("ask", revision, state.policy.targetKinds),
            )
            maint.replace_state(fallback)
            raise AutoGrantUnavailableError(
                "private Git metadata is unsafe for automatic approval; maintenance remains enabled with ask approval; use maintain setup --manual-hooks"
            ) from error
        updated = replace(state, enabled=True, policy=policy)
        maint.replace_state(updated)
        return updated


def _grant_document_for(store: LedgerStore, state, revision: int, status: str) -> dict[str, Any]:
    project = store.read_project()
    if project is None:
        raise ValueError("initialized project is required")
    return {
        "schemaVersion": GRANT_SCHEMA,
        "projectId": project.projectId,
        "root": str(store.root.resolve()),
        "policyRevision": _policy_revision(store),
        "targetKinds": sorted(state.policy.targetKinds),
        "grantRevision": revision,
        "status": status,
    }


def revoke_auto_approval(store: LedgerStore, *, enabled: bool = False):
    """Revoke private authority first, then persist ask/disabled policy state."""
    maint = MaintenanceStore(store.root, store)
    with store.mutation():
        state = maint.read_state()
        revision = state.policy.grantRevision + (1 if state.policy.approval == "auto" else 0)
        revocation_error: OSError | None = None
        try:
            path = _grant_path(store, allow_shared=True)
            if path.exists() or path.is_symlink():
                grant = _read_grant(store)
                if grant is None:
                    raise ValueError("private auto grant exists but is unreadable or malformed")
                if grant.get("status") == "active":
                    revoked = dict(grant)
                    revoked["status"] = "revoked"
                    revoked["grantRevision"] = revision
                    _write_grant(store, revoked, allow_shared=True)
        except (OSError, ValueError) as error:
            if state.policy.approval == "auto":
                revocation_error = OSError("private auto grant revocation could not be confirmed")
                revocation_error.__cause__ = error
        updated = replace(state, enabled=enabled,
                          policy=MaintenancePolicy("ask", revision, state.policy.targetKinds))
        maint.replace_state(updated)
        if revocation_error is not None:
            raise AutoGrantRevocationError("disabled in state, but private auto grant revocation failed") from revocation_error
        return updated
