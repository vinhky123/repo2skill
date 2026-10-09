# Final review and verification evidence

**Current disposition:** the whole-branch review identified four Important findings and no Critical findings. Product fixes are present in `d8d657c8ab2efb16505f25099ce556c162c68f4d`; focused and full verification passed. The scoped independent re-review of `68f30d6..78bd2c0` is clean: all four findings are addressed, none remain open, and no new Critical/Important breakage was found. The post-fix self-repository trial also completed successfully as a bounded local smoke: 10 candidates, 0 diagnostics, 24 unique coverage rows, matching pending-ID lists, export to both targets, and no ownership conflicts. Its size comparisons are internal before/after smoke observations, not benchmark evidence. Task 12 product, review, and trial gates are complete; the six hosted CI cells remain pending and version `0.1.0` remains unpublished.

**Fix-wave evidence:** focused regressions: 5 passed in 4.62s; affected-surface suite: 41 passed in 20.19s; full suite: 262 passed, 1 skipped in 96.35s. Wheel and sdist build plus fresh-wheel smoke exited 0 on Windows/Python 3.13. The wheel is `dist/final-fix1/repo2skill-0.1.0-py3-none-any.whl`, SHA-256 `586FD634884E0077051DDA1ADC06F5118D3090F42C85CF2885DB3F6EE10EE157`. The six hosted CI cells remain unexecuted; `0.1.0` remains local and unpublished.

The review covered bounded passes across CLI, extraction, validation, filesystem/ledger, export/recovery, review/check/explain/report, packaging/CI, and canonical contracts. It was not an exhaustive line-by-line audit. The reviewer's explicit scope limits, dispositions, four findings, and initial assessment are preserved below. The original final-fix report, scoped re-review, and post-fix trial are preserved byte-for-byte in [task-12-final-fix-report.md](task-12-final-fix-report.md), [task-12-final-fix-rereview.md](task-12-final-fix-rereview.md), and [task-12-manual-alpha-trial.md](task-12-manual-alpha-trial.md), retaining exact regression evidence, commands, results, review verdict, trial observations, and limitations before scratch cleanup. The review reports and trial record are also reproduced below for context.

## Initial whole-branch review (findings and dispositions preserved)

# Final whole-branch review

**Range:** `f0c60d852b02cc184b489ef98c9a628008dae4ec..68f30d602c16e6b89bbd5b61cb6d066ab695c90f`
**Review package:** `.superpowers/sdd/2026-10-07-local-generator/final-review-package-68f30d6.txt`
**Verdict:** Not ready to merge until the four Important findings below are fixed and scoped verification passes.

## Strengths

- The implementation separates candidate, reviewed, staged, and exported state, and routes mutations through the ledger/export boundaries. The single-pointer activation model and explicit interrupted-export journal behavior align with the contracts.
- File access, repository inventory, extraction, rendering, and export have dedicated modules. The contracts consistently keep repository text as data, avoid executing repository commands, and make the P0 no-AI/no-network path explicit.
- Provenance validation is strict about canonical IDs, sorted references, snapshot lineage, locators, and staged artifact hashes. The Python finding below is a producer/validator mismatch rather than a weak validator.
- Coverage and unsupported-source status are surfaced in CLI results rather than silently treated as complete support. The upstream source mapping and preserved notice make the limited MIT reuse auditable.
- The previous independent task gates were clean. Root reports the strengthened full suite at 258 passed and 1 skipped, plus the focused integration, distribution, build, and fresh-wheel checks. I relied on that evidence and did not rerun suites. The six hosted CI cells remain unexecuted, as the release checklist records.

## Issues

### Critical

None found in the inspected scope.

### Important

1. **Python single-test candidates are rejected by the canonical evidence-order check.** `src/repo2skill/extractors/python.py:196-202` emits the test-source evidence ID followed by the `pyproject.toml` evidence ID. `src/repo2skill/validation.py:194-197` requires `fact.evidenceIds` to be sorted and unique. In the supplied real-repository trial, the two evidence records existed in the same snapshot and supported the claim, but their IDs were in reverse lexical order; validation marked the claim invalid with `claim.invalid.fact-evidence-ids-invalid`. As a result, a supported pytest command is not reviewable/acceptable, and `check` derives pending IDs from valid claims only (`src/repo2skill/cli.py:589-592`) while `review list` displays every invalid claim with a pending fallback (`src/repo2skill/cli.py:519-523`). Sort/deduplicate evidence IDs at the producer boundary and retain a regression covering the claim and its review/check behavior.

2. **Documentation text-budget exhaustion creates unbounded duplicate coverage metadata.** `src/repo2skill/extractors/docs.py:38-52` appends a coverage entry for each non-empty line rejected by the snippet/fact budget. These rows contain the same selector, status, and reason and no line locator, so later rejected lines do not add distinct per-path information. The real-repository trial produced 1,571 coverage rows with only 24 unique full rows; the most repeated exact row appeared 210 times. A single `review show` serialized about 350 KB because it includes the complete coverage array, and `report --section summary` serialized about 901 KB with coverage and expanded findings. Full coverage fields in those responses are required by the output contract, but this per-line duplication defeats the documented input/snippet bounds: a file within the 2 MiB cap can have a very large number of short lines, each allocating another row and enlarging persisted state and output. Aggregate identical capped rows by selector/reason or retain a bounded count/summary while preserving useful path-level coverage visibility. Add a boundedness assertion for many rejected lines.

3. **Historical `explain --run` loses recorded live locations after a later export.** `src/repo2skill/cli.py:603-608` loads target manifests only through `_active_manifests_checked`, then passes those manifests to the history resolver. Trigger: export run A, later export run B so B replaces the active manifest pointer, then explain a claim from A. The result can omit A's recorded live locations even though A's immutable manifest still records them. The output contract says to resolve those locations from the supplied target-manifest claim spans and blocks, describing them as historical locations (`docs/engineering/output-contract.md:228-230`). Resolve and validate the manifests for the selected historical run/revision as well as the active set; this does not require claiming current live-byte ownership.

4. **An explicit historical `--run` can be mislabeled with the current run ID.** `src/repo2skill/cli.py:637-645` reconstructs the output run by matching only snapshot ID and claim pair, then prefers `state.currentRunId`. Trigger: repeat generation from the same snapshot such that runs A and B contain the same `(claimId, claimRevision)`, leave B current, then call `explain --run A`. The resolver selects A as required, but this adapter returns B's ID. That violates the output contract's exact named-run rule (`docs/engineering/output-contract.md:230`) and makes the envelope inconsistent with its selected provenance. Preserve the resolver's selected run identity through the adapter and return A directly.

### Minor

None requiring a product change was identified. The one deferred test-maintenance tradeoff is triaged below.

## Declined to judge / intentional scope

- **P1 Codex execution and pilot measurement:** P0 recognizes the valid grammar but returns the specified unavailable/invocation errors before mutation and does not call a provider, use the network, or write a measurement. This follows the recorded ruling; no P1 behavior is asserted.
- **Deleting an entire generated Skill when its final claim disappears:** the accepted Task 12 ruling limits deletion coverage to removed source/claim spans while other accepted claims remain. Full-file deletion intent remains unsupported by the existing P0 contract; I do not treat that explicit scope boundary as a defect.
- **Global atomicity across all exported files:** the CLI contract explicitly permits interruption between per-file atomic replacements and requires an incomplete journal, explicit `export --resume`, and third-hash preservation. I do not expect a multi-file transaction.
- **Current live-byte verification inside `explain`:** the output contract defines explain locations as recorded historical locations; current ownership/freshness belongs to `check`. The historical manifest selection defect above is a failure to provide the required recorded location, not a demand that explain re-read live bytes.
- **Full coverage in `review show` and `report`:** the implementation contract requires coverage in both response shapes. I do not object to coverage being present; I object to duplicate per-line rows with no additional information and no cardinality bound.
- **Diagnostics on stderr with JSON output:** JSON remains one envelope on stdout and informational diagnostics are emitted separately on stderr, as required by the CLI envelope contract. The manual trial's stage-location notes do not corrupt the machine-readable response.
- **Task 12 E2E reading `LedgerStore` descriptors/manifests directly:** `tests/integration/test_no_ai_alpha.py:112-153` reads the immutable representation to verify physical staged spans and hashes. This couples the test to the storage schema, but the public result does not expose the underlying exact bytes and the test checks a storage invariant. Treat as a maintainability cost: update the characterization if that representation intentionally changes; it is not a runtime defect or merge blocker.
- **Unexecuted hosted CI cells and unpublished alpha:** the available verification is Windows/Python 3.13; Windows/Linux/macOS × Python 3.11/3.13 is configured but pending, and the alpha remains unpublished. This is a release-evidence limitation, not evidence of a source defect. Do not describe cross-platform CI as passing.
- **Other deferred/minor items in the task ledger:** the Task 3 no-Git-spy concern was addressed in its later gate; the Task 10 ambiguous-candidate record was an optional direct-test gap and did not change the implemented contract. No additional open behavior was silently set aside.

## Inspected scope and limits

I reviewed the frozen base-to-head package in bounded passes rather than claiming a line-by-line audit of the roughly 18,000-line diff. Passes covered CLI grammar/orchestration and result assembly; extraction, normalization, evidence and coverage production; claim validation/deduplication; filesystem and ledger boundaries; stage/export ownership, journals and manifests; report/review/explain flows; packaging, release docs and CI; and the applicable design, implementation, output, CLI and plan contracts. I also used the supplied real-repository trial artifacts and the ledger's task-gate evidence to check cross-module behavior. I did not inspect behavior outside those paths, rerun any test suite, change source/index/HEAD, or claim the six hosted CI cells passed.

The manual trial reported 10 claims (6 auto-accepted, 4 pending) before its focused review action; accepting one valid pending claim left 7 accepted and 3 pending, of which `check` listed only the 2 valid pending claims. Export succeeded and the final check found fresh sources and no ownership conflicts, but the invalid Python claim remained absent from `check`'s pending-ID list. The trial also showed that duplicate coverage rows materially inflate normal CLI responses. These are the only runtime trial observations treated as findings here.

## Assessment

**Ready to merge? No, with fixes.** The architecture and independent gates provide a strong base, but the evidence-order producer bug breaks a supported Python claim, coverage metadata can grow with each capped Markdown line despite the input bounds, and two historical explain paths fail to preserve exact-run results. Fix these four findings together, then run only the directly affected focused tests plus the required final gate; keep the unexecuted CI matrix clearly marked pending.

## Final fix report (verbatim)

# Final whole-branch fix report

**Result:** DONE. The four Important findings in `final-review.md` are fixed in source commit `d8d657c8ab2efb16505f25099ce556c162c68f4d` on `codex/p0-foundation`.

## Changes

- Python single-test facts now emit sorted unique evidence IDs from both the selected test file and `pyproject.toml`. The strict validator remains unchanged.
- Invalid candidates remain visible as pending in `check`, matching `review list`; review mutation still validates first and cannot accept an invalid candidate.
- Documentation text-budget coverage now records one full coverage row per selector, preserving distinct paths while preventing line-count-driven duplicate growth.
- `explain` validates committed manifest sets and referenced target manifests through the guarded `LedgerStore` read APIs, and supplies historical records alongside active context. Missing or mismatched committed artifacts fail closed. It does not read live target bytes.
- Explicit `explain --run` now preserves the selected run ID in the response envelope. No model fields or validator rules changed.

## RED evidence

Command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/extractors/test_python.py::test_python_manifest_lock_defaults_and_source_test_target tests/extractors/test_docs.py::test_docs_budget_coverage_is_bounded_by_path_after_exhaustion -q
```

Observed before the producer and coverage fixes: `2 failed in 0.68s`. Python evidence IDs were emitted in reverse lexical order (`evidence_6a468ef59e79f8d46be5c334` before `evidence_525b34815b65c6d76115bbe1`). Documentation coverage had `2001` identical budget rows where the regression requires `2` path rows.

The pre-fix CLI regression command was:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_lifecycle.py::test_check_lists_invalid_pending_candidates_and_review_cannot_accept_them tests/test_cli_lifecycle.py::test_explicit_explain_run_id_is_preserved_when_current_run_shares_claim_pair tests/test_cli_lifecycle.py::test_explain_keeps_historical_live_spans_after_active_manifest_changes -q
```

Observed before the CLI fixes: explicit `--run A` returned the current same-pair run B, and historical explain returned `liveLocations: []` after the active manifest pointer moved. The invalid-pending omission was also reproduced in the supplied manual trial: `review list` showed invalid pending claim `claim_c074f3f72a6e46549fb4a5e1`, while `check` listed only the two valid pending claims. The new regression verifies matching review/check visibility and rejection of an attempted accept.

## Verification

Focused regression command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/extractors/test_python.py::test_python_manifest_lock_defaults_and_source_test_target tests/extractors/test_docs.py::test_docs_budget_coverage_is_bounded_by_path_after_exhaustion tests/test_cli_lifecycle.py::test_check_lists_invalid_pending_candidates_and_review_cannot_accept_them tests/test_cli_lifecycle.py::test_explicit_explain_run_id_is_preserved_when_current_run_shares_claim_pair tests/test_cli_lifecycle.py::test_explain_keeps_historical_live_spans_after_active_manifest_changes -q
```

Output: `5 passed in 4.62s`.

Broader affected-surface command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_lifecycle.py tests/test_explain.py tests/test_check.py tests/extractors/test_python.py tests/extractors/test_docs.py -q
```

Output: `41 passed in 20.19s`.

Single final full-suite command after all product fixes:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Output: `262 passed, 1 skipped in 96.35s (0:01:36)`.

Build and fresh-wheel smoke:

```powershell
.\.venv\Scripts\python.exe -m build --no-isolation --outdir dist/final-fix1
.\.venv\Scripts\python.exe scripts/wheel_smoke.py dist/final-fix1/repo2skill-0.1.0-py3-none-any.whl
```

Both exited 0. The build produced wheel and sdist; the smoke installed the wheel in a fresh temporary environment outside the checkout and verified import location, packaged templates, exact third-party notice bytes, `--help`, and `--version`. The build emitted the existing non-fatal `*.py[cod]` manifest warning. Wheel SHA-256: `586FD634884E0077051DDA1ADC06F5118D3090F42C85CF2885DB3F6EE10EE157`.

`git diff --check` was clean before commit. Changed paths: `src/repo2skill/cli.py`, `src/repo2skill/extractors/docs.py`, `src/repo2skill/extractors/python.py`, `tests/test_cli_lifecycle.py`, `tests/extractors/test_docs.py`, and `tests/extractors/test_python.py`.

## Concerns and limits

The build and smoke ran on Windows/Python 3.13. The six hosted Windows/Linux/macOS × Python 3.11/3.13 CI cells remain pending. No other residual concern was found in this wave. Root owns the scoped re-review and final permanent status documentation.

## Scoped fix re-review (verbatim)

### Finding Verdicts

1. **Python single-test evidence order and invalid-pending visibility — ADDRESSED.** `src/repo2skill/extractors/python.py:202` now sorts and deduplicates both evidence IDs before creating the fact, keeping the validator strict. `src/repo2skill/cli.py:559-563,594` uses a shared candidate projection that preserves invalid claims as pending in `check`; review mutation still builds its effective candidates from `validate_claims(...).validClaims` (`src/repo2skill/cli.py:534-548`), so invalid claims remain unreviewable. Regressions assert evidence provenance from both files (`tests/extractors/test_python.py:4-19`) and pending visibility plus attempted-invalid-accept rejection (`tests/test_cli_lifecycle.py:228-252`).

2. **Documentation budget coverage row amplification — ADDRESSED.** `src/repo2skill/extractors/docs.py:30-35,54,60` emits one coverage row per `InputSelector` after budget exhaustion while preserving separate rows for distinct paths. The regression feeds 2,000 rejected lines over two selectors and asserts exactly two coverage rows (`tests/extractors/test_docs.py:28-45`).

3. **Historical explain live locations after later export — ADDRESSED.** `src/repo2skill/cli.py:607-649` enumerates committed export journals, reads manifest sets and referenced target manifests through `LedgerStore` read APIs, validates IDs and canonical reference paths, and combines the historical records with active manifests. The resolver filters by selected run, so older recorded spans remain available without reading current live bytes. The regression exports run A, creates a later committed manifest set and advances the active manifest pointer, verifies historical locations remain, then removes A's manifest set and verifies fail-closed corruption (`tests/test_cli_lifecycle.py:277-325`). The reads use the store's guarded APIs; this path adds no mutation/lock acquisition or unchecked glob/read.

4. **Explicit historical run ID in explain envelope — ADDRESSED.** `src/repo2skill/cli.py:688-689` prefers `args.run` when matching the response run, while retaining the current-run preference when `--run` is omitted. The regression constructs same-snapshot runs with the same claim pair and checks that `explain --run A` returns A (`tests/test_cli_lifecycle.py:255-275`).

### New Breakage in the Fix Diff

None found in the inspected fix diff. Historical manifest-set references are followed through each recorded `ManifestReference.path` after validating it matches the canonical export/target path; this preserves subset references to an earlier export's immutable target manifest. The helper performs only guarded reads. The response still uses frozen model fields.

### Out-of-Scope Observations

- The report-only commit adds `.superpowers/sdd/2026-10-07-local-generator/final-fix-report.md` to the branch. Root has already identified the handoff plan to preserve its evidence in permanent engineering documentation and untrack the scratch copy; this is documentation packaging, not a product defect.
- The six hosted CI cells remain pending, as stated in the fix report. Windows/Python 3.13 focused, affected-surface, full-suite, build, and fresh-wheel smoke results are reported there; I did not rerun them.

### Verification Evidence Reviewed

The fix report names the focused regression command and reports `5 passed in 4.62s`, the affected-surface command and reports `41 passed in 20.19s`, the final full suite and reports `262 passed, 1 skipped in 96.35s`, plus successful build and fresh external wheel smoke. The diff contains corresponding regressions for all four findings. These commands were not rerun during this scoped re-review.

### Verdict

**Fix round: All findings addressed, no new Critical/Important breakage.** No findings remain open.

## Pre-fix manual alpha trial (verbatim)

The first self-repository trial ran against `68f30d602c16e6b89bbd5b61cb6d066ab695c90f`, before the four fixes. Its findings are historical reproductions, not current product status. The separate post-fix trial is pending; do not treat this older trial as post-fix validation or user-demand/benchmark evidence.

# Manual alpha trial: repo2skill on its own tracked tree

This was one isolated, manual, no-AI validation trial on the worktree at `C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill`, HEAD `68f30d602c16e6b89bbd5b61cb6d066ab695c90f`. It is validation evidence from a self-repository run, not user-demand evidence or a benchmark. No product source/index/HEAD files were changed; `git status --short --untracked-files=all` was empty at completion. All trial state, captured JSON, and live outputs are under `dist/manual-alpha-trial`; the required report is this file.

## Isolation and commands

The input was made from tracked HEAD bytes only; the snapshot has no `.git`, virtualenv, prior `dist`, or ignored working data. `repo2skill` came from the existing worktree `.venv` and every product command used the absolute trial root below. No input repository command, script, test, build, or install was executed. No model or network was used.

```powershell
git archive --format=zip --output='dist/manual-alpha-trial/real-repo-snapshot.zip' HEAD
Expand-Archive -LiteralPath 'dist\manual-alpha-trial\real-repo-snapshot.zip' -DestinationPath 'dist\manual-alpha-trial\repo'
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json init --git-mode no-git
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json generate --no-ai --git-mode no-git
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json review list --status pending --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json review show --claim claim_5684fd46dc8e3481b3cb91a9 --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json review show --claim claim_ab2d5bf85ec9d49735119130 --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json review show --claim claim_c074f3f72a6e46549fb4a5e1 --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json review show --claim claim_dd904cf81ba1cbf90361a4ab --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json explain claim_5684fd46dc8e3481b3cb91a9 --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json explain claim_dd904cf81ba1cbf90361a4ab --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json export --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json check
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json report --run run_c60317cc436470919c659117 --section summary
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json review accept --claim claim_dd904cf81ba1cbf90361a4ab --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json export --run run_c60317cc436470919c659117
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json check
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial\repo' --format json report --run run_c60317cc436470919c659117 --section summary
```

## Results

`init` exited 0 and created project `698362de-d758-48eb-bf7d-91c097cbf0fd` in `.repo2skill`. `generate` exited 3 with run `run_c60317cc436470919c659117`, snapshot `snapshot_d0bc7eb7874e1b18596d6350`, revision 1. It proposed 10 claims: 6 observed/deterministic claims were accepted by the generator, and 4 inferred/deterministic claims remained pending. The initial stage had 3 unique target paths in accepted output and 3 in preview output (6 staged files total).

I did not accept the pending pip/artifact claim (`claim_5684fd46dc8e3481b3cb91a9`) or install command (`claim_ab2d5bf85ec9d49735119130`): their recorded evidence was only a `pyproject.toml` project-key locator or file-presence locator, which did not establish those exact claims. I also left `claim_c074f3f72a6e46549fb4a5e1` pending because generation diagnosed its fact/evidence links as invalid. I accepted only `claim_dd904cf81ba1cbf90361a4ab`, the repository-root `python -m pytest` command: its recorded evidence points to the exact pytest configuration in `pyproject.toml`, and `CONTRIBUTING.md` documents `python -m pytest -q`. Review acceptance exited 3 because other proposals remained pending and created stage revision 2. No input tests were run.

The invalid claim's raw lineage is saved in `show-claim_c074f3f72a6e46549fb4a5e1.json`. It is inferred/deterministic, pending, with payload `python -m pytest tests/extractors/test_base.py::test_prior_history_selects_max_revision_even_when_older_record_is_last`, fact `fact_575dc813d824d67f44c0b7d6`, and evidence IDs `evidence_20fa94b640f23cac4c4a38db` and `evidence_26965178dffa09ba81cd695b`. The fact is a heuristic `command.invocation` under `python.project/python-shallow-v1` and references both IDs. Both corresponding evidence records exist in the same snapshot and revision: `pyproject.toml`, semantic key `python-pytest-configuration`, locator `tool/pytest/ini_options`, hash `sha256:da12e224121348d818e06679aa05420516fd3e9d9b118a7c0c62bdd5b1ea6eca`; and `tests/extractors/test_base.py`, semantic key `python-test-target`, lines 15–29, hash `sha256:e7af03361d42927977760e1eb6685a39e93912ef8d9941a98436262dbd07a2eb`. Despite those matching records, generation/report emitted `claim.invalid.fact-evidence-ids-invalid` with reason `fact-evidence-ids-invalid`. `check` omitted this pending claim from its review finding.

The final export exited 0 as `export_416e04abccc10181f9c173d2`, committing stage revision 2 to both targets with 7 accepted claim revisions and 3 pending proposals. It created these live outputs inside the trial snapshot: `AGENTS.md` (SHA-256 `b69602b56d46212d65fe410cd5bf360efe6799285602b9cc7c4d4c8ae3a0a0a7`), `.agents/skills/repo/SKILL.md` (`069a2eefa0e07b95b897f082c9e99de827255d300de975e815dfa760cf857907`), and `.agents/skills/repo/repo2skill-provenance.json` (`274205867576f8201965a733a62520648ff98be8b8729210ca953c820bf84fca`). These are preserved for inspection under `dist/manual-alpha-trial/repo`.

Final `check` exited 3: source status was fresh, review status pending, with informational coverage gaps and no target ownership findings. Its pending finding listed only `claim_5684fd46dc8e3481b3cb91a9` and `claim_ab2d5bf85ec9d49735119130`, omitting the invalid pending single-test claim. Final `report --section summary` exited 0 and reported 739 evidence records, 596 facts, 10 candidate claims, 1 diagnostic, and 1,571 coverage records.

## Product observations and artifacts

`review show` returned 1,571 coverage records for each pending claim; one result was 349,810 bytes. The 1,571 records contain only 24 unique full rows and 22 duplicate groups. One identical full row for `docs/superpowers/plans/2026-10-07-local-generator.md` capped by `documentation-text-budget` appears 210 times. The two inventory rows are distinct full records: `inventory.boundary-excluded` and `inventory.gitignored`. `generate` also printed the same large repeated coverage body. `report --section summary` exited 0 but produced a 900,787-byte JSON file containing the entire coverage array and findings. The representative `explain` commands exited 0 with no coverage list, but each wrote two `explain.stage-location.v1` messages to stderr, naming `AGENTS.md` and `.agents/skills/repo/SKILL.md`.

These are usability/consistency observations from one self-repository run. No code or tests were changed, and no source test/build/install command was run. Raw captured outputs and the tracked snapshot are in `dist/manual-alpha-trial`; the trial does not establish general demand or benchmark performance.


## Post-fix manual alpha trial (verbatim)

# Manual alpha trial: post-fix repo2skill self-run

This bounded no-AI smoke used the final tracked tree at HEAD `78bd2c0ce2d20d648e057ebba98b30a47f32a733` (including fix commit `d8d657c8ab2efb16505f25099ce556c162c68f4d`). It follows the prior trial report at `manual-alpha-trial.md`; the prior `dist/manual-alpha-trial` tree and report were left unchanged. The tracked-only input snapshot and all generated state/outputs/captures are under `dist/manual-alpha-trial-fixed`. No source, index, or test files were changed by this trial. No model/network or input-repository command, script, test, build, or install was run.

## Snapshot and commands

```powershell
git archive --format=zip --output='dist/manual-alpha-trial-fixed/real-repo-snapshot.zip' HEAD
Expand-Archive -LiteralPath 'dist\manual-alpha-trial-fixed\real-repo-snapshot.zip' -DestinationPath 'dist\manual-alpha-trial-fixed\repo'
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json init --git-mode no-git
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json generate --no-ai --git-mode no-git
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json review list --status pending --run run_0e23e5e179b845455140ea89
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json review show --claim claim_9ca21677e36feea9129f6c5d --run run_0e23e5e179b845455140ea89
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json review show --claim claim_729dfdbdd689b5a1443f215c --run run_0e23e5e179b845455140ea89
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json review accept --claim claim_729dfdbdd689b5a1443f215c --run run_0e23e5e179b845455140ea89
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json export --run run_0e23e5e179b845455140ea89
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json review list --status pending --run run_0e23e5e179b845455140ea89
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json check
.\.venv\Scripts\python.exe -m repo2skill --root 'C:\Users\Kyyy\.codex\worktrees\p0-foundation\repo2skill\dist\manual-alpha-trial-fixed\repo' --format json report --run run_0e23e5e179b845455140ea89 --section summary
```

## Results

`init` exited 0 (project `0c9e8d13-8460-4401-8610-12e92438ce4b`). `generate` exited 3 with run `run_0e23e5e179b845455140ea89`, snapshot `snapshot_878d77d844f5326a6f976a2e`, 10 candidates (6 generator-accepted observed claims, 4 pending inferred claims). It returned 24 coverage rows, and all 24 full serialized rows were unique. The documentation budget now has one row per path (22 rows); the two inventory negatives are distinct. There were 0 diagnostics and no `claim.invalid.fact-evidence-ids-invalid` warning.

Pending single-test claim `claim_9ca21677e36feea9129f6c5d` remained inferred/deterministic and reviewable. Its claim and fact both reference the same sorted IDs: `evidence_a915c64bda6ca544272c8c15` and `evidence_d3d80f7e5b25c9e9fa8ba1d7`. The records resolve in the same snapshot to `tests/extractors/test_base.py` lines 15–29 (hash `sha256:e7af03361d42927977760e1eb6685a39e93912ef8d9941a98436262dbd07a2eb`) and `pyproject.toml` key path `tool/pytest/ini_options` (hash `sha256:da12e224121348d818e06679aa05420516fd3e9d9b118a7c0c62bdd5b1ea6eca`). `review show` exited 0, returned 24 coverage rows, and emitted no diagnostic.

I accepted only the already supported repository-root test command, `python -m pytest`, claim `claim_729dfdbdd689b5a1443f215c`. Its evidence points to the pytest configuration, and the tracked `CONTRIBUTING.md` documents `python -m pytest -q`. The review acceptance exited 3 because three other proposals remained pending; it created stage revision 2. The single-test claim was not accepted.

Export `export_291fe0dd6f49e585eac03f65` exited 0 at stage revision 2 and completed both targets with 3 proposals pending. It created the live `AGENTS.md`, Skill `SKILL.md`, and provenance file inside the snapshot. `check` exited 3 with source status fresh, review status pending, informational coverage gaps, and zero target ownership findings. Its three pending IDs exactly matched `review list`: `claim_833c58ca138ae77f1354634a`, `claim_8850d3e4873f34d7c22172a2`, and the valid pending single-test claim `claim_9ca21677e36feea9129f6c5d`. `report --section summary` exited 0 and reported 739 evidence, 596 facts, 10 claims, 0 diagnostics, and 24 coverage rows (24 unique full rows).

## Serialized-size comparison with the earlier trial

These are internal before/after smoke measurements for analogous JSON commands, not a benchmark. The old single-test `review show` JSON was 350,168 bytes (`show-claim_c074f3f72a6e46549fb4a5e1.json`); the fixed single-test `review show` was 8,060 bytes, with coverage reduced from 1,571 rows to 24 unique full rows. The old summary report JSON was 900,787 bytes (`report-final.json`); the fixed summary report was 13,864 bytes. Both prior serialized outputs included repeated coverage records; neither fixed output did.

The generated `AGENTS.md` and Skill files are preserved beneath `dist/manual-alpha-trial-fixed/repo`. Captured CLI JSON files are alongside the snapshot under `dist/manual-alpha-trial-fixed`. At the end of the trial, shared-worktree status showed edits to `docs/engineering/decision-log.md`, `docs/engineering/p0-acceptance.md`, `docs/release-checklist.md`, and `docs/superpowers/plans/2026-10-07-local-generator.md`, plus untracked `docs/engineering/p0-final-review.md`; these were not part of this trial and were left untouched.
