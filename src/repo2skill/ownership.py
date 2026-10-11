"""Owned-output inspection, export journaling, and crash recovery."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import difflib
import hashlib
from pathlib import Path
import re
import secrets

from . import __version__
from .dependencies import DependencyRecheckResult, recheck_dependencies
from .ids import canonical_json_bytes
from .ledger import LedgerStore, encode_record
from .models import (
    ExportJournal, ExportResult, Finding, JournalOperation, ManifestBlock,
    ManifestFile, ManifestReference, ManifestSet, OwnershipState, ProjectDocument,
    ProjectState, RepositoryIdentity, RepositoryPath, RunId, ExportId, TargetId,
    RunLedger, TargetManifest, TargetSpec,
)
from .output_fs import (
    OutputConflictError, create_live_directory, missing_parent_directories,
    read_live_file, replace_live_file, unlink_owned_file,
)
from .render import format_managed_markers, managed_block_id
from .stage import read_stage_descriptor


class ExportFreshnessError(RuntimeError):
    """The recorded dependency read set could not be confirmed fresh."""

    def __init__(self, result: DependencyRecheckResult) -> None:
        self.result = result
        super().__init__(f"export dependencies are {result.status}")


_MAX_LIVE_BYTES = 2 * 1024 * 1024


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def manifest_set_id_for_export(export_id: str) -> str:
    """Return the P0 manifest-set ID for an export, shared by apply recovery."""
    return "manifestset_" + hashlib.sha256(export_id.encode()).hexdigest()[:24]


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _decode_live(data: bytes) -> tuple[str, str]:
    if b"\r\n" in data:
        normalized = data.replace(b"\r\n", b"")
        if b"\n" in normalized or b"\r" in normalized:
            raise ValueError("mixed line endings in live output")
        eol = "crlf"
    else:
        if b"\r" in data:
            raise ValueError("mixed line endings in live output")
        eol = "lf"
    return data.decode("utf-8", errors="strict"), eol


def _marker_slice(data: bytes, project_id: str, block_id: str) -> tuple[int, int, bytes]:
    text, _eol = _decode_live(data)
    begin, end = format_managed_markers(project_id, block_id)
    lines = text.splitlines(keepends=True)
    begins: list[int] = []
    ends: list[int] = []
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode("utf-8")))
        bare = line.rstrip("\r\n")
        if bare == begin:
            begins.append(len(offsets) - 2)
        elif bare == end:
            ends.append(len(offsets) - 2)
        elif "repo2skill:begin" in bare or "repo2skill:end" in bare:
            raise ValueError("malformed managed marker")
    if len(begins) != 1 or len(ends) != 1 or ends[0] <= begins[0]:
        raise ValueError("managed marker pair is missing, duplicate, or mismatched")
    first, last = offsets[begins[0]], offsets[ends[0] + 1]
    return first, last, data[first:last]


def _active_manifests(
    store: LedgerStore, state: ProjectState | None,
) -> tuple[ManifestSet | None, dict[str, TargetManifest]]:
    if state is None or state.activeExportId is None or state.activeManifestSetId is None:
        if state is not None and (state.activeExportId is None) != (state.activeManifestSetId is None):
            raise ValueError("active export pointer is incomplete")
        return None, {}
    path = f"manifests/{state.activeExportId}/{state.activeManifestSetId}.json"
    manifest_set = store.read_manifest_set(path)
    if manifest_set is None or manifest_set.exportId != state.activeExportId or manifest_set.manifestSetId != state.activeManifestSetId:
        raise ValueError("active manifest set is missing or mismatched")
    manifests: dict[str, TargetManifest] = {}
    for target_id, reference in manifest_set.targets.items():
        if reference.targetId != target_id:
            raise ValueError("active target manifest reference is inconsistent")
        manifest = store.read_target_manifest(reference.path)
        if (manifest is None or manifest.manifestId != reference.manifestId
            or manifest.targetId != target_id or manifest.exportId != reference.exportId):
            raise ValueError("active target manifest reference is missing or mismatched")
        manifests[target_id] = manifest
    return manifest_set, manifests


def _ownership(manifests: Mapping[str, TargetManifest]) -> dict[str, OwnershipState]:
    result: dict[str, OwnershipState] = {}
    for target_id, manifest in manifests.items():
        hashes = {item.path: item.exactHash for item in manifest.files}
        blocks = {item.path: tuple(block.blockId for block in item.blocks)
                  for item in manifest.files if item.blocks}
        whole = tuple(item.path for item in manifest.files if item.ownershipMode == "whole-files")
        result[target_id] = OwnershipState(
            target_id, manifest.manifestId, hashes, blocks, whole,
            tuple(manifest.createdDirectories),
        )
    return result


def _conflict(target_id: str, path: str | None, message: str, code: str = "ownership.conflict") -> Finding:
    return Finding(1, code, "conflict", message, path, target_id, (), {})


def _ownership_findings(
    specs: Sequence[TargetSpec], manifests: Mapping[str, TargetManifest], store: LedgerStore,
) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    project = store.read_project()
    if project is None:
        return ()
    for spec in specs:
        manifest = manifests.get(spec.targetId)
        if manifest is None:
            continue
        for file in manifest.files:
            try:
                data = read_live_file(store.root, file.path, limit=_MAX_LIVE_BYTES)
                if data is None:
                    findings.append(_conflict(spec.targetId, file.path, "owned output is missing"))
                    continue
                if file.ownershipMode == "whole-files":
                    if _digest(data) != file.exactHash:
                        findings.append(_conflict(spec.targetId, file.path, "whole-file owned output changed"))
                else:
                    if len(file.blocks) != 1:
                        raise ValueError("managed target manifest must contain one block")
                    block = file.blocks[0]
                    _start, _end, owned = _marker_slice(data, project.projectId, block.blockId)
                    if _digest(owned) != block.exactHash:
                        findings.append(_conflict(spec.targetId, file.path, "managed block changed"))
            except (OSError, ValueError) as error:
                findings.append(_conflict(spec.targetId, file.path, str(error)))
    return tuple(findings)


def inspect_target_ownership(specs: Sequence[TargetSpec], *, store: LedgerStore) -> tuple[Finding, ...]:
    """Read current owned paths without creating a lock or state files."""
    project = store.read_project()
    if project is None:
        if specs:
            raise FileNotFoundError("project configuration is required")
        return ()
    configured = {item.targetId: item for item in project.targets}
    if len(configured) != len(project.targets):
        raise ValueError("duplicate configured target ID")
    seen: set[str] = set()
    for spec in specs:
        if spec.targetId in seen or configured.get(spec.targetId) != spec:
            raise ValueError(f"selected target does not match configured target: {spec.targetId}")
        seen.add(spec.targetId)
    state = store.read_state()
    _manifest_set, manifests = _active_manifests(store, state)
    return _ownership_findings(specs, manifests, store)


def _validate_export_context(
    run_id: str, targets: Sequence[str], ownership: Mapping[str, OwnershipState], *,
    store: LedgerStore,
) -> tuple[ProjectDocument, ProjectState, RunLedger, dict[str, object], dict[str, TargetSpec], dict[str, TargetManifest], ManifestSet | None]:
    store._require_mutation()
    project = store.read_project()
    state = store.read_state()
    if project is None or state is None:
        raise FileNotFoundError("project and active state are required")
    if project.projectId != state.projectId:
        raise ValueError("project and state IDs do not match")
    if state.currentRunId != run_id or state.activeStageRevision is None:
        raise ValueError("requested run is not the current active stage")
    run = store.read_run(run_id)
    if run is None:
        raise FileNotFoundError("active run is missing")
    from .maintenance_context import verify_application_authorization
    verify_application_authorization(store, run)
    if tuple(sorted(set(targets))) != tuple(sorted(targets)):
        raise ValueError("target IDs must be unique")
    configured = {spec.targetId: spec for spec in project.targets}
    if len(configured) != len(project.targets):
        raise ValueError("duplicate configured target ID")
    selected_specs: dict[str, TargetSpec] = {}
    for target_id in targets:
        spec = configured.get(target_id)
        if spec is None:
            raise ValueError(f"unknown selected target: {target_id}")
        selected_specs[target_id] = spec
    descriptor = read_stage_descriptor(store, run_id, state.activeStageRevision)
    if not isinstance(descriptor, dict) or not isinstance(descriptor.get("targets"), dict):
        raise ValueError("active stage descriptor is invalid")
    descriptor_targets = descriptor["targets"]
    for target_id, spec in selected_specs.items():
        entry = descriptor_targets.get(target_id)
        expected = {"targetId": spec.targetId, "kind": spec.kind, "livePath": spec.livePath,
                    "ownershipMode": spec.ownershipMode, "emptyBlockPolicy": spec.emptyBlockPolicy}
        if not isinstance(entry, dict) or entry.get("spec") != expected:
            raise ValueError(f"active stage target does not match configured target: {target_id}")
    manifest_set, manifests = _active_manifests(store, state)
    actual = _ownership(manifests)
    if dict(ownership) != actual:
        raise ValueError("supplied ownership does not match active manifest view")
    return project, state, run, descriptor, selected_specs, manifests, manifest_set


@dataclass(frozen=True, slots=True)
class _PlannedFile:
    target_id: str
    path: str
    action: str
    old: bytes | None
    intended: bytes | None
    file_manifest: ManifestFile | None
    patch: bytes | None


def _managed_block(data: bytes, project_id: str, block_id: str) -> bytes:
    return _marker_slice(data, project_id, block_id)[2]


def _replace_block(data: bytes, project_id: str, block_id: str, generated: bytes | None) -> bytes:
    start, end, _old = _marker_slice(data, project_id, block_id)
    return data[:start] + (generated or b"") + data[end:]


def _generated_block(data: bytes, project_id: str) -> tuple[str, bytes]:
    text, _eol = _decode_live(data)
    marker = re.compile(r"<!-- repo2skill:begin project=([^ ]+) block=([^ ]+) -->")
    matches = [marker.fullmatch(line.rstrip("\r\n")) for line in text.splitlines(keepends=True)]
    found = [item for item in matches if item is not None]
    if len(found) != 1 or found[0].group(1) != project_id:
        raise ValueError("staged managed markers do not match project")
    block_id = found[0].group(2)
    return block_id, _managed_block(data, project_id, block_id)


def _eol_bytes(data: bytes, eol: str) -> bytes:
    normalized = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return normalized if eol == "lf" else normalized.replace(b"\n", b"\r\n")


def _line_offset(data: bytes, start: int) -> int:
    return data[:start].count(b"\n")


def _diff(old: bytes, new: bytes) -> bytes:
    old_lines = old.decode("utf-8").splitlines(keepends=True)
    new_lines = new.decode("utf-8").splitlines(keepends=True)
    return "".join(difflib.unified_diff(old_lines, new_lines, fromfile="live", tofile="generated")).encode("utf-8")


def _recheck(
    run: RunLedger, store: LedgerStore, current_identity: RepositoryIdentity,
    current_root_label: str, current_policy_revision: str,
    current_extractor_versions: Mapping[str, str], excluded_paths: Sequence[str],
) -> None:
    result = recheck_dependencies(
        run, current_identity, current_policy_revision, current_extractor_versions,
        root=store.root, current_root_label=current_root_label,
        excluded_paths=excluded_paths,
    )
    if result.status != "fresh":
        raise ExportFreshnessError(result)


def _next_timestamp() -> str:
    return _timestamp()


def _target_inputs(
    store: LedgerStore, descriptor: Mapping[str, object], specs: Mapping[str, TargetSpec],
    run: RunLedger, project: ProjectDocument, state: ProjectState,
    manifests: Mapping[str, TargetManifest], *, delete_empty_owned_blocks: bool,
) -> tuple[list[_PlannedFile], dict[str, list[str]]]:
    entries = descriptor["targets"]
    if not isinstance(entries, dict):
        raise ValueError("stage descriptor target map is invalid")
    accepted: dict[str, tuple[dict[str, object], dict[str, bytes]]] = {}
    prefix = f"staging/{run.runId}/r{state.activeStageRevision}"
    for target_id, spec in specs.items():
        entry = entries[target_id]
        assert isinstance(entry, dict)
        mode = entry.get("accepted")
        if not isinstance(mode, dict):
            raise ValueError("active stage accepted map is invalid")
        files: dict[str, bytes] = {}
        for path, metadata in mode.items():
            if not isinstance(metadata, dict):
                raise ValueError("stage file metadata is invalid")
            data = store.read_immutable_bytes(f"{prefix}/targets/{target_id}/accepted/{path}")
            if data is None or _digest(data) != metadata.get("sha256"):
                raise ValueError("active staged bytes failed descriptor hash validation")
            files[path] = data
            spans = metadata.get("claimSpans")
            if not isinstance(spans, dict):
                raise ValueError("stage claim spans are invalid")
            for claim_ref in spans:
                claim_id, revision_text = claim_ref.rsplit("@", 1)
                if state.acceptedClaimRevisions.get(claim_id) != int(revision_text):
                    raise ValueError("active stage contains a claim that is not currently accepted")
        accepted[target_id] = (mode, files)

    planned: list[_PlannedFile] = []
    mkdirs: dict[str, list[str]] = {}
    for target_id in sorted(specs):
        spec = specs[target_id]
        prior = manifests.get(target_id)
        prior_files = {item.path: item for item in prior.files} if prior else {}
        dirs_for_target = mkdirs.setdefault(target_id, [])
        mode, files = accepted[target_id]
        if spec.kind == "agents-md":
            path = spec.livePath
            old = read_live_file(store.root, path, limit=_MAX_LIVE_BYTES)
            previous = prior_files.get(path)
            staged = files.get(path)
            if (staged is not None and delete_empty_owned_blocks
                    and not mode[path]["claimSpans"]):
                if previous is None:
                    continue
                if old is None or len(previous.blocks) != 1:
                    raise ValueError("owned managed output is missing or malformed")
                block = previous.blocks[0]
                _start, _end, current_block = _marker_slice(old, project.projectId, block.blockId)
                if _digest(current_block) != block.exactHash:
                    raise ValueError("owned managed block changed")
                intended = _replace_block(old, project.projectId, block.blockId, None)
                planned.append(_PlannedFile(target_id, path, "replace", old, intended, None, _diff(old, intended)))
                continue
            if staged is None:
                if previous is None:
                    continue
                if not delete_empty_owned_blocks:
                    raise ValueError("active stage omitted an owned managed output")
                if len(previous.blocks) != 1:
                    raise ValueError("managed manifest must contain one block")
                if old is None:
                    raise ValueError("owned managed output is missing")
                block = previous.blocks[0]
                _start, _end, current_block = _marker_slice(old, project.projectId, block.blockId)
                if _digest(current_block) != block.exactHash:
                    raise ValueError("owned managed block changed")
                intended = _replace_block(old, project.projectId, block.blockId, None)
                planned.append(_PlannedFile(target_id, path, "replace", old, intended, None, _diff(old, intended)))
                continue
            generated = staged
            block_id, block_bytes = _generated_block(generated, project.projectId)
            eol = "lf"
            if old is None:
                intended = generated
                blocks = (ManifestBlock(1, block_id, _digest(block_bytes),
                                        tuple(sorted({ref.split("@", 1)[0] for ref in mode[path]["claimSpans"]}))),)
                spans = mode[path]["claimSpans"]
            else:
                _text, eol = _decode_live(old)
                generated = _eol_bytes(generated, eol)
                block_bytes = _managed_block(generated, project.projectId, block_id)
                if previous is not None:
                    if len(previous.blocks) != 1:
                        raise ValueError("managed manifest must contain one block")
                    prior_block = previous.blocks[0]
                    current_block = _managed_block(old, project.projectId, prior_block.blockId)
                    if _digest(current_block) != prior_block.exactHash:
                        raise ValueError("owned managed block changed")
                    start, _end, _ = _marker_slice(old, project.projectId, prior_block.blockId)
                    intended = _replace_block(old, project.projectId, prior_block.blockId, block_bytes)
                    offset = _line_offset(intended, start) - _line_offset(generated, _marker_slice(generated, project.projectId, block_id)[0])
                else:
                    # Existing unmanaged text is preserved exactly; unknown markers fail closed.
                    if b"repo2skill:begin" in old or b"repo2skill:end" in old:
                        raise ValueError("unowned managed marker already exists")
                    separator = b"" if not old or old.endswith((b"\n", b"\r")) else (b"\n" if eol == "lf" else b"\r\n")
                    intended = old + separator + generated
                    offset = old.count(b"\n") + (1 if separator else 0)
                old_start = _marker_slice(intended, project.projectId, block_id)[0]
                actual_offset = _line_offset(intended, old_start)
                staged_start = _marker_slice(generated, project.projectId, block_id)[0]
                base_offset = _line_offset(generated, staged_start)
                delta = actual_offset - base_offset
                spans = {key: [[pair[0] + delta, pair[1] + delta] for pair in value]
                         for key, value in mode[path]["claimSpans"].items()}
                claim_ids = tuple(sorted({key.split("@", 1)[0] for key in spans}))
                old_block_revision = previous.blocks[0].recordRevision if previous else 0
                blocks = (ManifestBlock(old_block_revision + 1, block_id, _digest(block_bytes), claim_ids),)
            if old is None:
                # Stage spans are already measured in the generated full file.
                spans = mode[path]["claimSpans"]
            file_manifest = ManifestFile(
                (previous.recordRevision + 1) if previous else 1, path,
                "managed-blocks", _digest(intended), eol, blocks,
                {key: tuple(tuple(pair) for pair in value) for key, value in spans.items()},
            )
            if old != intended:
                planned.append(_PlannedFile(target_id, path, "replace", old, intended, file_manifest, _diff(old or b"", intended)))
            else:
                planned.append(_PlannedFile(target_id, path, "replace", old, intended, file_manifest, None))
        else:
            expected_paths = set(files)
            if prior_files and not expected_paths:
                raise ValueError(
                    "active Skill stage is empty and does not prove explicit whole-file deletion intent"
                )
            for path, staged in sorted(files.items()):
                old = read_live_file(store.root, path, limit=_MAX_LIVE_BYTES)
                previous = prior_files.get(path)
                if previous is not None and (old is None or _digest(old) != previous.exactHash):
                    raise ValueError("whole-file owned output changed or is missing")
                if previous is None and old is not None:
                    raise ValueError("unowned whole-file output already exists")
                text, eol = _decode_live(staged)
                del text
                if old is not None:
                    _old_text, eol = _decode_live(old)
                    staged = _eol_bytes(staged, eol)
                spans = mode[path]["claimSpans"]
                file_manifest = ManifestFile(
                    (previous.recordRevision + 1) if previous else 1, path,
                    "whole-files", _digest(staged), eol, (),
                    {key: tuple(tuple(pair) for pair in value) for key, value in spans.items()},
                )
                if old != staged:
                    planned.append(_PlannedFile(target_id, path, "replace", old, staged, file_manifest, _diff(old or b"", staged)))
                else:
                    planned.append(_PlannedFile(target_id, path, "replace", old, staged, file_manifest, None))
        for item in planned:
            if item.target_id == target_id:
                dirs_for_target.extend(missing_parent_directories(store.root, item.path))
        mkdirs[target_id] = list(dict.fromkeys(dirs_for_target))
    return planned, mkdirs


def _operation_id(export_id: str, target_id: str, path: str, action: str) -> str:
    raw = canonical_json_bytes({"exportId": export_id, "targetId": target_id, "path": path, "action": action})
    return "operation_" + hashlib.sha256(raw).hexdigest()[:24]


def _state_prefix(store: LedgerStore) -> str:
    return store.state_dir.relative_to(store.root).as_posix()


def _result(journal: ExportJournal, manifest_ids: Mapping[str, str] | None = None) -> ExportResult:
    complete = tuple(
        target for target in journal.targetIds
        if all(op.status == "complete" for op in journal.operations if op.targetId == target)
    )
    remaining = tuple(target for target in journal.targetIds if target not in complete)
    conflicts = tuple(_conflict(op.targetId, op.path, "live output changed during export")
                      for op in journal.operations if op.status == "conflict")
    return ExportResult(journal.exportId, "complete" if journal.status == "committed" else
                        "conflict" if journal.status == "conflict" else "incomplete",
                        complete, remaining, conflicts, dict(manifest_ids or {}))


def _write_journal(store: LedgerStore, journal: ExportJournal) -> ExportJournal:
    updated = replace(journal, updatedAt=_next_timestamp())
    store.write_export_journal(updated)
    return updated


def _write_immutable_record(store: LedgerStore, relative_path: str, record: object) -> None:
    payload = canonical_json_bytes(encode_record(record)) + b"\n"
    if store.immutable_path_exists(relative_path):
        existing = store.read_immutable_bytes(relative_path)
        if existing != payload:
            raise ValueError(f"immutable export record already exists with different contents: {relative_path}")
        return
    store.write_immutable(relative_path, encode_record(record))


def _perform_journal(store: LedgerStore, journal: ExportJournal) -> ExportJournal:
    prefix = _state_prefix(store)
    operations = list(journal.operations)
    created = {key: list(value) for key, value in journal.createdDirectories.items()}
    for index, op in enumerate(operations):
        if op.status == "complete":
            continue
        if op.action == "mkdir":
            proof = op.path in created.get(op.targetId, [])
            try:
                made = create_live_directory(store.root, op.path)
                if not made and not proof:
                    operations[index] = replace(op, status="conflict")
                    journal = _write_journal(store, replace(journal, operations=tuple(operations), status="conflict", createdDirectories={k: tuple(v) for k, v in created.items()}))
                    return journal
                if made and op.path not in created.setdefault(op.targetId, []):
                    created[op.targetId].append(op.path)
                operations[index] = replace(op, status="complete")
                journal = _write_journal(store, replace(journal, operations=tuple(operations), createdDirectories={k: tuple(v) for k, v in created.items()}, status="prepared"))
            except (FileExistsError, OutputConflictError):
                operations[index] = replace(op, status="conflict")
                journal = _write_journal(store, replace(
                    journal, operations=tuple(operations), status="conflict",
                    createdDirectories={k: tuple(v) for k, v in created.items()},
                ))
                return journal
            except OSError:
                return _write_journal(store, replace(journal, operations=tuple(operations), status="incomplete", createdDirectories={k: tuple(v) for k, v in created.items()}))
            continue
        try:
            current = read_live_file(store.root, op.path, limit=_MAX_LIVE_BYTES)
            current_hash = None if current is None else _digest(current)
            if op.action == "replace":
                intended_path = f"{prefix}/staging/exports/{journal.exportId}/new/{op.operationId}"
                intended = store.read_immutable_bytes(intended_path.removeprefix(prefix + "/"))
                if intended is None or _digest(intended) != op.stagedNewHash:
                    raise ValueError("immutable intended output payload failed its hash")
                if current_hash == op.stagedNewHash:
                    pass
                elif current_hash == op.expectedOldHash:
                    replace_live_file(store.root, op.path, intended, expected_old_hash=op.expectedOldHash)
                else:
                    raise OutputConflictError("third live hash observed during recovery")
            elif op.action == "delete":
                backup_path = op.backupPath
                if backup_path is None:
                    raise ValueError("delete operation has no backup")
                backup = store.read_immutable_bytes(backup_path.removeprefix(prefix + "/"))
                if backup is None or _digest(backup) != op.backupHash:
                    raise ValueError("immutable backup payload failed its hash")
                if current_hash is None:
                    pass
                elif current_hash == op.expectedOldHash:
                    unlink_owned_file(store.root, op.path, expected_old_hash=op.expectedOldHash or "")
                else:
                    raise OutputConflictError("third live hash observed during recovery")
            operations[index] = replace(op, status="complete")
            journal = _write_journal(store, replace(journal, operations=tuple(operations), status="prepared", createdDirectories={k: tuple(v) for k, v in created.items()}))
        except OutputConflictError:
            operations[index] = replace(op, status="conflict")
            return _write_journal(store, replace(journal, operations=tuple(operations), status="conflict", createdDirectories={k: tuple(v) for k, v in created.items()}))
        except (OSError, ValueError):
            return _write_journal(store, replace(journal, operations=tuple(operations), status="incomplete", createdDirectories={k: tuple(v) for k, v in created.items()}))
    return _write_journal(store, replace(journal, operations=tuple(operations), status="prepared", createdDirectories={k: tuple(v) for k, v in created.items()}))


def _validate_journal_payloads(store: LedgerStore, journal: ExportJournal) -> None:
    prefix = _state_prefix(store)
    for op in journal.operations:
        if op.action == "replace":
            path = f"{prefix}/staging/exports/{journal.exportId}/new/{op.operationId}"
            data = store.read_immutable_bytes(path.removeprefix(prefix + "/"))
            if data is None or _digest(data) != op.stagedNewHash:
                raise ValueError("immutable intended output payload failed its hash")
        if op.backupPath is not None:
            data = store.read_immutable_bytes(op.backupPath.removeprefix(prefix + "/"))
            if data is None or _digest(data) != op.backupHash:
                raise ValueError("immutable output backup failed its hash")


def _commit_manifests(
    store: LedgerStore, journal: ExportJournal, planned: Sequence[_PlannedFile],
    manifests: Mapping[str, TargetManifest],
    previous_set: ManifestSet | None, project: ProjectDocument, state: ProjectState,
) -> tuple[ExportJournal, dict[str, str]]:
    if any(op.status != "complete" for op in journal.operations):
        return journal, {}
    manifest_ids: dict[str, str] = {}
    references = dict(previous_set.targets) if previous_set is not None else {}
    planned_by_target: dict[str, list[_PlannedFile]] = {}
    for item in planned:
        planned_by_target.setdefault(item.target_id, []).append(item)
    for target_id in journal.targetIds:
        prior = manifests.get(target_id)
        files_by_path = {item.path: item for item in prior.files} if prior else {}
        for item in planned_by_target.get(target_id, []):
            if item.file_manifest is None:
                files_by_path.pop(item.path, None)
            else:
                files_by_path[item.path] = item.file_manifest
        manifest_id = "manifest_" + hashlib.sha256(f"{journal.exportId}:{target_id}".encode()).hexdigest()[:24]
        manifest_ids[target_id] = manifest_id
        old_dirs = list(prior.createdDirectories) if prior else []
        confirmed_dirs = list(journal.createdDirectories.get(target_id, ()))
        target_manifest = TargetManifest(
            "1.0", __version__, manifest_id, journal.exportId, target_id,
            journal.runId, journal.stageRevision,
            tuple(files_by_path[path] for path in sorted(files_by_path)),
            tuple(dict.fromkeys((*old_dirs, *confirmed_dirs))), journal.createdAt,
        )
        relative = f"manifests/{journal.exportId}/{target_id}.json"
        _write_immutable_record(store, relative, target_manifest)
        prior_reference = references.get(target_id)
        references[target_id] = ManifestReference(
            (prior_reference.recordRevision + 1) if prior_reference else 1,
            target_id, manifest_id, journal.exportId, relative,
        )
    manifest_set_id = manifest_set_id_for_export(journal.exportId)
    manifest_set = ManifestSet("1.0", __version__, manifest_set_id, journal.exportId,
                                references, journal.createdAt)
    _write_immutable_record(store, f"manifests/{journal.exportId}/{manifest_set_id}.json", manifest_set)
    journal = _write_journal(store, replace(journal, manifestSetId=manifest_set_id))
    store.replace_state(replace(state, activeExportId=journal.exportId,
                                activeManifestSetId=manifest_set_id))
    journal = _write_journal(store, replace(journal, status="committed"))
    return journal, manifest_ids


def _create_export_journal(
    export_id: str, run: RunLedger, stage_revision: int, planned: Sequence[_PlannedFile],
    mkdirs: Mapping[str, Sequence[str]], store: LedgerStore,
) -> ExportJournal:
    prefix = _state_prefix(store)
    owned_mkdir: dict[str, str] = {}
    for target_id in sorted(mkdirs):
        for path in sorted(set(mkdirs[target_id]), key=lambda value: (value.count("/"), value)):
            owner = owned_mkdir.setdefault(path, target_id)
            if owner != target_id:
                continue
    operations: list[JournalOperation] = []
    for path, target_id in owned_mkdir.items():
        operation_id = _operation_id(export_id, target_id, path, "mkdir")
        operations.append(JournalOperation(1, operation_id, target_id, path, "mkdir", None, None,
                                           None, None, None, "planned"))
    for item in planned:
        action = item.action
        operation_id = _operation_id(export_id, item.target_id, item.path, action)
        backup_path = None
        backup_hash = None
        staged_hash = None
        if item.old is not None:
            backup_path = f"{prefix}/staging/exports/{export_id}/backup/{operation_id}"
            backup_hash = _digest(item.old)
            store.write_immutable_bytes(f"staging/exports/{export_id}/backup/{operation_id}", item.old)
        if item.intended is not None:
            staged_hash = _digest(item.intended)
            store.write_immutable_bytes(f"staging/exports/{export_id}/new/{operation_id}", item.intended)
        patch_path = None
        if item.patch:
            patch_path = f"{prefix}/staging/exports/{export_id}/patch/{operation_id}.diff"
            store.write_immutable_bytes(f"staging/exports/{export_id}/patch/{operation_id}.diff", item.patch)
        operation = JournalOperation(1, operation_id, item.target_id, item.path, action,
                                     None if item.old is None else _digest(item.old), staged_hash,
                                     backup_path, backup_hash, patch_path, "planned")
        operations.append(operation)
    operations.sort(key=lambda op: (op.targetId, op.path, op.action))
    target_ids = tuple(sorted(set((*mkdirs.keys(), *(item.target_id for item in planned)))))
    stamp = _next_timestamp()
    journal = ExportJournal("1.0", __version__, export_id, run.runId, stage_revision,
                            target_ids, tuple(operations), {}, None, "prepared", stamp, stamp)
    return journal


def _export_fresh(
    run_id: str, targets: Sequence[str], ownership: Mapping[str, OwnershipState], *,
    store: LedgerStore, current_identity: RepositoryIdentity, current_root_label: str,
    current_policy_revision: str, current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[str], delete_empty_owned_blocks: bool,
    source_guard=None, on_prepared_export: Callable[[ExportJournal], None] | None = None,
) -> ExportResult:
    store.require_no_unresolved_export()
    project, state, run, descriptor, specs, manifests, previous_set = _validate_export_context(
        run_id, targets, ownership, store=store
    )
    try:
        planned, mkdirs = _target_inputs(
            store, descriptor, specs, run, project, state, manifests,
            delete_empty_owned_blocks=delete_empty_owned_blocks,
        )
        # Include target parents even when the target is a first, empty stage.
        for target_id, spec in specs.items():
            if spec.kind == "skill":
                mkdirs[target_id].extend(missing_parent_directories(store.root, f"{spec.livePath}/SKILL.md"))
                mkdirs[target_id] = list(dict.fromkeys(mkdirs[target_id]))
    except (OSError, ValueError) as error:
        conflict = _conflict(None, None, str(error), "export.preflight-conflict")
        export_id = "export_" + secrets.token_hex(12)
        return ExportResult(export_id, "conflict", (), tuple(sorted(specs)), (conflict,), {})
    export_id = store.id_context.record_id("export", {
        "runId": run_id, "stageRevision": state.activeStageRevision,
        "targets": sorted(specs), "nonce": secrets.token_hex(8),
    })
    journal = _create_export_journal(
        export_id, run, state.activeStageRevision, planned, mkdirs, store
    )
    if source_guard is None:
        _recheck(run, store, current_identity, current_root_label, current_policy_revision,
                 current_extractor_versions, excluded_paths)
    else:
        from .maintenance_context import recheck_committed_source
        recheck_committed_source(store, source_guard)
    if on_prepared_export is not None:
        on_prepared_export(journal)
    store.write_export_journal(journal)
    if source_guard is not None:
        from .maintenance_context import recheck_committed_source
        recheck_committed_source(store, source_guard)
    journal = _perform_journal(store, journal)
    if journal.status != "prepared" or any(op.status != "complete" for op in journal.operations):
        return _result(journal)
    journal, ids = _commit_manifests(store, journal, planned, manifests,
                                     previous_set, project, state)
    return _result(journal, ids)


def export_staged(
    run_id: RunId, targets: Sequence[TargetId],
    ownership: Mapping[TargetId, OwnershipState], *,
    store: LedgerStore, current_identity: RepositoryIdentity, current_root_label: str,
    current_policy_revision: str, current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[RepositoryPath], delete_empty_owned_blocks: bool = False,
    source_guard=None, on_prepared_export: Callable[[ExportJournal], None] | None = None,
) -> ExportResult:
    """Publish selected outputs from the active immutable accepted stage."""
    if not store._mutation_active:
        raise RuntimeError("caller must hold store.mutation() while exporting")
    if delete_empty_owned_blocks is not True and delete_empty_owned_blocks is not False:
        raise ValueError("delete_empty_owned_blocks must be a boolean")
    return _export_fresh(
        run_id, targets, ownership, store=store, current_identity=current_identity,
        current_root_label=current_root_label,
        current_policy_revision=current_policy_revision,
        current_extractor_versions=current_extractor_versions,
        excluded_paths=excluded_paths,
        delete_empty_owned_blocks=delete_empty_owned_blocks,
        source_guard=source_guard, on_prepared_export=on_prepared_export,
    )


def resume_export(
    export_id: ExportId, ownership: Mapping[TargetId, OwnershipState], *, store: LedgerStore,
    current_identity: RepositoryIdentity, current_root_label: str,
    current_policy_revision: str, current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[RepositoryPath], source_guard=None,
) -> ExportResult:
    """Recover one explicitly selected incomplete export journal."""
    if not store._mutation_active:
        raise RuntimeError("caller must hold store.mutation() while resuming export")
    journal = store.read_export_journal(f"exports/{export_id}.json")
    if journal is None:
        raise FileNotFoundError(f"export journal does not exist: {export_id}")
    state = store.read_state()
    if state is None:
        raise FileNotFoundError("project state is required")
    if journal.status == "committed":
        if source_guard is not None:
            from .maintenance_context import recheck_committed_source
            recheck_committed_source(store, source_guard)
        return _result(journal)
    store.require_no_unresolved_export(resume_export_id=export_id)
    if state.activeExportId == journal.exportId and state.activeManifestSetId == journal.manifestSetId:
        if source_guard is not None:
            from .maintenance_context import recheck_committed_source
            recheck_committed_source(store, source_guard)
        manifest_set = store.read_manifest_set(f"manifests/{journal.exportId}/{journal.manifestSetId}.json") if journal.manifestSetId else None
        if (manifest_set is None or manifest_set.exportId != journal.exportId
                or manifest_set.manifestSetId != journal.manifestSetId):
            raise ValueError("committed pointer references an invalid manifest set")
        for target_id, reference in manifest_set.targets.items():
            target_manifest = store.read_target_manifest(reference.path)
            if (target_manifest is None or reference.targetId != target_id
                    or target_manifest.targetId != target_id
                    or target_manifest.manifestId != reference.manifestId
                    or target_manifest.exportId != reference.exportId):
                raise ValueError("committed pointer references an invalid target manifest")
        journal = _write_journal(store, replace(journal, status="committed"))
        return _result(journal)
    project, state, run, descriptor, specs, manifests, previous_set = _validate_export_context(
        journal.runId, journal.targetIds, ownership, store=store
    )
    if source_guard is None:
        _recheck(run, store, current_identity, current_root_label, current_policy_revision,
                 current_extractor_versions, excluded_paths)
    else:
        from .maintenance_context import recheck_committed_source
        recheck_committed_source(store, source_guard)
    _validate_journal_payloads(store, journal)
    if source_guard is not None:
        from .maintenance_context import recheck_committed_source
        recheck_committed_source(store, source_guard)
    journal = _perform_journal(store, journal)
    if journal.status != "prepared" or any(op.status != "complete" for op in journal.operations):
        return _result(journal)
    # Recreate planned manifests from immutable output payloads and the saved operation plan.
    planned: list[_PlannedFile] = []
    descriptor_targets = descriptor["targets"]
    assert isinstance(descriptor_targets, dict)
    for op in journal.operations:
        if op.action == "mkdir":
            continue
        data = None
        if op.action == "replace":
            data = store.read_immutable_bytes(f"staging/exports/{journal.exportId}/new/{op.operationId}")
        prior = manifests.get(op.targetId)
        prior_file = next((item for item in prior.files if item.path == op.path), None) if prior else None
        fm = None
        if op.action == "replace" and data is not None:
            spec = specs[op.targetId]
            stage_target = descriptor_targets[op.targetId]
            assert isinstance(stage_target, dict)
            stage_entries = stage_target["accepted"]
            assert isinstance(stage_entries, dict)
            stage_metadata = stage_entries.get(op.path)
            if spec.ownershipMode == "managed-blocks" and stage_metadata is None:
                # Explicit empty-block deletion removes the file record only if no outside bytes remain.
                fm = None
            elif spec.ownershipMode == "managed-blocks":
                assert isinstance(stage_metadata, dict)
                block_id = managed_block_id(project.projectId, op.targetId, id_context=store.id_context)
                start, _end, block_bytes = _marker_slice(data, project.projectId, block_id)
                stage_prefix = f"staging/{run.runId}/r{state.activeStageRevision}/targets/{op.targetId}/accepted/{op.path}"
                staged = store.read_immutable_bytes(stage_prefix)
                if staged is None:
                    raise ValueError("active staged bytes are missing during recovery")
                staged_start, _staged_end, _staged_block = _marker_slice(staged, project.projectId, block_id)
                delta = _line_offset(data, start) - _line_offset(staged, staged_start)
                spans = stage_metadata.get("claimSpans", {})
                shifted = {key: tuple((pair[0] + delta, pair[1] + delta) for pair in value)
                           for key, value in spans.items()}
                claim_ids = tuple(sorted({key.split("@", 1)[0] for key in spans}))
                blocks = (ManifestBlock((prior_file.blocks[0].recordRevision + 1)
                                        if prior_file and prior_file.blocks else 1,
                                        block_id, _digest(block_bytes), claim_ids),)
                backup = None
                if op.backupPath is not None:
                    backup = store.read_immutable_bytes(op.backupPath.removeprefix(_state_prefix(store) + "/"))
                eol = "crlf" if backup and b"\r\n" in backup else "lf"
                fm = ManifestFile((prior_file.recordRevision + 1) if prior_file else 1,
                                  op.path, "managed-blocks", _digest(data), eol, blocks, shifted)
            else:
                assert isinstance(stage_metadata, dict)
                backup = None
                if op.backupPath is not None:
                    backup = store.read_immutable_bytes(op.backupPath.removeprefix(_state_prefix(store) + "/"))
                eol = "crlf" if backup and b"\r\n" in backup else "lf"
                spans = stage_metadata.get("claimSpans", {})
                fm = ManifestFile((prior_file.recordRevision + 1) if prior_file else 1,
                                  op.path, "whole-files", _digest(data), eol, (),
                                  {key: tuple(tuple(pair) for pair in value) for key, value in spans.items()})
        planned.append(_PlannedFile(op.targetId, op.path, op.action,
                                    None, data, fm if op.action == "replace" else None, None))
    journal, ids = _commit_manifests(store, journal, planned, manifests,
                                     previous_set, project, state)
    return _result(journal, ids)
