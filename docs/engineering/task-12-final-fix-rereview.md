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
