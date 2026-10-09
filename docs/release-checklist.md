# Alpha release checklist

repo2skill 0.1.0 is an unpublished local alpha. Local implementation, scoped review, and the bounded post-fix manual smoke are complete on Windows/Python 3.13; the six-cell hosted CI matrix remains pending. Update it only from recorded commands and workflow results; a configured matrix cell is not a completed result.

## Current evidence

- Windows/Python 3.13: after the final product fix wave, the full suite passed (262 passed, 1 skipped in 96.35s), the affected-surface suite passed (41 in 20.19s), and five focused regressions passed (4.62s). The wheel and sdist build and fresh external-cwd wheel smoke exited 0. Wheel: `dist/final-fix1/repo2skill-0.1.0-py3-none-any.whl`, SHA-256 `586FD634884E0077051DDA1ADC06F5118D3090F42C85CF2885DB3F6EE10EE157`; smoke checked import location, templates, exact notice bytes, `--help`, and `--version`.
- The built sdist includes the test package and fixture tree, including the mixed alpha fixture and integration test. The distribution test checks this archive content and confirms wheel/sdist notice text equality.
- The no-AI integration test blocks socket connection APIs and confirms the synthetic package-script marker was not created. No model or network result is used by product operations.
- The post-fix self-repository smoke generated 10 candidates with 0 diagnostics and 24 unique coverage rows; the valid pending single-test claim was reviewable, export completed to both targets, all 3 pending IDs matched between `check` and `review list`, and the final check found no ownership conflicts. The trial was isolated to `dist/manual-alpha-trial-fixed` and executed no input-repository command. Its `review show` and summary JSON shrank from 350,168 to 8,060 bytes and 900,787 to 13,864 bytes; these are internal before/after smoke observations, not a benchmark. Generated snapshot outputs: [`AGENTS.md`](../../dist/manual-alpha-trial-fixed/repo/AGENTS.md), [`SKILL.md`](../../dist/manual-alpha-trial-fixed/repo/.agents/skills/repo/SKILL.md), and [provenance JSON](../../dist/manual-alpha-trial-fixed/repo/.agents/skills/repo/repo2skill-provenance.json). Full commands and scope are preserved in [`task-12-manual-alpha-trial.md`](engineering/task-12-manual-alpha-trial.md).
- Linux/Python 3.11, Linux/Python 3.13, Windows/Python 3.11, Windows/Python 3.13, macOS/Python 3.11, and macOS/Python 3.13 workflow results remain pending. The earlier Windows/Python 3.13 Task 10 baseline is not a Task 12 workflow result.
- Publication: none. Do not describe this alpha as available from a package index.
- Final product verification is complete locally. The scoped independent re-review and bounded post-fix manual alpha trial are clean. All hosted CI cells remain pending; no package publication has occurred.

## Before publication

- [x] Confirm all P0 F001–F035 features have an owning task and evidence in the release evidence map.
- [x] Run the Task 12 integration characterization and record its actual passing result.
- [x] On Windows/Python 3.13, run the full suite, build both archives, install and smoke-test the wheel, and verify the packaged MIT notice.
- [ ] Complete the CI matrix and record each actual result; leave failed, cancelled, or unrun cells pending.
- [x] Confirm no-AI product operations use no network/model and no fixture repository commands are executed.
- [x] Review `THIRD_PARTY_NOTICES` byte preservation and the pinned upstream source map.
- [x] Update installation, contribution, threat, schema, README, and plan status text to match the current implementation and evidence.
- [x] Complete the scoped independent Task 12 fix re-review and record its verdict.
- [x] Complete the bounded post-fix manual trial and record its limitations.
- [ ] Complete the six-cell hosted CI matrix before claiming cross-platform verification.
- [ ] Publish only after the project owner approves a concrete release artifact and its remaining limits.
