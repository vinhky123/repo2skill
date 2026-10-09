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
