# repo2skill CLI contract

The [implementation contract](implementation-contract.md) supplies the exact JSON envelope, record types and corrected internal signatures used to implement this command contract.

## Scope and status

Status: planned contract for review; no command described here is implemented yet.

This is the command-line contract for the planned repo2skill MVP. The examples are labeled **FUTURE** because no product CLI exists yet. Commands operate on one repository root and local `.repo2skill/` state. They never execute commands found in the repository. No-AI behavior is the default and performs no network access.

The executable form is `repo2skill`. All commands accept `--root PATH` (default: current directory), `--config PATH` (default: `<root>/.repo2skill/project.json`), `--format text|json` (default: `text`), and `--quiet` (default: false). Paths printed in JSON are normalized repository-relative paths unless a field explicitly says it is absolute. `--quiet` suppresses successful human summaries, never diagnostics or JSON.

There is one authoritative project document at `<stateDir>/project.json`. `--config` selects that document; an explicit path may be absolute or root-relative, but must resolve canonically inside `--root`, and the decoded document's `stateDir` must identify the document's parent. On `init`, omitting `--config` uses `<root>/.repo2skill/project.json`, except that a custom `--state-dir` selects `<root>/<state-dir>/project.json`. If `--config` is explicit on `init`, it must resolve to `<stateDir>/project.json`; a mismatch is an invocation/configuration error before writes. For a custom state directory, pass its project document on later invocations, for example `repo2skill --config .cache/repo2skill/project.json scan`. No duplicate project documents or config synchronization are supported.

Every mutating invocation acquires the project's OS-owned exclusive state lock before reading authoritative mutable state and holds it through its pointer commit or journal update. Lock contention fails immediately with exit 1 and performs no write. Scan/generate activation, review, export, and `export --resume` use the same lock, so concurrent writers cannot lose a state update.

Global `--version` and `--help` exit 0. Unknown commands/options, mutually exclusive options, missing operands, invalid enum values, and unreadable explicit config paths are invocation errors.

## Commands

### `init`

```text
repo2skill init [--git-mode auto|head|worktree|no-git]
                [--target agents-md] [--target skill]
                [--state-dir PATH]
```

Creates the state directory and its authoritative `project.json`; it does not scan or create live outputs. Defaults are `--git-mode auto`, both targets, and `<root>/.repo2skill`. A state directory and output target must resolve inside the repository root. Re-running with an identical compatible project returns 0. Existing incompatible state, a project UUID mismatch, or nonempty unrecognized state returns 1 and writes nothing. With `--state-dir .cache/repo2skill`, initialize with `repo2skill init --state-dir .cache/repo2skill`, then use `--config .cache/repo2skill/project.json` on later commands. An explicit `--config` on init must resolve to that same state directory's `project.json`.

`auto` records current worktree content. `head` requires a clean Git worktree. `worktree` requires Git but permits dirty/untracked inputs. `no-git` forbids using Git identity even if present. Outside Git, `auto` selects no-Git mode.

### `scan`

```text
repo2skill scan [--git-mode auto|head|worktree|no-git]
                [--include PATH_OR_GLOB ...] [--exclude PATH_OR_GLOB ...]
                [--strict]
```

Collects inventory, repository identity, extraction reads, evidence, facts, diagnostics, and coverage into a new immutable run. It does not derive claims, render, review, or export. `--include` and `--exclude` are repeatable. An explicit exclude always wins. An exact include may opt into a normal Git-ignored file, but never into `.repo2skill/`, a configured live output, a symlink target, filesystem boundary, or file rejected by the generated/security policy. `--strict` turns every coverage gap into exit 2. Without `--strict`, non-required skipped/unsupported inputs are reported but a usable scan exits 0. Required input failure or exhausted global caps exit 2 in all modes. A failed scan does not change the current good run.

### `generate`

```text
repo2skill generate [--no-ai | --ai codex]
                    [--codex-config NAME] [--yes-ai]
                    [--target agents-md] [--target skill]
                    [--from-run RUN_ID] [--strict] [--measure]
                    [--git-mode auto|head|worktree|no-git]
```

Runs scan unless `--from-run` names a compatible completed scan, derives and validates claims, deduplicates against documentation, and renders selected targets under `.repo2skill/staging/<runId>/`. It never changes live output. Both targets are selected by default. `--git-mode` has the same default and semantics as `scan` and is invalid with `--from-run`. Before reuse and again before activating a newly staged run, generation checks raw-byte file hashes, glob match sets, negative predicates, HEAD/worktree identity, policy revision, and extractor versions. Any mismatch exits 4; generation never proceeds from or activates a stale run. Evidence hashing and parsing use the same captured bytes. This check does not claim an atomic repository-wide snapshot.

No-AI is the default. `--no-ai` is accepted as an explicit assertion and cannot be combined with any Codex option. `--ai codex` is a P1 explicit opt-in. It requires a successful installed-capability probe, prints the fact/claim counts, payload bytes, estimated input/context, and estimated output before invocation, and requires interactive confirmation unless `--yes-ai` is supplied. `--yes-ai` consents only to the displayed EvidencePack for this invocation; it is invalid without `--ai codex`. `--codex-config NAME` selects a pre-existing official Codex CLI configuration. There is no `--model`, provider, credential, arbitrary argument, or shell-command option.

P0 Task 11 implements the P1 option grammar without a provider: valid explicit `generate --ai codex` and `doctor --codex` requests return an unavailable-capability result with exit 6 before mutation. Invalid Codex option combinations remain invocation errors with exit 1. The P1 capability and transport behavior described above remains future behavior; this P0 unavailable result does not claim that a capability probe occurred.

`--measure` is a P1 pilot opt-in and appends one local JSONL record to `.repo2skill/pilot-measurements.jsonl`. The allowlist is schema/tool version, OS family, Git mode, AI/no-AI mode, target kinds, inventory/fact/claim/diagnostic/coverage counts, stage byte counts, phase durations, final status, and exit code. It contains no repository/source path, project or claim ID, branch/commit, URL, command text, fact value, evidence snippet, error text, or secret. It never transmits the file. Without this flag no pilot measurement is written.

P0 Task 11 recognizes `--measure` but returns an unavailable-invocation error with exit 1 before mutation; it writes no measurement. The local P1 pilot behavior above remains future behavior.

Successful staging with only accepted claims exits 0. A valid stage containing pending proposals exits 3. Coverage failure exits 2. Codex unavailable, declined, timed out, nonzero, malformed, or schema-invalid exits 6 and does not fall back silently; the user can rerun explicitly with `--no-ai`. JSON output includes `runId`, `snapshotId`, stage paths and hashes, counts by claim status/kind/origin, diagnostics, dedup counts, coverage, and `nextActions`.

### `review`

```text
repo2skill review list [--run RUN_ID] [--status pending|accepted|rejected|contested]
repo2skill review show --claim CLAIM_ID [--run RUN_ID]
repo2skill review accept (--claim CLAIM_ID ... | --all-observed) [--run RUN_ID]
repo2skill review reject --claim CLAIM_ID ... --reason TEXT [--run RUN_ID]
repo2skill review contest --claim CLAIM_ID ... --reason TEXT [--run RUN_ID]
```

`list` and `show` are read-only. `show` displays the proposed text/structured payload, kind, origin, revision, facts, evidence locators/hashes, rule, dedup result, and coverage warnings.

`accept`, `reject`, and `contest` validate all transitions, render and hash a new immutable stage revision, then atomically replace `state.json` once to append review events and activate that revision. A render failure leaves review state unchanged. `--claim` is repeatable. Every requested claim must exist at the selected revision and the entire operation writes nothing if one transition is invalid. Accept and reject require pending status; contest accepts pending or accepted status when support is disputed. A contested live claim remains in the current live bytes until a later explicit export removes its owned span. `--all-observed` is limited to pending claims with `kind=observed`, `origin=deterministic`, and complete validation; it cannot be combined with `--claim`. There is no unrestricted `--all`. Reject and contest reasons are stored with the revision. Acceptance changes status only and never upgrades kind or origin. P0 reserves `human_policy` but provides no command that creates one.

After the mutation, exit 3 if proposals remain pending and 0 otherwise. Invalid state transition or claim ID exits 1. State corruption exits 7.

### `check`

```text
repo2skill check [--run RUN_ID] [--target agents-md|skill] [--strict]
```

Read-only. Re-inventories only selectors required by the run's file, glob, and negative dependencies and compares live managed blocks with manifests. It does not derive claims, render, invoke Codex, repair, persist stale status, or access the network. Results are effective `fresh`, `pending`, `stale`, `conflict`, plus coverage gaps with reason codes for this invocation. `--strict` makes any coverage gap exit 2.

Exit 5 if any selected target has an ownership conflict; otherwise 2 for required/strict coverage failure, 4 for any stale dependency, 3 for pending review, or 0 when fresh. JSON lists affected claims and the exact dependency change: added glob match, invalidated negative predicate, changed hash, deleted path, extractor/policy revision, or block hash mismatch.

### `explain`

```text
repo2skill explain CLAIM_ID [--revision N] [--run RUN_ID]
```

Read-only. Resolves a claim through its structured payload, status, origin/kind, facts, evidence paths/locators/hashes, snapshot, rule versions, dedup decision, staged targets, and live block/span where applicable. Suppressed, rejected, stale, and superseded claims remain explainable. A missing claim/revision is exit 1; corrupt or broken lineage is exit 7. It does not read raw source merely to enrich the response, so its answer describes the recorded snapshot.

### `report`

```text
repo2skill report [--run RUN_ID]
                  [--section summary|coverage|claims|dependencies|dedup|outputs]
```

Read-only. Defaults to all sections. The report distinguishes facts from claims, observed from inferred, deterministic from LLM/human origin, supported from unsupported ecosystems, and skipped/capped/failed sources. It reports candidate and suppression counts without treating suppression as deletion. Exit is 0 for a readable report, 2 if the requested run is incomplete due to coverage, and 7 for corrupt state.

### `export`

```text
repo2skill export [--run RUN_ID | --resume EXPORT_ID]
                  [--target agents-md] [--target skill]
                  [--delete-empty-owned-blocks]
```

Exports accepted claims from a completed staged run. Pending inferred proposals do not block export of accepted claims; they remain pending and absent from live output. If a run has no accepted claims and only pending proposals, the command exits 3 without writing. Both configured targets are selected by default. The command preflights every selected target, verifies the staged hash and all prior ownership hashes, prepares same-directory temporary files, and then replaces files one by one under an export journal. A preflight conflict prevents all changes and produces a proposed patch in staging. When only one target is selected, the new active manifest set carries forward immutable references to every unselected target's prior manifest; subset export cannot discard its ownership record.

The command preserves bytes outside owned markers. It may remove only claim spans proven by the previous manifest. `--delete-empty-owned-blocks` removes empty owned markers; it never deletes a surrounding hand-written file. A whole generated file or Skill companion may be deleted only when the prior manifest records whole-file ownership, its exact current hash still matches, and the staged proposal explicitly removes it. Explicit export may create missing Agent Skill directories only beneath the repository root, only through non-symlink/non-reparse ancestors, and records each created directory as owned. Earlier commands never create these live directories.

Each individual file replacement is atomic, but a multi-file target set is not globally atomic. The journal is the recovery record and manifests/state are the commit point after every file reaches its intended hash. An interruption may leave a recoverable incomplete export and some new live files. Every non-committed export journal blocks other mutating commands with exit 7 until explicit `export --resume EXPORT_ID` resolves it. Resume accepts a current file only when its hash is the recorded old or intended new hash; any third hash is a conflict and is preserved. `--resume` cannot be combined with run, target, or deletion options.

Immediately before replacement, export rechecks the complete used read set and repository/policy/extractor identity. A concurrent file, glob, or negative-predicate change exits 4 before replacement. A preflight ownership conflict exits 5 without replacement. A completed export with accepted claims exits 0 and prints target paths, new exact hashes, manifest IDs, exported claim revisions, and remaining pending count. Recovery conflict exits 5; an unrecoverable journal or corrupt state exits 7.

### `doctor`

```text
repo2skill doctor [--codex]
```

Read-only. Without `--codex`, checks Python/platform support, state/output path confinement, configured filesystem permissions, schema readability, Git availability, and interrupted transaction journals. Actual atomic-replace behavior is verified by platform tests and by the write path when used; doctor does not probe it by writing. It performs no network call. `--codex` additionally locates the installed official executable and checks its local version/help for the required P1 transport surface. This does not validate authentication, network reachability, an actual structured model response, or effective disabling of tools/repository access/instruction autoload; those are verified only by the P1 capability spike and an explicitly consented `generate --ai codex` transport probe. Doctor does not log in, inspect credentials, execute config commands, or invoke a model. Missing or locally unsupported Codex capability exits 6 when `--codex` was requested.

## Output streams and exit codes

Text mode writes the primary result to stdout and diagnostics to stderr. JSON mode writes exactly one UTF-8 JSON object plus newline to stdout; stderr remains for fatal pre-JSON failures and concise operator diagnostics. Progress is disabled in JSON mode. Secret values and unredacted snippets never appear in either stream.

| Code | Meaning | Typical commands |
|---:|---|---|
| 0 | Requested operation completed; informational coverage gaps may still be reported | all |
| 1 | Invocation, configuration, lookup, invalid state transition, or state-lock busy | all |
| 2 | Required input/coverage failure, or any gap under `--strict` | scan, generate, check, report |
| 3 | Valid pending review prevents completion/export | generate, review, check, export |
| 4 | Source or dependency state is stale, including a concurrent input change | scan, generate, check, export |
| 5 | Managed ownership/manual-edit conflict | check, export |
| 6 | Explicit Codex capability, consent, transport, or response failure | generate, doctor |
| 7 | Ledger corruption, invariant failure, recovery failure, or unexpected internal error | all |

Argument parsing fails immediately with 1. When a command aggregates independent findings, it returns the first applicable code in this priority: `7 > 5 > 6 > 2 > 4 > 3 > 1 > 0`. This priority is part of the machine interface; JSON still contains every finding. Codes 2 and 3 are reserved for required/strict coverage and pending review; informational coverage gaps do not change code 0. Export may be interrupted between per-file atomic replacements. In that case its journal reports `incomplete`, manifests remain at the prior commit point, and only explicit `export --resume` may continue; every non-committed journal, including a conflict, blocks other mutation. Recovery never overwrites a third, manually edited hash.

## Managed markers and paths

The exact marker grammar is renderer-owned and versioned: `<!-- repo2skill:begin project=<projectId> block=<blockId> -->` and `<!-- repo2skill:end project=<projectId> block=<blockId> -->`. The block ID binds the project, target, and fixed section key `repository-guide`; target identity is therefore verified through the expected block ID and target manifest. Marker parsing requires one balanced pair per expected block. Nested, duplicate, mismatched, or project-foreign markers conflict. The manifest stores the exact marker-inclusive block bytes, their SHA-256, claim-to-line spans, renderer version, EOL mode, and whether repo2skill owns the whole file.

Default live targets are `<root>/AGENTS.md` and `<root>/.agents/skills/<project-slug>/`. `--target` selects configured logical targets; it never accepts an arbitrary destination. Paths are resolved before access and must remain under the configured output root. Symlinked target components conflict.

## FUTURE examples

The commands below specify intended flow and are not runnable today.

```powershell
# FUTURE: initialize, generate entirely locally, inspect pending proposals
repo2skill init --git-mode auto
repo2skill generate --no-ai
repo2skill review list --status pending
repo2skill review show --claim claim.command.test.root.ab12cd34
```

```powershell
# FUTURE: accept named claims, use constrained bulk acceptance for observed claims,
# then export only after check succeeds
repo2skill review accept --claim claim.convention.tests.root.12ab34cd
repo2skill review accept --all-observed
repo2skill check --format json
repo2skill export --target agents-md --target skill
```

```powershell
# FUTURE P1: explicit Codex use with a pre-existing Codex configuration
repo2skill doctor --codex
repo2skill generate --ai codex --codex-config repo-docs
```

```powershell
# FUTURE: trace a live or suppressed claim without rescanning
repo2skill explain claim.command.test.root.ab12cd34 --format json
repo2skill report --section coverage
```

Scripts should consume `--format json`, the documented fields and exit codes, not prose or staging filenames inferred from terminal text. Compatibility follows schema major versions; fields may be added within a major version, but their meaning and exit precedence do not change.
