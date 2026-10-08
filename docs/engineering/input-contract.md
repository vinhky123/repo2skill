# Repository input contract

This document is normative for P0 inventory control inputs and the corrected identity API. It supplements implementation-contract.md and supersedes older interface sketches. Only the clauses published here are approved; scratch drafts remain proposals.

## 1. Inventory control handoff (P0-03 first deliverable)

`InventoryResult` remains unchanged. `inventory.py` declares this runtime-only frozen record:

```python
@dataclass(frozen=True, slots=True)
class IgnoreControlObservation:
    selector: InputSelector
    outcome: Literal["present", "missing", "matched", "no-match",
                     "capped", "decode-error", "io-error"]
    contentHash: Sha256 | None
    matchPaths: tuple[RepositoryPath, ...]
    matchSetHash: Sha256 | None
    openedFileCount: int
    decodedByteCount: int
```

`openedFileCount` is 1 when an OS file handle for that unique control path is successfully opened, even if the later bounded read, stability check, or decode fails; an open failure is 0. Repeated logical reads of that path do not add another charge. `decodedByteCount` is the complete captured raw-byte length only after a configured decoder succeeds, otherwise 0; partial and decode-failed buffers charge zero decoded bytes. These counts initialize the Task 4 shared budgets; inventory control reads do not receive a second allowance. The observation carries no bytes, source text, IDs, diagnostic text, or parser state.

File observations use only `present|capped|decode-error|io-error`, have `matchPaths=()` and `matchSetHash=None`, and may have `contentHash` only for a complete stable buffer. The aggregate glob uses only `matched|no-match|capped|io-error`, has `contentHash=None`, and has `openedFileCount=decodedByteCount=0`. Counts are nonnegative and file `openedFileCount` is only 0 or 1.

The corrected inventory signature is:

```python
def collect_inventory(
    root: Path,
    policy: ScanPolicy,
    *,
    excluded_paths: Sequence[RepositoryPath] = (),
    control_sink: Callable[[IgnoreControlObservation], None] | None = None,
) -> InventoryResult: ...
```

When `control_sink` is present, the control-discovery universe is every directory actually visited by the root-confined traversal before that directory is pruned; each such directory is checked exactly once for its literal `.gitignore` child:

1. Immediately after capture and before its decoded rules govern traversal, emit one required file observation for each detected `.gitignore`, using `InputSelector("file", path, ".", True)`. A successful observation is `present` with the raw-byte SHA-256 of the exact immutable buffer, empty `matchPaths`, and null `matchSetHash`.
2. After traversal completes or stops, emit one required aggregate observation for `InputSelector("glob", "**/.gitignore", ".", True)`. Its membership is every detected `.gitignore` in the visited-directory universe, including a control file whose subsequent open/decode fails. A complete empty result is `no-match`, `matchPaths=()`, and `matchSetHash=sha256(canonical_json_bytes([]))`.

Emission order is therefore traversal-order file observations followed by the aggregate glob. Consumers canonical-sort observations before binding/persistence; the sink must not pretend the final glob is known before traversal.

Control caps, decoding failures, I/O failures, or incomplete traversal produce observations plus required diagnostics/coverage; they never become absence. Any such control failure makes the aggregate control glob incomplete (`capped` for a traversal/budget cap, otherwise `io-error`), retains the known sorted membership prefix, sets `matchSetHash=None`, and makes the run incomplete. Traversal does not enumerate descendants whose applicable control file could not be decoded completely; it fails closed rather than treating the control as empty. Control observations are bound as extractor `inventory`, version equal to the inventory implementation version, rule `inventory.gitignore-controls`, with its explicit rule version, and converted to ordinary `ExtractionRead` records without reopening files. The always-present aggregate selector records scanner absence as a stable complete empty set and records later control-file additions. Because raw buffers are not transferred or persisted, `.gitignore` itself produces no Evidence.

### Normative P0 ignore-source limitation

P0 applies root-confined, per-directory worktree `.gitignore` files only. It does not read `.git/info/exclude`, `core.excludesFile`, global excludes, or ignore sources outside the scan root. A descendant `.gitignore` below a pruned ignored directory is outside the visited-directory universe; the ancestor control that pruned it remains a dependency. Recheck repeats this exact control-discovery/traversal algorithm rather than evaluating an ordinary glob only over already eligible inventory entries. In Git modes, the tracked-input name set overrides normal `.gitignore` matches. Additions/deletions in that tracked set are represented by repository identity/dirty-summary changes and therefore stale the run; no external ignore source is implied. Exact includes may restore normal ignored untracked paths under the existing intrinsic-exclusion rules.

This deliberately narrows the design's general “Git ignore semantics” wording so every discovery control has a root-relative selector, same-buffer hash, and reproducible recheck.

Repository identity also needs the exact effective eligibility inputs; deleted tracked paths may be absent from `InventoryResult` and cannot be filtered from hidden configuration. The corrected signature is:

```python
def capture_repository(
    root: Path,
    inventory: InventoryResult,
    git_mode: Literal["auto", "head", "worktree", "no-git"],
    *,
    project_id: str,
    policy: ScanPolicy,
    excluded_paths: Sequence[RepositoryPath] = (),
) -> RepositoryIdentity: ...
```

Identity applies `policy` and `excluded_paths` when constructing tracked/untracked/deleted input summaries. It does not read project configuration or attach hidden attributes to inventory.

An include value prefixed with `literal:` is an explicit canonical path, including filenames containing glob metacharacters. For example, `literal:ignored/a[b].txt` can override an ordinary ignore for that exact file. Normalize Windows separators in the suffix before validating the original slash-separated components. Unprefixed include values without glob metacharacters retain their existing exact-path meaning; other unprefixed include values are patterns and do not bypass normal ignores. `literal:` applies only to include values, never to configured excluded paths or selectors. Fingerprints retain the explicit include value.

## 2. Extraction runtime without ambient state

Sections 2–7 below are normative execution rulings for P0-04 and its P0-05 consumers.

The exact frozen `ExtractionContext(root, snapshotId, inventory, policy, read)` remains unchanged. `extractors/base.py` owns one mutable runtime service per scan:

```python
class ExtractionSession:
    def __init__(
        self,
        *,
        root: Path,
        snapshot_id: SnapshotId,
        inventory: InventoryResult,
        policy: ScanPolicy,
        repository_id: str,
        prior_evidence: Sequence[Evidence],
        prior_facts: Sequence[Fact],
        excluded_paths: Sequence[RepositoryPath],
        control_observations: Sequence[IgnoreControlObservation],
        id_context: CanonicalIdContext | None = None,
    ) -> None: ...

    def bind(
        self,
        extractor_id: str,
        version: str,
        rule_id: str,
        rule_version: str,
    ) -> ExtractionContext: ...
```

The session owns the canonical-path capture cache, finalized logical-read cache, diagnostics/coverage sinks, evidence/fact revision indexes, and shared opened/decoded/snippet budget counters. Its initial counters include `control_observations`. `id_context` is the optional per-run `CanonicalIdContext`; create a new one for the session when omitted. Allocate every `readId`, `evidenceId`, and `factId` through this context so truncated-ID collisions fail closed. The CLI passes the active store's `id_context` when constructing the session. `bind` returns a context whose `read` closure contains the four provenance values. There is one bound context per rule. Rule IDs are globally namespaced, such as `generic.ci-job` or `docs.normalized-line`.

An extractor may expose an optional explicit `rules` sequence. Each rule object provides `rule_id`, `rule_version`, `discover(ctx)`, and `extract(ctx, selectors)`. The registry sorts rules by `rule_id` and binds each separately under the parent's `extractor_id` and `version`; those parent provenance values remain the `ExtractionRead.extractorId`/`extractorVersion`. If `rules` is absent, adapt the legacy extractor's own `discover`/`extract` pair as one rule. Preserve `extractor.rule_id` and `extractor.rule_version` when supplied; if missing, use `<extractor_id>.extract` for the rule ID and the extractor `version` for the rule version. Reject duplicate extractor IDs (including conflicting versions) and duplicate rule IDs before executing any rule. This is runtime dispatch metadata only: it adds no persisted record and no dependency field.

The registry entry point accepts an `ExtractionSession`, sorts extractors by `(extractor_id, version)`, sorts explicit rules by `rule_id`, binds rule contexts, and returns one canonically sorted `ExtractionBatch`. Extractor constructors may explicitly retain their session; builders may be session methods or functions receiving the session/bound context explicitly. Module globals, thread locals, cwd, and “current extractor/rule” variables are forbidden.

The extractor protocol remains unchanged. An extractor never opens a path outside `ctx.read`. Every logical key `(extractorId, ruleId, canonical selector)` has one finalized dependency; repeated calls return that dependency and captured result.

## 3. Selector grammar and read outcomes

All P0 selectors have `scope="."`. File values are NFC normalized repository paths. Glob values use positive gitignore-style/pathspec grammar over canonical `/` paths; leading `!` negation is rejected. Absolute paths, backslashes, dot segments, and root escape are rejected.

Negative values use this closed grammar:

```text
filename-absent:<NFC literal basename>
```

The parameter is one nonempty basename with no `/`, `\`, `.`/`..`, glob metacharacters, or control characters. It tests eligible inventory paths by exact final component. Unknown predicate names or malformed parameters fail closed before reading. The registry may grow only by a versioned code change.

Outcome fields are exact:

| selector | complete result | outcome/hash fields |
|---|---|---|
| file | captured and decoded | `present`, raw `contentHash`, `matchPaths=()`, `matchSetHash=None` |
| file | eligible candidate absent | `missing`, hashes null, `matchPaths=()` |
| file | present but intrinsic/excluded/ineligible | `io-error`, hashes null, stable `read-forbidden` diagnostic |
| file | cap, decode, unstable/I/O failure | respective `capped`, `decode-error`, or `io-error`; raw hash only for a complete stable buffer |
| glob/negative | nonempty complete match | `matched`, `contentHash=None`, sorted unique paths and canonical-array set hash |
| glob/negative | complete empty match | `no-match`, `contentHash=None`, `matchPaths=()`, canonical-empty-array set hash |
| glob/negative | incomplete enumeration | `capped`, known sorted prefix allowed, `matchSetHash=None` |

`dependencyType == selector.type` and `required == selector.required`; mismatch is an invariant failure. The dependency is registered and finalized before parser entry. Evidence is created only from a successful stable capture that supports a fact: ordinarily a `present` file read with its exact raw hash. Failed, capped, missing, forbidden, or decode-failed reads produce only the read plus diagnostics/coverage. Inventory-control observations produce dependencies but no evidence.

## 4. Shared budgets and deterministic allocation

- `maxOpenedFiles` counts each unique canonical path whose OS open succeeds once, even if its later bounded read, stability check, or decode fails. An OS open failure does not charge it. Cache hits and repeated rule reads do not charge it. Ignore-control opens are already charged.
- `maxDecodedBytes` counts captured raw bytes once for each unique path after decoding succeeds. It does not count Python characters or failed decoding. Ignore-control decoded bytes are already charged.
- `maxFileBytes` uses a bounded `limit + 1` read; capped content is neither fully read nor hashed.
- `maxSnippetBytes` counts the UTF-8 bytes of each separately retained post-redaction freeform `task.definition.body`, `documentation.line.text`, and each `Evidence.redactedPreview`. A `documentation.line` Fact text is charged once per unique normalized text, independent of how many paths support it; each distinct supporting Evidence preview is charged once. Redact before bounding and truncate only at a valid Unicode boundary. Discarded text is not charged again, and a common truncated prefix is never proof of text identity.
- Registry entries are sorted by `(extractorId, version)`, known rule work is sorted by `ruleId`, and each rule sorts its known selectors by `(scope, type, value, required)`. Conditional reads discovered only after parsing execute in deterministic discovery order within that rule. Shared caps are allocated in this published execution order, never registry insertion/input order; the contract does not claim that conditional reads can be globally preplanned. A cap hit remains a read and visible coverage; required cap/failure makes the run incomplete.

The session cache stores immutable buffers only for the running process. Raw buffers never enter `RunLedger`.

## 5. Snapshot, freshness, and affected claims

`build_snapshot` retains the normative signature. It validates identical absolute inventory/identity roots, snapshot IDs, selector/dependency agreement, globally namespaced rule IDs, extractor versions, sorted unique match paths, and one logical read per `(extractorId, ruleId, canonical selector)` before hashing.

`inventoryHash` proves the composition of the original capture and remains in `SourceSnapshot`/`inputDigest`. Recheck does not compare a newly computed whole-inventory hash as an independent stale condition. Recorded file/glob/negative/control selectors define freshness relevance; unrelated eligible additions do not stale the run unless a selector observes them.

Affected claims are resolved conservatively:

```text
changed read.ruleId
  -> every Fact with the same globally namespaced ruleId
  -> every Claim whose factIds include those facts

changed documentation read
  -> documentation Evidence produced by that rule/path
  -> DedupDecision.documentationEvidenceIds
  -> DedupDecision.candidateClaimIds
```

Repository identity, current root-label mismatch against `SourceSnapshot.rootLabel`, and effective policy changes affect all claims. A root-label mismatch is stale and reports every claim ID even though the root label is supplied separately from `RepositoryIdentity`; the recheck caller passes it explicitly rather than reading project metadata implicitly. An extractor-version change affects facts/claims from that extractor's rules. All selectors are re-evaluated even after a stale/unknown result so findings are complete; precedence remains `stale > unknown > fresh`.

## 6. Generic and documentation collection boundary

The approved Task 5 versioned categories and runner/command values are defined below. `package-script.v1` denotes a directly observed literal package-manifest script; executable manager choice stays in a separate invocation.

Task 4 is root-scoped and shallow:

- CI: selected workflow files may yield observed workflow/job presence from structural keys only.
- Taskfile: selected task name and bounded redacted literal body are observed in `task.definition`; `task <name>` is a deterministic-derived invocation.
- Root Make/Just: target names are observed; `make <name>` / `just <name>` are deterministic-derived invocations. Recipe bodies are not policy.
- Container: selected Dockerfile/Compose presence only.
- JSON/TOML/YAML: only versioned selected-key summaries from the exact allowlist below. Task 4 does not discover `**/*.json`, `**/*.toml`, or `**/*.{yml,yaml}`. Source-file names and extensions are case-sensitive exactly as written. Ecosystem-specific interpretation remains Task 5. Arbitrary files, keys, and prose never become policy or commands.
- JVM-native build files yield explicit unsupported coverage, not inferred commands or absence.

Documentation reads include every selected root README-like file and `docs/**/*.md`. A normalized matched line produces one `documentation.line` fact whose `evidenceIds` contains every distinct path/locator evidence record for identical normalized text; every file read remains in `reads`.

The minimal Fact vocabulary shared with Task 5 is:

```text
factType="artifact.presence"
value={"category": "ci-workflow"|"ci-job"|"container"|"ecosystem"|"package.v1"|"package-manager.v1"|"runtime.v1"|"dependency.v1"|"convention.v1",
       "name": str, "path": RepositoryPath}

factType="task.definition"
value={"category": "task", "runner": "task"|"make"|"just"|"package-script.v1",
       "name": str, "body": str|null}

factType="command.invocation"
value={"category": "test"|"build"|"run"|"task"|"install"|"single-test.v1",
       "argv": list[str]}

factType="config.structure"
value={"category": "structural-summary", "format": "json"|"toml"|"yaml",
       "keys": list[str]}

factType="documentation.line"
value={"category": "documentation", "text": str}
```

All object keys are exact; omitted information is not represented by extra keys. `argv` is data, never shell text. Literal bodies/text are redacted and bounded. Rule version changes when selected keys, normalization, or derivation changes. Task 5 uses these exact envelopes and the versioned values below. The allowed `artifact.presence.category` additions are `package.v1`, `package-manager.v1`, `runtime.v1`, `dependency.v1`, and `convention.v1`; continue to use `name` for the concise typed identity/version and `path` for the source. `task.definition.runner="package-script.v1"` means a directly observed literal entry from a package manifest, not an executable package manager. Keep literal script body in `body`; manager selection belongs to a separate `command.invocation` and must not alter the script-definition identity. Support manager values such as `npm`, `pnpm`, `yarn`, and `bun` in invocation argv. Observed lockfile presence uses `artifact.presence` category `package-manager.v1` with the lock filename/path. A manager inferred from a recognized lockfile name is deterministic-derived, separate from observed lock presence. Use `command.invocation.category="install"` for installation commands and versioned `"single-test.v1"` for a one-test command/target. Template fallback commands for unsupported or insufficiently evidenced ecosystems are `template`, never observed.

`config.structure.keys` is the sorted intersection of present known key paths; values are never included. The exact source/key allowlist is:

| source | permitted key paths |
|---|---|
| root `package.json` | `name`, `scripts`, `dependencies`, `devDependencies`, `packageManager`, `engines`, `workspaces`, `type` |
| root `pyproject.toml` | `project`, `build-system`, `tool.pytest.ini_options`, `tool.ruff`, `tool.poetry` |
| root `Cargo.toml` | `package`, `workspace`, `dependencies`, `dev-dependencies`, `features` |
| root `Taskfile.yml`, `Taskfile.yaml` | `version`, `tasks` |
| root `compose.yml`, `compose.yaml`, `docker-compose.yml`, `docker-compose.yaml` | `services`, `networks`, `volumes`, `secrets`, `configs` |
| `.github/workflows/*.yml`, `.github/workflows/*.yaml` | `on`, `jobs` |

Nested key paths above mean structural path presence, not traversal of arbitrary descendants for output. A file with none of its permitted paths yields no `config.structure` fact.

## 7. YAML and redaction defaults

YAML is composed with `yaml.BaseLoader`; an identity-based active-node set rejects recursive aliases before construction. Only selected known keys receive explicit conversion. YAML 1.1 spellings such as `on`, `off`, `yes`, and `no` remain strings unless that exact selected field defines another conversion.

Persisted labels are category-only and contain no secret-derived hash:

```text
[REDACTED:secret-key]
[REDACTED:private-key]
[REDACTED:authorization]
[REDACTED:credential-url]
[REDACTED:high-entropy]
```

Defaults are frozen as follows:

- Secret-key matching casefolds a key and removes `-`, `_`, `.`, and spaces. It matches the exact set `password`, `passwd`, `secret`, `token`, `apikey`, `accesstoken`, `accesskey`, `clientsecret`, `privatekey`, or any normalized key ending in `password`, `passwd`, `secret`, `token`, `apikey`, `accesskey`, or `privatekey`.
- Private-key material matches a line beginning `-----BEGIN ` and ending ` PRIVATE KEY-----`.
- Authorization matches case-insensitive `Authorization:` or `Proxy-Authorization:` headers with a nonempty value.
- Credential URL matches an absolute URI with nonempty userinfo before `@`; the complete userinfo is replaced.
- High entropy applies only to ASCII token candidates of 24–256 characters from `[A-Za-z0-9_+/=-]`, requires at least three of lowercase/uppercase/digit/symbol, and Shannon entropy at least 3.5 bits per character.

Non-string values under secret-like keys are replaced too. Diagnostics contain category/path/locator and stable error codes, never raw values, source lines, parser exception representations, or credential URLs. Custom secret paths are outside P0 because `ScanPolicy` has no such field.

## Required focused tests

Task 3 tests `control_sink=None`, traversal-order file observations followed by the aggregate glob, consumer canonical sorting, complete-empty `.gitignore` glob, same-buffer file hash, cap/failure visibility, opened/decoded accounting, tracked-name override, ancestor pruning, and the explicit absence of info/global excludes.

Task 4 tests dependency-before-parser ordering; mutation after capture; every outcome row; excluded-present behavior; one physical open with two rule reads; control-budget deduction; published cap allocation including conditional reads; empty glob/negative hashes; unknown negative rejection; digest stability; selector-only freshness; docs-to-dedup affected claims; cyclic YAML rejection; scalar preservation; every redaction category; duplicate documentation evidence consolidation; sorted present-key intersections with no values; and exact secret-key fixtures `api_token`, `numeric_secret`, `boolean_secret`, and `empty_secret` including non-string/empty values.
