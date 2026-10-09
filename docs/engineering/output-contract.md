# P0 output and claim lifecycle contract

**Status:** normative supplement for P0-06 through P0-09. This document supplements `docs/engineering/implementation-contract.md`, `docs/engineering/input-contract.md`, and the MVP design. Where its corrected signatures or lifecycle rules differ from older task briefs/design sketches, this contract governs. It retains the frozen persisted dataclass fields; the `stage.json` descriptor is a versioned immutable stage artifact, not a new field on `RunLedger`, `ProjectState`, or `StagedRun`.

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

Retain the positional `specs` parameter and add policy explicitly:

```python
def render_targets(
    claims: Sequence[Claim],
    specs: Sequence[TargetSpec],
    *,
    policy: ClaimPolicy,
) -> RenderBundle: ...
```

The caller passes review-effective valid `renderableClaims`, `run.effectiveClaimPolicy`, and an explicit subset of configured specs. Process claims in canonical design order, considering accepted claims first. For each target and each mode, `renderBudget` caps the sum of UTF-8 byte lengths of every output file, ordered by output repository path. Count templates, markers, labels, companions, and newlines. Accepted and preview budgets are independent. Preview starts with exactly accepted content that fit, then adds visibly labelled pending inferred claims in canonical order while space remains. Pending content never enters `relativeFiles`; rejected, contested, invalid, or dedup-suppressed claims enter neither mapping. Each omitted-for-budget claim has a stable diagnostic naming claim and target IDs.

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

The caller holds `store.mutation()` from authoritative reads through activation; the operation does not lock a second time. This is an initial-generation operation: under the lock, require that the new run ID/path is absent, rather than comparing the not-yet-persisted run with a nonexistent authoritative record. Verify that selected specs exactly match configured target entries by ID, bundle target IDs equal the selection, `run.stagedTargets` equals the bundle's accepted/preview path/hash projection, each hash/span mapping has matching keys, every span reference resolves to an exact revision in `run.claims`, and accepted spans reference only accepted render-selection claims. Verify output paths and enforce the effective run byte limit independently. Since `RenderBundle` stores no policy identity, the controller must pass `run.effectiveClaimPolicy` to rendering; stage cannot reconstruct the renderer policy or selected specs from the bundle.

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

`review_claims` reads authoritative state and run through the same locked store and rejects stale supplied records. Validate the complete event batch before publishing any change. Accept/reject apply only to pending exact claim revisions; contest may apply to pending or accepted revisions; reject/contest require reasons. The state records event history and accepted revisions. For rerendering, overlay state on valid run candidates by exact `(claimId, claimRevision)` transiently, then deduplicate/render using the run's effective claim policy and the explicit selected configured specs. Do not mutate the immutable run or compare a later stage against its initial `stagedTargets`.

Use the same immutable stage publisher through a separate review path, not a review flag on `stage_run`. Allocate the next unused positive revision for the run, store its own `stage.json` and files, validate all descriptors/hashes, then update `ProjectState.reviewEvents`, `acceptedClaimRevisions`, and `activeStageRevision` in one pointer replacement. A failed validation/publish leaves the previous state pointer active. The new descriptor is authoritative for that revision's target specs, paths, hashes, and spans.

## Task 9 — export from the active immutable stage

While holding the caller-owned mutation, export resolves `ProjectState.currentRunId` and `activeStageRevision`, loads that run and exact `stage.json`, and validates descriptor identity, selected target specs, stage file hashes, paths, and accepted claim spans. Export reads only accepted-mode files; preview files are never export input. The descriptor's target specs are checked against authoritative configured specs. Existing R4/R5 manifest, ownership, journal, recovery, and commit-point rules remain in force. Before the first live replacement, perform the same single dependency recheck required by R5; write the journal before mutation and change the active manifest pointer only after the immutable manifest set is complete. A conflict never authorizes overwriting a third live hash.
