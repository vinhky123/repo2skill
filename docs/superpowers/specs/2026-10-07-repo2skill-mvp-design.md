# repo2skill MVP design

## Status and design brief

Status: planned contract for review; no implementation is asserted by this document.

This document defines the architecture for the first repo2skill pilot. The product is a local-first repository knowledge generator for maintainers and senior developers who need concise agent instructions but require every generated factual statement to remain traceable to a source. Success means a maintainer can generate useful `AGENTS.md` and Agent Skill content without AI, review it, export it without losing hand-written text, detect when its inputs have changed, and explain any live claim back to a hashed source locator.

The implementation baseline is Python 3.11 or newer. It selectively ports useful MIT-licensed rules and managed-section behavior from `osmaneb23/agents-md` at commit `346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`. It is neither a wholesale fork nor a black-box invocation of that CLI. Reused files or translated rules must record their upstream path and pinned commit, preserve the MIT notice, and be covered by local fixtures.

Feature identifiers and phase membership come from the product catalog: P0 is F001-F035, P1 is F036-F041, P2 is F042-F045, and F046-F052 are excluded. This design groups those capabilities by subsystem rather than duplicating the catalog.

## Goals and boundaries

P0 delivers the complete no-AI product loop: initialize local metadata, scan a repository, emit typed evidence and facts, derive deterministic candidates, retain proof-preserving dedup decisions, stage both output targets, review candidates, export owned blocks, check freshness, explain claims, and report coverage. JavaScript/TypeScript, Python, Go, and Rust receive shallow ecosystem collectors from day one. Other languages still receive generic inventory, configuration, CI/workflow, task-file, Docker, and documentation collection. Coverage output must distinguish supported, unsupported, skipped, capped, generated, invalid, and failed inputs.

P1 adds an optional adapter for an already-installed official Codex CLI. It may propose wording and inferred claims from a bounded, redacted EvidencePack. It does not manage credentials, choose a public model alias, use provider SDKs, or weaken validation. P2 is demand-gated work: nested scopes, native JVM extraction, deeper artifact-specific extraction, and additional output targets.

The MVP does not execute repository commands, access the network during no-AI generation or `check`, infer universal policy, build a semantic truth engine, or use a graph/vector/database service. It does not auto-commit, open pull requests, run hooks or daemons, or accept normative claims automatically. Deep AST, type, and call-graph analysis are excluded; only bounded artifact extractors such as a native manifest parser are demand-gated P2 work. A claim records support and lineage; it does not assert that an inference is semantically true for every runtime situation.

## Architecture and file map

The package stays intentionally small. Interfaces are functions and a few frozen dataclasses rather than a framework of abstract classes.

```text
src/repo2skill/
  __init__.py                 package version and schema compatibility
  __main__.py                 python -m entry point
  cli.py                      argparse tree and all command dispatch
  config.py                   versioned project config and policy defaults
  models.py                   enums and frozen records
  ids.py                      stable IDs, revisions, canonical serialization
  identity.py                 normalized paths, Git/no-Git and snapshots
  inventory.py                ignore-aware bounded file discovery
  ledger.py                   JSON validation, reads, indexes and transactions
  claims.py                   deterministic claim derivation and lifecycle
  validation.py               cross-record and support validation
  dedup.py                    proof-preserving suppression decisions
  redact.py                   safe snippet construction and secret redaction
  render.py                   AGENTS.md, Skill, manifests and staging
  ownership.py                marker hashes, export journal and recovery
  extractors/
    base.py registry.py javascript.py python.py go.py rust.py
    generic.py docs.py
  templates/                  renderer text templates, no executable code
  codex/                       P1 only
    capability.py adapter.py
```

`inventory` decides which paths are eligible. Extractors then register reads through `ExtractionContext`; they never open a path behind the context's back. Each read produces both a dependency and, when parsing succeeds, evidence. Facts are emitted at the observation point before deduplication or rendering. Claims are derived from facts, validated, optionally suppressed by documented evidence, and written to an immutable staged run. Review changes candidate disposition in a new state transaction. Rendering uses only accepted claims plus explicit, visibly labeled pending inferred material in the review preview; live export uses accepted claims only.

## Repository identity, traversal, and coverage

`--git-mode auto` is the default. In a Git worktree it records the repository's stable identity, absolute scan root, `HEAD` object ID, current branch when available, and whether tracked, untracked, or ignored-included inputs make the scan dirty. Dirty content is identified by inventory and content hashes and is never described as the `HEAD` snapshot. `--git-mode head` refuses a dirty repository; `--git-mode worktree` always hashes current files. Outside Git, `auto` becomes `no-git` and derives a local repository ID from the normalized absolute root plus a random project UUID created by `init`; it never invents a commit.

Inventory applies Git ignore semantics when Git metadata is available and compatible ignore matching otherwise. An exact explicit include may opt a normal Git-ignored file back in, and the report records that override; an explicit exclude always wins. Intrinsic exclusions for `.repo2skill/`, configured live outputs, symlink targets, filesystem boundaries, and generated/security-policy matches cannot be overridden. Symlinks are inventoried as symlinks and never followed. Submodules and filesystem mounts are boundaries.

The checked-in default policy sets practical safety caps: 100,000 inventory entries, 5,000 extractor-opened files, 2 MiB per text file, 64 MiB total decoded extractor input, and 200 KiB of retained redacted snippets per run. These are versioned defaults, not empirical completeness claims. A user may lower or raise them in config. Every skip or cap creates a diagnostic and coverage entry; required manifest parse failure makes generation incomplete rather than equivalent to absence.

Paths use `/`, are relative to the scan root, Unicode-normalized, contain no `..`, and are compared case-sensitively in records even on case-insensitive filesystems; case collisions are fatal. Source `contentHash` is SHA-256 over raw bytes. Text parsing accepts UTF-8 (with optional BOM) by default; another explicitly detected or configured encoding is recorded. Unsupported decoding is a coverage gap. Renderers build canonical UTF-8/LF bytes. Export adopts an existing target's consistent CRLF or LF style; mixed EOL is a conflict. The manifest hashes the exact exported block bytes after EOL conversion.

## Typed records and stable identity

Every persisted root document has `schemaVersion`, `toolVersion`, and a document-specific ID. Records also carry `recordRevision`; mutable semantic records carry their domain revision. Timestamps aid auditing but never participate in stable IDs.

At scan start repo2skill allocates a random `snapshotId` and passes it to every read, evidence, and fact record. The ID is identity, not a claim that scanning completed. After extraction, `build_snapshot` freezes the record with an `inputDigest` over the canonical inventory, every read outcome, policy revision, extractor versions, and repository identity. Only a completed run may activate that snapshot. This removes the cycle between read records needing a snapshot ID and the final snapshot consuming the reads.

Two scans of identical bytes and settings have the same `inventoryHash` and `inputDigest` but distinct random `snapshotId` values. Equality of inputs is established by the digests, never by snapshot ID reuse.

`SourceSnapshot` contains `snapshotId`, repository ID, scan root label, Git mode, commit/branch/dirty description, inventory hash, input digest, policy revision, extractor ID/version map, and completed-at time. `Evidence` contains `evidenceId`, revision, snapshot ID, normalized path, raw-byte content hash, media/encoding metadata, locator, optional redacted preview, and availability. Locators are typed: JSON Pointer, TOML/YAML key path, line range, file presence, or inventory predicate. A line hint may accompany a structural locator but is never the sole identity when a stronger locator exists.

`Fact` contains `factId`, `factRevision`, snapshot ID, fact type, semantic key, scope, normalized typed value, `derivation` (`observed`, `deterministic-derived`, `heuristic`, or `template`), rule ID/version, and evidence IDs. Template facts still cite evidence for the ecosystem/context that activated the template. Heuristics identify the exact rule; neither is rendered as observed repository truth.

`Claim` contains `claimId`, `claimRevision`, semantic key, scope, structured payload, rendered text or template key, `kind` (`observed`, `inferred`, or `human_policy`), `origin` (`deterministic`, `llm`, or `human`), fact/evidence IDs, `proposalStatus` (`pending`, `accepted`, `rejected`, or `contested`), lineage, and `lastVerifiedSnapshotId`. Kind and origin are independent. An observed claim requires only observed support and an agreeing payload. `human_policy` requires a separately authored deliberate policy record; P0 reserves the value but does not create one. Review changes proposal status only. Formatting does not increment `claimRevision`; semantic payload changes do. Split and merge events use `supersedes` and `derivedFrom`. Staleness is an effective freshness result, not a persisted proposal status.

Stable evidence IDs derive from repository ID, path, locator, and evidence semantic key rather than content. Fact and claim IDs derive from repository ID, scope, record type, and semantic key rather than prose. Content changes retain the ID and increment the relevant revision. IDs are lowercase, URL-safe strings with a type prefix and truncated SHA-256 suffix; collision detection compares the full canonical identity and fails closed.

`ExtractionRead` is created before a parser returns and has `readId`, `snapshotId`, `extractorId`, `extractorVersion`, `ruleId`, `ruleVersion`, `dependencyType`, `selector`, `required`, `outcome`, `contentHash`, `matchPaths`, `matchSetHash`, and `diagnosticIds`. Types are `file` (path plus observed existence/hash), `glob` (pattern, matched paths, and match-set hash), and `negative` (a named predicate plus inventory subset/hash proving no match). A missing candidate path is a valid file dependency. New files invalidate matching glob and negative reads; modifications and deletions invalidate file reads. `DedupDecision` has `decisionId`, `snapshotId`, `candidateClaimIds`, `candidateFactIds`, `documentationEvidenceIds`, `ruleId`, `ruleVersion`, `result`, and `reasonCode`, so suppression never erases proof.

## Persistence and transaction model

There is no database. `.repo2skill/` contains four root JSON document types:

```text
project.json                 project UUID, config/schema revisions
runs/<runId>.json            immutable snapshot, reads, evidence, facts,
                             candidates, diagnostics, dedup, staged hashes
state.json                   accepted/rejected/contested decisions and current run
manifests/<exportId>/<targetId>.json
                             immutable exported blocks, hashes and spans
index.json                   disposable, regenerated lookup acceleration
staging/<runId>/...          rendered review files and proposed patches
```

Each file is a complete versioned JSON document; record arrays may be indexed by ID in memory. `project.json` has `schemaVersion`, `toolVersion`, `projectId`, `rootLabel`, `stateDir`, `gitMode`, `scanPolicy`, `claimPolicy`, and `targets`. A run document has the `RunLedger` fields below. `state.json` has the `ProjectState` fields below; each `reviewEvent` has `eventId`, `claimId`, `claimRevision`, `action`, `reason`, and `createdAt`. Each target manifest has `schemaVersion`, `toolVersion`, `manifestId`, `exportId`, `targetId`, `runId`, `stageRevision`, `files`, and `createdAt`; every file entry has `path`, `ownershipMode`, `exactHash`, `eol`, `blocks`, and `claimSpans`, and every block has `blockId`, `exactHash`, and `claimIds`. `index.json` is never authoritative. Unknown major schemas are refused. Unknown additive fields on the current major version are preserved when a document is rewritten.

A generation transaction writes the immutable `runs/<runId>.json` file and `staging/<runId>/` tree under temporary names, fsyncs where supported, and atomically renames each into place. Only then does one atomic `state.json` replacement activate the run. A crash before that pointer commit may leave immutable orphan artifacts, which are ignored and can be reported for cleanup. Review first renders a new immutable stage revision and validates all its hashes; one atomic state replacement then records the review events and activates that revision. A render failure therefore cannot advance review state.

Every state-mutating command, including scan/generate activation, review, export, and export recovery, must hold one OS-owned exclusive lock for the project state directory from its authoritative-state read through its pointer commit or journal update. Lock acquisition is fail-fast; a busy invocation exits 1 without writing. The OS releases the lock after process death. A recovery-required journal is handled while holding the same lock. This serialization prevents two writers from accepting the same old state pointer and losing one update.

Export first validates all selected targets, their live block hashes, and staging hashes; if preflight finds a conflict, none are changed. Each target file is then replaced atomically in its own directory. There is no globally atomic multi-file export.

Before the first replacement, an export journal records `exportId`, ordered target operations, expected old hash, staged new hash, backup path/hash, and status. Per-file completion is journaled. After every file reaches the intended hash, repo2skill writes an immutable `manifests/<exportId>/` set and atomically replaces the single `state.json` pointer to that set; this pointer is the commit point. For a subset export, the new set contains new manifests for selected targets and immutable references to the prior active manifests for every unselected target, so their ownership is not lost. An interruption can leave some live files replaced while the prior manifest set remains authoritative. All later mutating commands refuse to proceed until explicit `export --resume <exportId>` recovery. Resume revalidates each current hash: an intended new hash is complete, an expected old hash may be replaced, and any third hash is a manual-edit conflict left untouched. It never restores over a third hash. Incomplete manifest sets are orphans until the state pointer commits. A failed scan, parser, renderer, Codex call, validation, or export preflight never advances the last good run or manifest; an interrupted export remains an explicit recoverable transaction rather than being reported as rolled back.

Candidate, accepted, and live state are distinct. Runs are immutable candidate history. `state.json` records review events and selects accepted claim revisions. A manifest records only what was exported. Rejecting or superseding a claim does not silently delete live bytes; a later staged export proposes removal of only the span owned by its manifest. History remains explainable.

## Extraction and generation contracts

Internal APIs use these exact aliases: `JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]`; `RepositoryPath`, `SnapshotId`, `RunId`, `FactId`, `ClaimId`, `TargetId`, and `ExportId` are validated `str` aliases; `Sha256` is `sha256:` followed by 64 lowercase hexadecimal characters. `Diagnostic` has `code`, `severity`, `message`, `path`, `extractorId`, and `details`. `CoverageEntry` has `selector`, `collector`, `status`, `reasonCode`, and `diagnosticIds`.

Argument and return records are frozen dataclasses with the following field names:

- `ScanPolicy(schemaVersion, revision, include, exclude, followSymlinks, generatedMode, maxInventoryEntries, maxOpenedFiles, maxFileBytes, maxDecodedBytes, maxSnippetBytes, encodings)`; `followSymlinks` is false in P0. `ClaimPolicy(schemaVersion, revision, autoAcceptObserved, renderBudget)`; `autoAcceptObserved` is true by default and never applies to inferred or human-policy claims.
- `InventoryEntry(path, kind, sizeBytes, contentHash, ignored, generated, symlinkTarget)` and `InventoryResult(root, entries, inventoryHash, diagnostics, coverage)`.
- `RepositoryIdentity(repositoryId, root, gitMode, commitId, branch, dirty, dirtySummary)` and `SourceSnapshot(snapshotId, repositoryId, rootLabel, gitMode, commitId, branch, dirty, dirtySummary, inventoryHash, inputDigest, policyRevision, extractorVersions, completedAt)`.
- `InputSelector(type, value, scope, required)` where type is `file`, `glob`, or `negative`; `ExtractionContext(root, snapshotId, inventory, policy, read)` where `read(selector)` returns `ReadResult(selector, dependency, bytes, encoding, error)` and appends the dependency before returning; `ExtractionBatch(reads, evidence, facts, diagnostics, coverage)`.
- `RunLedger(schemaVersion, runId, snapshot, reads, evidence, facts, claims, dedupDecisions, diagnostics, coverage, stagedTargets, completed)`; `ClaimSet(claims, diagnostics)`; and `ProjectState(schemaVersion, projectId, currentRunId, activeStageRevision, reviewEvents, acceptedClaimRevisions, activeExportId, activeManifestSetId)`.
- `ValidationResult(validClaims, rejectedClaims, diagnostics)` and `DedupResult(renderableClaims, decisions, diagnostics)`.
- `TargetSpec(targetId, kind, livePath, ownershipMode, emptyBlockPolicy)`; `RenderedTarget(targetId, relativeFiles, exactHashes, claimSpans, diagnostics)`; `RenderBundle(targets, diagnostics)`; and `StagedRun(runId, stageRoot, targetHashes)`.
- `OwnershipState(targetId, manifestId, expectedHashes, ownedBlocks, wholeFiles)` and `ExportResult(exportId, status, completedTargets, remainingTargets, conflicts, manifestIds)`.
- `CheckResult(sourceStatus, reviewStatus, coverageStatus, affectedClaimIds, findings)`, where source is `fresh|stale|unknown`, review is `clear|pending`, and coverage is `complete|informational-gaps|required-gap`. An ownership conflict is a finding with status `conflict` and has higher exit precedence.
- `Explanation(claim, facts, evidence, snapshot, dedupDecision, stagedLocations, liveLocations, diagnostics)`.
- `LedgerStoreSnapshot(project, state, runs, manifestSets, stageLocations)` is a read-only, internally consistent view captured after resolving the active state pointer. `project` is the loaded project document, `runs` maps `RunId` to `RunLedger`, `manifestSets` maps export ID to target manifests, and `stageLocations` maps `(RunId, stageRevision)` to validated repository-relative staged paths.

The principal Python signatures are:

```python
def collect_inventory(root: Path, policy: ScanPolicy) -> InventoryResult: ...
def capture_repository(root: Path, inventory: InventoryResult,
                       git_mode: Literal["auto", "head", "worktree", "no-git"]
                       ) -> RepositoryIdentity: ...
def build_snapshot(snapshot_id: SnapshotId, identity: RepositoryIdentity,
                   inventory: InventoryResult,
                   extractor_versions: Mapping[str, str],
                   reads: Sequence[ExtractionRead]) -> SourceSnapshot: ...

class Extractor(Protocol):
    extractor_id: str
    version: str
    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]: ...
    def extract(self, ctx: ExtractionContext,
                selectors: Sequence[InputSelector]) -> ExtractionBatch: ...

def derive_claims(facts: Sequence[Fact], policy: ClaimPolicy) -> ClaimSet: ...
def validate_claims(claims: Sequence[Claim], run: RunLedger) -> ValidationResult: ...
def deduplicate(candidates: Sequence[Claim], docs: Sequence[Fact],
                snapshot: SourceSnapshot) -> DedupResult: ...
def render_targets(claims: Sequence[Claim], specs: Sequence[TargetSpec]) -> RenderBundle: ...
def stage_run(bundle: RenderBundle, run: RunLedger) -> StagedRun: ...
def export_staged(run_id: RunId, targets: Sequence[TargetId],
                  ownership: OwnershipState) -> ExportResult: ...
def check_freshness(run: RunLedger, current_identity: RepositoryIdentity,
                    current_inventory: InventoryResult,
                    current_policy_revision: str,
                    current_extractor_versions: Mapping[str, str]
                    ) -> CheckResult: ...
def explain_claim(claim_id: ClaimId, store: LedgerStoreSnapshot,
                  revision: int | None = None,
                  run_id: RunId | None = None) -> Explanation: ...
```

`ExtractionContext.read(selector) -> ReadResult` is the only extractor I/O seam. It registers an `ExtractionRead` before opening or matching input, then finalizes that same record with bytes/hash, match set, missing status, cap, decode failure, or I/O error before any parser consumes the result. Parser outcomes add diagnostics but never erase the recorded read. `ExtractionBatch` contains reads, evidence, facts, diagnostics, and coverage. Registry execution order cannot change stable output order: canonical sorting is by scope, claim category, semantic key, and ID. The renderer includes all accepted ecosystem commands that fit the configured length budget and reports omitted claim IDs; it never silently chooses the first language's command.

For a file read, hashing, decoding, locator extraction, and parsing all consume the same in-memory byte buffer; evidence can never hash one read while facts use another. Immediately before activating a run or starting export replacement, repo2skill rechecks the complete used read set, including file hashes/existence, glob match sets, negative predicates, repository identity, policy revision, and extractor versions. Any concurrent change returns stale exit 4 and leaves the prior pointer/live outputs unchanged. This is a consistency check, not a claim that repo2skill captured an atomic repository-wide filesystem snapshot.

Freshness recomputes the repository identity and each recorded dependency without changing state. A changed commit/HEAD where relevant, dirty-state description, no-Git/worktree raw-byte input hash, policy revision, extractor version, file existence/hash, glob match set, or negative predicate yields effective `stale` and affected claim IDs. An unreadable dependency yields `unknown` plus a required or informational coverage result; it is never treated as fresh. `check` reports these effective values but persists no claim/review/live status mutation.

No-AI claim generation accepts observed claims automatically only when schema validation and exact support checks pass. Acceptance changes status only; it never changes kind or origin. Inferred/template/heuristic claims remain pending until individually reviewed. Accepted observed claims can be exported while inferred proposals remain pending; reports keep the pending count visible. Dedup may suppress rendering but cannot mark a candidate accepted or delete its support. Staged `AGENTS.md` uses explicit managed markers containing project/block IDs. The Agent Skill target is a portable directory staged as a unit with a concise `SKILL.md` and deterministic, manifest-listed companions. Each companion is exported and journaled per file.

## Review, ownership, export, and deletion

Review is file-backed and CLI-driven, with no TUI. A reviewer can inspect proposals and accept, reject, or contest named claim IDs. Accept and reject apply only to pending revisions; contest may mark a pending or accepted revision whose support is disputed. A constrained `--all-observed` accepts only currently pending, deterministically generated observed claims that pass validation. There is no unrestricted bulk acceptance. Rejection and contest require a short reason and apply to that claim revision; a later semantic revision becomes pending again.

repo2skill owns only blocks whose marker project ID and block ID match a manifest. Text outside those markers is always preserved. Before export, the existing block's exact hash must match the manifest's last exported hash. Missing/duplicated markers, mixed EOL, a changed block, or a target that changed after preflight produces a conflict and a proposed patch; repo2skill does not overwrite it. Explicit export may create missing Agent Skill parent directories when the nearest existing ancestor resolves inside the repository root and no path component is a symlink/reparse point. Every directory it creates is recorded as owned. No scan, generation, or review command creates live target directories.

Deletion is provenance-based. A removed claim disappears from live output only through a later export whose old manifest proves ownership of its exact span. An empty managed block may be removed with its markers if the target policy says so; the surrounding file is never deleted unless the manifest proves repo2skill created the whole file, its current full-file hash still matches, and the user explicitly exports that deletion. Skill companion files follow the same whole-file ownership rule.

## Redaction and Codex boundary

The local ledger stores hashes, structural locators, normalized typed values needed for claims, and short redacted previews. It does not persist arbitrary raw source blobs. Secret-like key names, high-entropy tokens, private-key material, authorization headers, credential URLs, and values from configured secret paths are replaced with stable redaction labels before preview persistence.

P1 builds an EvidencePack from a strict allowlist: typed fact values approved by fact type, redacted previews, claim IDs, diagnostics, scope, and schema instructions. Raw files, Git configuration, environment variables, credentials, ignored files, and unbounded documentation are excluded. The same pack written to the staging audit file is the pack sent to Codex. Repository text is quoted as untrusted data, never concatenated as executable instructions.

Before use, the adapter inspects the installed `codex` executable's local version and help for the required transport surface. This local check cannot validate authentication, network access, or a real model's structured response. The actual transport/response probe occurs only inside an explicitly opted-in P1 AI invocation after consent; unsupported capability or probe failure exits 6. The P1 capability spike must prove a version-specific invocation that runs from a sanitized temporary working directory containing only the redacted EvidencePack and output schema, disables tools, repository access, and automatic repository-instruction loading, and prevents a selected user configuration from silently re-enabling them. A profile or installed CLI that cannot prove those restrictions is unavailable and exits 6. A read-only sandbox label alone is not evidence that tools or instruction autoload are disabled. Transport flags and stdin/stdout framing must be implemented from installed help and fixtures; this design assumes no future flag spelling. Users may select a compatible named Codex configuration through repo2skill's narrow option. Arbitrary Codex arguments, shell interpolation, config execution, provider selection, and credential access are forbidden. Timeout, nonzero status, malformed JSON, partial output, invented IDs, wrong snapshot, or unsupported claims leave the last good state unchanged.

## Platform, dependencies, testing, and phases

Windows, Linux, and macOS are first-class. Path, case-collision, symlink, CRLF/LF, atomic-replace, and executable discovery behavior require platform fixtures. Core uses the Python standard library plus two deliberately bounded parser dependencies: [`pathspec`](https://python-path-specification.readthedocs.io/en/stable/) for Gitignore-compatible matching and [`PyYAML`](https://pyyaml.org/wiki/PyYAMLDocumentation) for generic YAML. PyYAML uses `BaseLoader`, rejects recursive aliases, and performs typed conversion only for selected known keys, avoiding YAML 1.1 implicit coercion such as `on`/`off`. Implementation Task 1 pins versions only after recording each license, supported Python/platform range, and focused fixtures; no dependency is installed as part of this design. Upstream's zero-dependency status does not bind this project.

P0 should be implemented as vertical slices ending in the complete no-AI loop, with fixtures for all four ecosystems, generic-only repositories, no-Git and dirty worktrees, file/glob/negative invalidation, caps and parse failures, dedup evidence changes, forged references, manual block edits, deletion, crash recovery, and both EOL styles. Dedicated tests must separately cover: two writers contending for the exclusive lock with no lost pointer update; a file/glob/negative input changing between extraction and activation/export and producing exit 4; first Skill export creating only confined, owned directories while rejecting a symlink/reparse ancestor; and a one-target export retaining the other target's prior manifest/ownership reference. P1 begins only after P0 acceptance and adds capability probing, displayed usage estimates, explicit consent, EvidencePack validation, and failure isolation before the pilot. Its capability spike and tests distinguish local help detection from an explicitly opted-in transport/response probe and prove the sanitized directory, no-tools, no-repository-access, no-instruction-autoload boundary for every supported CLI version/profile. Estimates are labeled estimates and never presented as quota or price guarantees. P2 starts only from measured pilot coverage gaps.

Acceptance requires that every live machine-generated factual claim reaches a valid fact, evidence locator, raw-byte hash, snapshot, block, and rendered span; added, changed, and deleted dependencies stale the right claims; unsupported coverage is visible; AI failure cannot advance state; and manual content is preserved or converted into a reviewable conflict. These checks establish provenance and lifecycle integrity, not semantic correctness beyond the observed inputs and declared rules.
