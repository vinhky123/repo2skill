# Alpha release checklist

repo2skill 0.1.0 is an unpublished local alpha. The checklist separates completed local implementation verification from pending independent review and cross-platform CI. Update it only from recorded commands and workflow results; a configured matrix cell is not a completed result.

## Current evidence

- Windows/Python 3.13: the full suite passed (258 passed, 1 skipped in 85.90s) before the final integration/provenance and sdist-content assertion refinements. The final strengthened integration test passed (1 in 12.72s) after review fix round 1; distribution/archive tests passed (2 in 6.99s), then the final wheel and sdist were rebuilt and the wheel smoke was rerun. The final wheel installed in a fresh environment outside the checkout and passed import-location, template, notice-byte, `--help`, and `--version` checks.
- The built sdist includes the test package and fixture tree, including the mixed alpha fixture and integration test. The distribution test checks this archive content and confirms wheel/sdist notice text equality.
- The no-AI integration test blocks socket connection APIs and confirms the synthetic package-script marker was not created. No model or network result is used by product operations.
- Linux/Python 3.11, Linux/Python 3.13, Windows/Python 3.11, Windows/Python 3.13, macOS/Python 3.11, and macOS/Python 3.13 workflow results remain pending. The earlier Windows/Python 3.13 Task 10 baseline is not a Task 12 workflow result.
- Publication: none. Do not describe this alpha as available from a package index.
- Task 12 implementation verification and review fix round 1 are complete locally; scoped Task 12 re-review and final project review remain pending.

## Before publication

- [x] Confirm all P0 F001–F035 features have an owning task and evidence in the release evidence map.
- [x] Run the Task 12 integration characterization and record its actual passing result.
- [x] On Windows/Python 3.13, run the full suite, build both archives, install and smoke-test the wheel, and verify the packaged MIT notice.
- [ ] Complete the CI matrix and record each actual result; leave failed, cancelled, or unrun cells pending.
- [x] Confirm no-AI product operations use no network/model and no fixture repository commands are executed.
- [x] Review `THIRD_PARTY_NOTICES` byte preservation and the pinned upstream source map.
- [x] Update installation, contribution, threat, schema, README, and plan status text to match the current implementation and evidence.
- [ ] Complete independent Task 12 and final project reviews; record any remaining platform or coverage limits.
- [ ] Publish only after the project owner approves a concrete release artifact and its remaining limits.
