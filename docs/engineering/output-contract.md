# P0 output and claim lifecycle contract

**Status:** normative supplement for P0-06 through P0-11. This document supplements `docs/engineering/implementation-contract.md`, `docs/engineering/input-contract.md`, and the MVP design. Where its corrected signatures or lifecycle rules differ from older task briefs/design sketches, this contract governs. It retains the frozen persisted dataclass fields; the `stage.json` descriptor is a versioned immutable stage artifact, not a new field on `RunLedger`, `ProjectState`, or `StagedRun`.

## Task 6 — claim derivation, validation, and deduplication

Use the identity-aware signature from R1:

```python
def derive_claims(
    facts: Sequence[Fact],
    policy: ClaimPolicy,
    *,
    repository_id: str,
    prior_claims: Sequence[Claim] = (),
    id_context: CanonicalIdContext | None = None,
) -> ClaimSet: ...

def deduplicate(
    candidates: Sequence[Claim],
    docs: Sequence[Fact],
    snapshot: SourceSnapshot,
    *,
    id_context: CanonicalIdContext | None = None,
) -> DedupResult: ...
```

`prior_claims` contains this repository's prior valid immutable claim history, including valid inactive orphan runs; derivation groups records by stable claim ID and compares only those at the maximum `claimRevision`. Derivation creates every candidate with `proposalStatus="pending"`; it never accepts, rejects, or contests. It uses canonical stable identity and the R1 semantic projection (`payload`, `kind`, `origin`): unchanged semantic payload retains the maximum revision, while a changed payload increments it. Formatting-only changes (including `templateKey`/rendered representation changes) do not affect revision. Equal-maximum prior records with conflicting semantic payloads fail closed. Prefer a versioned `templateKey` for structured deterministic claims; use `renderedText` only when bounded literal prose is itself the semantic payload. P0 creates no `human_policy` or `llm` claim. Task 11 enumerates history under the caller-held mutation; unreadable/invalid enumerated records fail closed. With no persisted history index, missing historical files cannot be detected, and no new index or schema field is introduced.

`id_context` is an optional caller-owned `CanonicalIdContext` used to preserve collision detection across multiple builders in one session. When omitted, each call creates a fresh context; no module-global collision state is used.

The controller next calls `validate_claims(ClaimSet.claims, draft_run)`. A returned valid claim may become `accepted` only when it is `observed`/`deterministic` and `draft_run.effectiveClaimPolicy.autoAcceptObserved` is true. Other valid claims remain pending. `rejectedClaims` means invalid for this run, not review-rejected; invalid candidates stay pending. An observed claim requires observed support with an agreeing payload. Validation checks all referenced facts/evidence, snapshot, scope, and provenance. Each candidate's direct `evidenceIds` is the sorted union of cited facts' evidence IDs unless a declared versioned rule narrows the chain and validation proves it.

For a new run, the controller applies matching prior review disposition from authoritative `ProjectState` to valid claims only, after validation autoaccept, using exact `(claimId, claimRevision)`. This preserves an explicit matching accept/reject/contest and never accepts an invalid candidate. It then merges effective valid-claim statuses back into every original candidate by exact pair; invalid candidates remain pending. Persist every original candidate and all support in `RunLedger.claims` with those initial effective statuses. Dedup receives the effective valid claims. This ordering ensures the staged accepted spans agree with the candidate statuses in the run. A new semantic revision has no matching prior disposition and remains pending unless current validation auto-accepts it. Formatting changes do not make a new revision. Later review of a persisted run uses transient `ProjectState` overlays and never rewrites that immutable run.

Dedup receives valid candidates and documentation facts. Every `DedupDecision` records sorted unique `candidateClaimIds`, `candidateFactIds`, and `documentationEvidenceIds`; include every evidence record used, including identical normalized text at multiple paths. Define `decisionId = canonical_record_id("dedup", identity)` with identity containing repository ID, rule ID/version, and those three ID tuples. Prose, previews, hashes, diagnostics, and input order are excluded. `snapshotId`, result, and `reasonCode` remain recorded fields. Persist all candidates and decisions, including dedup-suppressed candidates. Only `DedupResult.renderableClaims` proceeds to rendering; suppression never changes status or erases proof.

P0 claim templates are a pure allowlist in `src/repo2skill/claim_templates.py`, shared by Task 6 dedup and Task 7 rendering. Structured payloads use versioned template keys; unknown keys or unsupported typed payloads fail closed. Both consumers use the same formatter text. Dedup collapses whitespace for exact line matching, groups identical candidate text, and compares it with normalized documentation facts. A documentation match suppresses rendering while preserving every candidate and documentation evidence ID in its decision; identical candidate text without a documentation match keeps one renderable representative and records the complete candidate proof group.

## Task 7 — deterministic rendering and initial stage

Retain the positional `specs` parameter; pass policy and all rendering identity context explicitly:

```python
def render_targets(
    claims: Sequence[Claim],
    specs: Sequence[TargetSpec],
    *,
    policy: ClaimPolicy,
    project_id: str,
    id_context: CanonicalIdContext | None = None,
) -> RenderBundle: ...
```

The caller passes review-effective valid `renderableClaims`, `run.effectiveClaimPolicy`, the explicit project ID, an optional shared `CanonicalIdContext`, and an explicit subset of configured specs. `project_id` is required; rendering reads no project/store/cwd state. When `id_context` is omitted, rendering uses a fresh per-call context. Process claims in canonical design order, considering accepted claims first. For each target and each mode, `renderBudget` caps the sum of UTF-8 byte lengths of every output file, ordered by output repository path. Count templates, markers, labels, companions, and newlines. Accepted and preview budgets are independent. Preview starts with exactly accepted content that fit, then adds visibly labelled pending inferred claims in canonical order while space remains. Pending content never enters `relativeFiles`; rejected, contested, invalid, or dedup-suppressed claims enter neither mapping. Each omitted-for-budget claim has a stable diagnostic naming claim and target IDs.

A managed `agents-md` target has exactly one fixed `repository-guide` block. Its marker grammar is:

```text
<!-- repo2skill:begin project=<projectId> block=<blockId> -->
<!-- repo2skill:end project=<projectId> block=<blockId> -->
```

Derive `blockId` from the canonical `managed-block` identity containing exactly `projectId`, `targetId`, and fixed `sectionKey="repository-guide"`, allocated through the supplied collision context. The ID is independent of claims, revisions, ordering, and rendered bytes. Marker formatting/parsing are pure helpers reusable by ownership/export; malformed, duplicate, or mismatched pairs fail closed.

A `skill` target emits concise `SKILL.md` and deterministic `repo2skill-provenance.json`. The companion contains stable claim revision references plus their sorted fact/evidence IDs only; it carries no source blobs, run/stage IDs, timestamps, or self-hash. It is generated from the same accepted or preview selection as the content. `stage.json` remains authoritative for exact hashes and spans. The companion's bytes count toward the per-target accepted/preview budget, and its key is a full repository-relative output path.

`RenderedTarget.relativeFiles` / `exactHashes` / `claimSpans` are accepted-only canonical UTF-8/LF output; preview counterparts may contain visibly labelled pending content. File keys in every bytes/hash/spans triplet are normalized `/` paths relative to the repository root, including the full target output path (for example `AGENTS.md` or `.agents/skills/foo/SKILL.md`). They are never paths relative to the target directory. Spans use `<claimId>@<claimRevision>` and identify content in that exact file. Hashes are SHA-256 of exact bytes. Path rules are those of R6.

Before initial staging, the controller constructs the existing `RunLedger.stagedTargets` field from the bundle:

```text
targetId -> {
  "accepted": {full-output-repository-path: sha256},
  "preview":  {full-output-repository-path: sha256}
}
```

Use one entry for each selected target and both mode maps, permitting an empty map. The keys equal the full repository-relative output paths in `RenderedTarget`; they omit physical staging prefixes, target IDs, and mode names. This frozen field records initial revision 1 only. Later review stages never compare their new projections with it or alter the run.

Initial generation uses this API:

```python
def stage_run(
    bundle: RenderBundle,
    run: RunLedger,
    *,
    store: LedgerStore,
    current_identity: RepositoryIdentity,
    current_root_label: str,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[RepositoryPath],
    selected_specs: Sequence[TargetSpec],
) -> StagedRun: ...
```

The caller holds `store.mutation()` from authoritative reads through activation; the operation does not lock a second time. This is an initial-generation operation: under the lock, require that the new run ID/path is absent, rather than comparing the not-yet-persisted run with a nonexistent authoritative record. Verify that selected specs exactly match configured target entries by ID, bundle target IDs equal the selection, `run.stagedTargets` equals the bundle's accepted/preview path/hash projection, each hash/span mapping has matching keys, every span reference resolves to an exact revision in `run.claims`, and accepted spans reference only accepted render-selection claims. Verify output paths and enforce the effective run byte limit independently. Configuration validation and the stage publication boundary reject target outputs that escape, equal, contain, or are contained by `stateDir`; duplicate or overlapping output paths after canonical normalization also fail closed. Verify paths before writing immutable artifacts. Since `RenderBundle` stores no policy identity, the controller must pass `run.effectiveClaimPolicy` to rendering; stage cannot reconstruct the renderer policy or selected specs from the bundle.

Publish the immutable run and stage artifacts under temporary names and atomically rename them into place. The immutable stage revision starts at 1; never replace an existing revision. Runtime `StagedRun.stageRoot` is `<store.state_dir>/staging/<runId>/r1`. Place target bytes at `targets/<targetId>/<accepted|preview>/<full-output-repository-path>`; the full output path is preserved under the physical stage mode directory. `StagedRun.targetHashes` and `previewTargetHashes` map target IDs to full output repository-relative paths and hashes. Persisted stage locations are normalized repository-relative `/` paths beneath `stateDir`, including the configured `stateDir` prefix, as required by R6. Recheck immediately before changing the single `ProjectState` pointer; if activation fails, immutable run/stage artifacts may remain as inactive orphans.

Each immutable revision includes a versioned `stage.json` descriptor at `<store.state_dir>/staging/<runId>/r<stageRevision>/stage.json`. It contains no source blobs and carries the exact target specs plus accepted/preview paths, hashes, and claim spans for its stage files:

```json
{
  "schemaVersion": "1.0",
  "toolVersion": "<tool version>",
  "runId": "<run id>",
  "stageRevision": 1,
  "targets": {
    "<targetId>": {
      "spec": {"targetId": "...", "kind": "...", "livePath": "...", "ownershipMode": "...", "emptyBlockPolicy": "..."},
      "accepted": {"<full-output-repository-path>": {"sha256": "...", "claimSpans": {"<claimId>@<revision>": [[1, 2]]}}},
      "preview": {"<full-output-repository-path>": {"sha256": "...", "claimSpans": {"<claimId>@<revision>": [[1, 2]]}}}
    }
  }
}
```

Validate descriptor projections against staged bytes before activation. `ProjectState.currentRunId` plus `activeStageRevision` selects the immutable descriptor. Initial generation also checks its descriptor projections against `RunLedger.stagedTargets`.

Immediately before activation call the only `recheck_dependencies` implementation under the caller-held lock. Pass current identity, current root label, policy revision, extractor versions, store root, and the explicit exclusion set. Reject `stale` and `unknown`; do not accept a precomputed status or implement another freshness algorithm. R5 compares root label as described in the implementation contract. No generation or review staging writes live target bytes.

## Task 8 — review a persisted run

```python
def review_claims(
    state: ProjectState, run: RunLedger, events: Sequence[ReviewEvent],
    specs: Sequence[TargetSpec], *, store: LedgerStore,
    current_identity: RepositoryIdentity, current_root_label: str,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[RepositoryPath],
) -> tuple[ProjectState, StagedRun]: ...
```

`review_claims` reads authoritative state and run through the same locked store and rejects stale supplied records. The caller holds the mutation and supplies current identity, root label, policy revision, extractor versions, and exclusions explicitly; review invokes the shared dependency recheck immediately before pointer activation and performs no hidden project/config read or second freshness algorithm. Validate the complete event batch before publishing any change. Accept/reject apply only to pending exact claim revisions; contest may apply to pending or accepted revisions; reject/contest require reasons. The state records event history and accepted revisions. For rerendering, overlay state on valid run candidates by exact `(claimId, claimRevision)` transiently, then deduplicate/render using the run's effective claim policy and the explicit selected configured specs. Do not mutate the immutable run or compare a later stage against its initial `stagedTargets`.

Use the shared immutable stage publisher and descriptor reader through a separate review path, not a review flag on `stage_run`. Task 7 owns them in `src/repo2skill/stage.py`, separate from pure formatting in `render.py`. Minimal immutable-byte LedgerStore reads/writes are allowed because the existing immutable writer handles JSON only; choose the smallest operations that fit the current store. Do not introduce a general storage framework or domain fields. Allocate the next unused positive revision for the run, store its own `stage.json` and files, validate all descriptors/hashes, then update `ProjectState.reviewEvents`, `acceptedClaimRevisions`, and `activeStageRevision` in one pointer replacement. A failed validation/publish leaves the previous state pointer active. The new descriptor is authoritative for that revision's target specs, paths, hashes, and spans.

## Task 9 — export from the active immutable stage

The exact export APIs add explicit live-repository freshness context to R3:

```python
class ExportFreshnessError(RuntimeError):
    def __init__(self, result: DependencyRecheckResult) -> None: ...


def export_staged(
    run_id: RunId, targets: Sequence[TargetId],
    ownership: Mapping[TargetId, OwnershipState], *, store: LedgerStore,
    current_identity: RepositoryIdentity, current_root_label: str,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[RepositoryPath],
    delete_empty_owned_blocks: bool = False,
) -> ExportResult: ...

def resume_export(
    export_id: ExportId,
    ownership: Mapping[TargetId, OwnershipState], *, store: LedgerStore,
    current_identity: RepositoryIdentity, current_root_label: str,
    current_policy_revision: str,
    current_extractor_versions: Mapping[str, str],
    excluded_paths: Sequence[RepositoryPath],
) -> ExportResult: ...


def inspect_target_ownership(
    specs: Sequence[TargetSpec], *, store: LedgerStore,
) -> tuple[Finding, ...]: ...
```

The caller holds `store.mutation()` from authoritative reads through the commit or journal update. `store.root` supplies R5's `root`; all other freshness values are explicit. Resolve the current `ProjectState.currentRunId` and `activeStageRevision`, require the requested run to be current, then load that immutable run and exact `stage.json` descriptor. Check descriptor identity, full selected `TargetSpec` equality against current configuration, repository-relative paths, accepted-mode hashes, spans, and current acceptance via `ProjectState.acceptedClaimRevisions`. Preview files are never export input. `RunLedger.stagedTargets` is only the initial revision-1 projection; later review exports use the active descriptor. Reconstruct supplied `OwnershipState` from active manifest references and target manifests and require canonical equality. A selected target without prior history may be absent from the mapping; reject duplicate/unknown target IDs. Ownership and target validation are distinct from source freshness.

`inspect_target_ownership` is the separate read-only ownership seam for Task 10 `check` and `explain`. It creates neither the state directory nor a lock, accepts only selected specs equal to configured project targets, resolves the coherent active manifest references and target manifests, and applies the same managed-block versus whole-file ownership comparison as export. It returns ownership `Finding`s; a configured target with no prior ownership is clean and unowned. It does not recheck source dependencies, render, stage, or mutate records. The CLI composes its results with the shared R5 freshness result. Export retains its caller-locked preflight and race checks.

Immediately before the first live operation, call the one R5 `recheck_dependencies` with the run, `store.root`, current identity/root label/policy/extractor versions, and exclusions. If its status is `stale` or `unknown`, raise `ExportFreshnessError(result)` before writing a journal or touching live paths; CLI maps either to exit 4, never to ownership-conflict exit 5. Resume repeats this recheck before any further live operation. If the active manifest pointer already names this journal's exact completed manifest set, validate it and finalize the journal without replaying writes or requiring fresh source inputs.

Preflight every selected target and every intended mkdir/replace/delete before any live write. A conflict returns an `ExportResult(status="conflict")`, may save a proposed diff under staging, and creates no unresolved journal. For managed markers, require the current marker-inclusive block hash to match the prior `ManifestBlock`; a changed full-file hash caused only by outside-marker edits is allowed, and outside bytes are preserved byte-for-byte. A whole-file-owned Skill output requires the full prior file hash to match. Existing text files must use consistent LF or CRLF; mixed EOL conflicts. New files use LF. Convert only generated output to the existing style. `ManifestFile.exactHash` is the final full live-file hash; each `ManifestBlock.exactHash` is the exact marker-inclusive block bytes after EOL conversion. Claim spans are measured in final merged live files. The journal's `expectedOldHash` is the full current preflight file hash (or null if absent), used to detect changes between preflight and replacement; it is not the managed-block ownership check. `stagedNewHash` is the hash of final intended live bytes after merge/EOL conversion.

`delete_empty_owned_blocks` defaults false. When explicitly true, it overrides the frozen `TargetSpec.emptyBlockPolicy="keep"` for this invocation and may remove only empty owned markers while preserving the surrounding file; this AGENTS empty-block flag is unchanged. P0 has no producer or CLI flag for whole-file/Skill deletion intent, so omission of a prior-owned Skill file—including a budget-driven empty accepted map—is not explicit deletion intent and must produce a preflight conflict with no change. A future explicit deletion intent would still require prior whole-file ownership and a matching current full hash; never infer deletion from an unselected target. No directory cleanup is performed.

The persisted journal is `<store.state_dir>/exports/<exportId>.json` (its logical location includes the `stateDir` prefix under R6). Under mutation, the store enumerates journals and atomically creates/updates typed journals at this path. Every non-`committed` journal, including `prepared`, `incomplete`, or `conflict`, blocks every mutating operation except explicit resume of that journal. The minimal store/encoding surface is:

```python
def LedgerStore.list_export_journals(self) -> tuple[ExportJournal, ...]: ...
def LedgerStore.write_export_journal(self, journal: ExportJournal) -> None: ...
class ExportRecoveryRequired(RuntimeError): ...
def LedgerStore.require_no_unresolved_export(
    self, *, resume_export_id: ExportId | None = None,
) -> None: ...
def encode_record(record: object) -> JsonValue: ...
```

`list_export_journals` is read-only: it creates neither a lock nor state files, matching the existing `read_*` methods so doctor/check/report can inspect unresolved journals without mutation. A mutating caller must already hold `store.mutation()` when relying on the enumeration as authoritative; consistent snapshot/retry handling for concurrent read-only inspection belongs to that caller. `write_export_journal` requires the active mutation and atomically creates or replaces the journal at its ID-derived path. `encode_record` is pure and supplies typed immutable manifests/sets to the existing `write_immutable(relative_path, JsonValue)`. Do not add a general storage framework. Target manifests use the already-frozen `manifests/<exportId>/<targetId>.json`; the manifest set uses `manifests/<exportId>/<manifestSetId>.json`. A subset export copies each unselected `ManifestReference` unchanged into the new complete set.

`LedgerStore.require_no_unresolved_export` requires the caller-held mutation lock and uses the journal enumeration as authoritative. With no resume ID, any non-committed journal raises `ExportRecoveryRequired`; with a resume ID, only that exact journal may remain non-committed. A missing or committed selected journal is handled by the resume operation's normal lookup/validation. High-level mutating entry points call this guard before their first write; do not put it in `mutation()` or low-level write helpers, because resume must update its journal and an already-prepared export must finish its operations.

`stage_run`, `review_claims`, `export_staged`, and `resume_export` call the shared guard under the caller-held mutation before their first write. Resume passes its selected export ID. They must not change the `mutation()` signature or guard lower-level writes used after a prepared export has been authorized.

After successful preflight, derive deterministic operation IDs and persist final intended bytes at `<store.state_dir>/staging/exports/<exportId>/new/<operationId>` and exact old bytes for replace/delete backups under `.../backup/<operationId>` with `backupPath` storing its normalized repository-relative path; `stagedNewHash`/`backupHash` must verify those immutable payloads before live writes. Persist actual proposed diffs under `.../patch/<operationId>.diff` and store their root-relative paths in `proposedPatchPath`. New-payload paths are derived from journal IDs, so no journal field is added and resume never recomputes a merge from changed outside bytes.

After R5 returns fresh, write the complete `prepared` journal before any live mkdir, replacement, or deletion. Keep R4's exact deterministic `(targetId, path, action)` operation ordering; canonical parent paths sort before descendants. A missing parent shared by targets is assigned once to the first target in canonical `targetId` order. Journal `createdDirectories` and target-manifest directory records contain only paths confirmed created by this tool; carry forward previously owned target directories, add newly confirmed paths, and do not clean directories up. If resume sees a planned mkdir path already present without journal proof that this transaction created it, mark conflict and do not adopt it. Journal each successful operation atomically; a third hash becomes a conflict without touching that path. A mid-transaction conflict is unresolved and blocks other mutation until resume resolves it.

For file replacement/deletion, use a small guarded publication seam in `safe_fs` or a focused `output_fs` helper: root-relative component traversal with retained non-redirected ancestor guards, safe mkdir, same-directory temp+fsync+atomic replace, and owned-file deletion, each checking expected old state. The current `safe_fs` public surface only guards reads; do not use unchecked `Path.write_bytes` or `os.replace` for live targets. The operation log remains sorted by `(targetId, path, action)`; parent mkdir operations precede descendant paths by canonical path ordering.

Write immutable selected `TargetManifest`s and the complete `ManifestSet` only after every operation reaches its intended hash. Copy references for unselected targets unchanged. Replace `ProjectState.activeExportId` and `activeManifestSetId` once; this is the commit point. Then atomically mark the journal `committed`. If the process stops after pointer commit but before that status update, resume verifies the exact active manifest set and marks committed without replaying operations or checking source freshness. If interrupted earlier, resume accepts only each operation's recorded old or intended new state; a third hash remains untouched and conflicted.

## Task 10 — read-only check and explanation inputs

Task 10 reuses R5 `recheck_dependencies` through `check_freshness` and Task 9 `inspect_target_ownership`; it does not implement a second freshness or ownership algorithm. `check` combines dependency, review, coverage, selected-target ownership, and unresolved export-journal findings. Unresolved noncommitted journals are visible as incomplete/conflict state without creating a lock or state files; `report` may include the same read-only journal status. The CLI applies the existing exit priority to all findings.

Task 10 may add `LedgerStore.list_run_ids() -> tuple[RunId, ...]` as a guarded, read-only enumeration that creates no lock/state files. Validate each path against its decoded run ID and fail closed if enumerated history cannot be read or validated; do not silently omit a corrupt run. Reuse the history enumerator already owned by Task 11 if available. Generation/history mutations call it under the existing mutation lock; read-only commands do not acquire or create a lock.

Keep `LedgerStore.snapshot()` active-only and keep `LedgerStoreSnapshot` and `Explanation` domain fields unchanged. The existing snapshot contains the active run, active manifest set, and active stage location only; it does not load historical runs, stage descriptors, or target manifests. The CLI builds a selected snapshot's existing `runs` mapping from the requested/history run records, and supplies only validated stage descriptors and target manifests to the pure resolver:

```python
def explain_claim(
    claim_id: ClaimId, store: LedgerStoreSnapshot,
    revision: int | None = None, run_id: RunId | None = None, *,
    staged_descriptors: Mapping[tuple[RunId, int], Mapping[str, object]],
    target_manifests: Sequence[TargetManifest],
) -> Explanation: ...
```

The resolver uses `RunLedger` claims, facts, evidence, and dedup decisions for recorded provenance. A candidate has at most one matching dedup decision per run; multiple matches are corrupt provenance and fail closed rather than changing `Explanation.dedupDecision`. Resolve stage locations and claim spans only from validated descriptors for relevant existing revisions (initial r1, current active revision, or revisions referenced by manifests); a path by itself is not proof that stage bytes are valid. Resolve live locations from supplied target-manifest claim spans and blocks; these are recorded historical locations, not proof that current live bytes still match ownership. The separate check path reports current ownership.

For `--run`, resolve only the exact named run; an omitted revision selects that claim's highest revision in the run. Without `--run`, prefer the current run when it contains the exact requested `(claimId, revision)` pair. Otherwise, if revision is omitted, choose the highest claim revision across matching valid history; break remaining ties by most recent `SourceSnapshot.createdAt`, then `runId`. With an explicit revision, use the matching pair and the same tie-break. A missing pair is not found; corrupt references, duplicate canonical records, or broken lineage fail closed as state corruption. `explain_claim` resolves recorded provenance only: no raw-source reads, network, renderer, mutation, or R5 recheck merely to enrich its answer. The CLI may run `check_freshness` separately and include the resulting stale finding for the requested claim.

## Task 11 — CLI orchestration and state transitions

Keep argparse grammar, JSON formatting, and command-to-exit mapping in `cli.py`; use one focused `pipeline.py` for shared scan/generate orchestration and construction of immutable run records. No provider, AI, or arbitrary repository-command execution is introduced by this task. The existing R7 JSON envelope and exit priority remain unchanged; map Task 11 outcomes to the established statuses/codes.

Task 11 wires all nine commands, including read-only assembly. The single authoritative project document is `<stateDir>/project.json`; `--config` must resolve to that document, and explicit `init --config` must agree with `--state-dir`. With a custom init state directory and no explicit config, create its `project.json`, which later commands select through `--config`. P1 Codex grammar is recognized but explicit `--ai codex`/`doctor --codex` return unavailable exit 6 before mutation; invalid Codex combinations return invocation exit 1. P1 `--measure` is recognized but unavailable in P0 and returns invocation exit 1 before mutation, without writing measurements.

Every mutating controller acquires the existing `store.mutation()` lock, then calls `store.require_no_unresolved_export()` before its first write. This includes init, scan activation, generation/staging, review, and export. `export --resume ID` calls the same guard with `resume_export_id=ID`, allowing only that journal to remain unresolved. Map `ExportRecoveryRequired` to exit 7. Do not change `LedgerStore.mutation()` or guard low-level operations inside an already-authorized prepared export; those operations must update/finish the selected journal.

A successful, usable `scan` creates and persists an immutable completed `RunLedger` with no claims or staged targets, performs the shared R5 recheck, then activates it with one `ProjectState` pointer replacement: set `currentRunId` to the new run and `activeStageRevision` to null. Preserve review history, accepted-revision history, and active export/manifest pointers. Scan does not derive claims, render, or create a stage. A failed or unusable scan never advances the pointer; a required-input/global-cap failure leaves prior state untouched. `--strict` makes any coverage gap an exit-2/no-activation condition, but is an invocation criterion, not a new effective-policy input or hashed option. An inactive immutable run may remain for reporting/history after a late R5 failure; stale/unknown R5 maps to exit 4.

`generate --from-run ID` accepts only a compatible completed scan with empty `claims` and `stagedTargets`. It reuses that run's frozen snapshot, reads, evidence, and facts, but derives and stages under a fresh new `runId` and absent immutable run path; never pass the source run ID to `stage_run`. Check compatibility and run the shared R5 recheck before deriving any claim, then perform the activation-time R5 recheck through `stage_run`. For a fresh scan, collect inputs and build the run before deriving and staging. The effective policy revision always uses every configured target; CLI-selected render targets do not change it.

For each scan/generation, reconstruct high-water Evidence, Fact, and Claim history from all valid immutable runs for the same repository, including inactive orphans. Enumerate run IDs under the existing mutation lock using Task 10's read-only `LedgerStore.list_run_ids()`; validate each ID/path and fail closed on any enumerated unreadable/invalid record. Pass prior Evidence/Fact records to `ExtractionSession` and prior Claims to `derive_claims`, using `store.id_context`; add no builder parameters, persisted high-water index, or domain fields.

The pure review overlay seam is:

```python
def apply_review_dispositions(
    claims: Sequence[Claim], state: ProjectState,
) -> tuple[Claim, ...]: ...
```

The caller passes policy-validated claims only. Apply `ProjectState.acceptedClaimRevisions` and review events by exact `(claimId, claimRevision)` and return transient claims with effective `proposalStatus`; preserve `kind` and `origin`. Reuse this helper from the later-review path in `claims.py`. Initial generation overlays the valid candidates, merges those effective statuses back into the complete candidate set for the new immutable run, and leaves invalid candidates pending. Deduplicate/render only the effective valid claims. Thus accepted-but-dedup-suppressed candidates retain accepted status in `RunLedger.claims`, while invalid candidates cannot be accepted; no event or claim creator or unrestricted bulk-accept behavior is added.

Activation freshness errors are distinct from argument/record validation errors:

```python
class ActivationFreshnessError(ValueError):
    def __init__(self, result: DependencyRecheckResult) -> None: ...
```

`stage_run` and `review_claims` raise this error with the exact R5 result for stale or unknown final checks, rather than generic `ValueError`. CLI catches it before generic `ValueError`, maps it to exit 4, and includes the carried findings. There remains one R5 implementation and no duplicate preflight recheck at the CLI boundary. `ExportFreshnessError` remains Task 9's separate public runtime error with the same exit 4 mapping.
