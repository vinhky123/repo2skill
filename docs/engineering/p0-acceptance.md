# P0 release evidence map

**Status:** Task 12 evidence map, updated after the Windows/Python 3.13 local alpha verification. P0-01–P0-11 ownership and review evidence below is retained from their task reports; the Task 12 full-suite evidence was recorded against base `fccc9f4`; the final review-fix tree has a strengthened integration run (1 passed in 12.72s), distribution/archive run (2 passed in 6.99s), final wheel/sdist build, and fresh-wheel smoke. No full-suite rerun is claimed for these scoped test/doc changes. Task 12 independent and final project reviews, plus all CI matrix cells, remain pending.
Feature ownership follows `docs/product/feature-catalog.md` P0 F001–F035 and the task briefs. Each feature keeps its owning test paths; current-checkout coverage was exercised by Task 12 full suite (258 passed, 1 skipped in 85.90s) before final integration and sdist-content refinements. The strengthened integration test and distribution/archive tests passed separately afterward.

Local verification host: Windows/Python 3.13. No Linux/macOS or Python 3.11 result is claimed. The six-cell workflow is configured; every matrix result remains pending until CI executes it.

## Feature ownership and evidence

| Feature | Owner(s) | Existing evidence / status |
|---|---|---|
| F001 | Task 1; Task 12 release replay | `tests/test_distribution.py`; Task 1 root verification on `466645e`; Final Task 12 fix-round-1 wheel/sdist build and fresh-wheel smoke passed on Windows/Python 3.13. Smoke verified import under the clean venv, templates, help/version, and byte-identical MIT notice. |
| F002 | Task 1 config; Task 11 CLI | `tests/test_config.py`; Task 1 review-clean commit `466645e` covers path/config boundary. Task 11 CLI grammar and dispatch are implemented and covered by `tests/test_cli.py`, `tests/test_cli_json.py`, and `tests/test_cli_lifecycle.py`; full Task 12 suite passed 258/1. |
| F003 | Task 3 | `tests/test_inventory.py`; Task 3 review-clean through `fd6f973`; full suite 88 passed/1 skipped before final fix, latest affected gate 40 passed plus three final affected tests. |
| F004 | Task 2 schema; Task 3 identity/inventory | `tests/test_models.py`, `tests/test_identity.py`, `tests/test_inventory.py`; Task 3 commit `fd6f973`, review clean; full suite 88/1 predates last fix. |
| F005 | Task 2 ledger records; Task 4 dependency tracking; Task 10 check | `tests/test_ledger_transactions.py`, `tests/test_dependencies.py`; Task 4 complete through `322568e` with 13 latest affected passes (107/1 baseline predates review fixes). Task 10 CLI check is covered by `tests/test_check.py` and Task 11 lifecycle tests; Task 12 full suite passed 258/1. |
| F006 | Task 2 schema; Task 4 extraction; Task 5 ecosystem provenance | `tests/extractors/test_base.py`, `tests/extractors/test_docs.py`, ecosystem extractor tests; Task 5 complete through `2ffd0be`, 134/1 baseline and 10 latest affected passes (no full-suite rerun on final fix). |
| F007 | Task 2 schema; Task 4/5 fact extraction | `tests/extractors/test_generic.py`, `test_docs.py`, `test_javascript.py`, `test_python.py`, `test_go.py`, `test_rust.py`; Task 5 report records 134 passed/1 skipped baseline; latest affected gate 10 passed. |
| F008 | Task 2 model; Task 6 validation | `tests/test_validation.py`, `tests/test_claims.py`; Task 6 complete through `5f076da`; 159/1 baseline plus 40 covering passes after scoped fixes. |
| F009 | Task 2 IDs/lineage schema; Task 6 revisioning | `tests/test_ids.py`, `tests/test_claims.py`; Task 6 review clean through `5f076da`, with 40 covering passes after history/collision fixes. |
| F010 | Task 4 extractor registry/coverage | `tests/extractors/test_registry.py`, `test_parsing.py`, `test_generic.py`, `tests/test_dependencies.py`; Task 4 complete through `322568e`; final duplicate-preflight/dependency gate 13 passed. |
| F011 | Task 5 JavaScript/TypeScript | `tests/extractors/test_javascript.py`, `test_mixed.py`; Task 5 report: 134/1 baseline, 10 latest affected passes; final independent review clean. |
| F012 | Task 5 Python | `tests/extractors/test_python.py`, `test_mixed.py`; same Task 5 evidence: 134/1 baseline, 10 latest affected passes; final independent review clean. |
| F013 | Task 5 Go | `tests/extractors/test_go.py`, `test_mixed.py`; same Task 5 evidence: 134/1 baseline, 10 latest affected passes; final independent review clean. |
| F014 | Task 5 Rust | `tests/extractors/test_rust.py`, `test_mixed.py`; same Task 5 evidence: 134/1 baseline, 10 latest affected passes; final independent review clean. |
| F015 | Task 4 generic/docs; Task 5 generic-only ecosystem coverage | `tests/extractors/test_generic.py`, `test_docs.py`, `test_mixed.py`; Task 4 final affected 13-pass gate and Task 5 final review clean. |
| F016 | Task 4 coverage ledger; Task 5 ecosystem status | `tests/test_dependencies.py`, `tests/extractors/test_registry.py`, `test_generic.py`, ecosystem tests; Task 4 13 latest affected passes; Task 5 10 latest affected passes. |
| F017 | Task 6 deterministic candidate generation | `tests/test_claims.py`, `tests/test_validation.py`; Task 6 40 covering passes, review clean through `5f076da`. |
| F018 | Task 6 support-chain validation | `tests/test_validation.py`, `tests/test_claims.py`; Task 6 40 covering passes after fixes, review clean. |
| F019 | Task 6 proof-preserving dedup; Task 10 explain integration | `tests/test_dedup.py`, `tests/test_claims.py`; Task 6 40 covering passes. Task 10/11 CLI explanation is covered by `tests/test_explain.py` and Task 11 lifecycle tests; Task 12 full suite passed 258/1. |
| F020 | Task 6 claim templates; Task 7 render | `tests/test_claims.py`, `tests/test_dedup.py`, `tests/test_render.py`; Task 7 final implementation `82610aa` full suite 176/1, then post-gate `cde4a71` 34 covering passes and independent review clean. |
| F021 | Task 7 AGENTS staging | `tests/test_render.py`, `tests/test_stage_transaction.py`; Task 7 baseline full 176/1 and post-gate 34 covering passes; final review clean. |
| F022 | Task 7 Skill/provenance staging | `tests/test_render.py`, `tests/test_stage_transaction.py`; same Task 7 evidence; output path/hash/span and empty-map boundaries covered. |
| F023 | Task 8 review lifecycle | `tests/test_review.py`, `tests/test_ledger_transactions.py`; Task 8 commit `b2d1780`, independent review clean, focused 6 and persistence combined 25 passes; full suite 182/1. |
| F024 | Task 9 managed-block ownership | `tests/test_ownership.py`, `tests/test_output_fs.py`; Task 9 independent review clean through `6f569da`; final integration gate 36 passed. |
| F025 | Task 9 explicit export/journal recovery | `tests/test_export_recovery.py`, `tests/test_ownership.py`; Task 9 final integration gate 36 passed; baseline full suite 202/1 predates narrow final safety fixes. |
| F026 | Task 9 manual-edit conflict/recovery | `tests/test_export_recovery.py`, `tests/test_ownership.py`; same Task 9 final gate 36 passed and independent review clean through `6f569da`. |
| F027 | Task 10 read-only check | `tests/test_check.py`, `tests/test_cli_lifecycle.py`; Task 10 and Task 11 review-clean implementation; Task 12 full suite 258 passed, 1 skipped. |
| F028 | Task 10 negative/glob wake-up check | `tests/test_check.py`, `tests/test_dependencies.py`, `tests/test_cli_lifecycle.py`; final local full suite 258/1. |
| F029 | Task 10 explanation | `tests/test_explain.py`, `tests/test_cli_lifecycle.py`; Task 10 read-only history/stage/manifest resolution and Task 11 assembly covered by the final local full suite (258/1). |
| F030 | Task 11 versioned CLI JSON | `tests/test_cli.py`, `tests/test_cli_json.py`; Task 11 implementation at `fccc9f4`, round-2 review clean; Task 12 full suite 258/1. |
| F031 | Task 11 report/exit aggregation | `tests/test_cli_json.py`, `tests/test_cli_lifecycle.py`; Task 11 implementation at `fccc9f4`, round-2 review clean; Task 12 full suite 258/1. |
| F032 | Task 3 untrusted traversal; Task 4 bounded redaction | `tests/test_inventory.py`, `tests/test_redact.py`; Task 4 report has 107/1 baseline, final scoped gates 13 and 30 passed; Task 3 review clean through `fd6f973`. |
| F033 | Task 2 persistence invariant; Task 9 transaction/recovery | `tests/test_ledger_transactions.py`, `tests/test_export_recovery.py`; Task 2 review clean through `c0244ca`; Task 9 final 36-pass integration gate, independent review clean. |
| F034 | Task 5 ecosystem fixtures; Task 12 alpha update E2E | Existing `tests/fixtures/ecosystems/` plus ecosystem extractor tests support JavaScript/Python/Go/Rust and mixed inputs. `tests/fixtures/alpha-mixed/`, `tests/integration/test_no_ai_alpha.py`; final strengthened focused characterization passed (1 in 12.72s) and compared every reported staged/live span to expected claim content, physical stage descriptors and manifests, export revisions, and hashes of raw stage/live bytes across initial, changed, and added claims including CRLF. It also verifies deletion from both outputs and provenance while other accepted claims remain, conflict/read-only behavior, and the synthetic script marker stays absent. |
| F035 | Task 12 contributor/install/threat/schema/release docs and CI evidence | `CONTRIBUTING.md`, `docs/install.md`, `docs/threat-model.md`, `docs/schema-migrations.md`, `docs/release-checklist.md`, `.github/workflows/ci.yml`; six CI cells configured and pending. Windows/Python 3.13 docs, wheel/sdist smoke, MIT notice and local verification evidence recorded. Task 12 independent review remains pending. |

## Remaining release gates

- P0 F001–F035 all have an owning task and evidence path in the table. Task 12 local verification and fix round 1 are complete; scoped Task 12 re-review and final project review remain pending.
- Windows/Linux/macOS × Python 3.11/3.13 is configured, but no workflow result is recorded yet; do not claim those cells verified.
- The alpha is local and unpublished at version `0.1.0`.
