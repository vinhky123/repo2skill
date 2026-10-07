# Optional Codex and Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **Status:** Ready for review; not executed. Commands are FUTURE and are not runnable in the current documentation-only repository.

**Goal:** Add an explicit, failure-isolated Codex CLI reasoner and local pilot measurement workflow without weakening the complete P0 no-AI product.

**Architecture:** Probe installed Codex, build a bounded EvidencePack, obtain per-run consent, and validate proposals before publishing a stage. Measurement is opt-in local JSONL.

**Tech Stack:** Python 3.11+, P0 package and ledger, standard-library subprocess/JSON; official installed Codex CLI discovered at runtime.

**Spec:** `docs/superpowers/specs/2026-10-07-repo2skill-mvp-design.md`; CLI: `docs/engineering/cli-contract.md`; pilot: `docs/product/pilot-validation.md`.

## Global Constraints

- P1 starts after P0 acceptance; default `generate` remains no-AI and zero-network.
- No credential management, model alias, provider SDK, arbitrary arguments or shell/config execution.
- `doctor --codex` reads local version/help and a non-network fixture only. Authentication, network and structured-response probing occurs only inside consented `generate --ai codex`.
- Derive transport argv and stdin/stdout framing from installed `codex --help` plus a checked fixture; do not encode remembered flags.
- Invocation uses a sanitized temp directory containing only EvidencePack/schema; verified capabilities disable tools, repository access and instruction autoload. Selected user config must not re-enable them; otherwise exit 6.
- Raw files, Git config, environment, credentials, ignored files and unbounded docs never enter EvidencePack.
- Capability, consent, transport or response failure exits 6 without advancing active state.
- No automatic telemetry, upload, outreach or pilot enrollment.

## Review Focus

- Symlink/wrapper discovery must resolve to an approved CLI boundary; test in P1-01.
- The persisted audit EvidencePack must byte-match the payload sent; test in P1-02.
- A valid ID with altered observed command/path/version must still be rejected; test in P1-03.
- Mixed valid/forged proposals must be rejected; test in P1-03.
- `--measure` must omit identifiers, paths, snippets and error text even on failure; test in P1-05.

---

### Task P1-01: Installed capability and transport-contract spike

**Feature IDs:** F036

**Dependencies:** P0-12.

**Files:**
- Create: `src/repo2skill/codex/capability.py`, `docs/engineering/codex-transport.md`
- Modify: `src/repo2skill/models.py`
- Test: `tests/codex/test_capability.py`, `tests/fixtures/codex/help/`

**Interfaces:**
- Produces: frozen `CodexCapability(executable, version, structuredOutput, transportFixtureId, available, diagnostic)` and `probe_codex(executable: Path | None) -> CodexCapability`.
- Consumes: local executable/version/help only; no authentication or network.

- [ ] **Red:** Test missing executable, unsupported help and wrapper resolution. A fake executable asserts sanitized cwd/argv contains no repository path/raw content; incompatible isolation/config returns 6.
- [ ] Run `python -m pytest tests/codex/test_capability.py -v`; expect FAIL because the adapter does not exist.
- [ ] **Green:** Implement local-help-only discovery for `doctor`. In consented generate, require verified noninteractive EvidencePack-only transport with instruction autoload, repository access and tools disabled. A separately opted-in real smoke distinguishes help evidence from runtime guarantees and records exact version/results.
- [ ] Rerun tests; expect PASS and unsupported versions unavailable.
- [ ] Review F036 and commit `feat: probe installed Codex capabilities`.

### Task P1-02: Bounded EvidencePack, estimate, and consent

**Feature IDs:** F037, F038

**Dependencies:** P1-01; P0 redaction and immutable run contracts.

**Files:**
- Create: `src/repo2skill/codex/adapter.py`
- Modify: `src/repo2skill/models.py`
- Test: `tests/codex/test_evidence_pack.py`, `tests/codex/test_consent.py`

**Interfaces:**
- Produces: frozen `EvidencePack(schemaVersion, snapshotId, facts, claims, diagnostics, scopes, instructions)`; `UsageEstimate(factCount, claimCount, payloadBytes, estimatedInput, estimatedOutput)`; `build_evidence_pack(run: RunLedger) -> EvidencePack`; `estimate_usage(pack: EvidencePack) -> UsageEstimate`.
- Consumes: allowlisted values, redacted previews and related claims from one completed run.

- [ ] **Red:** Assert forbidden data is absent, caps hold, audit bytes equal transport bytes, estimate is not price/quota, and decline makes no subprocess call.
- [ ] Run `python -m pytest tests/codex/test_evidence_pack.py tests/codex/test_consent.py -v`; expect FAIL.
- [ ] **Green:** Serialize once for audit/transport; show estimate before one-invocation consent (`--yes-ai` or interactive approval).
- [ ] Rerun tests; expect PASS with no unredacted secret in failure output.
- [ ] Review F037/F038 and commit `feat: build consented Codex evidence packs`.

### Task P1-03: Structured proposals and failure isolation

**Feature IDs:** F039, F040

**Dependencies:** P1-01, P1-02.

**Files:**
- Modify: `src/repo2skill/codex/adapter.py`, `src/repo2skill/validation.py`, `src/repo2skill/ledger.py`, `src/repo2skill/models.py`
- Test: `tests/codex/test_adapter.py`, `tests/codex/test_proposal_validation.py`, `tests/codex/test_failure_isolation.py`

**Interfaces:**
- Produces: `CodexResponse(status, stdoutBytes, stderrDiagnostic, proposals)`; `invoke_codex(capability: CodexCapability, pack: EvidencePack, config: str | None, timeout_seconds: float) -> CodexResponse`; `validate_codex_proposals(response: CodexResponse, run: RunLedger) -> ValidationResult`.
- Consumes: proved transport and claim-chain validation; `origin=llm` never upgrades `kind`.

- [ ] **Red:** Cover timeout, nonzero, malformed/partial JSON, unknown ID, wrong snapshot/scope, altered canonical payload, unsupported policy and mixed forged responses; capture state first.
- [ ] Run `python -m pytest tests/codex/test_adapter.py tests/codex/test_proposal_validation.py tests/codex/test_failure_isolation.py -v`; expect FAIL.
- [ ] **Green:** Invoke without shell interpolation, parse bounded output, reject the response transaction on any structural/reference violation, then render and validate an immutable new stage before one state-pointer activation.
- [ ] Rerun tests; expect PASS and identical pre/post pointer bytes for every failure case.
- [ ] Review F039/F040 and commit `feat: validate Codex proposals atomically`.

### Task P1-04: Codex CLI integration

**Feature IDs:** F036–F040

**Dependencies:** P1-01–P1-03.

**Files:**
- Modify: `src/repo2skill/cli.py`
- Test: `tests/codex/test_cli_codex.py`

**Interfaces:**
- Consumes: P1 probe/pack/estimate/invoke/validate contracts.
- Produces: exact `doctor --codex` and `generate --ai codex [--codex-config NAME] [--yes-ai]` behavior; exit 6 on explicit Codex failure, with no silent no-AI fallback.

- [ ] **Red:** Assert option exclusions, pre-invocation estimate, consent scope, named config only, JSON diagnostics, exit precedence and unchanged P0 `generate --no-ai` results.
- [ ] Run `python -m pytest tests/codex/test_cli_codex.py tests/test_cli_lifecycle.py -v`; expect FAIL.
- [ ] **Green:** Wire the narrow P1 path into the existing single-file dispatcher without exposing arbitrary argv or credentials.
- [ ] Rerun tests; expect PASS; run the full P0 suite and expect no regressions.
- [ ] Review CLI contract and commit `feat: expose opt-in Codex generation`.

### Task P1-05: Local pilot measurement and beta gate

**Feature IDs:** F041

**Dependencies:** P1-04.

**Files:**
- Modify: `src/repo2skill/cli.py`, `docs/product/pilot-validation.md`
- Create: `tests/test_pilot_measurement.py`, `docs/product/pilot-runbook.md`

**Interfaces:**
- Consumes: completed generate phase timings/counts and canonical final status/exit code.
- Produces: opt-in append-only `.repo2skill/pilot-measurements.jsonl` containing only the CLI contract allowlist; manual task packet/review log instructions.

- [ ] **Red:** Assert no file without `--measure`; one record with the flag; recursive denylist checks for source paths, IDs, URLs, command/fact/snippet/error text and secrets; no network calls.
- [ ] Run `python -m pytest tests/test_pilot_measurement.py -v`; expect FAIL.
- [ ] **Green:** Append one local versioned record and document consent, redaction, retention, baseline/no-AI comparison and Continue/Narrow/Stop gates. Do not recruit or contact participants.
- [ ] Run `python -m pytest -q`; expect all P0/P1 tests PASS, then run `python -m build`; expect successful artifacts with notices.
- [ ] Review all F036–F041 coverage and commit `docs: prepare opt-in pilot beta`.

Plan is ready for review and has not been executed.
