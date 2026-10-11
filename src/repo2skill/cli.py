"""Canonical command grammar, output envelope, and exit mapping."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import fields, is_dataclass, replace
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
from typing import Sequence

from . import SCHEMA_VERSION, __version__
from .checks import check_freshness, explain_claim
from .claims import apply_review_dispositions, review_claims
from .config import (
    PathBoundaryError, ProjectDataCorruptionError, canonical_repository_path,
    initialize_project_document, load_project_document,
)
from .dependencies import ActivationFreshnessError, recheck_dependencies
from .identity import RepositoryIdentityError
from .ids import canonical_json_bytes, canonical_record_id, effective_policy_revision
from .inventory import InventoryError
from .ledger import ExportRecoveryRequired, LedgerStore, LockBusyError
from .models import Claim, Diagnostic, Finding, ProjectDocument, ProjectState, ReviewEvent, RunLedger, TargetManifest
from .maintenance_hooks import HookInstallResult, install_hooks, preflight_hooks, remove_owned_hooks
from .maintenance_store import MaintenanceStore
from .ownership import (
    ExportFreshnessError, _active_manifests, _ownership, export_staged,
    inspect_target_ownership, resume_export,
)
from .pipeline import (
    collect_scan, coverage_is_unusable, current_repository_context,
    is_global_cap,
    generate_run, utc_now,
)
from .stage import read_stage_descriptor, stage_run
from .validation import validate_claims


_PRIORITY = (7, 5, 6, 2, 4, 3, 1, 0)
_STATUS_BY_CODE = {0: "ok", 2: "incomplete", 3: "pending", 4: "stale", 5: "conflict", 6: "error", 7: "error", 1: "error"}


class InvocationError(ValueError):
    pass


class LedgerCorruptionError(RuntimeError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InvocationError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if message:
            self._print_message(message, sys.stdout if status == 0 else sys.stderr)
        raise _ParserExit(status)


class _ParserExit(Exception):
    def __init__(self, status: int):
        self.status = status


def _common(parser: argparse.ArgumentParser, *, default_root: bool = False) -> None:
    parser.add_argument("--root", default="." if default_root else argparse.SUPPRESS)
    parser.add_argument("--config", default=argparse.SUPPRESS)
    parser.add_argument("--format", choices=("text", "json"), default=argparse.SUPPRESS)
    parser.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="repo2skill", description="Local-first, traceable repository knowledge generator.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _common(parser, default_root=True)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    init = commands.add_parser("init")
    _common(init)
    init.add_argument("--git-mode", choices=("auto", "head", "worktree", "no-git"), default="auto")
    init.add_argument("--target", action="append", choices=("agents-md", "skill"), default=[])
    init.add_argument("--state-dir")

    scan = commands.add_parser("scan")
    _common(scan)
    scan.add_argument("--git-mode", choices=("auto", "head", "worktree", "no-git"))
    scan.add_argument("--include", action="append", default=[])
    scan.add_argument("--exclude", action="append", default=[])
    scan.add_argument("--strict", action="store_true")

    generate = commands.add_parser("generate")
    _common(generate)
    ai = generate.add_mutually_exclusive_group()
    ai.add_argument("--no-ai", action="store_true")
    ai.add_argument("--ai", choices=("codex",))
    generate.add_argument("--codex-config")
    generate.add_argument("--yes-ai", action="store_true")
    generate.add_argument("--target", action="append", choices=("agents-md", "skill"), default=[])
    generate.add_argument("--from-run")
    generate.add_argument("--strict", action="store_true")
    generate.add_argument("--measure", action="store_true")
    generate.add_argument("--git-mode", choices=("auto", "head", "worktree", "no-git"))

    review = commands.add_parser("review")
    _common(review)
    review_commands = review.add_subparsers(dest="review_command", required=True, parser_class=_Parser)
    review_list = review_commands.add_parser("list")
    _common(review_list)
    review_list.add_argument("--run")
    review_list.add_argument("--status", choices=("pending", "accepted", "rejected", "contested"))
    review_show = review_commands.add_parser("show")
    _common(review_show)
    review_show.add_argument("--claim", required=True)
    review_show.add_argument("--run")
    for action in ("accept", "reject", "contest"):
        sub = review_commands.add_parser(action)
        _common(sub)
        if action == "accept":
            selection = sub.add_mutually_exclusive_group(required=True)
            selection.add_argument("--claim", action="append")
            selection.add_argument("--all-observed", action="store_true")
        else:
            sub.add_argument("--claim", action="append", required=True)
        sub.add_argument("--run")
        if action in {"reject", "contest"}:
            sub.add_argument("--reason", required=True)

    check = commands.add_parser("check")
    _common(check)
    check.add_argument("--run")
    check.add_argument("--target", action="append", choices=("agents-md", "skill"), default=[])
    check.add_argument("--strict", action="store_true")

    explain = commands.add_parser("explain")
    _common(explain)
    explain.add_argument("claim_id")
    explain.add_argument("--revision", type=int)
    explain.add_argument("--run")

    report = commands.add_parser("report")
    _common(report)
    report.add_argument("--run")
    report.add_argument("--section", action="append", choices=("summary", "coverage", "claims", "dependencies", "dedup", "outputs"), default=[])

    export = commands.add_parser("export")
    _common(export)
    selection = export.add_mutually_exclusive_group()
    selection.add_argument("--run")
    selection.add_argument("--resume")
    export.add_argument("--target", action="append", choices=("agents-md", "skill"), default=[])
    export.add_argument("--delete-empty-owned-blocks", action="store_true")

    doctor = commands.add_parser("doctor")
    _common(doctor)
    doctor.add_argument("--codex", action="store_true")

    maintain = commands.add_parser("maintain")
    _common(maintain)
    maintain_commands = maintain.add_subparsers(dest="maintain_command", required=True, parser_class=_Parser)
    setup = maintain_commands.add_parser("setup")
    _common(setup)
    setup.add_argument("--manual-hooks", action="store_true")
    setup.add_argument("--auto-approve", action="store_true")
    status = maintain_commands.add_parser("status")
    _common(status)
    for action in ("approve", "skip", "retry"):
        sub = maintain_commands.add_parser(action)
        _common(sub)
        sub.add_argument("job_id")
    catch_up = maintain_commands.add_parser("catch-up")
    _common(catch_up)
    catch_up.add_argument("--rebuild", action="store_true")
    disable = maintain_commands.add_parser("disable")
    _common(disable)
    return parser


def _jsonable(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _jsonable(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _finding(code: str, status: str, message: str, *, path: str | None = None, target: str | None = None, claims=(), details=None, exit_code: int | None = None) -> Finding:
    payload = dict(details or {})
    if exit_code is not None:
        payload["exitCode"] = exit_code
    return Finding(1, code, status, message, path, target, tuple(claims), payload)


def _exit_code(findings: Sequence[Finding]) -> int:
    codes = {"error": 1, "incomplete": 2, "pending": 3, "stale": 4, "unknown": 4, "conflict": 5}
    observed = [item.details.get("exitCode", codes.get(item.status, 0)) for item in findings]
    return next(code for code in _PRIORITY if code in observed) if observed else 0


def _superseded_resolved(state, job_id: str, lifecycle) -> bool:
    if lifecycle.resolutionReason == "no-impact":
        return True
    seen = {job_id}
    successor_id = lifecycle.supersededByJobId
    while successor_id is not None and successor_id not in seen:
        seen.add(successor_id)
        successor = state.jobs.get(successor_id)
        if successor is None:
            return False
        if successor.status == "applied":
            return True
        if successor.status != "stale":
            return False
        successor_id = successor.supersededByJobId
    return False


def _envelope(command: str, result: object = None, findings: Sequence[Finding] = (), diagnostics: Sequence[Diagnostic] = (), next_actions: Sequence[str] = ()) -> dict[str, object]:
    code = _exit_code(findings)
    if code == 6:
        status = "error"
    elif code == 7:
        status = "error"
    else:
        status = _STATUS_BY_CODE[code]
    return {
        "schemaVersion": SCHEMA_VERSION,
        "toolVersion": __version__,
        "command": command,
        "status": status,
        "exitCode": code,
        "result": _jsonable(result),
        "findings": _jsonable(tuple(findings)),
        "diagnostics": _jsonable(tuple(diagnostics)),
        "nextActions": list(next_actions),
    }


def _diagnostic(findings: Sequence[Finding]) -> tuple[Diagnostic, ...]:
    return tuple(Diagnostic(1, item.code, "error" if item.status in {"error", "conflict", "incomplete"} else "warning",
                           item.message, item.path, None, {"targetId": item.targetId, "claimIds": list(item.claimIds), "details": item.details})
                 for item in findings if item.status != "info")


def _print_envelope(envelope: dict[str, object], fmt: str, quiet: bool) -> int:
    code = int(envelope["exitCode"])
    diagnostics = envelope.get("diagnostics", ())
    if fmt == "json":
        sys.stdout.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
    elif code == 0:
        if not quiet:
            sys.stdout.write(f"{envelope['command']}: completed\n")
    else:
        for finding in envelope["findings"]:
            sys.stderr.write(f"{finding['code']}: {finding['message']}\n")
    for diagnostic in diagnostics:
        path = f"{diagnostic['path']}: " if diagnostic.get("path") else ""
        sys.stderr.write(f"{diagnostic['code']}: {path}{diagnostic['message']}\n")
    return code


def _store_context(args: argparse.Namespace, *, required: bool = True) -> tuple[ProjectDocument | None, LedgerStore | None]:
    root = Path(args.root).absolute()
    config = getattr(args, "config", None)
    if config is None:
        config = ".repo2skill/project.json"
    try:
        project, state_dir = load_project_document(root, config)
    except ProjectDataCorruptionError as error:
        raise LedgerCorruptionError("project configuration is invalid") from error
    except FileNotFoundError:
        if required:
            raise
        return None, None
    return project, LedgerStore(root, state_dir)


def _read_state(store: LedgerStore) -> ProjectState | None:
    try:
        return store.read_state()
    except (OSError, ValueError, TypeError) as error:
        raise LedgerCorruptionError("project state is unreadable or invalid") from error


def _read_run(store: LedgerStore, run_id: str) -> RunLedger | None:
    try:
        return store.read_run(run_id)
    except (OSError, ValueError, TypeError) as error:
        raise LedgerCorruptionError("selected run ledger is unreadable or invalid") from error


def _active_manifests_checked(store: LedgerStore, state: ProjectState):
    try:
        return _active_manifests(store, state)
    except (OSError, ValueError, TypeError) as error:
        raise LedgerCorruptionError("active manifest records are unreadable or invalid") from error


def _existing_directory(path: Path) -> Path:
    current = path
    if current.exists() and not current.is_dir():
        current = current.parent
    while not current.exists():
        parent = current.parent
        if parent == current:
            return current
        current = parent
    return current if current.is_dir() else current.parent


def _target_specs(project: ProjectDocument, selected: Sequence[str]):
    if not selected:
        return project.targets
    if len(set(selected)) != len(selected):
        raise InvocationError("target options must be unique")
    by_id = {item.targetId: item for item in project.targets}
    if any(item not in by_id for item in selected):
        raise InvocationError("selected target is not configured")
    return tuple(by_id[item] for item in selected)


def _coverage_findings(run: RunLedger, *, strict: bool) -> tuple[Finding, ...]:
    gaps = [item for item in run.coverage if item.status not in {"supported", "generated"}]
    findings = []
    for item in gaps:
        required = item.selector.required
        global_cap = item.status == "capped" and is_global_cap(item)
        code = "coverage.required-gap" if required else "coverage.gap"
        status = "incomplete" if required or strict or global_cap else "info"
        findings.append(_finding(code, status, f"coverage {item.status}: {item.reasonCode}",
                                 path=item.selector.value if item.selector.type == "file" else None,
                                 details={"collector": item.collector, "reasonCode": item.reasonCode,
                                          "selectorType": item.selector.type, "selector": item.selector.value,
                                          "required": required}))
    return tuple(findings)


def _counts(claims: Sequence[Claim]) -> dict[str, dict[str, int]]:
    return {
        "status": dict(Counter(item.proposalStatus for item in claims)),
        "kind": dict(Counter(item.kind for item in claims)),
        "origin": dict(Counter(item.origin for item in claims)),
    }


def _run_result_scan(run: RunLedger) -> dict[str, object]:
    return {"runId": run.runId, "snapshotId": run.snapshot.snapshotId,
            "inventoryHash": run.snapshot.inventoryHash, "inputDigest": run.snapshot.inputDigest,
            "counts": {"evidence": len(run.evidence), "facts": len(run.facts), "claims": len(run.claims),
                       "diagnostics": len(run.diagnostics)}, "coverage": _jsonable(run.coverage)}


def _run_result_generate(run: RunLedger, stage_revision: int) -> dict[str, object]:
    dedup_counts = dict(Counter(item.result for item in run.dedupDecisions))
    pending = sum(item.proposalStatus in {"pending", "contested"} for item in run.claims)
    return {"runId": run.runId, "snapshotId": run.snapshot.snapshotId, "stageRevision": stage_revision,
            "stagedTargets": _jsonable(run.stagedTargets), "claimCounts": _counts(run.claims),
            "dedupCounts": dedup_counts, "coverage": _jsonable(run.coverage),
            "remainingPendingCount": pending}


def _load_history(store: LedgerStore, repository_id: str | None = None) -> tuple[RunLedger, ...]:
    all_runs = []
    try:
        for run_id in store.list_run_ids():
            run = store.read_run(run_id)
            if run is None or run.runId != run_id or not run.completed:
                raise ValueError("enumerated run history is invalid")
            all_runs.append(run)
    except (OSError, ValueError, TypeError) as error:
        raise LedgerCorruptionError("enumerated immutable run history is invalid") from error
    if repository_id is None:
        return tuple(all_runs)
    return tuple(item for item in all_runs if item.snapshot.repositoryId == repository_id)


def _freshness_findings(result) -> tuple[Finding, ...]:
    if result.status == "fresh":
        return tuple(result.findings)
    return tuple(result.findings) or (_finding("source." + result.status, result.status, f"source dependencies are {result.status}", claims=result.affectedClaimIds),)


def _command_init(args: argparse.Namespace) -> tuple[object, tuple[Finding, ...], tuple[Diagnostic, ...], list[str]]:
    root = Path(args.root).absolute()
    state_dir = getattr(args, "state_dir", None) or ".repo2skill"
    config = getattr(args, "config", None) or f"{state_dir.rstrip('/\\')}/project.json"
    project, created = initialize_project_document(
        root, state_dir=state_dir, config_path=config,
        config_was_explicit=getattr(args, "config", None) is not None,
        git_mode=args.git_mode, targets=tuple(args.target),
    )
    return ({"projectId": project.projectId, "rootLabel": project.rootLabel,
             "stateDir": project.stateDir, "targets": [item.targetId for item in project.targets],
             "created": created}, (), (), [])


def _command_scan(args: argparse.Namespace):
    project, store = _store_context(args)
    assert project and store
    with store.mutation():
        store.require_no_unresolved_export()
        state = _read_state(store)
        if state is None:
            raise FileNotFoundError("project state is missing")
        all_runs = _load_history(store)
        try:
            products = collect_scan(Path(args.root).absolute(), project, git_mode=args.git_mode,
                                    include=args.include, exclude=args.exclude, prior_runs=all_runs,
                                    id_context=store.id_context)
        except RepositoryIdentityError as error:
            if error.code == "identity.inventory-incomplete":
                finding = _finding("coverage.required-gap", "incomplete", "repository inventory is incomplete", exit_code=2)
                return None, (finding,), (), ["resolve required inventory coverage gaps and scan again"]
            raise
        run = products.run
        findings = list(_coverage_findings(run, strict=args.strict))
        if coverage_is_unusable(run, strict=args.strict):
            return _run_result_scan(run), tuple(findings), run.diagnostics, ["resolve coverage gaps and scan again"]
        store.write_immutable("runs/" + run.runId + ".json", json.loads(canonical_json_bytes(run)))
        freshness = recheck_dependencies(
            run, products.identity, products.policy_revision, products.extractor_versions,
            root=store.root, current_root_label=project.rootLabel,
            excluded_paths=products.excluded_paths,
        )
        findings.extend(_freshness_findings(freshness))
        if freshness.status == "fresh":
            store.replace_state(replace(state, currentRunId=run.runId, activeStageRevision=None))
        return _run_result_scan(run), tuple(findings), run.diagnostics, []


def _command_generate(args: argparse.Namespace):
    if args.measure:
        raise InvocationError("--measure is unavailable in P0")
    if args.yes_ai and args.ai != "codex":
        raise InvocationError("--yes-ai requires --ai codex")
    if args.codex_config and args.ai != "codex":
        raise InvocationError("--codex-config requires --ai codex")
    if args.ai == "codex":
        finding = _finding("codex.unavailable", "error", "Codex generation is unavailable in this P0 build", exit_code=6)
        return None, (finding,), (), []
    if getattr(args, "git_mode", None) and args.from_run:
        raise InvocationError("--git-mode cannot be combined with --from-run")
    project, store = _store_context(args)
    assert project and store
    selected_specs = _target_specs(project, args.target)
    with store.mutation():
        store.require_no_unresolved_export()
        state = _read_state(store)
        if state is None:
            raise FileNotFoundError("project state is missing")
        all_runs = _load_history(store)
        from_run = None
        if args.from_run:
            from_run = _read_run(store, args.from_run)
            if from_run is None or not from_run.completed or from_run.claims or from_run.stagedTargets:
                raise ValueError("--from-run requires a completed scan-only run")
            if (from_run.configuredTargets != project.targets
                    or from_run.effectiveClaimPolicy != project.claimPolicy
                    or from_run.snapshot.policyRevision != effective_policy_revision(
                        project.scanPolicy, project.claimPolicy, project.targets, generation_options={})):
                raise ValueError("--from-run is incompatible with the current project configuration")
            try:
                identity, policy_revision, versions, exclusions = current_repository_context(Path(args.root).absolute(), project, from_run)
            except RepositoryIdentityError as error:
                if error.code == "identity.inventory-incomplete":
                    finding = _finding("source.unknown", "unknown", "current repository identity could not be captured completely", exit_code=4)
                    return _run_result_scan(from_run), (finding,), from_run.diagnostics, []
                raise
            freshness = recheck_dependencies(
                from_run, identity, policy_revision, versions, root=store.root,
                current_root_label=project.rootLabel, excluded_paths=exclusions,
            )
            if freshness.status != "fresh":
                return _run_result_scan(from_run), _freshness_findings(freshness), from_run.diagnostics, []
            scan_run = from_run
            products_identity, products_policy, products_versions, products_exclusions = identity, policy_revision, versions, exclusions
        else:
            try:
                products = collect_scan(Path(args.root).absolute(), project, git_mode=args.git_mode,
                                        prior_runs=all_runs, id_context=store.id_context)
            except RepositoryIdentityError as error:
                if error.code == "identity.inventory-incomplete":
                    finding = _finding("coverage.required-gap", "incomplete", "repository inventory is incomplete", exit_code=2)
                    return None, (finding,), (), ["resolve required inventory coverage gaps and generate again"]
                raise
            scan_run = products.run
            products_identity, products_policy = products.identity, products.policy_revision
            products_versions, products_exclusions = products.extractor_versions, products.excluded_paths
        if coverage_is_unusable(scan_run, strict=args.strict):
            return _run_result_scan(scan_run), _coverage_findings(scan_run, strict=args.strict), scan_run.diagnostics, []
        run, bundle = generate_run(scan_run, state=state, prior_runs=all_runs,
                                   selected_specs=selected_specs, project=project,
                                   id_context=store.id_context)
        staged = stage_run(
            bundle, run, store=store, current_identity=products_identity,
            current_root_label=project.rootLabel, current_policy_revision=products_policy,
            current_extractor_versions=products_versions, excluded_paths=products_exclusions,
            selected_specs=selected_specs,
        )
        findings = list(_coverage_findings(run, strict=args.strict))
        pending = sum(claim.proposalStatus in {"pending", "contested"} for claim in run.claims)
        if pending:
            findings.append(_finding("review.pending", "pending", "generated claims remain pending review",
                                     claims=tuple(claim.claimId for claim in run.claims if claim.proposalStatus in {"pending", "contested"})))
        return _run_result_generate(run, staged.stageRevision), tuple(findings), run.diagnostics, []


def _selected_run(store: LedgerStore, run_id: str | None, state: ProjectState) -> RunLedger:
    selected = run_id or state.currentRunId
    if selected is None:
        raise LookupError("no active run exists")
    run = _read_run(store, selected)
    if run is None:
        raise LookupError("requested run does not exist")
    return run


def _review_events(args, claims: Sequence[Claim], run: RunLedger) -> tuple[ReviewEvent, ...]:
    action = args.review_command
    if action == "accept" and args.all_observed:
        candidates = [claim for claim in claims if claim.proposalStatus == "pending" and claim.kind == "observed" and claim.origin == "deterministic"]
        valid = {(claim.claimId, claim.claimRevision) for claim in validate_claims(run.claims, run).validClaims}
        selected = [claim for claim in candidates if (claim.claimId, claim.claimRevision) in valid]
    else:
        requested = getattr(args, "claim", None) or []
        if len(set(requested)) != len(requested):
            raise InvocationError("claim options must be unique")
        by_id = {claim.claimId: claim for claim in claims}
        if any(item not in by_id for item in requested):
            raise LookupError("one or more requested claims do not exist")
        selected = [by_id[item] for item in requested]
    now = utc_now()
    return tuple(ReviewEvent(
        1, canonical_record_id("event", {"claim": claim.claimId, "revision": claim.claimRevision,
                                           "action": action, "nonce": str(uuid.uuid4())}),
        claim.claimId, claim.claimRevision, action,
        args.reason if action in {"reject", "contest"} else None, now,
    ) for claim in selected)


def _command_review(args: argparse.Namespace):
    project, store = _store_context(args)
    assert project and store
    if args.review_command in {"list", "show"}:
        state = _read_state(store)
        if state is None:
            raise FileNotFoundError("project state is missing")
        run = _selected_run(store, args.run, state)
        candidates = _effective_run_candidates(run, state)
        if args.review_command == "list":
            selected = [item for item in candidates if args.status is None or item.proposalStatus == args.status]
            return {"runId": run.runId, "claims": selected}, (), (), []
        claim = next((item for item in candidates if item.claimId == args.claim), None)
        if claim is None:
            raise LookupError("claim does not exist at the selected run revision")
        facts = [item for item in run.facts if item.factId in claim.factIds]
        evidence_ids = set(claim.evidenceIds)
        evidence = [item for item in run.evidence if item.evidenceId in evidence_ids]
        decision = [item for item in run.dedupDecisions if claim.claimId in item.candidateClaimIds]
        if len(decision) > 1:
            raise ValueError("claim has ambiguous dedup provenance")
        return {"runId": run.runId, "claim": claim, "facts": facts, "evidence": evidence,
                "dedupDecision": decision[0] if len(decision) == 1 else None,
                "coverage": run.coverage}, (), (), []
    with store.mutation():
        store.require_no_unresolved_export()
        state = _read_state(store)
        if state is None:
            raise FileNotFoundError("project state is missing")
        run = _selected_run(store, args.run, state)
        validation = validate_claims(run.claims, run)
        effective = apply_review_dispositions(validation.validClaims, state)
        events = _review_events(args, effective, run)
        identity, policy_revision, versions, exclusions = current_repository_context(Path(args.root).absolute(), project, run)
        new_state, staged = review_claims(
            state, run, events, project.targets, store=store,
            current_identity=identity, current_root_label=project.rootLabel,
            current_policy_revision=policy_revision, current_extractor_versions=versions,
            excluded_paths=exclusions,
        )
    after = apply_review_dispositions(validate_claims(run.claims, run).validClaims, new_state)
    pending = sum(item.proposalStatus in {"pending", "contested"} for item in after)
    findings = (_finding("review.pending", "pending", "claims remain pending review",
                         claims=tuple(item.claimId for item in after if item.proposalStatus in {"pending", "contested"})),) if pending else ()
    return {"runId": run.runId, "stageRevision": staged.stageRevision, "events": events,
            "acceptedClaimRevisions": new_state.acceptedClaimRevisions,
            "remainingPendingCount": pending}, findings, (), []


def _effective_run_candidates(run: RunLedger, state: ProjectState) -> tuple[Claim, ...]:
    validation = validate_claims(run.claims, run)
    effective = apply_review_dispositions(validation.validClaims, state)
    effective_by_ref = {(item.claimId, item.claimRevision): item for item in effective}
    return tuple(effective_by_ref.get((item.claimId, item.claimRevision), replace(item, proposalStatus="pending"))
                 for item in run.claims)


def _command_check(args: argparse.Namespace):
    project, store = _store_context(args)
    assert project and store
    state = _read_state(store)
    if state is None:
        raise FileNotFoundError("project state is missing")
    run = _selected_run(store, args.run, state)
    identity, policy_revision, versions, exclusions = current_repository_context(Path(args.root).absolute(), project, run)
    result = check_freshness(run, identity, policy_revision, versions, root=store.root,
                             current_root_label=project.rootLabel, excluded_paths=exclusions)
    specs = _target_specs(project, args.target)
    try:
        ownership_findings = inspect_target_ownership(specs, store=store)
    except (OSError, ValueError, TypeError) as error:
        raise LedgerCorruptionError("target ownership records are unreadable or invalid") from error
    findings = [item for item in result.findings if item.code != "check.review-pending"]
    findings.extend(ownership_findings)
    try:
        for journal in store.list_export_journals():
            if journal.status != "committed":
                findings.append(_finding(
                    "export.recovery-required", "incomplete", "an export journal requires explicit recovery",
                    details={"exportId": journal.exportId, "status": journal.status}, exit_code=7,
                ))
    except (OSError, ValueError, TypeError) as error:
        raise LedgerCorruptionError("export journal history is invalid") from error
    candidates = _effective_run_candidates(run, state)
    pending_ids = tuple(item.claimId for item in candidates if item.proposalStatus in {"pending", "contested"})
    if pending_ids:
        findings.append(_finding("check.review-pending", "pending", "claims await review", claims=pending_ids))
    if args.strict and result.coverageStatus == "informational-gaps":
        findings.append(_finding("check.coverage-strict", "incomplete", "strict check rejects coverage gaps"))
    return {"runId": run.runId, "sourceStatus": result.sourceStatus,
            "reviewStatus": "pending" if pending_ids else "clear",
            "coverageStatus": result.coverageStatus,
            "affectedClaimIds": result.affectedClaimIds,
            "targets": {"selected": [item.targetId for item in specs],
                        "findings": ownership_findings}}, tuple(findings), (), []


def _recorded_target_manifests_checked(store: LedgerStore, runs: Sequence[RunLedger]) -> tuple[TargetManifest, ...]:
    run_ids = {item.runId for item in runs}
    manifests: dict[tuple[str, str, str], TargetManifest] = {}
    for journal in store.list_export_journals():
        if journal.status != "committed":
            continue
        if journal.manifestSetId is None:
            raise ValueError("committed export has no manifest set")
        relative = f"manifests/{journal.exportId}/{journal.manifestSetId}.json"
        manifest_set = store.read_manifest_set(relative)
        if (manifest_set is None or manifest_set.exportId != journal.exportId
            or manifest_set.manifestSetId != journal.manifestSetId):
            raise ValueError("committed export manifest set is missing or mismatched")
        for target_id, reference in manifest_set.targets.items():
            if reference.targetId != target_id:
                raise ValueError("target manifest reference is inconsistent")
            expected_path = f"manifests/{reference.exportId}/{target_id}.json"
            if reference.path != expected_path:
                raise ValueError("target manifest reference path is inconsistent")
            manifest = store.read_target_manifest(reference.path)
            if (manifest is None or manifest.manifestId != reference.manifestId
                or manifest.targetId != target_id or manifest.exportId != reference.exportId):
                raise ValueError("recorded target manifest is missing or mismatched")
            if manifest.runId in run_ids:
                key = (manifest.manifestId, manifest.targetId, manifest.runId)
                prior = manifests.get(key)
                if prior is not None and prior != manifest:
                    raise ValueError("duplicate target manifest record has conflicting contents")
                manifests[key] = manifest
    return tuple(manifests[key] for key in sorted(manifests))


def _load_explain_artifacts(store: LedgerStore, runs: Sequence[RunLedger], state: ProjectState):
    _manifest_set, active = _active_manifests_checked(store, state)
    manifest_records: dict[tuple[str, str, str], TargetManifest] = {}
    try:
        for manifest in (*active.values(), *_recorded_target_manifests_checked(store, runs)):
            key = (manifest.manifestId, manifest.targetId, manifest.runId)
            prior = manifest_records.get(key)
            if prior is not None and prior != manifest:
                raise ValueError("duplicate target manifest record has conflicting contents")
            manifest_records[key] = manifest
    except (OSError, ValueError, TypeError) as error:
        raise LedgerCorruptionError("recorded historical manifest artifacts are unreadable or invalid") from error
    manifests = list(manifest_records.values())
    descriptors = {}
    for run in runs:
        revisions = {1, *(item.stageRevision for item in manifests if item.runId == run.runId)}
        if state.currentRunId == run.runId and state.activeStageRevision:
            revisions.add(state.activeStageRevision)
        for revision in revisions:
            try:
                descriptors[(run.runId, revision)] = read_stage_descriptor(store, run.runId, revision)
            except FileNotFoundError:
                continue
    return descriptors, manifests


def _command_explain(args: argparse.Namespace):
    if args.revision is not None and args.revision < 1:
        raise InvocationError("--revision must be a positive integer")
    project, store = _store_context(args)
    assert project and store
    state = _read_state(store)
    if state is None:
        raise FileNotFoundError("project state is missing")
    all_runs = _load_history(store)
    if args.run and not any(item.runId == args.run for item in all_runs):
        raise LookupError("requested run does not exist")
    try:
        snapshot = store.snapshot()
        runs = {item.runId: item for item in all_runs}
        full_snapshot = replace(snapshot, runs=runs)
        descriptors, manifests = _load_explain_artifacts(store, all_runs, state)
        explanation = explain_claim(args.claim_id, full_snapshot, args.revision, args.run,
                                    staged_descriptors=descriptors, target_manifests=manifests)
    except (OSError, ValueError, TypeError) as error:
        raise LedgerCorruptionError("claim provenance or staged artifacts are invalid") from error
    matches = [item for item in all_runs if item.snapshot.snapshotId == explanation.snapshot.snapshotId
               and any(claim.claimId == explanation.claim.claimId and claim.claimRevision == explanation.claim.claimRevision
                       for claim in item.claims)]
    preferred_run_id = args.run or state.currentRunId
    selected_run = next((item for item in matches if item.runId == preferred_run_id), matches[-1] if matches else None)
    if selected_run is None:
        raise ValueError("explanation did not resolve to an immutable run")
    result = {"runId": selected_run.runId,
              "claim": explanation.claim, "facts": explanation.facts, "evidence": explanation.evidence,
              "snapshot": explanation.snapshot, "dedupDecision": explanation.dedupDecision,
              "stagedLocations": explanation.stagedLocations, "liveLocations": explanation.liveLocations}
    return result, (), explanation.diagnostics, []


def _command_report(args: argparse.Namespace):
    project, store = _store_context(args)
    assert project and store
    state = _read_state(store)
    if state is None:
        raise FileNotFoundError("project state is missing")
    run = _selected_run(store, args.run, state)
    selected = args.section or ["summary", "coverage", "claims", "dependencies", "dedup", "outputs"]
    sections = {}
    if "summary" in selected:
        sections["summary"] = {"snapshotId": run.snapshot.snapshotId, "completed": run.completed,
                                "repositoryId": run.snapshot.repositoryId, "createdAt": run.snapshot.completedAt}
    if "coverage" in selected:
        sections["coverage"] = run.coverage
    if "claims" in selected:
        sections["claims"] = {"counts": _counts(run.claims), "claims": run.claims}
    if "dependencies" in selected:
        sections["dependencies"] = run.reads
    if "dedup" in selected:
        sections["dedup"] = {"counts": dict(Counter(item.result for item in run.dedupDecisions)),
                              "decisions": run.dedupDecisions}
    if "outputs" in selected:
        sections["outputs"] = {"stageRevision": state.activeStageRevision if state.currentRunId == run.runId else None,
                               "stagedTargets": run.stagedTargets}
    findings = _coverage_findings(run, strict=False)
    return {"runId": run.runId, "sections": sections,
            "counts": {"evidence": len(run.evidence), "facts": len(run.facts), "claims": len(run.claims),
                       "diagnostics": len(run.diagnostics), "coverage": len(run.coverage)},
            "coverage": run.coverage}, findings, run.diagnostics, []


def _command_export(args: argparse.Namespace):
    if args.resume and (args.target or args.delete_empty_owned_blocks):
        raise InvocationError("--resume cannot be combined with --target or --delete-empty-owned-blocks")
    project, store = _store_context(args)
    assert project and store
    with store.mutation():
        store.require_no_unresolved_export(resume_export_id=args.resume)
        state = _read_state(store)
        if state is None:
            raise FileNotFoundError("project state is missing")
        _manifest_set, manifests = _active_manifests_checked(store, state)
        ownership = _ownership(manifests)
        run_id = args.run or state.currentRunId
        if run_id is None:
            raise LookupError("there is no active run to export")
        run = _read_run(store, run_id)
        if run is None:
            raise LookupError("requested run does not exist")
        identity, policy_revision, versions, exclusions = current_repository_context(Path(args.root).absolute(), project, run)
        if args.resume:
            outcome = resume_export(args.resume, ownership, store=store, current_identity=identity,
                                    current_root_label=project.rootLabel, current_policy_revision=policy_revision,
                                    current_extractor_versions=versions, excluded_paths=exclusions)
        else:
            effective_valid = apply_review_dispositions(validate_claims(run.claims, run).validClaims, state)
            accepted = [claim for claim in effective_valid if claim.proposalStatus == "accepted"]
            status_by_ref = {(item.claimId, item.claimRevision): item.proposalStatus for item in effective_valid}
            pending = [claim for claim in run.claims if status_by_ref.get((claim.claimId, claim.claimRevision), "pending") in {"pending", "contested"}]
            if not accepted and pending:
                finding = _finding("review.pending", "pending", "no accepted claims are available to export",
                                   claims=tuple(item.claimId for item in pending))
                return None, (finding,), (), []
            target_ids = tuple(item.targetId for item in _target_specs(project, args.target))
            outcome = export_staged(run_id, target_ids, ownership, store=store, current_identity=identity,
                                     current_root_label=project.rootLabel, current_policy_revision=policy_revision,
                                     current_extractor_versions=versions, excluded_paths=exclusions,
                                     delete_empty_owned_blocks=args.delete_empty_owned_blocks)
        status_finding = {
            "incomplete": _finding("export.incomplete", "incomplete", "export requires explicit recovery", exit_code=7),
            "conflict": _finding("export.conflict", "conflict", "export encountered an ownership conflict"),
        }.get(outcome.status)
        findings = (status_finding,) if status_finding else ()
        try:
            journal = store.read_export_journal(f"exports/{outcome.exportId}.json")
        except (OSError, ValueError, TypeError) as error:
            raise LedgerCorruptionError("export journal is unreadable or invalid") from error
        stage_revision = journal.stageRevision if journal else state.activeStageRevision or 1
        current_effective = apply_review_dispositions(validate_claims(run.claims, run).validClaims, state)
        effective_status = {(item.claimId, item.claimRevision): item.proposalStatus for item in current_effective}
        pending_count = sum(effective_status.get((item.claimId, item.claimRevision), "pending") in {"pending", "contested"}
                            for item in run.claims)
        latest_state = _read_state(store)
        if latest_state is None:
            raise LedgerCorruptionError("project state disappeared during export")
        _latest_set, latest_manifests = _active_manifests_checked(store, latest_state)
        exported_targets = {}
        for target_id, manifest in latest_manifests.items():
            if target_id not in outcome.manifestIds:
                continue
            paths = {}
            for file in manifest.files:
                paths[file.path] = {
                    "exactHash": file.exactHash,
                    "exportedClaimRevisions": sorted(file.claimSpans),
                }
            exported_targets[target_id] = {"paths": paths, "manifestId": manifest.manifestId}
        return {"exportId": outcome.exportId, "runId": outcome.completedTargets and run_id or (journal.runId if journal else run_id),
                "stageRevision": stage_revision, "manifestSetId": journal.manifestSetId if journal else state.activeManifestSetId,
                "completedTargets": outcome.completedTargets, "remainingTargets": outcome.remainingTargets,
                "targets": exported_targets,
                "remainingPendingCount": pending_count}, findings, (), []


def _command_doctor(args: argparse.Namespace):
    if args.codex:
        return (
            {"checks": {}, "codexRequested": True, "codexCapability": "unavailable"},
            (_finding("codex.unavailable", "error", "Codex capability checks are unavailable in this P0 build", exit_code=6),),
            (), [],
        )
    root = Path(args.root).absolute()
    checks: dict[str, object] = {
        "python": sys.version_info >= (3, 11),
        "platform": sys.platform,
        "platformSupported": sys.platform in {"win32", "linux", "darwin"},
        "projectInitialized": False,
        "statePathConfined": False,
        "outputPathsConfined": False,
        "filesystemPermissions": {
            "rootReadable": os.access(root, os.R_OK),
            "rootWritable": os.access(root, os.W_OK),
            "stateReadable": None,
            "stateWritable": None,
            "outputWritable": None,
        },
        "gitAvailable": shutil.which("git") is not None,
        "schemaReadable": None,
        "journals": "unknown",
    }
    findings: list[Finding] = []
    if not checks["python"]:
        findings.append(_finding("doctor.python-unsupported", "error", "Python 3.11 or newer is required"))
    if not checks["platformSupported"]:
        findings.append(_finding("doctor.platform-unsupported", "error", "this operating system is unsupported"))
    if not checks["gitAvailable"]:
        findings.append(_finding("doctor.git-unavailable", "info", "Git is not available on PATH"))
    try:
        project, store = _store_context(args, required=False)
    except PathBoundaryError:
        checks["schemaReadable"] = False
        findings.append(_finding("doctor.path-unconfined", "error", "project configuration path is not confined"))
        return {"checks": checks, "codexRequested": False, "codexCapability": None}, tuple(findings), (), []
    except LedgerCorruptionError:
        checks["schemaReadable"] = False
        findings.append(_finding("doctor.schema-invalid", "error", "project configuration is unreadable or invalid", exit_code=7))
        return {"checks": checks, "codexRequested": False, "codexCapability": None}, tuple(findings), (), []
    checks["projectInitialized"] = project is not None
    if store is not None:
        assert project is not None
        try:
            state = _read_state(store)
            checks["schemaReadable"] = state is not None
            if state is None:
                findings.append(_finding("doctor.state-missing", "error", "project state document is missing"))
        except LedgerCorruptionError:
            checks["schemaReadable"] = False
            state = None
            findings.append(_finding("doctor.state-invalid", "error", "project state is unreadable or invalid", exit_code=7))
        try:
            checks["statePathConfined"] = canonical_repository_path(root, project.stateDir) == project.stateDir
            checks["outputPathsConfined"] = all(
                canonical_repository_path(root, target.livePath) == target.livePath
                for target in project.targets
            )
        except PathBoundaryError:
            checks["statePathConfined"] = False
            checks["outputPathsConfined"] = False
        permissions = checks["filesystemPermissions"]
        assert isinstance(permissions, dict)
        permissions["stateReadable"] = os.access(store.state_dir, os.R_OK)
        permissions["stateWritable"] = os.access(store.state_dir, os.W_OK)
        output_directories = [_existing_directory(root / target.livePath) for target in project.targets]
        permissions["outputWritable"] = all(os.access(path, os.W_OK) for path in output_directories)
        if not checks["statePathConfined"] or not checks["outputPathsConfined"]:
            findings.append(_finding("doctor.path-unconfined", "error", "a configured state or output path escapes the repository"))
        if not all(permissions.values()):
            findings.append(_finding("doctor.permissions", "error", "required repository, state, or output permissions are unavailable"))
        try:
            journals = store.list_export_journals()
            unresolved = [item for item in journals if item.status != "committed"]
            checks["journals"] = "unresolved" if unresolved else "clear"
            for item in unresolved:
                findings.append(_finding("export.recovery-required", "incomplete", "an export journal requires explicit resume",
                                         details={"exportId": item.exportId, "status": item.status}, exit_code=7))
        except (OSError, ValueError, TypeError):
            checks["journals"] = "invalid"
            findings.append(_finding("doctor.journal-invalid", "error", "export journal history is unreadable or invalid", exit_code=7))
    elif project is None:
        checks["statePathConfined"] = True
        checks["outputPathsConfined"] = True
        checks["schemaReadable"] = True
    return {"checks": checks, "codexRequested": False, "codexCapability": None}, tuple(findings), (), []


def _command_maintain(args: argparse.Namespace):
    _project, store = _store_context(args, required=args.maintain_command not in {"status", "catch-up"})
    if store is None:
        message = ("initialize this repository with `repo2skill init`, then run `maintain setup` and "
                   "`maintain catch-up --rebuild` to establish an approval-gated baseline")
        return {"observedEventCursor": None, "appliedBaseline": None, "jobs": [],
                "bootstrapRequired": True, "targetCommit": None}, (
            _finding("maintenance.rebuild-required", "stale", message, exit_code=4),
        ), (), ["run repo2skill init", "run repo2skill maintain setup", "run repo2skill maintain catch-up --rebuild"]
    assert store is not None
    maintenance = MaintenanceStore(store.root, store)
    if args.maintain_command in {"approve", "skip", "retry"} and not re.fullmatch(
        r"[a-z][a-z0-9-]*_[0-9a-f]{24}\Z", args.job_id if isinstance(args.job_id, str) else "",
    ):
        return None, (_finding("invocation.invalid", "error", "job ID is invalid", details={"exitCode": 1}),), (), []
    if args.maintain_command == "setup":
        if not args.manual_hooks:
            preflight = preflight_hooks(store.root, store)
            if not preflight.installed:
                finding = _finding("maintenance.manual-integration-required", "error", preflight.message,
                                   details={"exitCode": 1})
                return {"installed": False, "message": preflight.message,
                        "policy": maintenance.read_state().policy}, (finding,), (), [
                            "invoke maintain catch-up manually, then inspect maintain status",
                        ]
        if args.auto_approve:
            from .maintenance_approval import AutoGrantUnavailableError, configure_auto_approval
            try:
                configure_auto_approval(store)
            except AutoGrantUnavailableError as error:
                return {"installed": False, "policy": maintenance.read_state().policy}, (
                    _finding("maintenance.auto-grant-unavailable", "error", str(error), details={"exitCode": 1}),
                ), (), ["approve pending jobs individually"]
        else:
            from .maintenance_approval import revoke_auto_approval
            revoke_auto_approval(store, enabled=True)
        if args.manual_hooks:
            result = HookInstallResult(True, True, (), "maintenance enabled; install only the documented observation call in your own hook")
        else:
            result = install_hooks(store.root, store)
        findings = () if result.installed else (
            _finding("maintenance.manual-integration-required", "error", result.message,
                     details={"exitCode": 1}),
        )
        if result.manualIntegrationRequired and result.installed:
            findings = ()
        actions = [] if result.installed and not result.manualIntegrationRequired else ["invoke maintain catch-up manually, then inspect maintain status"]
        result_value = {"installed": result.installed, "manualIntegrationRequired": result.manualIntegrationRequired,
                        "hooks": result.hooks, "message": result.message,
                        "policy": maintenance.read_state().policy}
        return result_value, findings, (), actions
    if args.maintain_command == "disable":
        result = remove_owned_hooks(store.root, store)
        if not result.installed:
            return result, (_finding("maintenance.auto-grant-revocation-failed", "error", result.message,
                                     details={"exitCode": 7}),), (), []
        return {"disabled": True, "manualIntegrationRequired": result.manualIntegrationRequired,
                "hooks": result.hooks, "message": result.message,
                "policy": maintenance.read_state().policy}, (), (), []
    if args.maintain_command == "status":
        state = maintenance.read_state()
        from .maintenance_approval import _matches_current, _policy_revision, auto_approval_binding
        jobs = []
        findings = []
        live_bootstrap = any(
            lifecycle.status not in {"skipped", "applied"}
            for lifecycle in state.jobs.values()
        )
        if state.appliedBaseline is None and not live_bootstrap:
            findings.append(_finding(
                "maintenance.rebuild-required", "stale",
                "no verified applied baseline exists; run maintain catch-up --rebuild and approve its job",
                exit_code=4,
            ))
        for job_id, lifecycle in state.jobs.items():
            job = maintenance.read_job(job_id)
            if job is None:
                raise LedgerCorruptionError("maintenance state references a missing job")
            jobs.append({"jobId": job_id, "targetCommit": job.targetCommit,
                         "impactScopes": job.impactScopes, "status": lifecycle.status,
                         "approvalSource": lifecycle.approvalSource,
                         "approvalState": lifecycle.status,
                         "baselineCommit": job.baselineCommit,
                         "targetKinds": job.targetKinds,
                         "supersededByJobId": lifecycle.supersededByJobId,
                         "resolved": lifecycle.status == "stale" and _superseded_resolved(state, job_id, lifecycle)})
            auto_eligible = (
                lifecycle.status == "pending" and state.policy.approval == "auto"
                and auto_approval_binding(store, job, lifecycle) is not None
            )
            if lifecycle.status == "pending" and not auto_eligible:
                findings.append(_finding("maintenance.approval-required", "pending",
                                         "a maintenance job is waiting for approval", details={"jobId": job_id}, exit_code=3))
            elif lifecycle.status == "stale" and not _superseded_resolved(state, job_id, lifecycle):
                findings.append(_finding("maintenance.stale-binding", "stale",
                                         "a maintenance job must be rebuilt before it can run", details={"jobId": job_id}, exit_code=4))
            elif lifecycle.status == "failed":
                code = lifecycle.failureCode or "maintenance.agent-failed"
                if "conflict" in code:
                    findings.append(_finding("maintenance.source-conflict", "conflict",
                                             "maintenance encountered an owned-output conflict", details={"jobId": job_id}, exit_code=5))
                else:
                    findings.append(_finding("maintenance.agent-failed", "error",
                                             "maintenance agent attempt failed", details={"jobId": job_id}, exit_code=6))
        deferred = [{"eventId": item.eventId, "cursor": item.cursor, "targetCommit": item.targetCommit,
                     "reasonCode": item.reasonCode} for item in state.deferredEvents]
        resolved_deferred = [{"eventId": item.event.eventId, "cursor": item.event.cursor,
                              "targetCommit": item.event.targetCommit,
                              "reasonCode": item.event.reasonCode,
                              "resolutionReason": item.resolutionReason,
                              "resolvedByJobId": item.resolvedByJobId}
                             for item in state.resolvedDeferredEvents]
        for item in state.deferredEvents:
            capped = any(token in (item.reasonCode or "") for token in ("cap", "timeout"))
            findings.append(_finding(
                "maintenance.coverage-required" if capped else "maintenance.deferred-event",
                "incomplete" if capped else "stale",
                "a deferred Git event still needs bounded catch-up",
                details={"eventId": item.eventId, "reasonCode": item.reasonCode},
                exit_code=2 if capped else 4,
            ))
        return {"observedEventCursor": state.observedEventCursor, "appliedBaseline": state.appliedBaseline,
                "policyRevision": _policy_revision(store),
                "enabled": state.enabled,
                "policy": {"approval": state.policy.approval, "grantRevision": state.policy.grantRevision,
                           "targetKinds": state.policy.targetKinds,
                           "autoGrantActive": _matches_current(store, state) if state.policy.approval == "auto" else False},
                "jobs": jobs, "deferredEvents": deferred,
                "resolvedDeferredEvents": resolved_deferred}, tuple(findings), (), []
    if args.maintain_command in {"approve", "retry"}:
        from .maintenance_approval import OwnershipConflictError, StaleApprovalError, approve_job, verify_approval_binding
        job_id = args.job_id
        try:
            state = maintenance.read_state()
            lifecycle = state.jobs.get(job_id)
            if lifecycle is None:
                raise LookupError("maintenance job does not exist")
            job = maintenance.read_job(job_id)
            if job is None:
                raise LookupError("maintenance job does not exist")
            if args.maintain_command == "retry" and lifecycle.status not in {"failed", "running"}:
                return None, (_finding("maintenance.retry-not-allowed", "error",
                                       "retry requires an explicitly retried failed attempt", details={"exitCode": 1}),), (), []
            if args.maintain_command == "approve" and lifecycle.status == "pending":
                approved = approve_job(store, job_id, grant_revision=state.policy.grantRevision)
            elif (args.maintain_command == "approve" and lifecycle.status == "approved"
                  and lifecycle.activeAttemptId is None):
                # Approval was already persisted before a busy lease response. Reuse its exact binding.
                approved = verify_approval_binding(store, job, lifecycle)
            elif args.maintain_command == "retry" and lifecycle.status == "running":
                # Let the leased worker resolve the durable attempt before deciding whether a new attempt is needed.
                approved = lifecycle
            elif args.maintain_command == "retry" and lifecycle.status == "failed":
                approved = approve_job(store, job_id, grant_revision=state.policy.grantRevision)
            else:
                raise StaleApprovalError("job is not eligible for this command")
        except StaleApprovalError as error:
            return None, (_finding("maintenance.stale-binding", "stale", str(error), details={"exitCode": 4}),), (), []
        except OwnershipConflictError as error:
            return None, (_finding("maintenance.source-conflict", "conflict", str(error), details={"jobId": job_id, "exitCode": 5}),), (), []
        from .maintenance_worker import run_worker
        result = run_worker(store, foreground=True, job_id=job_id,
                            explicit_retry=args.maintain_command == "retry")
        code_by_status = {"applied": 0, "noop": 0, "agent-unavailable": 6, "agent-failed": 6,
                          "conflict": 5, "stale": 4, "approval-required": 4, "not-found": 1,
                          "disabled": 4, "busy": 4, "busy-expired": 4,
                          "rebuild-required": 4, "incomplete": 7, "unsupported": 6}
        exit_code = code_by_status.get(result.status, 6)
        findings = () if exit_code == 0 else (_finding(
            {5: "maintenance.source-conflict", 6: "maintenance.agent-unavailable", 4: "maintenance.rebuild-required", 7: "maintenance.rebuild-required"}.get(exit_code, "maintenance.run-failed"),
            {5: "conflict", 6: "error", 4: "stale"}.get(exit_code, "error"), result.message,
            details={"jobId": job_id, "exitCode": exit_code},
        ),)
        return {"jobId": job_id, "status": result.status, "approval": approved,
                "appliedBaseline": maintenance.read_state().appliedBaseline}, findings, (), []
    if args.maintain_command == "skip":
        with store.mutation():
            state = maintenance.read_state()
            lifecycle = state.jobs.get(args.job_id)
            job = maintenance.read_job(args.job_id)
            if lifecycle is None or job is None:
                raise LookupError("maintenance job does not exist")
            if lifecycle.status not in {"pending", "failed", "stale"}:
                return None, (_finding("maintenance.skip-not-allowed", "error", "only pending or unresolved jobs can be skipped",
                                       details={"exitCode": 1}),), (), []
            jobs = dict(state.jobs)
            jobs[args.job_id] = replace(lifecycle, status="skipped", approvalSource=None,
                                        approvalBindingHash=None, skippedTargetCommit=job.targetCommit)
            maintenance.replace_state(replace(state, jobs=jobs))
        return {"jobId": args.job_id, "status": "skipped", "appliedBaseline": state.appliedBaseline}, (), (), []
    if args.maintain_command == "catch-up":
        from .maintenance_git import (
            GitReadBudget, MaintenanceGitError, _resolve_commit, _proves_ancestor,
            _git, bootstrap_commit, compare_commits, enqueue_event,
        )
        from .maintenance_models import GitEvent
        from .ids import canonical_record_id
        state = maintenance.read_state()
        budget = GitReadBudget()
        try:
            target = _git(store.root, ("rev-parse", "--verify", "HEAD^{commit}"), 256, budget).decode("ascii").strip()
            target = _resolve_commit(store.root, target, budget)
            if state.appliedBaseline is None and not args.rebuild:
                return {"appliedBaseline": None, "targetCommit": target, "jobs": []}, (
                    _finding("maintenance.rebuild-required", "stale",
                             "no applied baseline exists; pass --rebuild to inventory this committed tree",
                             exit_code=4),
                ), (), ["run maintain catch-up --rebuild"]
            rebuilding = args.rebuild
            if state.appliedBaseline is not None and not rebuilding:
                if not _proves_ancestor(store.root, state.appliedBaseline, target, budget):
                    return {"appliedBaseline": state.appliedBaseline, "targetCommit": target,
                            "rebuildRequired": True, "jobs": []}, (
                        _finding("maintenance.rebuild-required", "stale",
                                 "the observed commit does not descend from the verified baseline; use --rebuild",
                                 exit_code=4),
                    ), (), ["run maintain catch-up --rebuild"]
                impact = compare_commits(store.root, state.appliedBaseline, target, budget=budget)
            elif rebuilding:
                impact = bootstrap_commit(store.root, target, budget=budget)
                impact = replace(impact, baselineCommit=state.appliedBaseline)
            else:
                impact = compare_commits(store.root, state.appliedBaseline, target, budget=budget)
        except (MaintenanceGitError, OSError, subprocess.SubprocessError) as error:
            error_code = getattr(error, "code", None)
            coverage_failure = error_code in {"maintenance.git-output-cap", "maintenance.git-timeout",
                                              "maintenance.context-file-cap", "maintenance.context-byte-cap"}
            if coverage_failure:
                return {"appliedBaseline": state.appliedBaseline, "rebuildRequired": False, "jobs": [],
                        "coverageFailure": error_code}, (
                    _finding("maintenance.coverage-required", "incomplete",
                             f"committed context could not be covered within the configured limit: {error}",
                             details={"reasonCode": error_code}, exit_code=2),
                ), (), ["reduce the repository context or retry maintain catch-up"]
            return {"appliedBaseline": state.appliedBaseline, "rebuildRequired": True, "jobs": []}, (
                _finding("maintenance.rebuild-required", "stale",
                         f"committed snapshot could not be proven or covered: {error}", exit_code=4),
            ), (), ["restore the baseline commit or run maintain catch-up --rebuild"]
        event_data = {"kind": "manual-catch-up", "targetCommit": target,
                      "baselineCommit": state.appliedBaseline, "rebuild": rebuilding}
        event = GitEvent(canonical_record_id("git-event", event_data), "manual-catch-up",
                         "manual:" + canonical_record_id("event-cursor", event_data), target)
        job = enqueue_event(store, event, impact, budget=budget,
                            allow_unreachable_target=rebuilding,
                            allow_baseline_bootstrap=rebuilding,
                            impact_projection="tree-bootstrap" if rebuilding else None)
        current = maintenance.read_state()
        deferred = next((item for item in current.deferredEvents if item.eventId == event.eventId), None)
        if deferred is not None and any(token in (deferred.reasonCode or "") for token in ("cap", "timeout")):
            return {"appliedBaseline": current.appliedBaseline, "targetCommit": target,
                    "rebuildRequested": rebuilding, "coverageFailure": deferred.reasonCode}, (
                _finding("maintenance.coverage-required", "incomplete",
                         "required committed context exceeds a read cap or time budget; no job was published",
                         details={"reasonCode": deferred.reasonCode}, exit_code=2),
            ), (), ["reduce the repository context or retry maintain catch-up"]
        if job is None:
            if any(item.status == "pending" for item in current.jobs.values()):
                pending = [{"jobId": key, "targetCommit": (maintenance.read_job(key).targetCommit)}
                           for key, item in current.jobs.items() if item.status == "pending"]
                return {"appliedBaseline": current.appliedBaseline, "targetCommit": target,
                        "jobs": pending, "rebuildRequested": rebuilding}, (
                    _finding("maintenance.approval-required", "pending",
                             "maintenance work is waiting for approval", exit_code=3),
                ), (), []
            if deferred is not None:
                return {"appliedBaseline": current.appliedBaseline, "targetCommit": target,
                        "jobs": [], "deferredEvent": {"eventId": deferred.eventId,
                        "reasonCode": deferred.reasonCode}}, (
                    _finding("maintenance.deferred-event", "stale",
                             "the observed event remains deferred and needs catch-up", details={
                                 "eventId": deferred.eventId, "reasonCode": deferred.reasonCode,
                             }, exit_code=4),
                ), (), ["retry maintain catch-up after resolving the reported condition"]
            unresolved = [
                (key, lifecycle) for key, lifecycle in current.jobs.items()
                if lifecycle.status in {"approved", "running", "failed"}
                or (lifecycle.status == "stale" and not _superseded_resolved(current, key, lifecycle))
            ]
            if unresolved:
                active = [{"jobId": key, "status": lifecycle.status,
                           "targetCommit": maintenance.read_job(key).targetCommit}
                          for key, lifecycle in unresolved]
                if any(lifecycle.status in {"approved", "running", "stale"} for _key, lifecycle in unresolved):
                    return {"appliedBaseline": current.appliedBaseline, "targetCommit": target,
                            "jobs": active, "recoveryRequired": True}, (
                        _finding("maintenance.recovery-required", "stale",
                                 "unresolved or in-progress maintenance work remains visible",
                                 exit_code=4),
                    ), (), ["inspect maintain status and resume the exact job"]
                failed = unresolved[0][1]
                conflict = "conflict" in (failed.failureCode or "")
                code = 5 if conflict else 6
                return {"appliedBaseline": current.appliedBaseline, "targetCommit": target,
                        "jobs": active, "retryRequired": True}, (
                    _finding("maintenance.source-conflict" if conflict else "maintenance.agent-failed",
                             "conflict" if conflict else "error",
                             "a failed maintenance attempt requires explicit retry",
                             exit_code=code),
                ), (), ["inspect maintain status, then run maintain retry with the exact job ID"]
            return {"appliedBaseline": current.appliedBaseline, "targetCommit": target,
                    "jobs": [], "rebuildRequested": rebuilding}, (), (), []
        return {"appliedBaseline": current.appliedBaseline, "targetCommit": target,
                "jobs": [{"jobId": job.jobId, "targetCommit": job.targetCommit,
                          "impactScopes": job.impactScopes, "status": current.jobs[job.jobId].status}],
                "rebuildRequested": rebuilding}, (
            _finding("maintenance.approval-required", "pending",
                     "review this exact committed-tree job, then approve its job ID", details={"jobId": job.jobId}, exit_code=3),
        ), (), [f"run maintain approve {job.jobId}"]
    raise InvocationError("unknown maintenance command")


def _dispatch(args: argparse.Namespace):
    command = args.command
    if command == "init":
        return _command_init(args)
    if command == "scan":
        return _command_scan(args)
    if command == "generate":
        return _command_generate(args)
    if command == "review":
        return _command_review(args)
    if command == "check":
        return _command_check(args)
    if command == "explain":
        return _command_explain(args)
    if command == "report":
        return _command_report(args)
    if command == "export":
        return _command_export(args)
    if command == "doctor":
        return _command_doctor(args)
    if command == "maintain":
        return _command_maintain(args)
    raise InvocationError("unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    requested_json = "--format=json" in raw_argv or any(
        raw_argv[index:index + 2] == ["--format", "json"] for index in range(len(raw_argv) - 1)
    )
    try:
        args = parser.parse_args(raw_argv)
        # Global options are accepted on either side of the command.
        args.root = getattr(args, "root", ".")
        args.config = getattr(args, "config", None)
        args.format = getattr(args, "format", "text")
        args.quiet = getattr(args, "quiet", False)
        if args.command == "review":
            args.review_command = args.review_command
        if args.command == "maintain":
            try:
                result, findings, diagnostics, next_actions = _dispatch(args)
            except (OSError, ValueError, TypeError) as error:
                raise LedgerCorruptionError("maintenance state or records are invalid") from error
        else:
            result, findings, diagnostics, next_actions = _dispatch(args)
    except _ParserExit as exit_request:
        return exit_request.status
    except InvocationError:
        command = "review" if "args" in locals() and getattr(args, "command", None) == "review" else getattr(locals().get("args"), "command", "invocation")
        envelope = _envelope(command, None, (_finding("invocation.invalid", "error", "command options are invalid"),))
        fmt = getattr(locals().get("args"), "format", "json" if requested_json else "text")
        quiet = getattr(locals().get("args"), "quiet", False)
        return _print_envelope(envelope, fmt, quiet)
    except LockBusyError:
        result, findings, diagnostics, next_actions = None, (_finding("state.lock-busy", "error", "project state is busy"),), (), []
    except ExportRecoveryRequired as error:
        findings = tuple(_finding("export.recovery-required", "incomplete", "an unresolved export journal blocks mutation",
                                  details={"exportId": item.exportId, "status": item.status}, exit_code=7) for item in error.journals)
        result, diagnostics, next_actions = None, (), ["resume the named export journal"]
    except (LedgerCorruptionError, ProjectDataCorruptionError):
        result, findings, diagnostics, next_actions = None, (_finding("ledger.corrupt", "error", "persisted repository records are invalid", exit_code=7),), (), []
    except ActivationFreshnessError as error:
        result, findings, diagnostics, next_actions = None, error.result.findings, (), ["refresh source inputs before activation"]
    except ExportFreshnessError as error:
        result, findings, diagnostics, next_actions = None, error.result.findings, (), ["refresh source inputs before export"]
    except (LookupError, FileNotFoundError):
        result, findings, diagnostics, next_actions = None, (_finding("lookup.not-found", "error", "requested local record does not exist"),), (), []
    except (PathBoundaryError, RepositoryIdentityError, InventoryError, ValueError, TypeError) as error:
        result, findings, diagnostics, next_actions = None, (_finding("state.invalid", "error", "configuration, source identity, or local state is invalid",
                                                                       details={"errorType": type(error).__name__}),), (), []
    except OSError:
        result, findings, diagnostics, next_actions = None, (_finding("io.failed", "error", "a required local file operation failed"),), (), []
    except Exception:
        result, findings, diagnostics, next_actions = None, (_finding("internal.error", "error", "an unexpected internal error occurred", exit_code=7),), (), []
    command_name = args.command
    if command_name == "review":
        command_name = f"review-{args.review_command}"
    if command_name == "export" and getattr(args, "resume", None):
        command_name = "export-resume"
    if command_name == "maintain":
        command_name = f"maintain {args.maintain_command}"
        if args.maintain_command in {"approve", "skip", "retry"}:
            command_name += f" {args.job_id}"
    envelope = _envelope(command_name, result, findings, diagnostics, next_actions)
    return _print_envelope(envelope, args.format, args.quiet)
