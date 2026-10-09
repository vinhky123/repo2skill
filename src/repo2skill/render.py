"""Pure deterministic formatting of accepted claims and review previews."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import unicodedata

from .claim_templates import format_claim_text
from .ids import CanonicalIdContext, canonical_record_id
from .models import (
    Claim,
    ClaimPolicy,
    Diagnostic,
    RenderBundle,
    RenderedTarget,
    TargetSpec,
)


_TEMPLATE_DIR = Path(__file__).with_name("templates")


def format_managed_markers(project_id: str, block_id: str) -> tuple[str, str]:
    """Format the exact begin/end marker lines used by managed AGENTS output."""
    if not project_id or not block_id:
        raise ValueError("project_id and block_id are required")
    return (
        f"<!-- repo2skill:begin project={project_id} block={block_id} -->",
        f"<!-- repo2skill:end project={project_id} block={block_id} -->",
    )


def parse_managed_markers(text: str) -> tuple[str, str, str]:
    """Return project ID, block ID, and the single managed block body."""
    lines = text.splitlines()
    marker = re.compile(r"<!-- repo2skill:(begin|end) project=([^ ]+) block=([^ ]+) -->\Z")
    begins: list[tuple[int, str, str]] = []
    ends: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = marker.fullmatch(line)
        if match:
            (begins if match.group(1) == "begin" else ends).append(
                (index, match.group(2), match.group(3))
            )
        elif "repo2skill:begin" in line or "repo2skill:end" in line:
            raise ValueError("malformed managed marker")
    if len(begins) != 1 or len(ends) != 1:
        raise ValueError("managed output must contain exactly one begin/end marker pair")
    begin_index, project_id, block_id = begins[0]
    end_index, end_project, end_block = ends[0]
    if end_index <= begin_index or (project_id, block_id) != (end_project, end_block):
        raise ValueError("managed marker pair is mismatched")
    return project_id, block_id, "\n".join(lines[begin_index + 1:end_index])


def managed_block_id(
    project_id: str,
    target_id: str,
    *,
    id_context: CanonicalIdContext | None = None,
) -> str:
    """Allocate the stable identity of the one repository-guide block."""
    identity = {"projectId": project_id, "targetId": target_id, "sectionKey": "repository-guide"}
    if id_context is None:
        return canonical_record_id("managed-block", identity)
    return id_context.record_id("managed-block", identity)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _claim_order(claim: Claim) -> tuple[str, str, str, str, int]:
    return (claim.scope, claim.templateKey or "", claim.semanticKey, claim.claimId, claim.claimRevision)


def _text(claim: Claim) -> str:
    if claim.templateKey is not None:
        return format_claim_text(claim.templateKey, claim.payload)
    if claim.renderedText is None:
        raise ValueError(f"claim has no renderable text: {claim.claimId}")
    return unicodedata.normalize("NFC", claim.renderedText).replace("\r\n", "\n").replace("\r", "\n").strip()


def _lines_for_claims(claims: Sequence[Claim], *, pending: bool) -> tuple[str, dict[str, tuple[tuple[int, int], ...]]]:
    output: list[str] = []
    spans: dict[str, tuple[tuple[int, int], ...]] = {}
    for claim in claims:
        rendered = _text(claim)
        if not rendered:
            raise ValueError(f"claim rendered to empty text: {claim.claimId}")
        start = len(output) + 1
        label = "**Pending inferred proposal:** " if pending and claim.proposalStatus == "pending" else ""
        claim_lines = rendered.split("\n")
        for index, line in enumerate(claim_lines):
            output.append(f"- {label if index == 0 else '  '}{line}")
        end = len(output)
        spans[f"{claim.claimId}@{claim.claimRevision}"] = ((start, end),)
    return "\n".join(output), spans


def _skill_name(path: str) -> str:
    name = PurePosixPath(path).name
    if not name or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", name):
        raise ValueError("skill target path must end in a simple skill name")
    return name


def _render_files(spec: TargetSpec, claims: Sequence[Claim], project_id: str,
                  block_id: str) -> tuple[dict[str, bytes], dict[str, dict[str, tuple[tuple[int, int], ...]]]]:
    rendered_claims, spans = _lines_for_claims(
        claims, pending=any(claim.proposalStatus == "pending" for claim in claims)
    )
    if spec.kind == "agents-md":
        template = _TEMPLATE_DIR.joinpath("agents.md.tmpl").read_text(encoding="utf-8")
        output = template.format(project_id=project_id, block_id=block_id, claims=rendered_claims)
        parsed_project, parsed_block, _body = parse_managed_markers(output)
        if (parsed_project, parsed_block) != (project_id, block_id):
            raise ValueError("generated managed markers do not match the target identity")
        path = spec.livePath
        # The claim lines begin after the two-line heading/marker prelude.
        offset = 3
        return {path: output.encode("utf-8")}, {
            path: {claim_ref: tuple((start + offset, end + offset) for start, end in ranges)
                   for claim_ref, ranges in spans.items()}
        }
    if spec.kind != "skill":
        raise ValueError(f"unsupported target kind: {spec.kind}")
    skill_name = _skill_name(spec.livePath)
    template = _TEMPLATE_DIR.joinpath("skill.md.tmpl").read_text(encoding="utf-8")
    skill_path = f"{spec.livePath}/SKILL.md"
    provenance_path = f"{spec.livePath}/repo2skill-provenance.json"
    skill_text = template.format(skill_name=skill_name, claims=rendered_claims)
    # YAML frontmatter occupies four lines and the title plus separator occupy three.
    content_offset = 7
    skill_spans = {
        claim_ref: tuple((start + content_offset, end + content_offset) for start, end in ranges)
        for claim_ref, ranges in spans.items()
    }
    provenance = {
        "schemaVersion": "1.0",
        "claims": [
            {
                "claimId": claim.claimId,
                "claimRevision": claim.claimRevision,
                "factIds": sorted(set(claim.factIds)),
                "evidenceIds": sorted(set(claim.evidenceIds)),
            }
            for claim in sorted(claims, key=_claim_order)
        ],
    }
    provenance_bytes = (json.dumps(provenance, ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":")) + "\n").encode("utf-8")
    return {skill_path: skill_text.encode("utf-8"), provenance_path: provenance_bytes}, {
        skill_path: skill_spans,
        provenance_path: {},
    }


def _fits(files: Mapping[str, bytes], budget: int) -> bool:
    return sum(len(files[path]) for path in sorted(files)) <= budget


def _diagnostic(claim: Claim, spec: TargetSpec, mode: str) -> Diagnostic:
    return Diagnostic(
        1, "render.budget-omitted", "warning",
        f"Claim {claim.claimId}@{claim.claimRevision} was omitted from target {spec.targetId} {mode} output because the byte budget was reached.",
        spec.livePath, "render", {"claimId": claim.claimId, "claimRevision": claim.claimRevision,
                                   "targetId": spec.targetId, "mode": mode},
    )


def _render_target(
    claims: Sequence[Claim], spec: TargetSpec, *, policy: ClaimPolicy, project_id: str,
    id_context: CanonicalIdContext,
) -> RenderedTarget:
    block_id = managed_block_id(project_id, spec.targetId, id_context=id_context)
    accepted_candidates = sorted(
        (claim for claim in claims if claim.proposalStatus == "accepted"), key=_claim_order
    )
    pending_candidates = sorted(
        (claim for claim in claims if claim.proposalStatus == "pending" and claim.kind == "inferred"),
        key=_claim_order,
    )
    accepted: list[Claim] = []
    diagnostics: list[Diagnostic] = []
    for claim in accepted_candidates:
        files, _ = _render_files(spec, (*accepted, claim), project_id, block_id)
        if _fits(files, policy.renderBudget):
            accepted.append(claim)
        else:
            diagnostics.append(_diagnostic(claim, spec, "accepted"))
    accepted_files, accepted_spans = _render_files(spec, accepted, project_id, block_id)
    if not _fits(accepted_files, policy.renderBudget):
        raise ValueError(f"target {spec.targetId} fixed output exceeds renderBudget")

    preview = list(accepted)
    for claim in pending_candidates:
        files, _ = _render_files(spec, (*preview, claim), project_id, block_id)
        if _fits(files, policy.renderBudget):
            preview.append(claim)
        else:
            diagnostics.append(_diagnostic(claim, spec, "preview"))
    preview_files, preview_spans = _render_files(spec, preview, project_id, block_id)
    return RenderedTarget(
        spec.targetId,
        accepted_files,
        {path: _sha256(data) for path, data in accepted_files.items()},
        accepted_spans,
        tuple(diagnostics),
        preview_files,
        {path: _sha256(data) for path, data in preview_files.items()},
        preview_spans,
    )


def render_targets(
    claims: Sequence[Claim],
    specs: Sequence[TargetSpec],
    *,
    policy: ClaimPolicy,
    project_id: str,
    id_context: CanonicalIdContext | None = None,
) -> RenderBundle:
    """Render configured targets without consulting repository or store state."""
    if not project_id:
        raise ValueError("project_id is required")
    context = id_context if id_context is not None else CanonicalIdContext()
    claim_ids: set[tuple[str, int]] = set()
    for claim in claims:
        key = (claim.claimId, claim.claimRevision)
        if key in claim_ids:
            raise ValueError(f"duplicate claim revision in renderer input: {key[0]}@{key[1]}")
        claim_ids.add(key)
    target_ids = [spec.targetId for spec in specs]
    if len(set(target_ids)) != len(target_ids):
        raise ValueError("duplicate target ID in renderer specs")
    targets = tuple(
        _render_target(claims, spec, policy=policy, project_id=project_id, id_context=context)
        for spec in sorted(specs, key=lambda item: item.targetId)
    )
    return RenderBundle(targets, tuple(item for target in targets for item in target.diagnostics))
