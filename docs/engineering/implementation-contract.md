# P0 local generator implementation contract

**Status:** normative correction to `docs/superpowers/specs/2026-10-07-repo2skill-mvp-design.md` and `docs/engineering/cli-contract.md` for P0 implementation. This contract narrows ambiguous implementation details; it adds no feature or framework. Where this document supplies a type or corrected signature, it governs implementation. All names and fields already stated by the design remain unchanged.

## Common notation and persistence rule

All records below are frozen dataclasses. Persisted sequences are JSON arrays and have Python type `tuple[T, ...]`; persisted keyed collections are JSON objects and have Python type `Mapping[str, T]`. `JsonValue`, validated string IDs, `RepositoryPath`, and `Sha256` retain the definitions in the design. Add these aliases only:

```python
Timestamp = str                 # RFC 3339 UTC
RecordRevision = int            # >= 1
LineSpan = tuple[int, int]       # one-based, inclusive start/end lines
ClaimRevisionRef = str           # canonical "<claimId>@<positive-decimal-revision>"
T = TypeVar("T")                 # frozen record type, runtime-only typing aid
```

Every persisted non-root record below has `recordRevision: RecordRevision`. It is the serialization/schema revision of that record instance. `Evidence.revision`, `Fact.factRevision`, and `Claim.claimRevision` are semantic revisions and are independent of `recordRevision`. Root documents have `schemaVersion` and `toolVersion`; `RunLedger`, `ProjectState`, target manifests, manifest sets, and export journals therefore add `toolVersion` to their previously stated fields. Timestamps never affect IDs, semantic revisions, or digests.

P0 writes `schemaVersion="1.0"`. The P0-01 defaults are frozen as follows: `ScanPolicy.revision="scan-policy-v1"`, `include=()`, `exclude=()`, `followSymlinks=False`, `generatedMode="exclude"`, caps of 100,000 inventory entries, 5,000 opened files, 2 MiB per file, 64 MiB decoded input, and 200 KiB retained snippets, and `encodings=("utf-8-sig",)` (which accepts ordinary UTF-8 and strips an optional BOM). `ClaimPolicy.revision="claim-policy-v1"`, `autoAcceptObserved=True`, and `renderBudget=16_384` UTF-8 bytes per target. `TargetSpec.kind` is `Literal["agents-md", "skill"]`; the respective `ownershipMode` values are `Literal["managed-blocks", "whole-files"]`; `emptyBlockPolicy="keep"` by default. Deletion remains an explicit later export option.

Readers reject an unknown schema major version. For the current major version, `LedgerStore` retains the original decoded JSON tree and, on mutable-document rewrite, schema-aware merges the newly encoded known fields into that tree. Unknown object members are preserved recursively, including root members and members on existing nested `ReviewEvent` objects matched by `eventId`; newly appended objects contain only current known fields. Unknown members are not added to the frozen domain dataclasses and never participate in IDs or digests. Immutable documents are never rewritten. This raw-tree merge is the sole additive-field-preservation mechanism; P0-02 tests preservation of both a root unknown member and a nested review-event unknown member across state replacement.

## R1 — frozen record contracts and stable identity

The exact descriptive and provenance records are:

```python
EvidenceLocator(
    kind: Literal["json-pointer", "key-path", "line-range",
                  "file-presence", "inventory-predicate"],
    value: JsonValue,
    lineHint: int | None,
)

Evidence(
    recordRevision: RecordRevision,
    evidenceId: str,
    revision: int,
    snapshotId: SnapshotId,
    path: RepositoryPath,
    contentHash: Sha256,
    semanticKey: str,
    mediaType: str,
    encoding: str | None,
    locator: EvidenceLocator,
    redactedPreview: str | None,
    availability: Literal["available", "missing", "unreadable", "capped", "unsupported"],
)

Fact(
    recordRevision: RecordRevision,
    factId: FactId,
    factRevision: int,
    snapshotId: SnapshotId,
    factType: str,
    semanticKey: str,
    scope: str,
    value: JsonValue,
    derivation: Literal["observed", "deterministic-derived", "heuristic", "template"],
    ruleId: str,
    ruleVersion: str,
    evidenceIds: tuple[str, ...],
)

Claim(
    recordRevision: RecordRevision,
    claimId: ClaimId,
    claimRevision: int,
    semanticKey: str,
    scope: str,
    payload: JsonValue,
    renderedText: str | None,
    templateKey: str | None,
    kind: Literal["observed", "inferred", "human_policy"],
    origin: Literal["deterministic", "llm", "human"],
    factIds: tuple[FactId, ...],
    evidenceIds: tuple[str, ...],
    proposalStatus: Literal["pending", "accepted", "rejected", "contested"],
    supersedes: tuple[ClaimRevisionRef, ...],
    derivedFrom: tuple[ClaimRevisionRef, ...],
    lastVerifiedSnapshotId: SnapshotId,
)

ExtractionRead(
    recordRevision: RecordRevision,
    readId: str,
    snapshotId: SnapshotId,
    extractorId: str,
    extractorVersion: str,
    ruleId: str,
    ruleVersion: str,
    dependencyType: Literal["file", "glob", "negative"],
    selector: InputSelector,
    required: bool,
    outcome: Literal["present", "missing", "matched", "no-match", "capped",
                     "decode-error", "io-error"],
    contentHash: Sha256 | None,
    matchPaths: tuple[RepositoryPath, ...],
    matchSetHash: Sha256 | None,
    diagnosticIds: tuple[str, ...],
)

DedupDecision(
    recordRevision: RecordRevision,
    decisionId: str,
    snapshotId: SnapshotId,
    candidateClaimIds: tuple[ClaimId, ...],
    candidateFactIds: tuple[FactId, ...],
    documentationEvidenceIds: tuple[str, ...],
    ruleId: str,
    ruleVersion: str,
    result: Literal["render", "suppress", "merge"],
    reasonCode: str,
)

Diagnostic(
    recordRevision: RecordRevision,
    code: str,
    severity: Literal["info", "warning", "error"],
    message: str,
    path: RepositoryPath | None,
    extractorId: str | None,
    details: JsonValue,
)

CoverageEntry(
    recordRevision: RecordRevision,
    selector: InputSelector,
    collector: str,
    status: Literal["supported", "unsupported", "skipped", "capped",
                    "generated", "invalid", "failed"],
    reasonCode: str,
    diagnosticIds: tuple[str, ...],
)

ReviewEvent(
    recordRevision: RecordRevision,
    eventId: str,
    claimId: ClaimId,
    claimRevision: int,
    action: Literal["accept", "reject", "contest"],
    reason: str | None,
    createdAt: Timestamp,
)
```

`renderedText` and `templateKey` are mutually exclusive and exactly one is non-null. `supersedes` and `derivedFrom` are the complete lineage fields; empty tuples mean no lineage. Reject and contest events require a nonblank `reason`; accept requires `reason is None`.

Stable IDs use the canonical identity rules already stated. In particular, evidence identity includes repository ID, `path`, canonical locator, and `semanticKey`; fact and claim identity includes repository ID, `scope`, record type, and `semanticKey`. Repository identity is operation context rather than duplicated in every fact. Claim construction is therefore corrected to:

```python
def derive_claims(
    facts: Sequence[Fact],
    policy: ClaimPolicy,
    *,
    repository_id: str,
    prior_claims: Sequence[Claim] = (),
) -> ClaimSet: ...
```

`prior_claims` comes from the prior active run for the same repository. Equal canonical semantic payload retains ID and `claimRevision`; a changed payload under the same stable identity increments `claimRevision`; a new identity starts at 1. Evidence and fact builders apply the same prior-run comparison for their domain revisions. Full canonical identities are retained in the in-memory collision index and a truncated-ID collision fails closed.

P0-02 provides one small pure revision helper, used by evidence/fact builders and by P0-06 claim derivation rather than by constructing placeholder claims:

```python
def revise_record(
    previous: T | None,
    new: T,
    *,
    semantic_payload: Callable[[T], JsonValue],
    revision_field: Literal["revision", "factRevision", "claimRevision"],
) -> T: ...
```

`previous` is either absent or the prior record with the same stable ID. The helper returns a frozen replacement of `new`: revision 1 when absent, the prior domain revision when semantic payloads are canonically equal, and prior revision plus one otherwise. It never changes the stable ID. A rejected or contested disposition is overlaid from review history only when both claim ID and semantic revision match; a new semantic revision is pending again, subject only to the existing deterministic-observed auto-accept rule.

`RunLedger.claims` always stores the complete candidate `ClaimSet`, including suppressed candidates. `DedupResult.renderableClaims` is only the renderer selection.

The exact root records retain their stated fields and types:

```python
ProjectDocument(
    schemaVersion: str, toolVersion: str, projectId: str, rootLabel: str,
    stateDir: RepositoryPath, gitMode: Literal["auto", "head", "worktree", "no-git"],
    scanPolicy: ScanPolicy, claimPolicy: ClaimPolicy, targets: tuple[TargetSpec, ...],
)

RunLedger(
    schemaVersion: str, toolVersion: str, runId: RunId, snapshot: SourceSnapshot,
    effectiveScanPolicy: ScanPolicy, effectiveClaimPolicy: ClaimPolicy,
    configuredTargets: tuple[TargetSpec, ...],
    reads: tuple[ExtractionRead, ...], evidence: tuple[Evidence, ...],
    facts: tuple[Fact, ...], claims: tuple[Claim, ...],
    dedupDecisions: tuple[DedupDecision, ...], diagnostics: tuple[Diagnostic, ...],
    coverage: tuple[CoverageEntry, ...],
    stagedTargets: Mapping[
        TargetId,
        Mapping[Literal["accepted", "preview"], Mapping[RepositoryPath, Sha256]],
    ],
    completed: bool,
)

ProjectState(
    schemaVersion: str, toolVersion: str, projectId: str,
    currentRunId: RunId | None, activeStageRevision: int | None,
    reviewEvents: tuple[ReviewEvent, ...],
    acceptedClaimRevisions: Mapping[ClaimId, int],
    activeExportId: ExportId | None, activeManifestSetId: str | None,
)
```

## R2 — deterministic digest projections

Canonical JSON means UTF-8, sorted object keys, no insignificant whitespace, arrays sorted as specified, and no platform path separators. `inventoryHash` is SHA-256 over the canonical array of inventory entries sorted by `path`, projecting exactly `path`, `kind`, `sizeBytes`, `contentHash`, `ignored`, `generated`, and `symlinkTarget`. It excludes `InventoryResult.root`, diagnostics, coverage, and incidental traversal order.

`inputDigest` is SHA-256 over one canonical object containing:

```text
repository = repositoryId, rootLabel, gitMode, commitId, branch, dirty, dirtySummary
inventory  = inventoryHash
policy     = policyRevision
extractors = extractor ID -> version, sorted by extractor ID
reads      = canonical projections sorted by
             (extractorId, ruleId, dependencyType, canonical selector, scope)
```

Each read projection includes `extractorId`, `extractorVersion`, `ruleId`, `ruleVersion`, `dependencyType`, all four `InputSelector` fields, `required`, `outcome`, `contentHash`, sorted canonical `matchPaths`, and `matchSetHash`. Thus the digest retains raw-byte identity through `contentHash`, existence/outcome, semantic selectors, match sets, rules, repository identity, policy, and extractor identity. It excludes `snapshotId`, `readId`, evidence/fact/claim/decision IDs, diagnostic IDs/messages, timestamps, runtime absolute roots, and incidental record order. `repositoryId` and stable `rootLabel` participate; runtime `Path` roots do not.

The effective policy revision is not a caller-authored label. It is `sha256:` plus SHA-256 over the canonical effective `ScanPolicy`, canonical effective `ClaimPolicy`, the complete configured `TargetSpec` tuple sorted by `targetId`, and other configuration options that affect discovery, claims, or rendering. It always includes every configured target, independent of the invocation's selected render/export subset; selecting one target cannot stale the source snapshot. `SourceSnapshot.policyRevision` stores this effective hash. The human-readable policy `revision` fields remain inputs to that projection, so any effective policy change stales a run even if a human revision string was not updated.

```python
def effective_policy_revision(
    scan_policy: ScanPolicy,
    claim_policy: ClaimPolicy,
    configured_targets: Sequence[TargetSpec],
    *,
    generation_options: Mapping[str, JsonValue],
) -> str: ...
```

`configured_targets` is always the complete configured collection and is canonicalized by `targetId`. `generation_options` contains only options that alter discovery, claim semantics, or rendered bytes; invocation target selection and other execution-only choices are excluded.

The immutable `RunLedger` persists those same inputs as `effectiveScanPolicy`, `effectiveClaimPolicy`, and `configuredTargets`. Command-line scan overrides are applied before hashing and the resulting effective records are stored, so recheck uses the exact recorded scan semantics rather than current defaults. `configuredTargets` is the full configured tuple, not the invocation subset.

The extraction signatures are corrected without hidden project/config reads:

```python
def collect_inventory(
    root: Path, policy: ScanPolicy, *,
    excluded_paths: Sequence[RepositoryPath] = (),
) -> InventoryResult: ...

def capture_repository(
    root: Path, inventory: InventoryResult,
    git_mode: Literal["auto", "head", "worktree", "no-git"], *,
    project_id: str,
) -> RepositoryIdentity: ...

def build_snapshot(
    snapshot_id: SnapshotId, identity: RepositoryIdentity,
    inventory: InventoryResult, extractor_versions: Mapping[str, str],
    reads: Sequence[ExtractionRead], *,
    policy_revision: str, root_label: str,
) -> SourceSnapshot: ...
```

`project_id` is the initialized project UUID used by no-Git repository identity. `root_label` is the stable persisted label. `excluded_paths` contains the configured state directory and every configured live output; inventory applies these intrinsic exclusions before include rules.

## R3 — explicit local mutation context

There is one store implementation:

```python
LedgerStore(root: Path, state_dir: Path)
```

Both constructor paths are resolved absolute paths; `state_dir` must be confined under `root`. The store never consults process cwd or module globals. `with store.mutation():` acquires the one fail-fast OS-owned exclusive state-directory lock before any authoritative state, run, journal, or manifest read. The caller owns this scope and holds it through the final state-pointer replacement or journal update. Mutation depth/ownership is per `LedgerStore` instance and lock owner, never module- or class-global; nested mutation entry is rejected. The store tracks that its mutation scope is active; every stateful operation below rejects an unlocked store before writing. The caller loads authoritative records through that same store, rejects an incomplete unrelated export journal, invokes the operation, and maps failure to the CLI result. Operations do not acquire a second lock.

The corrected signatures are additive and explicit:

```python
def stage_run(
    bundle: RenderBundle, run: RunLedger, *, store: LedgerStore,
) -> StagedRun: ...

def review_claims(
    state: ProjectState, run: RunLedger, events: Sequence[ReviewEvent],
    specs: Sequence[TargetSpec], *, store: LedgerStore,
) -> tuple[ProjectState, StagedRun]: ...

def export_staged(
    run_id: RunId, targets: Sequence[TargetId],
    ownership: Mapping[TargetId, OwnershipState], *, store: LedgerStore,
) -> ExportResult: ...

def resume_export(
    export_id: ExportId,
    ownership: Mapping[TargetId, OwnershipState], *, store: LedgerStore,
) -> ExportResult: ...
```

Each operation obtains both absolute paths only from `store.root` and `store.state_dir` and compares every supplied `state`, `run`, stage, ownership, and journal record for canonical equality with the authoritative record read inside the active lock. A stale passed record is rejected even when its ID matches. `stage_run` publishes immutable artifacts, performs the R5 recheck, then commits one state pointer. `review_claims` validates the whole batch, rerenders and publishes a new immutable stage revision, validates it, then commits the returned state once. Export journals before the first live mutation and commits manifests before the state pointer. Any mismatch or exception before the defined commit point fails closed and leaves the prior pointer unchanged.

## R4 — render, stage, ownership, manifest, and journal shapes

The renderer's existing record names are retained. Accepted/export content and review preview are separate mappings:

```python
RenderedTarget(
    targetId: TargetId,
    relativeFiles: Mapping[RepositoryPath, bytes],
    exactHashes: Mapping[RepositoryPath, Sha256],
    claimSpans: Mapping[RepositoryPath, Mapping[ClaimRevisionRef, tuple[LineSpan, ...]]],
    diagnostics: tuple[Diagnostic, ...],
    previewFiles: Mapping[RepositoryPath, bytes],
    previewHashes: Mapping[RepositoryPath, Sha256],
    previewClaimSpans: Mapping[RepositoryPath, Mapping[ClaimRevisionRef, tuple[LineSpan, ...]]],
)

RenderBundle(targets: tuple[RenderedTarget, ...], diagnostics: tuple[Diagnostic, ...])

StagedRun(
    runId: RunId,
    stageRoot: Path,
    targetHashes: Mapping[TargetId, Mapping[RepositoryPath, Sha256]],
    stageRevision: int,
    previewTargetHashes: Mapping[TargetId, Mapping[RepositoryPath, Sha256]],
)
```

`relativeFiles` and `exactHashes` contain canonical UTF-8/LF accepted-only export bytes. Preview mappings may additionally contain visibly labelled pending material. Keys must match within each bytes/hash/span triplet. `stageRoot` is runtime-only; every persisted stage location is a normalized repository-relative path, including its configured state-directory prefix. A stage revision is a positive integer unique within a run.

Manifest records are:

```python
ManifestBlock(
    recordRevision: RecordRevision, blockId: str, exactHash: Sha256,
    claimIds: tuple[ClaimId, ...],
)

ManifestFile(
    recordRevision: RecordRevision, path: RepositoryPath,
    ownershipMode: Literal["managed-blocks", "whole-files"], exactHash: Sha256,
    eol: Literal["lf", "crlf"], blocks: tuple[ManifestBlock, ...],
    claimSpans: Mapping[ClaimRevisionRef, tuple[LineSpan, ...]],
)

TargetManifest(
    schemaVersion: str, toolVersion: str, manifestId: str, exportId: ExportId,
    targetId: TargetId, runId: RunId, stageRevision: int,
    files: tuple[ManifestFile, ...],
    createdDirectories: tuple[RepositoryPath, ...], createdAt: Timestamp,
)

ManifestReference(
    recordRevision: RecordRevision, targetId: TargetId,
    manifestId: str, exportId: ExportId, path: RepositoryPath,
)

ManifestSet(
    schemaVersion: str, toolVersion: str, manifestSetId: str,
    exportId: ExportId, targets: Mapping[TargetId, ManifestReference],
    createdAt: Timestamp,
)
```

A subset export writes new target manifests for selected targets and copies `ManifestReference` values for every unselected target from the prior active set. The manifest set is complete for all targets with ownership history.

`TargetManifest.createdDirectories` is the authoritative durable directory-ownership record. `OwnershipState.createdDirectories` is reconstructed from the active manifests; the journal copy supports recovery but is not the sole record.

`OwnershipState` retains its fields, adds directory ownership, and is supplied as a zero-or-many mapping:

```python
OwnershipState(
    targetId: TargetId, manifestId: str | None,
    expectedHashes: Mapping[RepositoryPath, Sha256],
    ownedBlocks: Mapping[RepositoryPath, tuple[str, ...]],
    wholeFiles: tuple[RepositoryPath, ...],
    createdDirectories: tuple[RepositoryPath, ...],
)

ExportResult(
    exportId: ExportId,
    status: Literal["complete", "incomplete", "conflict"],
    completedTargets: tuple[TargetId, ...],
    remainingTargets: tuple[TargetId, ...],
    conflicts: tuple[Finding, ...],
    manifestIds: Mapping[TargetId, str],
)
```

An absent mapping entry means no prior ownership for that target. First export therefore uses an empty mapping. Created directories are normalized root-relative paths, recorded only after safe creation, and belong to their target.

The persisted recovery document is:

```python
JournalOperation(
    recordRevision: RecordRevision, operationId: str, targetId: TargetId,
    path: RepositoryPath, action: Literal["replace", "delete", "mkdir"],
    expectedOldHash: Sha256 | None, stagedNewHash: Sha256 | None,
    backupPath: RepositoryPath | None, backupHash: Sha256 | None,
    proposedPatchPath: RepositoryPath | None,
    status: Literal["planned", "complete", "conflict"],
)

ExportJournal(
    schemaVersion: str, toolVersion: str, exportId: ExportId,
    runId: RunId, stageRevision: int, targetIds: tuple[TargetId, ...],
    operations: tuple[JournalOperation, ...],
    createdDirectories: Mapping[TargetId, tuple[RepositoryPath, ...]],
    manifestSetId: str | None,
    status: Literal["prepared", "incomplete", "conflict", "committed"],
    createdAt: Timestamp, updatedAt: Timestamp,
)
```

Operations are in deterministic target/path/action order. Journal updates are atomic. `expectedOldHash is None` means the file/directory was absent; `stagedNewHash is None` is valid only for delete or mkdir. A third live hash changes the operation and journal to `conflict` without touching the live path. Proposed patches live under staging and their persisted paths are repository-relative.

## R5 — one dependency recheck

P0-04 owns and tests the only source recheck implementation:

```python
DependencyRecheckResult(
    status: Literal["fresh", "stale", "unknown"],
    affectedClaimIds: tuple[ClaimId, ...],
    findings: tuple[Finding, ...],
)

def recheck_dependencies(
    run: RunLedger,
    current_identity: RepositoryIdentity,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str],
    *,
    root: Path,
    excluded_paths: Sequence[RepositoryPath],
) -> DependencyRecheckResult: ...
```

It re-evaluates only the recorded file, glob, and negative selectors through the extraction read seam under `run.effectiveScanPolicy`. `excluded_paths` is the union of the configured state directory, `run.configuredTargets` live paths, and current configured live paths; it remains intrinsically excluded during selector matching. Recheck never falls back to a default policy or reads an arbitrary ignored/live path. It compares existence/raw-byte hashes, canonical match sets, negative predicates, repository identity, `current_policy_revision` against `run.snapshot.policyRevision`, and extractor versions, and maps changed reads through facts to all affected claims. A definite mismatch is `stale`; an unreadable/capped/I/O outcome is `unknown` with the applicable required or informational coverage finding and is never fresh. `stale` wins over `unknown` when both occur, while all findings remain present.

P0-09 calls this function immediately before the first export replacement. Generation orchestration calls it immediately before activation. P0-10 calls it for `check`; it does not implement a second comparison. Ownership inspection remains a separate P0-09 operation over selected live targets and active manifests. The CLI aggregates the source result, ownership findings, review state, and coverage using the published exit priority. Source status never absorbs an ownership conflict.

The corrected P0-10 source signature removes the misleading general inventory argument:

```python
def check_freshness(
    run: RunLedger, current_identity: RepositoryIdentity,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str], *, root: Path,
    excluded_paths: Sequence[RepositoryPath],
) -> CheckResult: ...
```

`check_freshness` delegates source comparison to `recheck_dependencies`; the CLI separately requests ownership findings.

## R6 — path representation

`TargetSpec.livePath` is a normalized, root-relative `RepositoryPath` string. `default_targets(root)` accepts `root` only to validate confinement and derive the project slug; it does not store an absolute live path. `InventoryResult.root` and `RepositoryIdentity.root` are resolved absolute runtime `Path` values. They are never persisted or emitted in JSON. Persisted repository identity uses the explicit stable `SourceSnapshot.rootLabel` string.

All persisted source paths, target paths, manifest paths, owned directories, stage locations, journal backup/proposed-patch paths, and CLI JSON paths are normalized `/` strings relative to the repository root, including paths beneath `stateDir`. `StagedRun.stageRoot`, `LedgerStore.root`, and `LedgerStore.state_dir` are runtime absolute `Path` values. Text output may display an absolute path only when it labels it as absolute; the JSON contracts below do not require one.

Every path is Unicode-normalized, contains no empty, `.` or `..` component, and is compared case-sensitively in records. Before access, implementations resolve beneath the configured root and reject case collisions, symlink/reparse traversal, filesystem-boundary escape, and redirected ancestors.

## R7 — minimum CLI JSON contract

JSON mode emits exactly one object plus newline. Every command uses this envelope:

```text
schemaVersion: string
toolVersion: string
command: string                    # includes review subcommand or export-resume
status: ok|pending|incomplete|stale|conflict|error
exitCode: integer
result: object|null
findings: Finding[]
diagnostics: Diagnostic[]
nextActions: string[]
```

`Finding` is the one shared machine finding record:

```python
Finding(
    recordRevision: RecordRevision,
    code: str,
    status: Literal["info", "pending", "incomplete", "stale", "unknown", "conflict", "error"],
    message: str,
    path: RepositoryPath | None,
    targetId: TargetId | None,
    claimIds: tuple[ClaimId, ...],
    details: JsonValue,
)
```

The envelope always contains every key, including empty arrays or null result. Diagnostics use the R1 shape. `exitCode` is computed from all findings with `7 > 5 > 6 > 2 > 4 > 3 > 1 > 0`; array order cannot change it. Command result objects have these required fields:

- `init`: `projectId`, `rootLabel`, `stateDir`, `targets`, `created`.
- `scan`: `runId`, `snapshotId`, `inventoryHash`, `inputDigest`, `counts`, `coverage`.
- `generate`: `runId`, `snapshotId`, `stageRevision`, `stagedTargets` (target to accepted/preview repository-relative paths and hashes), `claimCounts` (by status/kind/origin), `dedupCounts`, `coverage`, `remainingPendingCount`.
- `review-list`: `runId`, `claims`; `review-show`: `runId`, `claim`, `facts`, `evidence`, `dedupDecision`, `coverage`; each mutation: `runId`, `stageRevision`, `events`, `acceptedClaimRevisions`, `remainingPendingCount`.
- `check`: `runId`, `sourceStatus`, `reviewStatus`, `coverageStatus`, `affectedClaimIds`, `targets`.
- `explain`: `runId`, `claim`, `facts`, `evidence`, `snapshot`, `dedupDecision`, `stagedLocations`, `liveLocations`.
- `report`: `runId`, `sections`, `counts`, `coverage`.
- `export` and `export-resume`: `exportId`, `runId`, `stageRevision`, `manifestSetId`, `completedTargets`, `remainingTargets`, `targets` (paths, exact hashes, manifest IDs, exported claim revisions), `remainingPendingCount`.
- `doctor`: `checks`, `codexRequested`, `codexCapability`.

Result paths follow R6. Secret values, unredacted previews, raw bytes, exception representations, and absolute runtime roots never enter the envelope.

## Corrected task ownership and non-negotiable invariants

- P0-01 fixes its assertion to inspect `{target.targetId for target in default_targets(root)}` and implements root-relative `TargetSpec.livePath`.
- P0-02 owns the R1 records, `revise_record`, one `LedgerStore`, collision-safe IDs, versioned readers, recursive additive-field preservation, locking, immutable writes, and atomic pointer replacement.
- P0-03 accepts explicit `project_id` and configured `excluded_paths`; P0-04 accepts explicit snapshot policy/root labels and owns `recheck_dependencies` with its file/glob/negative/identity/policy/extractor tests. Extraction persists hashes, locators, normalized facts, and redacted previews, never arbitrary raw blobs.
- P0-05 also modifies `src/repo2skill/extractors/registry.py`; all five test paths are under `tests/extractors/`.
- P0-06 uses repository/prior context for IDs and revisions and keeps all candidates in `RunLedger.claims`.
- P0-07 implements the corrected stage types/signature; P0-08 implements the corrected review signature and returns both state and stage; P0-09 owns manifest sets, ownership inspection, journal/recovery, directory ownership, and corrected export signatures.
- P0-10 reuses P0-04 source recheck and reads P0-09 ownership state without redefining either. P0-11 alone aggregates CLI findings/envelopes. P0-12 consumes these contracts unchanged.

The existing safety rules remain mandatory: one fail-fast OS-owned lock spans authoritative read through commit; raw source blobs are not persisted; PyYAML `BaseLoader` is used with recursive aliases rejected before selected-key conversion; source hashes cover raw bytes; staging is UTF-8/LF; export adopts only a consistent LF or CRLF target and rejects mixed EOL; exact exported bytes are hashed after conversion; symlinks/reparse points are never followed; root escape and case collision are fatal; live target creation is export-only and confined through non-redirected ancestors; recovery never overwrites a third hash.

## Cross-reference ownership

After publication, the root plan owner should update `docs/superpowers/specs/2026-10-07-repo2skill-mvp-design.md` to point its ambiguous record/signature/path clauses to this normative correction, and update `docs/superpowers/plans/2026-10-07-local-generator.md` plus affected task briefs to consume the corrected signatures and file ownership above. Those cross-reference edits are outside this document-only task.
