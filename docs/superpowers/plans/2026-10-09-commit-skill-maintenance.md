# Commit-triggered skill maintenance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local Git-triggered queue that diffs committed source against the last successfully applied skill baseline, asks before AI by default, validates one bounded agent result, and applies it directly on success.

**Architecture:** Keep maintenance events, approvals, queue, and cross-store recovery in versioned `.repo2skill/maintenance/` documents. Analyze immutable Git tree bytes, call the installed headless Codex CLI through a fail-closed adapter, extend claims with an explicit evidence-backed prose mode, and reuse P0 validation, staging, ownership, and export journals.

**Tech Stack:** Python 3.11+, existing repo2skill JSON ledgers and standard-library subprocess/filesystem APIs, Git plumbing through `git_read.run_git_bounded`, installed official Codex CLI via a fake-tested adapter.

**Spec:** `docs/superpowers/specs/2026-10-09-commit-skill-maintenance-design.md`

## Global Constraints

- The baseline is the commit whose skill output was successfully applied and verified, never the newest commit merely observed by a hook.
- The normal sequence is: a supported Git hook observes a completed repository event; repo2skill reads the committed tree, compares it with the applied baseline, and queues a relevant change; the local CLI reports the pending job; the user approves that exact job; repo2skill then calls the installed headless agent, validates its structured operations, and applies the accepted result on success.
- That approval authorizes both the AI call and its bounded update. It does not produce a second draft-review approval step.
- A versioned, revocable explicit auto-approval setting may skip the approval before AI. Time passing never grants approval.
- Git input is the committed tree, never the dirty working tree. Do not execute repository code, tests, hooks, filters, or build commands while scanning.
- Changes limited to repo2skill state or outputs do not self-trigger. Source and generated-output changes together still process the source change and run output ownership checks.
- Any stale job binding, unknown reference, unsupported operation, malformed or partial response, timeout, cap error, or export conflict leaves the prior applied baseline and user bytes intact.
- `check` remains read-only. Hook processing never invokes AI or blocks for approval. No automatic Git commit/push, telemetry, remote notification, arbitrary shell command, UI automation, graph database, or vector store.
- Windows, Linux, and macOS are first-class. The core dependency set remains the standard library plus existing `pathspec` and `PyYAML`.
- Preserve P0 schema 1.0 readability, IDs, origin/kind meanings, manifests, and export recovery. Add optional backward-safe fields or new maintenance documents; do not rewrite immutable P0 runs.

## Review Focus

- Renamed/deleted files, glob additions, negative predicates, and control-file changes must be conservatively scoped from NUL-delimited committed-tree diffs; test in Task 3.
- A target commit or source/policy fingerprint change after approval must invalidate approval before invocation or apply; test in Tasks 5 and 8.
- A crash after P0 export commit but before baseline advancement must recover from verified manifests and exact hashes; test in Task 8.
- Mixed valid and forged agent operations must reject the whole response with no pointer change; test in Task 7.
- Dirty unrelated files must not alter committed analysis, while edits to an owned target must produce an ownership conflict; test in Tasks 3 and 8.

---

## File map

- Create `src/repo2skill/maintenance_models.py`: immutable maintenance job, event, queue, policy, and apply-journal records.
- Create `src/repo2skill/maintenance_store.py`: confined versioned JSON persistence under `.repo2skill/maintenance/`, under `LedgerStore.mutation()`.
- Create `src/repo2skill/maintenance_git.py`: hook event parsing, applied-baseline-to-target diff, commit-tree blob reads, and impact mapping.
- Create `src/repo2skill/maintenance_hooks.py`: safe repository-local hook installation/removal and short enqueue launch; refuse configured/shared hook directories and foreign hooks.
- Create `src/repo2skill/maintenance_worker.py`: worker lease/recovery, bounded snapshot materialization, CLI notification, and queued job execution dispatch.
- Create `src/repo2skill/maintenance_agent.py`: installed Codex CLI capability adapter and strict structured-operation parser.
- Create `src/repo2skill/maintenance_attempts.py`: durable invocation attempt states and explicit recovery/retry boundary.
- Modify `src/repo2skill/models.py`, `src/repo2skill/validation.py`, `src/repo2skill/render.py`, `src/repo2skill/claims.py`: explicit typed/prose support mode, claim construction, validation, rendering.
- Modify `src/repo2skill/cli.py`: future `maintain setup|status|approve|skip|retry|catch-up|disable` grammar and dispatch; retain `check` behavior.
- Modify `src/repo2skill/ledger.py`, `src/repo2skill/ownership.py`, `src/repo2skill/dependencies.py`: narrow integration seams for maintenance recovery, export verification, and committed-source freshness.
- Modify `docs/engineering/cli-contract.md`, `docs/schema-migrations.md`, and `docs/superpowers/specs/2026-10-07-repo2skill-mvp-design.md`: record future command/schema precedence; do not mark implementation complete until code ships.
- Modify `docs/product/feature-catalog.md`: add only a forward pointer; do not duplicate or renumber feature IDs.
- Tests live in `tests/maintenance/`, `tests/fixtures/maintenance/`, and existing `tests/test_validation.py`, `tests/test_ownership.py`, `tests/test_cli.py`, and `tests/test_cli_json.py` as ownership dictates.

## Shared interfaces

The names and types below are the integration contract for all tasks. Implement
them in their named files before downstream tasks consume them.

```python
class MaintenancePolicy:
    approval: Literal["ask", "auto"]
    grantRevision: int
    targetKinds: tuple[Literal["agents-md", "skill"], ...]

class MaintenanceLifecycle:
    status: Literal["pending", "approved", "running", "skipped", "applied", "failed", "stale"]
    approvalSource: Literal["user", "auto"] | None
    approvalBindingHash: str | None
    notificationState: Literal["pending", "reported"]
    skippedTargetCommit: str | None
    activeAttemptId: str | None
    failureCode: str | None

class MaintenanceJob:
    """Immutable; no status, approval, lease, or attempt fields."""
    jobId: str
    baselineCommit: str | None
    targetCommit: str
    eventIds: tuple[str, ...]
    changedPaths: tuple[str, ...]
    impactScopes: tuple[str, ...]
    diffHash: str
    sourceContextHash: str
    policyRevision: str
    targetKinds: tuple[str, ...]
    expectedManifestSetId: str | None
    expectedOutputHashes: Mapping[str, str]

class MaintenanceState:
    schemaVersion: str
    observedEventCursor: str | None
    appliedBaseline: str | None
    policy: MaintenancePolicy
    deferredEvents: tuple[GitEvent, ...]
    jobs: Mapping[str, MaintenanceLifecycle]
    runningJobId: str | None

class ClaimOperation:
    op: Literal["add", "update", "delete"]
    semanticKey: str
    scope: str
    renderedText: str | None
    factIds: tuple[str, ...]
    evidenceIds: tuple[str, ...]
    claimRef: str | None

class ApplicationAuthorization:
    authorizationId: str
    jobId: str
    approvalBindingHash: str
    responseHash: str
    acceptedClaimRefs: tuple[str, ...]
    targetKinds: tuple[str, ...]

class MaintenanceRunBundle:
    run: RunLedger
    selectedSpecs: tuple[TargetSpec, ...]
    rendered: RenderBundle
    authorization: ApplicationAuthorization

class CommittedSourceGuard:
    jobId: str
    targetCommit: str
    sourceContextHash: str
    policyRevision: str
    authorizationId: str

def enqueue_event(store: LedgerStore, event: GitEvent, impact: CommitImpact | None,
                  reason_code: str | None = None) -> MaintenanceJob | None: ...
def compare_commits(root: Path, baseline: str, target: str) -> CommitImpact: ...
def bootstrap_commit(root: Path, target: str) -> CommitImpact: ...
def materialize_commit(root: Path, commit: str, paths: Sequence[str], destination: Path) -> SnapshotManifest: ...
def approve_job(store: LedgerStore, job_id: str, *, grant_revision: int | None) -> MaintenanceLifecycle: ...
def invoke_agent(capability: AgentCapability, request: AgentRequest, timeout_seconds: float) -> AgentResponse: ...
def validate_operations(response: AgentResponse, context: MaintenanceContext) -> tuple[ClaimOperation, ...]: ...
def build_maintenance_run(store: LedgerStore, job: MaintenanceJob, context: MaintenanceContext,
                          operations: tuple[ClaimOperation, ...],
                          authorization: ApplicationAuthorization) -> MaintenanceRunBundle: ...
def apply_job(store: LedgerStore, job: MaintenanceJob, bundle: MaintenanceRunBundle,
              guard: CommittedSourceGuard) -> ApplyResult: ...
def recheck_committed_source(store: LedgerStore, guard: CommittedSourceGuard) -> None: ...
```

`LedgerStore` remains the P0 authority for project/runs/manifests. Maintenance
documents have separate IDs and schemas. `compare_commits` consumes commit
objects only; `bootstrap_commit` explicitly inventories a target tree when no
applied baseline exists. Event enqueue writes an immutable job then replaces
the single `MaintenanceState` pointer once; there is no separate queue file.
`apply_job` must re-read the immutable job and all fingerprints while holding
the project mutation lock. `ClaimOperation` is the one operation type used by
both validation and claim construction. Exact helper fields may be added, but
these names and behavioral contracts stay stable across tasks.

CLI JSON uses the P0 one-envelope stdout contract and stderr diagnostics.
`maintain status` reports `observedEventCursor`, `appliedBaseline`, policy
revision, and per-job `{jobId,targetCommit,impactScopes,status,approvalSource}`;
it exits 7 for corrupt state, 5 for an ownership conflict, 6 for an agent
failure, 4 for stale/rebuild-required work, 3 when a user decision is pending,
otherwise 0. `approve` and `retry` run in the foreground and wait through
validation and apply; they exit 0 only when applied/no-op, 4 when approval or
binding is stale, 5 on owned-output conflict, 6 on unavailable/failed agent,
and 7 on corrupt state/recovery. `skip` and successful `setup`/`disable` exit 0;
invalid job/options exit 1. `catch-up` exits 3 when approval is pending, 2 on
required snapshot coverage/cap failure, 4 when baseline cannot be proven, and
0 when nothing relevant changed. Diagnostics use stable codes
`maintenance.approval-required`, `.stale-binding`, `.source-conflict`,
`.agent-unavailable`, `.agent-failed`, and `.rebuild-required`.

## Tasks

### Task 1: Codex capability spike gate

**Files:** Create `src/repo2skill/maintenance_agent.py`; test `tests/maintenance/test_agent_capability.py`, `tests/fixtures/maintenance/codex/`.

**Interfaces:** Produces `AgentCapability(executable: Path | None, version: str | None, supported: bool, missingCapabilities: tuple[str, ...])` and `probe_agent(executable: Path | None) -> AgentCapability`. Check only the local CLI surface: stdin prompt, output schema, output-file framing, explicit read-only sandbox selection, user-config handling, working-directory selection, timeout, and bounded output. No authentication/model/network probe.

- [ ] **Red:** Fake executable asserts exact argument vector, stdin bytes, output path, temp cwd outside source root, read-only flag, configured timeout, bounded response read, and no source-tree write. Add one fixture missing each required surface and assert its name appears in `missingCapabilities`.
- [ ] Run `python -m pytest tests/maintenance/test_agent_capability.py -v`; expect the capability probe to be absent.
- [ ] **Green:** Derive supported arguments from installed local help/version. Establish a clean temp cwd with no source-owned `AGENTS.md`; if the installed version cannot select required stdin/schema/output/read-only/config/cwd behavior, mark it unsupported with a specific reason before any model call. Treat the installed CLI and saved auth as trusted local components; do not claim secret isolation or prove all instruction loading. No real call.
- [ ] Run the fake-executable tests; expect each unsupported fixture to fail closed and no executable invocation beyond local help inspection.
- [ ] Commit `feat: gate maintenance on Codex CLI capabilities`.

### Task 2: Versioned maintenance records and durable queue

**Files:** Create `src/repo2skill/maintenance_models.py`, `src/repo2skill/maintenance_store.py`; modify `src/repo2skill/ledger.py`; test `tests/maintenance/test_store.py`.

**Interfaces:** Produces `MaintenancePolicy`, immutable `MaintenanceJob`, `MaintenanceLifecycle`, `MaintenanceState`, and `MaintenanceStore(root: Path, ledger: LedgerStore)` with `read_state()`, `read_job(job_id)`, `write_job(job)`, and `replace_state(state)`. `MaintenanceState.jobs` is the sole queue/lifecycle authority; pending/running queue views derive from it. Every write requires active `ledger.mutation()`; immutable jobs use exclusive create and one atomic `maintenance/state.json` replacement commits cursor, job visibility, lifecycle, policy, and running lease together. Unknown major schema fails closed; old project documents remain readable.

- [ ] **Red:** Add `test_old_project_without_maintenance_reads_empty_state`, `test_job_is_immutable`, `test_single_pointer_commits_cursor_and_job_together`, `test_crash_after_job_write_before_pointer_leaves_only_orphan`, `test_unknown_maintenance_major_is_rejected`, and `test_duplicate_event_id_is_idempotent`. Pin the transaction with `assert state.observedEventCursor == event.cursor` and `assert state.jobs[job.jobId].status == "pending"`; after injected pre-pointer crash assert `read_state().observedEventCursor == old_cursor` and `job.jobId not in read_state().jobs`.
- [ ] Run `python -m pytest tests/maintenance/test_store.py -v`; expect failures for missing maintenance records/store.
- [ ] **Green:** Implement exact record validation and `.repo2skill/maintenance/{state.json,jobs/<jobId>.json}` persistence through `LedgerStore` lock and guarded store I/O. Write immutable job first, then atomically replace the single state pointer; orphan jobs are ignored.
- [ ] Run `python -m pytest tests/maintenance/test_store.py -v`; expect all six cases to pass, including reopening from disk.
- [ ] Commit `feat: add durable maintenance queue`.

### Task 3: Commit-tree diff, impact, and catch-up

**Files:** Create `src/repo2skill/maintenance_git.py`; modify `src/repo2skill/git_read.py` only if a bounded helper is needed; test `tests/maintenance/test_git.py`, `tests/fixtures/maintenance/git/`.

**Interfaces:** Produces `GitEvent(eventId, hookName, cursor, targetCommit)`, `CommitImpact(baselineCommit: str | None, targetCommit: str, paths, renames, diffHash, scopes, requiredFullScan, diagnostics)`, `SnapshotManifest(commit, files, omittedPaths, contextHash)`, `parse_hook_event(hook_name: str, stdin: bytes, argv: Sequence[str]) -> GitEvent`, `compare_commits(root: Path, baseline: str, target: str) -> CommitImpact` (both commits must exist), `bootstrap_commit(root: Path, target: str) -> CommitImpact` (explicit full-tree bootstrap when baseline is null), `enqueue_event(store: LedgerStore, event: GitEvent, impact: CommitImpact | None, reason_code: str | None = None) -> MaintenanceJob | None`, `read_commit_blob(root: Path, commit: str, path: str, max_bytes: int) -> bytes`, and `materialize_commit(root: Path, commit: str, paths: Sequence[str], destination: Path) -> SnapshotManifest`. `enqueue_event` writes an immutable job when impact completed or appends the event to `MaintenanceState.deferredEvents` with a reason; either path updates the cursor in the same state-pointer replacement. Consume `run_git_bounded` and existing dependency selectors.

- [ ] **Red:** Add `test_diff_uses_commit_blobs_when_worktree_is_dirty`, `test_nul_paths_preserve_renames_and_newlines`, `test_diff_disables_external_diff_and_textconv`, `test_added_glob_match_and_negative_invalidation_queue_scope`, `test_deleted_and_control_file_changes_are_conservative`, `test_state_and_output_only_change_has_no_impact`, `test_missing_baseline_requires_rebuild`, and `test_bootstrap_has_explicit_full_tree_impact`. Pin with `assert dirty_impact.diffHash == clean_impact.diffHash` and `assert bootstrap.requiredFullScan is True`. These pin Review Focus lines 1 and 5.
- [ ] Run `python -m pytest tests/maintenance/test_git.py -v`; expect failures because commit impact APIs do not exist.
- [ ] **Green:** Use bounded Git commands with `--no-ext-diff --no-textconv` and NUL path framing; parse trees and blobs from the specified commits. Map known selectors to scope and force bounded full reanalysis for ambiguous changes. Never fall back to worktree bytes.
- [ ] Run `python -m pytest tests/maintenance/test_git.py -v`; expect all cases to pass and cap/object failures to retain diagnostic codes.
- [ ] Commit `feat: analyze committed repository changes`.

### Task 4: Hook setup, worker launch, and platform lifecycle

**Files:** Create `src/repo2skill/maintenance_hooks.py`, `src/repo2skill/maintenance_worker.py`; modify `src/repo2skill/cli.py`; test `tests/maintenance/test_hooks.py`, `tests/maintenance/test_worker.py`.

**Interfaces:** Produces `install_hooks(root: Path, store: LedgerStore) -> HookInstallResult`, `remove_owned_hooks(root: Path, store: LedgerStore) -> HookInstallResult`, and `run_worker(store: LedgerStore, *, foreground: bool = False) -> WorkerResult`. The installed hook calls Task 3 `parse_hook_event` + `compare_commits` + single-pointer `enqueue_event`, then writes a concise stdout notice with job ID, affected targets, baseline/target, and `maintain approve JOB_ID`; if bounded diff fails it writes an “analysis queued” notice with `maintain catch-up`/`status`. The notice is durably represented by the job lifecycle. Install only absent hooks in an ordinary repository-local `.git/hooks` directory. Refuse configured `core.hooksPath`, linked worktrees/shared Git dirs, and any foreign existing hook; print a manual integration diagnostic. Do not chain arbitrary shell/Python/executable hooks or mutate shared/global config. Worker uses OS lock/lease; hook performs no AI.

- [ ] **Red:** Add `test_setup_refuses_foreign_hook_without_modifying_bytes`, `test_setup_refuses_configured_or_shared_hooks_directory`, `test_setup_installs_only_absent_local_hooks`, `test_disable_removes_only_owned_hook`, `test_hook_prints_job_targets_and_approve_command`, `test_hook_budget_failure_prints_catch_up_not_completed_diff`, `test_post_checkout_wrapper_returns_success_after_diagnostic`, `test_worker_recovers_dead_lease`, and platform fixtures for Windows hidden launch plus POSIX executable mode. Assert refused setup leaves `hook.read_bytes() == before` and `git_config.read_bytes() == before`; successful observation stdout contains `maintain approve <jobId>` and persists `notificationState == "pending"` before delivery.
- [ ] Run `python -m pytest tests/maintenance/test_hooks.py tests/maintenance/test_worker.py -v`; expect failures for missing lifecycle functions.
- [ ] **Green:** Resolve whether the repository is ordinary/local and whether `core.hooksPath` is configured; refuse unsupported coexistence before any write. Atomically install wrappers only at absent local hook paths. At event time perform bounded diff/impact and atomic queue publication in the foreground, then print the durable job summary/approval command; on cap/time failure persist the deferred event and print catch-up/status. Auto-approved jobs may start the hidden worker. Provide foreground fallback.
- [ ] Run both test files; expect a foreign hook/shared directory to produce a manual-integration result with no filesystem/config changes.
- [ ] Commit `feat: install local commit maintenance hooks`.

### Task 5: Approval policy and pending-job CLI

**Files:** Modify `src/repo2skill/cli.py`, `src/repo2skill/maintenance_store.py`, `src/repo2skill/maintenance_worker.py`; test `tests/maintenance/test_cli.py`, `tests/maintenance/test_approval.py`.

**Interfaces:** Add `maintain setup [--auto-approve]`, `maintain status`, `maintain approve JOB_ID`, `maintain skip JOB_ID`, `maintain retry JOB_ID`, `maintain catch-up [--rebuild]`, and `maintain disable`. `approve_job(store, job_id, *, grant_revision=None) -> MaintenanceLifecycle` binds current policy revision and exact job fingerprints; `approve` runs in the foreground and waits through the agent/apply flow. Auto-approved jobs may use the hidden worker. `retry` is limited to an explicitly retried failed/unknown attempt. Auto approval must be explicit, versioned, scoped to configured target kinds, shown in status, and revoked on disable or policy revision change. CLI JSON preserves the P0 `{command,result,findings,diagnostics,nextActions}` envelope; status fields and exit codes are fixed in Shared interfaces above.

- [ ] **Red:** Add `test_default_status_requires_approval`, `test_status_reports_agent_failure_and_conflict`, `test_approve_binds_job_fingerprints`, `test_policy_change_does_not_approve_pending_job`, `test_auto_grant_is_versioned_and_revocable`, `test_skip_does_not_advance_baseline_or_repeat_same_event`, `test_new_target_after_skip_can_notify`, and CLI JSON envelope cases. Assert `json["command"] == "maintain status"`, exit 3 for pending approval, 4 for stale binding, 5/6 for conflict/agent failure, 0 only after successful apply, and `json["result"]["appliedBaseline"]` stays unchanged after skip.
- [ ] Run `python -m pytest tests/maintenance/test_cli.py tests/maintenance/test_approval.py -v`; expect failures for absent command tree and approval transitions. This pins Review Focus line 2.
- [ ] **Green:** Add command grammar and dispatch without changing `check`; print pending job ID, baseline, target, impacted scopes, and approval state. `approve` durably binds approval then synchronously runs and applies exactly that job; auto-approved jobs dispatch to the hidden worker. `retry` requires explicit user action for failed/unknown attempts. No elapsed-time approval.
- [ ] Run both test files; expect invalid/stale approval to return the documented stale finding and perform no model call.
- [ ] Commit `feat: add maintenance approval commands`.

### Task 6: Committed-source evidence and prose claim validation

**Files:** Modify `src/repo2skill/models.py`, `src/repo2skill/validation.py`, `src/repo2skill/claims.py`, `src/repo2skill/render.py`, `src/repo2skill/dependencies.py`; create `src/repo2skill/maintenance_context.py`; test `tests/maintenance/test_prose_claims.py`, existing `tests/test_validation.py`.

**Interfaces:** Add `Claim.supportMode: Literal["typed", "evidence-backed-prose"] = "typed"`; add optional `RunLedger.applicationAuthorizationId: str | None = None`. Register `maintenance.source-fragment` v1 which emits `Fact.factType="source.fragment.v1"` from bounded captured Git blob bytes and raw-hash `EvidenceLocator(kind="line-range", value={"start": n, "end": m})`. Add frozen `MaintenanceContext(job: MaintenanceJob, snapshot: SourceSnapshot, facts: tuple[Fact, ...], evidence: tuple[Evidence, ...], priorClaims: tuple[Claim, ...], changedDiff: bytes, sourceContextHash: str)`. Produce `build_committed_context(store: LedgerStore, job: MaintenanceJob) -> MaintenanceContext`, `build_prose_claim(operation: ClaimOperation, context: MaintenanceContext) -> Claim`, `authorize_claim_revisions(job: MaintenanceJob, approval_binding_hash: str, response_hash: str, claims: Sequence[Claim]) -> ApplicationAuthorization`, `build_maintenance_run(store: LedgerStore, job: MaintenanceJob, context: MaintenanceContext, operations: tuple[ClaimOperation, ...], authorization: ApplicationAuthorization) -> MaintenanceRunBundle`, and `recheck_committed_source(store: LedgerStore, guard: CommittedSourceGuard) -> None`. Model operations initially yield pending inferred/llm claims. Run construction binds exact refs/response/job and changes only those revisions to `proposalStatus="accepted"` after full operation validation and the already-authorized job. It re-registers carried support from target commit blobs, rebuilding Fact/Evidence records with the target `snapshotId`; it never copies old-snapshot records into the new run. It retains a claim only when every support record validates, revises updates with lineage, and removes claims only for explicit delete operations referencing existing claim IDs; otherwise it re-analyzes or omits the unsupported claim with coverage. Unmatched or ambiguous refs reject the whole run. It renders all selected configured targets and returns `{run, selectedSpecs, rendered, authorization}`. Since `validate_claims()` resets inferred status, the run builder overlays accepted status only after validation and only for exact authorization refs. `stage_run` then populates `acceptedClaimRevisions`; this machine-apply path creates no `ReviewEvent`. Prose payload is `{"text": renderedText}`; refs must be same-snapshot and evidence IDs exactly equal their evidence union. Claim IDs remain canonical for semantic key; revisions/lineage track changes.

- [ ] **Red:** Add `test_legacy_claim_defaults_to_typed`, `test_source_fragment_fact_registers_captured_blob_hash_and_locator`, `test_evidence_backed_prose_requires_exact_fact_evidence_union`, `test_prose_cannot_be_observed_or_human_policy`, `test_changed_blob_creates_new_evidence_and_not_fabricated_id`, `test_retained_claim_support_is_reregistered_for_target_snapshot`, `test_unrevalidatable_retained_claim_is_reanalyzed_or_omitted`, `test_validate_claims_pending_reset_is_overlaid_only_by_matching_authorization`, `test_machine_acceptance_updates_accepted_map_without_review_event`, `test_wrong_commit_snapshot_fails_freshness`, and reopen tests for old P0 run/state fixtures. Assert `fact.factType == "source.fragment.v1"`, all carried `fact.snapshotId == target_snapshot.snapshotId`, and only an authorization's exact `claimId@revision` may yield `claim.proposalStatus == "accepted"` while `state.reviewEvents == prior_events`.
- [ ] Run `python -m pytest tests/maintenance/test_prose_claims.py tests/test_validation.py -v`; expect prose mode rejected and old fixtures to remain green.
- [ ] **Green:** Add strict validation branch only for the new support mode; register raw source fragments through `ExtractionContext.read` against captured commit bytes; preserve existing typed checks and renderer escaping/budgets. Persist `ApplicationAuthorization` before stage activation, include its ID in the immutable run, and set accepted refs through the explicit machine-apply path without fabricating user review events. Recheck exact target commit, policy, extractor versions, and context hash. Do not parse the live working tree for this context.
- [ ] Run both test files; expect legacy IDs and P0 claim status rules unchanged.
- [ ] Commit `feat: validate evidence-backed maintenance claims`.

### Task 7: Headless agent structured operations and attempt recovery

**Files:** Modify `src/repo2skill/maintenance_agent.py`, `src/repo2skill/maintenance_context.py`; create `src/repo2skill/maintenance_attempts.py`; test `tests/maintenance/test_agent.py`, `tests/fixtures/maintenance/codex/`.

**Interfaces:** Consumes Task 1's `AgentCapability` and `probe_agent`. Produces `AgentRequest(jobId: str, snapshotRoot: Path, promptBytes: bytes, schemaBytes: bytes, requestHash: str)`, `AgentResponse(status: str, outputBytes: bytes, diagnostic: str | None)`, `invoke_agent(capability: AgentCapability, request: AgentRequest, timeout_seconds: float) -> AgentResponse`, and `validate_operations(response: AgentResponse, context: MaintenanceContext) -> tuple[ClaimOperation, ...]`. `ClaimOperation` is the shared type defined above; no `AgentOperation` or `ValidatedOperations` alias is introduced. `maintenance_attempts.py` persists `prepared|invoking|responded|unknown`; a crash in `invoking` with no stored response never triggers automatic reinvocation.

- [ ] **Red:** Fake-executable tests assert stdin/schema/output framing and argv without shell; cover missing executable, unsupported help, timeout, nonzero, malformed/partial JSON, over-cap output, wrong job/snapshot, hallucinated path/ID, altered observed content, duplicate operation, mixed valid/forged operations, valid no-op, and a crash after invocation starts but before response persistence. Every invalid response leaves state and live outputs byte-identical; unknown attempts require explicit `maintain retry JOB_ID`. This pins Review Focus line 4.
- [ ] Run `python -m pytest tests/maintenance/test_agent.py -v`; expect failures because the adapter is absent.
- [ ] **Green:** Consume Task 1's tested capability contract and run the installed headless CLI from immutable snapshot context with structured schema/output. The supported command surface must match the versioned stdin/schema/output/read-only/config/cwd/timeout/output-cap behaviors; unsupported versions report the exact missing item. Persist `invoking` before subprocess start and `responded` with bounded response bytes before validation. Recovery of an `invoking` attempt without response marks it unknown and requires explicit retry, avoiding an unverified duplicate paid call. Fake tests are mandatory; real network/model smoke is separately authorized and never part of routine tests.
- [ ] Run tests with fake executable; expect all invalid outputs rejected as one transaction and valid no-op accepted without output mutation.
- [ ] Commit `feat: add bounded headless maintenance agent`.

### Task 8: Recoverable apply through P0 ownership/export

**Files:** Create `src/repo2skill/maintenance_apply.py`; modify `src/repo2skill/maintenance_worker.py`, `src/repo2skill/maintenance_store.py`, `src/repo2skill/stage.py`, `src/repo2skill/ownership.py`, `src/repo2skill/ledger.py`; test `tests/maintenance/test_apply.py`, `tests/test_ownership.py`, `tests/test_stage_transaction.py`.

**Interfaces:** Produces `ApplyJournal(jobId, status, expectedManifestSetId, expectedOutputHashes, intendedRunId, intendedStageRevision, intendedManifestSetId, intendedOutputHashes, exportId, completedAt)`, `apply_job(store: LedgerStore, job: MaintenanceJob, bundle: MaintenanceRunBundle, guard: CommittedSourceGuard) -> ApplyResult`, and `recover_apply(store: LedgerStore, job_id: str, guard: CommittedSourceGuard) -> ApplyResult`. `MaintenanceRunBundle` is built by Task 6 and supplies the immutable `RunLedger`, selected `TargetSpec`s, complete `RenderBundle`, and `ApplicationAuthorization`; `apply_job` does not reinterpret raw agent operations. Extend `stage.stage_run(bundle, run, ..., source_guard=None)` and `ownership.export_staged(..., source_guard=None)` plus `resume_export(..., source_guard=None)`. With `None`, stage/export keep current live-worktree freshness checks and resume keeps current journal/hash recovery semantics. With a guard, stage activation and export/resume call `recheck_committed_source(store, guard)` before their respective pointer/file commit. Reuse the existing ownership preflight and journal. Validate job fingerprints before invocation and again before stage/export; update `appliedBaseline` only after active manifest IDs and every target hash verify.

- [ ] **Red:** Add `test_valid_approval_applies_without_second_review`, `test_context_change_after_agent_response_invalidates_before_export`, `test_manual_owned_output_edit_conflicts`, `test_default_stage_and_export_keep_live_recheck`, `test_default_resume_keeps_journal_rules`, `test_committed_guard_is_used_by_stage_export_and_resume`, `test_crash_after_export_before_baseline_commit_recovers`, `test_third_hash_is_preserved`, `test_newer_commit_queues_behind_running_job`, and `test_failed_agent_does_not_advance_run_or_baseline`. Assert successful activation has `state.acceptedClaimRevisions[llm_claim.claimId] == llm_claim.claimRevision` while `state.reviewEvents == old_review_events`; assert moved HEAD prevents guarded stage/export/resume. This pins Review Focus lines 2, 3, and 5.
- [ ] Run `python -m pytest tests/maintenance/test_apply.py tests/test_ownership.py -v`; expect missing apply/recovery behavior.
- [ ] **Green:** Persist `ApplyJournal`, call `stage.stage_run` with the bundle's accepted run/render and committed-source guard, then call `ownership.export_staged` with the same guard; resume interrupted exports through `resume_export` with that guard. Each P0 call without a guard retains its established behavior (stage/export live `_recheck`; resume journal/hash rules). Verify exact hashes/manifests, then advance baseline in one maintenance state-pointer replacement. On crash, recover only if journal fingerprints agree; otherwise retain previous baseline and require explicit repair. Do not overwrite third-party bytes or commit to Git.
- [ ] Run both test files; expect idempotent recovery and preservation of all unrelated live bytes.
- [ ] Commit `feat: apply maintenance jobs with recovery`.

### Task 9: Catch-up, disable, migration and operator contract

**Files:** Modify `src/repo2skill/cli.py`, `src/repo2skill/maintenance_worker.py`, `docs/engineering/cli-contract.md`, `docs/schema-migrations.md`, `docs/superpowers/specs/2026-10-07-repo2skill-mvp-design.md`; test `tests/maintenance/test_recovery.py`, `tests/maintenance/test_cli.py`.

**Interfaces:** `catch-up --rebuild` explicitly establishes a fresh baseline/import job only after the user sees scope and confirms through the normal approval gate; `disable` removes owned hooks and stops future workers while preserving queue/history; `status` reports observed cursor separately from applied baseline. Document all command exit codes and state transitions. P0 documents say maintenance contracts are future additions and old `check` remains read-only.

- [ ] **Red:** Add `test_first_run_requires_explicit_bootstrap`, `test_existing_unowned_skill_requires_adoption_or_conflict`, `test_branch_switch_or_missing_object_requires_rebuild`, `test_disable_preserves_history_and_outputs`, `test_reopen_v10_state_with_maintenance_orphans`, and integration tests for post-merge/post-rewrite/post-checkout plus manual catch-up.
- [ ] Run `python -m pytest tests/maintenance/test_recovery.py tests/maintenance/test_cli.py -v`; expect missing bootstrap/rebuild behavior.
- [ ] **Green:** Implement explicit bootstrap/adoption with provenance and owned-target checks; make recovery diagnostics actionable; document hook coverage limits and no-AI default approval sequence. No silent baseline initialization from current HEAD.
- [ ] Run both test files; expect old P0 schema fixtures and the read-only `check` contract to pass.
- [ ] Commit `feat: document maintenance recovery and bootstrap`.

### Task 10: Cross-platform integration and acceptance suite

**Files:** Modify focused tests in `tests/maintenance/`, `tests/test_cli.py`, `tests/test_ownership.py`; add `docs/engineering/maintenance-acceptance.md`.

**Interfaces:** No new product interface. Acceptance record maps the state machine, CLI, hook event support, approval binding, policy grant, agent limits, apply journal, and migration behavior to named tests and operating systems.

- [ ] **Red:** Add end-to-end fake Git/agent scenario `test_commit_to_approved_apply_to_verified_baseline` and cross-platform hook/path fixtures for CRLF, path casing, symlinks/reparse ancestors, permissions, executable discovery, worker lease contention, and hidden Windows launch. Confirm no real network/model call occurs.
- [ ] Run `python -m pytest tests/maintenance tests/test_cli.py tests/test_ownership.py tests/test_validation.py -v`; expect any uncovered platform behavior to fail with an explicit reason.
- [ ] **Green:** Fix only integration gaps from the acceptance matrix; document platforms where a required isolation or hook capability makes the agent unavailable.
- [ ] Run the focused suite and the repository's standard CI matrix; expect pass on Windows, Linux, and macOS or a clearly scoped unsupported capability result.
- [ ] Commit `test: cover commit maintenance lifecycle`.

## Dependency and review order

Task 1 is the capability gate. Task 2 defines durable job records; Task 3 uses
them for commit analysis; Task 4 installs hooks that call Task 3; Task 5
exposes approval. Task 6 defines committed evidence and run materialization
consumed by Task 7. Task 8 joins the run, queue, and P0 export commit points.
Task 9 completes bootstrap/operator behavior. Task 10 covers the integrated
platform lifecycle. Each task has a focused reviewer gate and its own commit.

## Handoff

This plan is proposed and unexecuted. Review it together with the design
before implementation. All command names, records, hooks, and tests in this
plan describe future behavior until code is delivered.
