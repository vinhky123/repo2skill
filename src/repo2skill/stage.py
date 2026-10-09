"""Immutable stage publication and descriptor validation for initial and review stages."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import unicodedata

from .dependencies import recheck_dependencies
from .ids import canonical_json_bytes
from .ledger import LedgerStore
from .models import (
    Claim,
    ProjectDocument,
    ProjectState,
    RenderBundle,
    RenderedTarget,
    RepositoryIdentity,
    RunLedger,
    StagedRun,
    TargetSpec,
)


_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_CLAIM_REF = re.compile(r"([a-z][a-z0-9-]*_[a-f0-9]{24})@([1-9][0-9]*)\Z")
_TARGET_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _repo_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path:
        raise ValueError("stage output path must be normalized and repository-relative")
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive
            or posix.as_posix() != path or unicodedata.normalize("NFC", path) != path
            or any(part in {"", ".", ".."} for part in posix.parts)):
        raise ValueError(f"stage output path is not normalized: {path}")
    return posix.parts


def _output_paths(spec: TargetSpec) -> tuple[str, ...]:
    _repo_path(spec.livePath)
    if spec.kind == "agents-md":
        if spec.ownershipMode != "managed-blocks":
            raise ValueError("agents-md targets require managed-blocks ownership")
        return (spec.livePath,)
    if spec.kind == "skill":
        if spec.ownershipMode != "whole-files":
            raise ValueError("skill targets require whole-files ownership")
        return (f"{spec.livePath}/SKILL.md", f"{spec.livePath}/repo2skill-provenance.json")
    raise ValueError(f"unsupported target kind: {spec.kind}")


def _overlap(first: str, second: str) -> bool:
    left = tuple(part.casefold() for part in _repo_path(first))
    right = tuple(part.casefold() for part in _repo_path(second))
    return left[:len(right)] == right or right[:len(left)] == left


def _validate_paths(store: LedgerStore, project: ProjectDocument,
                    specs: Sequence[TargetSpec]) -> None:
    state_path = store.state_dir.relative_to(store.root).as_posix()
    if project.stateDir != state_path:
        raise ValueError("configured stateDir differs from the LedgerStore state directory")
    target_ids = [spec.targetId for spec in specs]
    if len(set(target_ids)) != len(target_ids):
        raise ValueError("duplicate configured target ID")
    if any(not _TARGET_ID.fullmatch(target_id) for target_id in target_ids):
        raise ValueError("configured target ID is not a safe path component")
    outputs = [(spec.targetId, path) for spec in specs for path in _output_paths(spec)]
    for index, (target_id, path) in enumerate(outputs):
        if _overlap(path, state_path):
            raise ValueError(f"target output overlaps stateDir: {target_id} ({path})")
        for other_id, other_path in outputs[index + 1:]:
            if _overlap(path, other_path):
                raise ValueError(f"target outputs overlap after canonical normalization: {target_id}, {other_id}")


def _spec_record(spec: TargetSpec) -> dict[str, str]:
    return {
        "targetId": spec.targetId,
        "kind": spec.kind,
        "livePath": spec.livePath,
        "ownershipMode": spec.ownershipMode,
        "emptyBlockPolicy": spec.emptyBlockPolicy,
    }


def _claim_index(run: RunLedger) -> dict[str, Claim]:
    index: dict[str, Claim] = {}
    for claim in run.claims:
        reference = f"{claim.claimId}@{claim.claimRevision}"
        if reference in index:
            raise ValueError(f"run contains duplicate claim revision: {reference}")
        index[reference] = claim
    return index


def _validate_target(target: RenderedTarget, spec: TargetSpec, run: RunLedger,
                     *, accepted_only: bool = False) -> dict[str, object]:
    modes = (
        ("accepted", target.relativeFiles, target.exactHashes, target.claimSpans),
        ("preview", target.previewFiles, target.previewHashes, target.previewClaimSpans),
    )
    descriptor_modes: dict[str, object] = {}
    claims = _claim_index(run)
    for mode, files, hashes, spans in modes:
        if set(files) != set(hashes) or set(files) != set(spans):
            raise ValueError(f"{target.targetId} {mode} bytes, hashes, and spans have different paths")
        if files and set(files) != set(_output_paths(spec)):
            raise ValueError(f"{target.targetId} {mode} paths must exactly match target output paths")
        total_bytes = 0
        projection: dict[str, object] = {}
        for path in sorted(files):
            _repo_path(path)
            if path not in _output_paths(spec):
                raise ValueError(f"output path is not part of target {target.targetId}: {path}")
            data = files[path]
            if not isinstance(data, bytes) or b"\r" in data or not data.endswith(b"\n"):
                raise ValueError(f"{target.targetId} output must be canonical UTF-8/LF bytes: {path}")
            try:
                data.decode("utf-8", errors="strict")
            except UnicodeDecodeError as error:
                raise ValueError(f"{target.targetId} output is not UTF-8: {path}") from error
            digest = _sha256(data)
            if hashes[path] != digest or not _SHA256.fullmatch(hashes[path]):
                raise ValueError(f"{target.targetId} output hash does not match exact bytes: {path}")
            total_bytes += len(data)
            line_count = data.count(b"\n")
            path_spans: dict[str, list[list[int]]] = {}
            for reference, ranges in sorted(spans[path].items()):
                match = _CLAIM_REF.fullmatch(reference)
                if not match or reference not in claims:
                    raise ValueError(f"claim span does not resolve to a run claim revision: {reference}")
                if accepted_only and mode == "accepted" and claims[reference].proposalStatus != "accepted":
                    raise ValueError(f"accepted span references a nonaccepted claim: {reference}")
                checked: list[list[int]] = []
                for span in ranges:
                    if (not isinstance(span, tuple) or len(span) != 2
                            or any(isinstance(part, bool) or not isinstance(part, int) for part in span)
                            or span[0] < 1 or span[1] < span[0] or span[1] > line_count):
                        raise ValueError(f"claim span is outside staged file line bounds: {path}")
                    checked.append([span[0], span[1]])
                path_spans[reference] = checked
            projection[path] = {"sha256": digest, "claimSpans": path_spans}
        if total_bytes > run.effectiveClaimPolicy.renderBudget:
            raise ValueError(f"{target.targetId} {mode} output exceeds the run render budget")
        descriptor_modes[mode] = projection
    return descriptor_modes


def _prepare_descriptor(bundle: RenderBundle, run: RunLedger, store: LedgerStore,
                        selected_specs: Sequence[TargetSpec], stage_revision: int,
                        project: ProjectDocument, *, accepted_only: bool = False) -> tuple[dict[str, object], dict[str, bytes], dict[str, bytes]]:
    if isinstance(stage_revision, bool) or not isinstance(stage_revision, int) or stage_revision < 1:
        raise ValueError("stage revision must be a positive integer")
    _validate_paths(store, project, project.targets)
    if len({spec.targetId for spec in run.configuredTargets}) != len(run.configuredTargets):
        raise ValueError("duplicate configured target ID in run")
    configured = {spec.targetId: spec for spec in project.targets}
    run_configured = {spec.targetId: spec for spec in run.configuredTargets}
    if configured != run_configured:
        raise ValueError("run configured targets differ from authoritative project configuration")
    selected: dict[str, TargetSpec] = {}
    for spec in selected_specs:
        if not _TARGET_ID.fullmatch(spec.targetId):
            raise ValueError(f"target ID is not a safe path component: {spec.targetId!r}")
        if spec.targetId in selected:
            raise ValueError(f"duplicate selected target ID: {spec.targetId}")
        if configured.get(spec.targetId) != spec:
            raise ValueError(f"selected target does not match configured target: {spec.targetId}")
        selected[spec.targetId] = spec
    rendered = {target.targetId: target for target in bundle.targets}
    if len(rendered) != len(bundle.targets) or set(rendered) != set(selected):
        raise ValueError("bundle target IDs must exactly match selected target IDs")
    targets: dict[str, object] = {}
    accepted_files: dict[str, bytes] = {}
    preview_files: dict[str, bytes] = {}
    for target_id in sorted(selected):
        target = rendered[target_id]
        spec = selected[target_id]
        projection = _validate_target(target, spec, run, accepted_only=accepted_only)
        targets[target_id] = {"spec": _spec_record(spec), **projection}
        accepted_files.update({f"targets/{target_id}/accepted/{path}": data
                               for path, data in target.relativeFiles.items()})
        preview_files.update({f"targets/{target_id}/preview/{path}": data
                              for path, data in target.previewFiles.items()})
    descriptor: dict[str, object] = {
        "schemaVersion": "1.0",
        "toolVersion": run.toolVersion,
        "runId": run.runId,
        "stageRevision": stage_revision,
        "targets": targets,
    }
    return descriptor, accepted_files, preview_files


def _span_projection(value: object) -> dict[str, tuple[tuple[int, int], ...]]:
    if not isinstance(value, dict):
        raise ValueError("stage descriptor file entry must be an object")
    spans = value.get("claimSpans")
    if not isinstance(spans, dict):
        raise ValueError("stage descriptor claimSpans must be an object")
    result: dict[str, tuple[tuple[int, int], ...]] = {}
    for reference, ranges in spans.items():
        if not isinstance(reference, str) or not _CLAIM_REF.fullmatch(reference) or not isinstance(ranges, list):
            raise ValueError("stage descriptor has an invalid claim span reference")
        converted: list[tuple[int, int]] = []
        for span in ranges:
            if (not isinstance(span, list) or len(span) != 2
                    or any(type(part) is not int for part in span)):
                raise ValueError("stage descriptor has an invalid line span")
            converted.append((span[0], span[1]))
        result[reference] = tuple(converted)
    return result


def read_stage_descriptor(store: LedgerStore, run_id: str, stage_revision: int) -> dict[str, object]:
    """Read a descriptor and validate each referenced immutable stage file and hash."""
    if isinstance(stage_revision, bool) or not isinstance(stage_revision, int) or stage_revision < 1:
        raise ValueError("stage revision must be a positive integer")
    prefix = f"staging/{run_id}/r{stage_revision}"
    payload = store.read_immutable_bytes(f"{prefix}/stage.json")
    if payload is None:
        raise FileNotFoundError(f"stage descriptor is missing: {run_id}/r{stage_revision}")
    try:
        descriptor = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("stage descriptor is not valid UTF-8 JSON") from error
    if (not isinstance(descriptor, dict) or descriptor.get("schemaVersion") != "1.0"
            or descriptor.get("runId") != run_id or descriptor.get("stageRevision") != stage_revision
            or not isinstance(descriptor.get("toolVersion"), str)
            or not isinstance(descriptor.get("targets"), dict)):
        raise ValueError("stage descriptor identity or schema is invalid")
    listed: dict[str, str] = {}
    for target_id, target in descriptor["targets"].items():
        if not isinstance(target_id, str) or not _TARGET_ID.fullmatch(target_id) or not isinstance(target, dict):
            raise ValueError("stage descriptor target entry is invalid")
        spec = target.get("spec")
        if not isinstance(spec, dict) or set(spec) != {"targetId", "kind", "livePath", "ownershipMode", "emptyBlockPolicy"}:
            raise ValueError("stage descriptor target spec is invalid")
        if spec["targetId"] != target_id:
            raise ValueError("stage descriptor target ID does not match its spec")
        target_spec = TargetSpec(**spec)
        if set(target) != {"spec", "accepted", "preview"}:
            raise ValueError("stage descriptor target has unexpected fields")
        for mode in ("accepted", "preview"):
            entries = target[mode]
            if not isinstance(entries, dict):
                raise ValueError("stage descriptor mode must be an object")
            if entries and set(entries) != set(_output_paths(target_spec)):
                raise ValueError("stage descriptor paths must exactly match target output paths")
            for path, entry in entries.items():
                _repo_path(path)
                for prior, owner in listed.items():
                    if _overlap(path, prior) and (path != prior or owner != target_id):
                        raise ValueError("stage descriptor output paths overlap")
                if not isinstance(entry, dict) or set(entry) != {"sha256", "claimSpans"}:
                    raise ValueError("stage descriptor file entry is invalid")
                digest = entry["sha256"]
                if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                    raise ValueError("stage descriptor hash is invalid")
                relative = f"{prefix}/targets/{target_id}/{mode}/{path}"
                data = store.read_immutable_bytes(relative)
                if data is None or _sha256(data) != digest:
                    raise ValueError(f"stage file hash does not match its descriptor: {path}")
                if b"\r" in data or not data.endswith(b"\n"):
                    raise ValueError(f"stage file is not canonical UTF-8/LF: {path}")
                data.decode("utf-8", errors="strict")
                for ranges in _span_projection(entry).values():
                    lines = data.count(b"\n")
                    if any(first < 1 or last < first or last > lines for first, last in ranges):
                        raise ValueError("stage descriptor line span is outside staged bytes")
                listed[path] = target_id
    return descriptor


def publish_stage_revision(
    bundle: RenderBundle,
    run: RunLedger,
    *,
    store: LedgerStore,
    selected_specs: Sequence[TargetSpec],
    stage_revision: int,
) -> StagedRun:
    """Publish one immutable stage revision without changing the active state pointer."""
    store._require_mutation()
    store.require_no_unresolved_export()
    project = store.read_project()
    if project is None:
        raise FileNotFoundError("project configuration is required before staging")
    state = store.read_state()
    if state is None or project.projectId != state.projectId:
        raise ValueError("project and state IDs do not match")
    descriptor, accepted_files, preview_files = _prepare_descriptor(
        bundle, run, store, selected_specs, stage_revision, project
    )
    prefix = f"staging/{run.runId}/r{stage_revision}"
    if store.immutable_path_exists(prefix):
        raise FileExistsError(f"immutable stage revision already exists: {prefix}")
    for relative, data in sorted(accepted_files.items()):
        store.write_immutable_bytes(f"{prefix}/{relative}", data)
    for relative, data in sorted(preview_files.items()):
        store.write_immutable_bytes(f"{prefix}/{relative}", data)
    store.write_immutable_bytes(f"{prefix}/stage.json", canonical_json_bytes(descriptor) + b"\n")
    validated = read_stage_descriptor(store, run.runId, stage_revision)
    if canonical_json_bytes(validated) != canonical_json_bytes(descriptor):
        raise ValueError("published stage descriptor changed during validation")
    accepted_hashes = {
        target_id: {path: entry["sha256"] for path, entry in target["accepted"].items()}
        for target_id, target in descriptor["targets"].items()
    }
    preview_hashes = {
        target_id: {path: entry["sha256"] for path, entry in target["preview"].items()}
        for target_id, target in descriptor["targets"].items()
    }
    stage_root = store.state_dir / "staging" / run.runId / f"r{stage_revision}"
    return StagedRun(run.runId, stage_root, accepted_hashes, stage_revision, preview_hashes)


def stage_run(
    bundle: RenderBundle,
    run: RunLedger,
    *,
    store: LedgerStore,
    current_identity: RepositoryIdentity,
    current_root_label: str,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[str],
    selected_specs: Sequence[TargetSpec],
) -> StagedRun:
    """Publish initial revision 1 and activate it with one state pointer replacement."""
    store._require_mutation()
    store.require_no_unresolved_export()
    state = store.read_state()
    project = store.read_project()
    if state is None or project is None:
        raise FileNotFoundError("project and state documents are required before staging")
    if project.projectId != state.projectId:
        raise ValueError("project and state IDs do not match")
    if Path(current_identity.root) != store.root:
        raise ValueError("current identity root differs from the LedgerStore root")
    state_path = store.state_dir.relative_to(store.root).as_posix()
    required_exclusions = {state_path, *(spec.livePath for spec in project.targets)}
    if not required_exclusions.issubset(set(excluded_paths)):
        raise ValueError("excluded_paths must include stateDir and every configured target")
    if store.read_run(run.runId) is not None or store.immutable_path_exists(f"runs/{run.runId}.json"):
        raise FileExistsError(f"new run already exists: {run.runId}")
    if run.runId == state.currentRunId:
        raise ValueError("initial staging requires a new run ID")
    descriptor, _accepted, _preview = _prepare_descriptor(
        bundle, run, store, selected_specs, 1, project, accepted_only=True
    )
    expected_staged_targets = {
        target_id: {
            "accepted": {path: entry["sha256"] for path, entry in target["accepted"].items()},
            "preview": {path: entry["sha256"] for path, entry in target["preview"].items()},
        }
        for target_id, target in descriptor["targets"].items()
    }
    if run.stagedTargets != expected_staged_targets:
        raise ValueError("run stagedTargets do not match the initial render projection")
    stage_path = f"staging/{run.runId}/r1"
    if store.immutable_path_exists(stage_path):
        raise FileExistsError(f"immutable stage revision already exists: {stage_path}")

    store.write_immutable_bytes(f"runs/{run.runId}.json", canonical_json_bytes(run) + b"\n")
    staged = publish_stage_revision(
        bundle, run, store=store, selected_specs=selected_specs, stage_revision=1
    )
    result = recheck_dependencies(
        run, current_identity, current_policy_revision, current_extractor_versions,
        root=store.root, current_root_label=current_root_label, excluded_paths=excluded_paths,
    )
    if result.status != "fresh":
        raise ValueError(f"source dependencies are {result.status}; run was not activated")
    accepted_revisions = dict(state.acceptedClaimRevisions)
    current_claim_ids = {claim.claimId for claim in run.claims}
    for claim_id in current_claim_ids:
        accepted_revisions.pop(claim_id, None)
    accepted_revisions.update({
        claim.claimId: claim.claimRevision
        for claim in run.claims if claim.proposalStatus == "accepted"
    })
    store.replace_state(ProjectState(
        state.schemaVersion, state.toolVersion, state.projectId, run.runId, 1,
        state.reviewEvents, accepted_revisions,
        state.activeExportId, state.activeManifestSetId,
    ))
    return staged
