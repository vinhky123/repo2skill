# Commit-triggered skill maintenance design

**Status:** Implemented alpha. This document records the approved design; tested behavior and known limits are tracked in [maintenance acceptance](../../engineering/maintenance-acceptance.md). **Date:** 2026-10-09 (Asia/Saigon)

## Decision and user-visible sequence

repo2skill remains the product name. This design adds local maintenance of repo2skill-owned `AGENTS.md` and Agent
Skill outputs after repository changes. The baseline is the commit whose skill output was successfully applied and
verified, never the newest commit merely observed by a hook.

The normal sequence is: a supported Git hook observes a completed repository event; repo2skill reads the committed tree,
compares it with the applied baseline, and queues a relevant change; the local CLI reports the pending job; the user
approves that exact job; repo2skill then calls the installed headless agent, validates its structured operations, and
applies the accepted result on success. That approval authorizes both the AI call and its bounded update. It does not
produce a second draft-review approval step. A versioned, revocable explicit auto-approval setting may skip the approval
before AI. Time passing never grants approval.

The installed local hook performs bounded deterministic Git diff/impact analysis in the foreground, commits the
resulting job (or recoverable event) to the local queue, and prints the job ID, affected skill targets, and exact
`repo2skill maintain approve JOB_ID` command to its invoking terminal. It does not run AI, block for approval, or
display a prompt. If diff analysis exceeds its budget, it durably records the event and prints that analysis is queued
with `maintain catch-up` and `maintain status` commands; it never reports an unfinished diff as complete. GUI Git
clients that hide hook output still leave durable status available to `maintain status`; OS notifications are outside
this MVP. A pending notification record is committed with each job; repeat delivery uses the same stable job ID and is
idempotent. `check` remains read-only. Initial creation or adoption of a skill needs an explicit bootstrap job and the
same approval policy. Existing unowned `SKILL.md` or `repo2skill-provenance.json` at configured Skill output paths are
reported as conflicts before the agent is probed or invoked; their bytes and unrelated directory files are preserved.
Manually edited owned files are also reported as conflicts. No existing file is silently assumed to be repo2skill-owned.

## Scope and non-goals

The first supported surface is local Git hooks plus a durable local worker and CLI commands `maintain setup`, `maintain
status`, `maintain approve JOB_ID`, `maintain skip JOB_ID`, `maintain retry JOB_ID`, `maintain catch-up`, and `maintain
disable`. These commands are implemented locally; the current grammar, diagnostics, limits, and operating sequence are
maintained in the [CLI contract](../../engineering/cli-contract.md#maintain). Approving a job starts that exact job's AI
call and apply flow directly.
`retry` is only for an explicitly retried failed/unknown attempt and never means approval of a different job. Setup
installs only into an ordinary repository-local `.git/hooks` directory when this is not a linked worktree and each
target hook is absent or already repo2skill-owned. If `core.hooksPath` is configured, the directory is shared, the
repository uses a linked-worktree common Git directory, or a foreign hook already exists, setup refuses that hook and
prints manual integration instructions. It never edits, wraps, or replaces a foreign executable or changes
`core.hooksPath`; disable removes only its own wrapper. A background worker recovers queued jobs after restart.
Notification is the hook's terminal summary plus durable CLI status; there is no remote messaging or hosted service.

The worker and installer must behave on Windows, Linux, and macOS. Use Python-standard-library process and file
operations with platform-specific hidden-window handling on Windows. Detect Git's effective hooks directory and refuse
configured `core.hooksPath` or a shared directory. Support post-commit, post-merge, post-rewrite, and post-checkout
observations where available. Git documents post-commit as occurring after a completed commit, post-merge after
successful merge/pull, post-rewrite for amend/rebase, and post-checkout for checkout/switch/worktree events.
Post-checkout exit status can affect the invoking command, so its wrapper must always return success after recording a
diagnostic; post-commit itself cannot change commit status. Manual `catch-up` repairs missed events. Do not claim hooks
observe every remote or filesystem update.

When analysis completes, hook stdout prints either a concise no-impact summary or `repo2skill: maintenance job <JOB_ID>
affects <TARGETS> from <BASELINE> to <TARGET>; approve with: repo2skill maintain approve <JOB_ID>`. For cap/time failure
it prints `analysis queued; run repo2skill maintain catch-up` plus `repo2skill maintain status`. The job/outbox state is
committed before stdout. After a successful write, mark notification reported in a second state-pointer replacement; a
crash between print and mark may repeat the same job ID, which is safe. A GUI client that hides stdout still has durable
status, with no OS or remote notification guarantee.

The CLI uses the P0 JSON envelope `{command,result,findings,diagnostics,nextActions}`. `maintain status` reports
observed cursor, applied baseline, approval policy, and each job's ID, target commit, impact scopes, lifecycle status,
and approval source. Its exit priority is 7 for corrupt state, 5 for ownership conflict, 6 for agent failure, 4 for
stale/rebuild-required work, 3 for pending user approval, else 0. `approve` and `retry` run in the foreground; they exit
0 only after apply/no-op, 4 for stale binding, 5 for ownership conflict, 6 for agent failure/unavailability, and 7 for
corrupt state/recovery. Invalid commands or IDs exit 1. `catch-up` exits 3 for pending approval, 2 for required coverage
or caps, 4 when the baseline is unprovable, and 0 when no relevant work remains.

Out of scope: UI automation, synthesizing `/init` keystrokes, manually activating an MCP or workflow skill,
browser/desktop control, full semantic call graphs, graph databases, vector stores, other providers, telemetry,
automatic Git commits/pushes, and arbitrary shell commands.

## Commit snapshots, impact, and queue semantics

Git input is the committed tree, never the dirty working tree. `git_read` is the bounded subprocess seam. Diff endpoints
are object IDs; pass `--no-ext-diff --no-textconv`, use NUL-delimited paths, and read blob bytes from the target commit
tree. Do not execute repository code, tests, hooks, filters, or build commands while scanning. Apply existing path,
byte, decoded input, redaction, generated-file, and symlink limits. User edits outside a committed snapshot do not
poison its analysis. Edits to a currently owned output still cause the ownership check to conflict before apply.

For a linear series, analyze the aggregate applied-baseline-to-target diff; for merges, compare the tree at the verified
applied baseline with the merge result tree. Keep endpoints and changed paths in the job. Renames are represented as old
and new paths. Added, removed, renamed, glob-match, negative-predicate, workflow, task/configuration, and
control/instruction file changes are scoped conservatively by the dependency graph and selector policy. Unknown or
ambiguous changes queue a full bounded reanalysis. No diff or dependency impact means no model call. Changes limited to
repo2skill state or outputs do not self-trigger. If source and generated output both changed, process the source change
and let output ownership checks decide whether application is safe.

Maintain separate durable cursors:

* `observedEventCursor` advances when a hook event is durably queued or recorded as deferred.
* `appliedBaseline` advances only after output hashes and P0 manifests verify the target job's successful apply.

Pending jobs are coalesced to the latest reachable relevant target against the applied baseline. Coalescing retains the
full endpoint diff and unions impacts; it never advances the baseline. A user approval is bound to immutable job ID,
baseline and target commit, diff hash, source-context hash, policy revision, selected targets, and expected
owned-output/manifest fingerprints. A change to any binding before invocation or apply invalidates approval and requires
a new job. While one job runs, later commits remain queued and cannot authorize stale writes. Missing baseline objects,
branch changes, rebases with no provable baseline, shallow-history gaps, or unavailable targets fail with a specific
diagnostic and require explicit `catch-up --rebuild` or bootstrap; never guess that HEAD is applied.

Skip/decline records the exact target and avoids repeating the same notification for that event. It does not mark claims
or outputs fresh, and a later target commit may notify again. An auto-approval grant is versioned, scoped to target and
target kinds, visible in `status`, and revoked by `disable` or policy change. Grant changes do not retroactively
authorize already queued jobs.

Git diffs use Git's documented machine-readable framing. Invalid byte names, unsupported tree entries, cap exhaustion,
or corrupt object reads fail closed and preserve the pending job for explicit recovery. Snapshot construction has no
repository-wide atomicity claim.

## Durable jobs and crash recovery

P0 `ProjectState` has one current run and stage pointer; it is not an event or job store. Add a separate versioned
authority below `.repo2skill/maintenance/`:

```text
maintenance/state.json              sole mutable authority, queue pointer and deferred events
maintenance/jobs/<jobId>.json       immutable target/diff/context/policy bindings
maintenance/apply/<jobId>.json      cross-store apply/recovery journal
maintenance/snapshots/<jobId>/      bounded immutable Git-tree source context
maintenance/attempts/<attemptId>.json immutable invocation and response records
```

`MaintenanceJob` contains only immutable identity bindings: job ID, baseline and target commits, event IDs,
paths/scopes, diff/context hashes, policy revision, target kinds, and expected manifest/output fingerprints. It contains
no approval, status, lease, or attempt fields. A `MaintenanceLifecycle` value embedded in `MaintenanceState`, keyed by
job ID, holds status, approval source/binding, notification/skip state, active attempt ID, and failure code.
`MaintenanceState.deferredEvents` retains bounded events whose diff analysis could not complete, with a reason code. The
queue is derived from lifecycle values and deferred events. Each attempt is an immutable record; its current phase is in
the state pointer. `observedEventCursor`, `appliedBaseline`, policy/grant revision, queue membership, and all lifecycle
transitions are committed by one atomic replacement of `maintenance/state.json` under `LedgerStore.mutation()`.
Immutable job/snapshot/attempt artifacts are written before that pointer. A crash before pointer replacement leaves an
ignored orphan; a hook retry is idempotent by stable event ID. There is no separately replaced queue file, so the cursor
cannot advance without its queued job pointer. Tests inject crashes between every immutable write and pointer
replacement.

Apply spans two existing authorities and is not globally atomic. Prepare and persist an `ApplyJournal` with expected old
manifest/output fingerprints and intended validated target hashes; publish an immutable P0 run/stage; call the existing
ownership/export path and let its per-file journal protect live replacement; then verify active manifest set and exact
live hashes before committing `appliedBaseline` and completing the maintenance job. If a crash occurs after P0 export
commit but before maintenance state commit, recovery rechecks the journal's output hashes and manifest IDs and finishes
the baseline advance. If those checks fail, it leaves the prior baseline and job recoverable. It never rolls back by
overwriting a third-party edit. No later job writes until the incomplete apply journal is resolved.

Worker lifecycle must avoid duplicate runners by using an OS-owned lock/lease, recover stale leases after process death,
and bound subprocess/runtime. Hook work performs only capped deterministic diff/impact and durable queue publication.
Windows background helpers launch hidden for explicitly auto-approved jobs; all platforms offer a foreground `maintain
catch-up` fallback. Disable stops future observation and worker starts but retains job history and never changes
outputs. Each agent attempt records `prepared`, `invoking`, `responded`, or `unknown`. If recovery finds `invoking`
without a persisted response, it cannot know whether a paid provider call completed; mark the attempt unknown and
require an explicit retry. Never blindly invoke again during automatic recovery.

## Evidence, agent, and claim contracts

The current `Claim` supports `renderedText` or `templateKey`, but P0 validation checks deterministic fact types, payload
equality, and template output. Arbitrary LLM prose is not valid under current validation and must not be smuggled
through `renderedText`. Extend the schema compatibly with an explicit `supportMode` defaulting to `typed`; only
`supportMode="evidence-backed-prose"` may accept freeform text. Such a claim must have `origin="llm"`,
`kind="inferred"`, pending status, `payload={"text": renderedText}`, and at least one existing same-snapshot `factId`;
its sorted `evidenceIds` must equal the union of those facts' evidence IDs. It cannot be accepted as observed, create
`human_policy`, change an existing claim's kind/origin, or invent facts, evidence, paths, or IDs. Validation proves
lineage and shape, not semantic truth.

The runner receives immutable bounded source-context files and facts from the target Git tree. P0's selected
manifest/config/documentation facts cannot support arbitrary semantic code guidance, so maintenance registers a new
deterministic extractor `maintenance.source-fragment` version `1` with fact type `source.fragment.v1`. It reads captured
blob bytes under existing per-file and total snapshot caps, decodes supported UTF-8 source, emits bounded line-range
`Evidence` with raw blob SHA-256 and an available line-range locator, then emits `Fact` values containing normalized
path, fragment locator, bounded text, and language/media type. These facts document source; they do not assert runtime
behavior. Bootstrap enumerates eligible source files within caps; later jobs include changed files plus prior matched
dependencies and bounded related source fragments. Every included fragment is registered before the agent call. The
context manifest lists each included path/hash and cap omission; it never implies whole-repository understanding when
paths were omitted. Source text is untrusted data. The model returns structured operations: add/update/delete claim with
exact existing fact/evidence refs, target scope, and bounded prose; output files are written only by repo2skill after
complete validation. The maintenance run builder re-registers unchanged support against the target snapshot: it re-reads
each carried dependency from target commit blobs, recreates evidence and facts with the target `snapshotId`, and rebinds
claim references. It never copies old-snapshot Fact/Evidence records into a new run. A claim is carried forward only
when every supporting record can be revalidated; otherwise it is re-analyzed or omitted with a coverage diagnostic. P0
`validate_claims()` resets inferred proposals to pending, so the machine-apply path explicitly overlays accepted status
only after validation and only for refs in the persisted `ApplicationAuthorization`. A no-op response is valid.
Unaffected claims are retained after support re-registration; deletion can remove only claims with repo2skill-owned
provenance, then existing manifests prove the exact live span eligible for removal. Any unknown reference, path, target,
snapshot, unsupported operation, mixed valid/invalid response, stale fingerprint, truncation, timeout, malformed JSON,
or cap error rejects the entire response and leaves current run, state, and outputs unchanged.

Use an adapter boundary whose first implementation targets the installed official headless Codex CLI. The local verified
CLI version is `0.162.0-alpha.2`; local help confirms `exec`, stdin prompt `-`, `--output-schema`,
`--output-last-message`/`-o`, `--sandbox read-only`, `--cd`, `--skip-git-repo-check`, `--ignore-user-config`, and
`--ephemeral`. This proves only local command syntax. Official non-interactive documentation says `exec` uses saved CLI
authentication; neither help nor this document proves authentication, network/model availability, effective isolation,
or a real response. Do not make a paid/network call in setup or hook processing. Implementation begins with
fake-executable capability and failure tests; a real smoke test requires explicit later authorization.

Do not hardcode a model, credentials, arbitrary CLI arguments, or shell commands. The minimum versioned capability
contract is: local help confirms stdin prompt, output schema and output-file support; invocation explicitly selects
read-only sandbox behavior, ignores user config for agent settings, uses a prepared temp cwd outside the source
repository, and applies bounded time/output; the temp cwd has no repository `AGENTS.md` or source-owned instructions. A
bounded capability spike verifies argument framing, cwd, output cap, and no-write behavior with a fake executable before
integration proceeds. If installed help or the spike cannot establish these exact behaviors, mark that CLI version
unsupported and state the missing capability. This does not claim to isolate secrets or prove all CLI
instruction-loading behavior: the installed CLI and the user's saved authentication remain trusted local components. Do
not bypass sandbox or approval controls. Auth still uses configured `CODEX_HOME` and is neither inspected nor copied. A
real call is optional later evidence, never part of setup, hook processing, or this plan.

The user's approval (or active opt-in policy grant) authorizes one immutable job's model call and bounded machine
application. After structured validation, repo2skill writes an immutable `ApplicationAuthorization` binding the job and
approval, agent response hash, exact accepted claim revisions, target kinds, and timestamp. The maintenance run records
that authorization ID. A scoped `machine-apply` transition writes those exact revisions into
`ProjectState.acceptedClaimRevisions` in the same state-pointer commit that activates the run/stage; it creates no
`ReviewEvent` and does not claim a user reviewed generated prose. Validation requires authorization for every accepted
`origin="llm"` revision; export requires the run authorization and active state to agree. The existing ownership export
then applies those accepted spans. There is no second draft approval. New generated or changed claims retain
`kind="inferred"` and `origin="llm"`; job approval never upgrades either. Keep normal P0 human review for ordinary
deterministic proposals.

## P0 integration, migration, and compatibility

Reuse `git_read.run_git_bounded`, dependency checking/rechecking, `claims.derive_claims` and `claims.review_claims`,
`stage.stage_run` and `stage.publish_stage_revision`, `ownership.export_staged`, `checks.explain_claim`, and
`LedgerStore.mutation()` / `replace_state()`. Introduce a committed-tree source reader rather than using live inventory
for job analysis. Add a trusted `CommittedSourceGuard` and default-preserving optional guard parameters to
`stage.stage_run`, `ownership.export_staged`, and `ownership.resume_export`. If omitted, stage/export keep their current
live-worktree `_recheck` and resume keeps its current journal/hash recovery semantics. If provided by maintenance, each
seam invokes the committed-source guard at its existing freshness boundary before its state pointer or file replacement.
The guard verifies current HEAD equals the job target, re-reads target blobs and source-context hashes from Git, and
rechecks policy, extractor revisions, and authorization bindings, then allows unchanged output ownership preflight. It
ignores unrelated dirty source but rejects a moved HEAD or changed committed binding. Do not globally bypass freshness
checks. Test that default P0 stage/export still reject changed worktree dependencies, default resume retains its journal
rules, and guarded maintenance stage/export/resume reject a moved HEAD. An unowned existing output needs explicit
adoption/bootstrap with recorded provenance analysis; otherwise preserve it and report conflict.

Keep P0 `schemaVersion` 1.0 readable. Add optional fields with backward-safe defaults or place all new documents in
maintenance paths; do not rewrite old runs. Claim `supportMode` is optional and defaults to `typed`. Any schema major
change requires a separately documented migration; no hand-editing. Reopening an old project and validating old P0
fixtures must retain identical claim IDs, kind/origin behavior, review events, manifests, and export recovery.

## Verification and acceptance

Use fake Git repositories, fake executable runners, controlled clock/lease fixtures, subprocess failure injection, and
platform path/hook fixtures. Test that: approval precedes every model call by default; opt-in auto mode is versioned;
one approval directly applies valid results; no-impact diffs skip AI; applied baseline moves only after verified export;
skipped jobs remain stale; subsequent commits queue while a job is running; failures before apply preserve prior P0
pointers and bytes, while interrupted replacement remains visible in recovery journals; Git source is read from target
commit blobs; dirty unrelated files do not change committed analysis; manual output edits conflict; and recovery
resolves both the Git queue and P0 export journal safely. Run cross-platform CI after implementation. No real agent call
is required for unit acceptance; the separately authorized smoke result is reported as capability evidence, not product
correctness.

The P0 contracts remain authoritative for existing commands. This design supersedes prior P1
facts-only/no-agent-access limitations only for the `maintain` path described here. Task 10 remains the final local
acceptance and packaging gate; see the CLI contract for current implementation limits and status.

## References

- [Codex CLI non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode): saved CLI authentication and
  non-interactive execution.
- [Codex CLI command-line reference](https://learn.chatgpt.com/docs/cli/reference): invocation surface; use local help
  as the installed-version contract.
- [Git hooks](https://git-scm.com/docs/githooks): hook timing and arguments.
- [Git diff](https://git-scm.com/docs/git-diff): diff endpoints and path output behavior.
