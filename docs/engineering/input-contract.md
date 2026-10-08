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

