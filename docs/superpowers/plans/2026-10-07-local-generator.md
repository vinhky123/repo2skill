# Local No-AI Generator Implementation Plan

During execution, read the [implementation contract](../../engineering/implementation-contract.md) alongside the spec. Its exact record types, explicit store context, policy snapshot fields and corrected signatures supersede the original task-interface sketches below.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Status:** P0-01–P0-11 implementation is complete and their scoped reviews are recorded in task reports. Task 11 final code is at `fccc9f4` after clean round-2 review. Task 12 local verification on Windows/Python 3.13 passed: the full suite passed 258/1 before the final test refinements; the strengthened focused integration test passed 1, distribution tests passed 2, and the final wheel/sdist build and fresh external-cwd wheel smoke passed. The test now compares every reported stage/live span and its content, descriptor/manifest references, and raw-byte hashes. The six Windows/Linux/macOS × Python 3.11/3.13 CI cells remain pending. Task 12 independent re-review and final project review are pending; package version `0.1.0` remains unpublished.

**Goal:** Ship a useful local-only alpha that scans a repository, creates traceable claims, stages `AGENTS.md` and a portable Skill, reviews/exports owned content safely, and detects/explains drift without AI or network access.

**Architecture:** Keep a small Python package with frozen typed records and functional boundaries. A random snapshot ID is allocated before reads; an immutable run and stage are activated by one state pointer, and export uses per-file atomic replacement plus a recoverable journal and immutable manifest set.

**Tech Stack:** Python 3.11+, standard library, `pathspec` for compatible ignore matching, PyYAML for bounded YAML parsing, pytest; Windows/Linux/macOS CI on Python 3.11 and 3.13.

**Spec:** `docs/superpowers/specs/2026-10-07-repo2skill-mvp-design.md`; CLI: `docs/engineering/cli-contract.md`; catalog: `docs/product/feature-catalog.md`.

## Global Constraints

- Adapt only selected MIT material from `osmaneb23/agents-md@346b5f78dcbb48d9a700df98ac7f5f8e02f751ea`; preserve notice and source mapping.
- `generate` defaults to no-AI and no network; never execute repository commands or trust repository text as instructions.
- P0 supports shallow JS/TS, Python, Go, Rust plus generic config/CI; unsupported coverage must remain visible.
- Use canonical UTF-8/LF staging, raw-byte SHA-256 source hashes, normalized `/` repository paths, and exact exported-byte hashes.
- Never follow symlinks; reject root escape/case collision; caps default to 100,000 inventory entries, 5,000 opened files, 2 MiB/file, 64 MiB decoded input, and 200 KiB snippets/run.
- Candidate, accepted, staged, and live states remain distinct. Immutable artifacts become active only by one atomic `state.json` pointer replacement.
- Multi-file export is not globally atomic. Only explicit `export --resume EXPORT_ID` may recover an incomplete journal, and a third hash is preserved as a conflict.

## Review Focus

- Explicit exact-file include may override normal ignore, while an explicit exclude always wins; test in Task 3.
- Snapshot ID must exist before any read while `inputDigest` includes all final read outcomes; test in Task 4.
- A render failure during review must leave both review events and active stage unchanged; test in Task 8.
- `generate --from-run` must reject a stale/incompatible scan before deriving claims; test in Task 11.
- Export interruption followed by a manual third hash must preserve the edit and return conflict; test in Task 9.

---

### Task P0-01: Package, policy, and reuse boundary

**Feature IDs:** F001, F002

**Dependencies:** None.

**Files:**
- Create: `pyproject.toml`, `src/repo2skill/__init__.py`, `src/repo2skill/__main__.py`, `src/repo2skill/cli.py`, `src/repo2skill/config.py`, `src/repo2skill/models.py`, `THIRD_PARTY_NOTICES`, `docs/engineering/upstream-source-map.md`
- Test: `tests/test_config.py`, `tests/test_distribution.py`

**Interfaces:**
- Consumes: canonical `ScanPolicy`, `ClaimPolicy`, `TargetSpec` field sets from the spec.
- Produces: `ScanPolicy`, `ClaimPolicy`, `TargetSpec`; package entry point; `resolve_under(root: Path, candidate: Path) -> Path`; `default_scan_policy() -> ScanPolicy`; `default_targets(root: Path) -> tuple[TargetSpec, ...]`; versioned defaults and dependency records.

- [x] **Red:** Test that defaults create both logical targets under root, reject `../outside`, and built wheel contains `THIRD_PARTY_NOTICES`.
  ```python
  with pytest.raises(PathBoundaryError):
      resolve_under(root, Path("../outside"))
  assert "agents-md" in {target.targetId for target in default_targets(root)}
  ```
- [x] Run `python -m pytest tests/test_config.py tests/test_distribution.py -v`; expect FAIL because the package/config do not exist.
- [x] **Green:** Add the minimal package, dependency bounds, config defaults, notice, and file/rule reuse map; do not port code without an entry in the map.
- [x] Run the same command plus `python -m build`; expect PASS and wheel/sdist containing the notice.
- [x] Review F001/F002, then commit `build: establish package and reuse boundary`.

### Task P0-02: Canonical records, IDs, and versioned ledger

**Feature IDs:** F005, F006, F007, F008, F009, F033 (persistence invariant)

**Dependencies:** P0-01.

**Files:**
- Create: `src/repo2skill/ids.py`, `src/repo2skill/ledger.py`
- Modify: `src/repo2skill/models.py`
- Test: `tests/test_models.py`, `tests/test_ids.py`, `tests/test_ledger_transactions.py`

**Interfaces:**
- Consumes: all exact frozen dataclass fields and aliases in the spec, including `RunLedger`, `ProjectState`, `ExtractionRead`, `DedupDecision`, and `Sha256`.
- Produces: `canonical_record_id(prefix: str, identity: JsonValue) -> str`; `write_immutable_document(path: Path, document: JsonValue) -> None`; `replace_state(path: Path, state: ProjectState) -> None`; `acquire_project_lock(state_dir: Path) -> AbstractContextManager[None]`; stable revisions/lineage and additive-field-preserving JSON reads.

- [ ] **Red:** Assert prose-only changes keep `claimId`/revision, semantic payload changes increment revision, unknown major schema fails, crash before pointer replacement leaves old state active, and two writers prove the OS-owned exclusive mutation/recovery lock returns busy without losing a state-pointer update.
- [ ] Run `python -m pytest tests/test_models.py tests/test_ids.py tests/test_ledger_transactions.py -v`; expect FAIL on missing records.
- [ ] **Green:** Implement canonical serialization, collision checking, immutable writes, fsync/atomic pointer replacement, and one fail-fast OS-owned project-state lock shared by every mutation and recovery path.
- [ ] Rerun tests; expect PASS, including orphan artifact ignored after simulated interruption.
- [ ] Review type/field equality against the spec, then commit `feat: add typed provenance ledger`.

### Task P0-03: Bounded inventory and repository identity

**Feature IDs:** F003, F004, F032 (traversal boundary)

**Dependencies:** P0-01, P0-02.

**Files:**
- Create: `src/repo2skill/inventory.py`, `src/repo2skill/identity.py`
- Test: `tests/test_inventory.py`, `tests/test_identity.py`
- Create fixtures: `tests/fixtures/inventory/`

**Interfaces:**
- Consumes: `ScanPolicy`, `InventoryEntry`, `InventoryResult`, `RepositoryIdentity`.
- Produces: `collect_inventory(root: Path, policy: ScanPolicy) -> InventoryResult` and `capture_repository(root: Path, inventory: InventoryResult, git_mode: Literal["auto", "head", "worktree", "no-git"]) -> RepositoryIdentity`.

- [ ] **Red:** Cover deterministic ordering, exact include of ignored file, later exclude precedence, generated/state/live exclusions, symlink non-follow, caps, Windows-style separators, case collision, clean/dirty/no-Git and `head` refusal.
  ```python
  policy = dataclasses.replace(default_scan_policy(), include=("ignored.txt",))
  result = collect_inventory(repo, policy)
  assert tuple(entry.path for entry in result.entries) == ("ignored.txt", "src/app.py")
  ```
- [ ] Run `python -m pytest tests/test_inventory.py tests/test_identity.py -v`; expect FAIL.
- [ ] **Green:** Implement safe discovery and Git/no-Git identity without executing repository content.
- [ ] Rerun tests; expect PASS with a diagnostic for every cap/skip.
- [ ] Review platform path behavior, then commit `feat: collect bounded repository inventory`.

### Task P0-04: Extraction seam, dependency reads, generic coverage, and redaction

**Feature IDs:** F005, F006, F010, F015, F016, F032

**Dependencies:** P0-02, P0-03.

**Files:**
- Create: `src/repo2skill/redact.py`, `src/repo2skill/extractors/base.py`, `src/repo2skill/extractors/registry.py`, `src/repo2skill/extractors/generic.py`, `src/repo2skill/extractors/docs.py`
- Test: `tests/extractors/test_registry.py`, `tests/extractors/test_generic.py`, `tests/test_redact.py`

**Interfaces:**
- Consumes: `InputSelector`, `ExtractionContext`, `ReadResult`, `ExtractionRead`, `ExtractionBatch`.
- Produces: canonical `Extractor.discover(ctx) -> tuple[InputSelector, ...]`, `Extractor.extract(ctx, selectors) -> ExtractionBatch`, and `build_snapshot(snapshot_id: SnapshotId, identity: RepositoryIdentity, inventory: InventoryResult, extractor_versions: Mapping[str, str], reads: Sequence[ExtractionRead]) -> SourceSnapshot`.

- [ ] **Red:** Allocate `snapshotId`, then assert reads finalize before parsing and bytes hashed equal bytes parsed. Concurrent file, glob and negative changes—or repository identity, policy or extractor revision changes—during the full used-read-set recheck must exit stale 4 before activation and preserve prior state. Also cover generic JVM coverage, required parse failure, BaseLoader `on`/`off` and recursive aliases, and secret typed values.
- [ ] Run `python -m pytest tests/extractors tests/test_redact.py -v`; expect FAIL.
- [ ] **Green:** Make `ExtractionContext.read(InputSelector) -> ReadResult` the only I/O seam; hash and parse its same immutable bytes; freeze `inputDigest`, then recheck every used read plus identity/policy/extractor versions before pointer activation. Use BaseLoader plus selected-key conversion and recursive-alias rejection.
- [ ] Rerun tests; expect PASS and identical digest for canonically identical read sets.
- [ ] Review no raw-blob persistence, then commit `feat: add provenance extraction seam`.

### Task P0-05: Four shallow ecosystem collectors

**Feature IDs:** F011, F012, F013, F014, F034 (ecosystem fixtures)

**Dependencies:** P0-04.

**Files:**
- Create: `src/repo2skill/extractors/javascript.py`, `src/repo2skill/extractors/python.py`, `src/repo2skill/extractors/go.py`, `src/repo2skill/extractors/rust.py`
- Test: `tests/extractors/test_javascript.py`, `test_python.py`, `test_go.py`, `test_rust.py`, `test_mixed.py`
- Create fixtures: `tests/fixtures/ecosystems/{javascript,python,go,rust,mixed}/`

**Interfaces:**
- Consumes: P0-04 `Extractor` and context/read contracts.
- Produces: registry entries whose batches contain typed Evidence/Fact/diagnostic/coverage records and never prose.

- [ ] **Red:** Assert literal package script is observed, prefixed invocation is deterministic-derived, Python/Go/Rust defaults are template or inferred, and a mixed repo retains commands from every ecosystem.
- [ ] Run `python -m pytest tests/extractors/test_javascript.py tests/extractors/test_python.py tests/extractors/test_go.py tests/extractors/test_rust.py tests/extractors/test_mixed.py -v`; expect FAIL.
- [ ] **Green:** Port only audited shallow rules with upstream mapping and local locators; record unsupported syntax instead of absence.
- [ ] Rerun tests; expect PASS for all five fixture groups.
- [ ] Review derivation labels and attribution, then commit `feat: add shallow ecosystem collectors`.

### Task P0-06: Deterministic claims, chain validation, and proof-preserving dedup

**Feature IDs:** F017–F020

**Dependencies:** P0-02, P0-05.

**Files:**
- Create: `src/repo2skill/claims.py`, `src/repo2skill/validation.py`, `src/repo2skill/dedup.py`
- Test: `tests/test_claims.py`, `tests/test_validation.py`, `tests/test_dedup.py`

**Interfaces:**
- Produces: `derive_claims(facts: Sequence[Fact], policy: ClaimPolicy) -> ClaimSet`; `validate_claims(claims: Sequence[Claim], run: RunLedger) -> ValidationResult`; `deduplicate(candidates: Sequence[Claim], docs: Sequence[Fact], snapshot: SourceSnapshot) -> DedupResult`.
- Consumes: exact P0-02 records and P0-05 facts.

- [ ] **Red:** Reject unknown/wrong-snapshot/wrong-scope evidence; reject observed claim supported by derived fact; keep suppressed candidate and documentation evidence; retain all language commands within budget.
- [ ] Run `python -m pytest tests/test_claims.py tests/test_validation.py tests/test_dedup.py -v`; expect FAIL.
- [ ] **Green:** Derive observed/inferred candidates deterministically, auto-accept only validated deterministic observed claims, and persist every `DedupDecision`.
- [ ] Rerun tests; expect PASS and zero network mocks invoked.
- [ ] Review canonical payload equality, then commit `feat: derive and validate traceable claims`.

### Task P0-07: Deterministic staging for AGENTS.md and Skill

**Feature IDs:** F021, F022

**Dependencies:** P0-06.

**Files:**
- Create: `src/repo2skill/render.py`, `src/repo2skill/templates/agents.md.tmpl`, `src/repo2skill/templates/skill.md.tmpl`
- Test: `tests/test_render.py`, `tests/test_stage_transaction.py`

**Interfaces:**
- Produces: `render_targets(claims: Sequence[Claim], specs: Sequence[TargetSpec]) -> RenderBundle`; `stage_run(bundle: RenderBundle, run: RunLedger) -> StagedRun`.
- Consumes: validated/deduplicated claims; emits `RenderedTarget`, exact hashes and claim spans.

- [ ] **Red:** Assert canonical UTF-8/LF bytes, stable ordering, visible pending inferred preview, accepted-only export payload, both targets, companion manifest, and no live write.
- [ ] Run `python -m pytest tests/test_render.py tests/test_stage_transaction.py -v`; expect FAIL.
- [ ] **Green:** Render concise targets, atomically publish immutable run/stage, then activate them with one state pointer.
- [ ] Rerun tests; expect PASS; injected render failure leaves prior pointer unchanged.
- [ ] Review span/hash determinism, then commit `feat: stage deterministic agent outputs`.

### Task P0-08: Claim review lifecycle

**Feature IDs:** F023

**Dependencies:** P0-07.

**Files:**
- Modify: `src/repo2skill/claims.py`, `src/repo2skill/ledger.py`, `src/repo2skill/render.py`
- Test: `tests/test_review.py`

**Interfaces:**
- Consumes: `ProjectState.reviewEvents`, accepted revisions and active stage revision.
- Produces: `review_claims(state: ProjectState, run: RunLedger, events: Sequence[ReviewEvent], specs: Sequence[TargetSpec]) -> ProjectState`; atomic accept/reject/contest transitions, constrained all-observed selection and immutable rerendered stage before activation. `ReviewEvent` uses the exact fields listed in the spec.

- [ ] **Red:** Assert batch rollback on one invalid claim, reject reason required, contest pending/accepted, no unrestricted all, and simulated rerender failure does not persist review events.
- [ ] Run `python -m pytest tests/test_review.py -v`; expect FAIL.
- [ ] **Green:** Append revision-scoped events, rerender first, validate hashes, then replace the single state pointer.
- [ ] Rerun tests; expect PASS and inferred claims absent from export payload until accepted.
- [ ] Review kind/origin immutability, then commit `feat: add atomic claim review`.

### Task P0-09: Managed ownership, export journal, and recovery

**Feature IDs:** F024–F026, F033

**Dependencies:** P0-07, P0-08.

**Files:**
- Create: `src/repo2skill/ownership.py`
- Test: `tests/test_ownership.py`, `tests/test_export_recovery.py`

**Interfaces:**
- Produces: `export_staged(run_id: RunId, targets: Sequence[TargetId], ownership: OwnershipState) -> ExportResult`; `resume_export(export_id: ExportId, ownership: OwnershipState) -> ExportResult`.
- Consumes: active immutable stage, prior manifest-set pointer, `OwnershipState`; creates journal, per-file atomic writes, immutable manifests, then one state-pointer commit.

- [ ] **Red:** Cover outside-marker preservation, marker/EOL conflicts, interruption/resume, third hashes, and subset export retaining unselected manifest references. Recheck the full used read set plus identity/policy/extractor versions before export; concurrent file/glob/negative change exits 4. Missing Skill parents require under-root non-symlink/non-reparse ancestors, owned-directory records, and redirected-ancestor rejection.
  ```python
  assert resume_export(export_id, ownership).status == "conflict"
  assert live.read_bytes() == manual_third_bytes
  ```
- [ ] Run `python -m pytest tests/test_ownership.py tests/test_export_recovery.py -v`; expect FAIL.
- [ ] **Green:** Preflight inputs/targets, safely create and record owned Skill directories, journal before writes, replace each file atomically, write immutable manifests, then commit only the state pointer.
- [ ] Rerun tests; expect PASS; mutating calls refuse while journal incomplete.
- [ ] Review recovery safety, then commit `feat: export owned content recoverably`.

### Task P0-10: Read-only drift check and complete explanation

**Feature IDs:** F027, F028, F029

**Dependencies:** P0-06, P0-09.

**Files:**
- Modify: `src/repo2skill/ledger.py`
- Create: `src/repo2skill/checks.py`
- Test: `tests/test_check.py`, `tests/test_explain.py`

**Interfaces:**
- Produces: `check_freshness(run: RunLedger, current_identity: RepositoryIdentity, current_inventory: InventoryResult, current_policy_revision: str, current_extractor_versions: Mapping[str, str]) -> CheckResult`; `explain_claim(claim_id: ClaimId, store: LedgerStoreSnapshot, revision: int | None = None, run_id: RunId | None = None) -> Explanation`.
- Consumes: file/glob/negative reads and a consistent `LedgerStoreSnapshot(project, state, runs, manifestSets, stageLocations)`.

- [ ] **Red:** Change/delete file and add matching glob/negative file; change matched documentation to invalidate its `DedupDecision`; change only a source convention file to reproduce the former incomplete-fingerprint bug; assert only dependent claims stale, state bytes unchanged, and suppressed/stale/superseded claims explain through hash+locator+span.
- [ ] Run `python -m pytest tests/test_check.py tests/test_explain.py -v`; expect FAIL.
- [ ] **Green:** Re-evaluate recorded selectors only; build effective findings without persisting status or reading source merely to enrich explanation.
- [ ] Rerun tests; expect PASS and no renderer/network invocation.
- [ ] Review negative-dependency wake-up, then commit `feat: check and explain provenance drift`.

### Task P0-11: Canonical CLI, JSON, reports, and exit precedence

**Feature IDs:** F002, F030, F031

**Dependencies:** P0-03–P0-10.

**Files:**
- Modify: `src/repo2skill/cli.py`, `src/repo2skill/__main__.py`, `src/repo2skill/config.py`
- Test: `tests/test_cli.py`, `tests/test_cli_json.py`, `tests/test_cli_lifecycle.py`

**Interfaces:**
- Consumes: all canonical functions above.
- Produces: exact `init/scan/generate/review/check/explain/report/export/doctor` grammar and exit priority `7 > 5 > 6 > 2 > 4 > 3 > 1 > 0` from the CLI contract.

- [ ] **Red:** Test JSON is one versioned object, diagnostics do not corrupt stdout, no-AI default, exact review grammar, `--from-run` freshness/compatibility rejection, pending export semantics, exit aggregation, and busy mutation/recovery lock exits 1 without lost state.
- [ ] Run `python -m pytest tests/test_cli.py tests/test_cli_json.py tests/test_cli_lifecycle.py -v`; expect FAIL.
- [ ] **Green:** Wire argparse dispatch in the single `cli.py`; keep read-only/mutating boundaries and block mutation during incomplete export.
- [ ] Rerun tests; expect PASS, including stale `--from-run` rejected before claim derivation.
- [ ] Review every command against `cli-contract.md`, then commit `feat: expose stable repository CLI`.

### Task P0-12: Alpha fixtures, packaging, CI, and maintainer docs

**Feature IDs:** F034, F035; release verification for F001, F002, F003, F004, F005, F006, F007, F008, F009, F010, F011, F012, F013, F014, F015, F016, F017, F018, F019, F020, F021, F022, F023, F024, F025, F026, F027, F028, F029, F030, F031, F032, F033

**Dependencies:** P0-01–P0-11.

**Files:**
- Create: `tests/integration/test_no_ai_alpha.py`, `tests/fixtures/alpha-mixed/`, `.github/workflows/ci.yml`, `CONTRIBUTING.md`, `docs/install.md`, `docs/threat-model.md`, `docs/schema-migrations.md`, `docs/release-checklist.md`
- Modify: `docs/engineering/upstream-source-map.md`; modify `pyproject.toml` only if release verification identifies a required packaging-metadata correction.

**Interfaces:**
- Consumes: the complete public CLI contract.
- Produces: installable alpha locally verified on Windows/Python 3.13, a configured but still-pending Windows/Linux/macOS × Python 3.11/3.13 CI matrix, and a maintained release/contribution process.

- [x] **Characterization:** Add an end-to-end test proving source hash+locator → fact → claim/revision → both staged targets → export span survives update, CRLF, conflict and add/change/delete. The successful run was green; no product defect was exposed.
- [x] Run `.venv\Scripts\python.exe -m pytest tests/integration/test_no_ai_alpha.py -v`; final strengthened test 1 passed in 12.72s on Windows/Python 3.13.
- [x] **Green:** Add CI matrix, clean-environment wheel smoke test, notice assertion and concise setup/threat/schema/release docs; no prose-only test was added.
- [x] Run `.venv\Scripts\python.exe -m pytest -q` (258 passed, 1 skipped), `.venv\Scripts\python.exe -m build` (wheel and sdist), fresh wheel smoke, and installed `repo2skill --help`/`--version`; local checks passed on Windows/Python 3.13.
- [x] Self-review F001–F035 ownership and evidence; the alpha verification commit is followed by review fix round 1, with scoped re-review and final project review pending.

P0-01 through P0-11 are implemented and independently reviewed as recorded in their task reports. P0-12 local implementation verification is complete; its review and cross-platform CI matrix remain pending. Keep configured-but-unrun cells unchecked and do not claim publication.
