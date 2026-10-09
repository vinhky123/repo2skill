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
