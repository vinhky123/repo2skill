"""Rule-bound, guarded source reads and shared extraction budgets."""
from __future__ import annotations

from collections.abc import Sequence
import hashlib
import os
from pathlib import Path
import re
import unicodedata
from typing import Protocol

from pathspec import GitIgnoreSpec

from ..ids import CanonicalIdContext, canonical_json_bytes, revise_record
from ..inventory import INVENTORY_VERSION, IgnoreControlObservation
from ..models import (
    CoverageEntry, Diagnostic, Evidence, EvidenceLocator, ExtractionBatch,
    ExtractionContext, ExtractionRead, Fact, InputSelector, InventoryResult,
    ReadResult, RepositoryPath, ScanPolicy,
)
from ..redact import redact_text, redact_value
from ..safe_fs import GuardedDirectory, UnsafePathError, open_directory


class Extractor(Protocol):
    extractor_id: str
    version: str
    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]: ...
    def extract(self, ctx: ExtractionContext, selectors: Sequence[InputSelector]) -> ExtractionBatch: ...


def _hash(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _selector_key(selector: InputSelector) -> tuple[str, str, str, bool]:
    return (selector.scope, selector.type, unicodedata.normalize("NFC", selector.value), selector.required)


class ExtractionSession:
    """One scan's stateful cache; no process-global provenance is used."""

    def __init__(self, *, root: Path, snapshot_id: str, inventory: InventoryResult,
                 policy: ScanPolicy, repository_id: str, prior_evidence: Sequence[Evidence],
                 prior_facts: Sequence[Fact], excluded_paths: Sequence[RepositoryPath],
                 control_observations: Sequence[IgnoreControlObservation],
                 id_context: CanonicalIdContext | None = None):
        self.root = Path(os.path.abspath(root))
        if Path(os.path.abspath(inventory.root)) != self.root:
            raise ValueError("inventory root does not match extraction root")
        self.snapshot_id, self.inventory, self.policy = snapshot_id, inventory, policy
        self.repository_id = repository_id
        self.id_context = id_context or CanonicalIdContext()
        self.prior_evidence = {item.evidenceId: item for item in prior_evidence}
        self.prior_facts = {item.factId: item for item in prior_facts}
        self.excluded_paths = tuple(excluded_paths)
        self.control_observations = tuple(control_observations)
        self._validate_controls()
        self._entries = {entry.path: entry for entry in inventory.entries}
        self._inventory_incomplete = any(item.status in {"capped", "failed"} for item in inventory.coverage)
        self._root_guard = open_directory(self.root)
        self._directory_guards: dict[str, GuardedDirectory] = {"": self._root_guard}
        self._captures: dict[str, tuple[bytes | None, str | None, str | None, str, str | None]] = {}
        self._controls = {item.selector: item for item in control_observations}
        self._logical: dict[tuple[str, str, tuple[str, str, str, bool]], ReadResult] = {}
        self._opened = sum(item.openedFileCount for item in control_observations)
        self._decoded = sum(item.decodedByteCount for item in control_observations)
        self._snippet = 0
        self._literal_reservations: set[bytes] = set()
        self._reserved_previews: dict[bytes, int] = {}
        self._evidence_by_id: dict[str, Evidence] = {}
        self._diagnostics: list[Diagnostic] = []
        self._coverage: list[CoverageEntry] = []
        self._evidence: list[Evidence] = []
        self._facts: list[Fact] = []
        self._read_ids: set[str] = set()

    def close(self) -> None:
        for key, guard in tuple(self._directory_guards.items()):
            if key:
                guard.close()
        self._root_guard.close()

    def _validate_controls(self) -> None:
        aggregates = [item for item in self.control_observations if item.selector.type == "glob" and item.selector.value == "**/.gitignore"]
        if len(aggregates) != 1:
            raise ValueError("control handoff requires one aggregate **/.gitignore observation")
        selectors = [item.selector for item in self.control_observations]
        if len(set(selectors)) != len(selectors):
            raise ValueError("control handoff contains duplicate selectors")
        aggregate = aggregates[0]
        files = [item for item in self.control_observations if item.selector.type == "file"]
        if len(files) + 1 != len(self.control_observations):
            raise ValueError("control handoff contains an unsupported selector")
        if aggregate.selector != InputSelector("glob", "**/.gitignore", ".", True):
            raise ValueError("control aggregate selector must be required root **/.gitignore")
        if aggregate.contentHash is not None or aggregate.openedFileCount != 0 or aggregate.decodedByteCount != 0:
            raise ValueError("control aggregate cannot contain file bytes or budget charges")
        if aggregate.outcome not in {"matched", "no-match", "capped", "io-error"}:
            raise ValueError("control aggregate has an invalid outcome")
        if any(item.selector.value != unicodedata.normalize("NFC", item.selector.value) or "\\" in item.selector.value or item.selector.scope != "." for item in self.control_observations):
            raise ValueError("control handoff selectors must be canonical root paths")
        if any(item.selector.required is not True for item in files):
            raise ValueError("per-file control selectors must be required")
        if any(item.selector.value.rsplit("/", 1)[-1] != ".gitignore" for item in files):
            raise ValueError("per-file control selector must name a .gitignore file")
        if any(item.selector.value.startswith("/") or any(part in {"", ".", ".."} for part in item.selector.value.split("/")) for item in files):
            raise ValueError("per-file control selector must be a canonical repository path")
        if tuple(sorted(item.selector.value for item in files)) != aggregate.matchPaths:
            raise ValueError("control aggregate paths differ from file observations")
        if any(item.matchPaths or item.matchSetHash is not None for item in files):
            raise ValueError("per-file control observations cannot contain match sets")
        for item in files:
            if item.outcome not in {"present", "capped", "decode-error", "io-error"}:
                raise ValueError("per-file control observation has an invalid outcome")
            if item.contentHash is not None and item.outcome not in {"present"}:
                raise ValueError("only a present control observation can carry a source hash")
            if item.outcome == "present" and item.contentHash is None:
                raise ValueError("present control observation requires its source hash")
            if item.outcome == "present" and item.openedFileCount != 1:
                raise ValueError("present control observation must account for one opened file")
            if item.outcome == "decode-error" and (item.openedFileCount != 1 or item.decodedByteCount != 0):
                raise ValueError("decode-error control observation has inconsistent budgets")
            if item.outcome == "io-error" and (item.openedFileCount not in {0, 1} or item.decodedByteCount != 0):
                raise ValueError("io-error control observation has inconsistent budgets")
            if item.outcome == "capped" and (item.openedFileCount not in {0, 1} or item.decodedByteCount != 0):
                raise ValueError("capped control observation has inconsistent budgets")
            if item.outcome == "present" and item.decodedByteCount > self.policy.maxFileBytes:
                raise ValueError("control observation decoded bytes exceed the per-file cap")
            if item.outcome == "present" and not re.fullmatch(r"sha256:[0-9a-f]{64}", item.contentHash or ""):
                raise ValueError("present control observation has an invalid source hash")
        if aggregate.outcome in {"matched", "no-match"} and any(item.outcome != "present" for item in files):
            raise ValueError("complete control aggregate contains an incomplete file observation")
        if aggregate.outcome in {"matched", "no-match"}:
            if (aggregate.outcome == "matched") != bool(aggregate.matchPaths):
                raise ValueError("complete control aggregate outcome disagrees with its match paths")
            if (aggregate.outcome == "matched") != bool(files):
                raise ValueError("complete control aggregate outcome disagrees with file observations")
            expected = "sha256:" + hashlib.sha256(canonical_json_bytes(list(aggregate.matchPaths))).hexdigest()
            if aggregate.matchSetHash != expected:
                raise ValueError("control aggregate match hash is inconsistent")
        elif aggregate.matchSetHash is not None:
            raise ValueError("incomplete control aggregate cannot have a match hash")
        if tuple(sorted(set(aggregate.matchPaths))) != aggregate.matchPaths:
            raise ValueError("control aggregate match paths must be sorted and unique")
        for item in files:
            if item.outcome == "present" and item.contentHash is None:
                raise ValueError("present control observation requires its source hash")
            if item.openedFileCount < 0 or item.decodedByteCount < 0:
                raise ValueError("control observation budgets cannot be negative")
        if sum(item.openedFileCount for item in files) > self.policy.maxOpenedFiles or sum(item.decodedByteCount for item in files) > self.policy.maxDecodedBytes:
            raise ValueError("control observation budgets exceed policy caps")

    def __enter__(self) -> ExtractionSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    @property
    def opened_file_count(self) -> int:
        return self._opened

    @property
    def decoded_byte_count(self) -> int:
        return self._decoded

    @property
    def control_reads(self) -> tuple[ExtractionRead, ...]:
        reads = []
        for observation in self.control_observations:
            selector = observation.selector
            read_id = self.id_context.record_id("read", {"snapshot": self.snapshot_id, "extractor": "inventory", "rule": "inventory.ignore-control", "selector": selector})
            reads.append(ExtractionRead(1, read_id, self.snapshot_id, "inventory", INVENTORY_VERSION,
                "inventory.ignore-control", INVENTORY_VERSION, selector.type, selector, selector.required,
                observation.outcome, observation.contentHash, observation.matchPaths,
                observation.matchSetHash, ()))
        return tuple(reads)

    def bind(self, extractor_id: str, version: str, rule_id: str, rule_version: str) -> ExtractionContext:
        if not extractor_id or not version or not rule_id or not rule_version:
            raise ValueError("extractor and rule provenance fields are required")
        if "." not in rule_id or not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", rule_id):
            raise ValueError("rule_id must be globally namespaced")
        def read(selector: InputSelector) -> ReadResult:
            return self._read(extractor_id, version, rule_id, rule_version, selector)
        return ExtractionContext(self.root, self.snapshot_id, self.inventory, self.policy, read)

    def _diagnose(self, code: str, path: str | None, extractor_id: str) -> str:
        diagnostic_id = self.id_context.record_id("diagnostic", {"snapshot": self.snapshot_id, "code": code, "path": path, "extractor": extractor_id})
        self._diagnostics.append(Diagnostic(1, code, "warning", code.replace("-", " "), path, extractor_id, {}))
        return diagnostic_id

    def _read(self, extractor_id: str, version: str, rule_id: str, rule_version: str, selector: InputSelector) -> ReadResult:
        key = (extractor_id, rule_id, _selector_key(selector))
        if key in self._logical:
            return self._logical[key]
        if selector.scope != ".":
            raise ValueError("P0 selectors must use root scope '.'")
        diagnostics: tuple[str, ...] = ()
        content_hash: str | None = None
        match_paths: tuple[str, ...] = ()
        match_hash: str | None = None
        payload: bytes | None = None
        encoding: str | None = None
        error: str | None = None
        outcome: str
        try:
            control = self._controls.get(selector)
            if control is not None:
                content_hash = control.contentHash
                match_paths, match_hash = control.matchPaths, control.matchSetHash
                outcome = control.outcome
            elif selector.type == "file":
                path = unicodedata.normalize("NFC", selector.value.replace("\\", "/"))
                if path != selector.value or path.startswith("/") or any(p in {"", ".", ".."} for p in path.split("/")):
                    raise ValueError("invalid canonical file selector")
                entry = self._entries.get(path)
                intrinsic = any(path == name or path.startswith(name + "/") for name in (".git", ".repo2skill", ".superpowers", ".ssh", ".aws", ".gnupg"))
                if self._is_excluded(path) or intrinsic:
                    outcome = "io-error"
                    diagnostics = (self._diagnose("read-forbidden", path, extractor_id),)
                    error = "read-forbidden"
                elif entry is None:
                    if self._inventory_incomplete:
                        outcome, error = "capped", "inventory-incomplete"
                        diagnostics = (self._diagnose("read-inventory-incomplete", path, extractor_id),)
                    else:
                        outcome = "missing"
                elif entry.kind != "file" or entry.ignored or entry.generated:
                    outcome = "io-error"
                    diagnostics = (self._diagnose("read-forbidden", path, extractor_id),)
                    error = "read-forbidden"
                else:
                    payload, encoding, error, outcome, content_hash = self._capture(path, extractor_id)
            else:
                if selector.type == "glob":
                    if selector.value.startswith("!") or selector.value.startswith("/") or "\\" in selector.value or any(p in {".", ".."} for p in selector.value.split("/")):
                        raise ValueError("invalid positive glob selector")
                    spec = GitIgnoreSpec.from_lines([selector.value], backend="simple")
                    found = [path for path, entry in self._entries.items() if entry.kind == "file" and not entry.ignored and not entry.generated and not self._is_excluded(path) and spec.match_file(path)]
                else:
                    prefix, sep, name = selector.value.partition(":")
                    if not sep or prefix != "filename-absent" or not name or "/" in name or "\\" in name or name in {".", ".."} or re.search(r"[\*?\[\]\x00-\x1f]", name):
                        raise ValueError("unknown negative predicate")
                    found = [path for path, entry in self._entries.items() if entry.kind == "file" and not entry.ignored and not entry.generated and not self._is_excluded(path) and path.rsplit("/", 1)[-1] == name]
                match_paths = tuple(sorted(found))
                if self._inventory_incomplete:
                    match_hash = None
                    outcome, error = "capped", "inventory-incomplete"
                    diagnostics = (self._diagnose("read-inventory-incomplete", None, extractor_id),)
                else:
                    match_hash = _hash(canonical_json_bytes(list(match_paths)))
                    outcome = "matched" if match_paths else "no-match"
        except ValueError:
            raise
        except Exception:
            outcome, error = "io-error", "io-error"
            diagnostics = (self._diagnose("read-io-error", selector.value if selector.type == "file" else None, extractor_id),)
        read_id = self.id_context.record_id("read", {"snapshot": self.snapshot_id, "extractor": extractor_id, "rule": rule_id, "selector": selector})
        dependency = ExtractionRead(1, read_id, self.snapshot_id, extractor_id, version, rule_id, rule_version, selector.type, selector, selector.required, outcome, content_hash, match_paths, match_hash, diagnostics)  # type: ignore[arg-type]
        result = ReadResult(selector, dependency, payload, encoding, error)
        self._logical[key] = result
        if outcome in {"capped", "decode-error", "io-error"}:
            self._coverage.append(CoverageEntry(1, selector, extractor_id, "capped" if outcome == "capped" else "failed", f"read-{outcome}", diagnostics))
        return result

    def _is_excluded(self, path: str) -> bool:
        return any(path == base or path.startswith(base.rstrip("/") + "/") for base in self.excluded_paths)

    def _guard_for(self, directory: str) -> GuardedDirectory:
        if directory in self._directory_guards:
            return self._directory_guards[directory]
        parent, _, name = directory.rpartition("/")
        parent_guard = self._guard_for(parent)
        actual = self._actual_name(parent_guard, name)
        guard = parent_guard.open_directory(actual)
        self._directory_guards[directory] = guard
        return guard

    @staticmethod
    def _actual_name(directory: GuardedDirectory, canonical: str) -> str:
        matches = [name for name in directory.names() if unicodedata.normalize("NFC", name) == canonical]
        if len(matches) != 1:
            raise UnsafePathError("canonical path is missing or ambiguous")
        return matches[0]

    def _capture(self, path: str, extractor_id: str) -> tuple[bytes | None, str | None, str | None, str, str | None]:
        if path in self._captures:
            return self._captures[path]
        if self._opened >= self.policy.maxOpenedFiles:
            result = (None, None, "open-budget", "capped", None)
            self._captures[path] = result
            return result
        directory, _, leaf = path.rpartition("/")
        did_open = False
        try:
            parent = self._guard_for(directory)
            actual = self._actual_name(parent, leaf)
            with parent.open_file(actual) as guarded:
                did_open = True
                self._opened += 1
                raw = guarded.read_bounded(self.policy.maxFileBytes)
                if len(raw) > self.policy.maxFileBytes:
                    result = (None, None, "file-cap", "capped", None)
                elif not guarded.stable_after_read():
                    result = (None, None, "unstable", "io-error", None)
                else:
                    content_hash = _hash(raw)
                    selected = None
                    for candidate in self.policy.encodings:
                        try:
                            raw.decode(candidate)
                            selected = candidate
                            break
                        except (LookupError, UnicodeError):
                            continue
                    if selected is None:
                        result = (None, None, "decode-error", "decode-error", content_hash)
                    elif self._decoded + len(raw) > self.policy.maxDecodedBytes:
                        result = (None, None, "decoded-budget", "capped", None)
                    else:
                        self._decoded += len(raw)
                        result = (raw, selected, None, "present", content_hash)
        except UnsafePathError as ex:
            if ex.handle_opened and not did_open:
                self._opened += 1
            result = (None, None, "io-error", "io-error", None)
        except OSError:
            result = (None, None, "io-error", "io-error", None)
        self._captures[path] = result
        return result

    def add_evidence(self, *, extractor_id: str, path: str, content_hash: str, semantic_key: str,
                     media_type: str, encoding: str | None, locator: EvidenceLocator,
                     preview: str | None) -> Evidence:
        identity = {"repository": self.repository_id, "path": path, "locator": locator, "semanticKey": semantic_key}
        evidence_id = self.id_context.record_id("evidence", identity)
        existing = self._evidence_by_id.get(evidence_id)
        if existing is not None:
            return existing
        if preview is not None:
            preview = redact_text(preview)
            encoded = preview.encode("utf-8")
            preview_key = canonical_json_bytes(identity)
            reserved = self._reserved_previews.pop(preview_key, None)
            if reserved is not None:
                if reserved != len(encoded):
                    raise ValueError("reserved preview differs from emitted preview")
            else:
                remaining = self.policy.maxSnippetBytes - self._snippet
                if len(encoded) > remaining:
                    encoded = encoded[:max(0, remaining)]
                    while True:
                        try:
                            preview = encoded.decode("utf-8")
                            break
                        except UnicodeDecodeError:
                            encoded = encoded[:-1]
                    self._coverage.append(CoverageEntry(1, InputSelector("file", path, ".", False), extractor_id, "capped", "snippet-budget", ()))
                self._snippet += len(preview.encode("utf-8"))
        item = Evidence(1, evidence_id, 1, self.snapshot_id, path, content_hash, semantic_key, media_type, encoding, locator, preview, "available")
        item = revise_record(self.prior_evidence.get(evidence_id), item, semantic_payload=lambda e: {"contentHash": e.contentHash, "locator": e.locator, "preview": e.redactedPreview}, revision_field="revision")
        self._evidence.append(item)
        self._evidence_by_id[evidence_id] = item
        return item

    def add_fact(self, *, extractor_id: str, rule_id: str, rule_version: str, fact_type: str,
                 scope: str, semantic_key: str, value: object, derivation: str,
                 evidence_ids: Sequence[str]) -> Fact | None:
        safe_value = redact_value(None, value)
        identity = self._fact_identity(scope, fact_type, semantic_key)
        fact_id = self.id_context.record_id("fact", identity)
        item = Fact(1, fact_id, 1, self.snapshot_id, fact_type, semantic_key, scope, safe_value, derivation, rule_id, rule_version, tuple(evidence_ids))  # type: ignore[arg-type]
        item = revise_record(self.prior_facts.get(fact_id), item, semantic_payload=lambda f: f.value, revision_field="factRevision")
        self._facts.append(item)
        return item

    def _bounded_text(self, text: str) -> str:
        return redact_text(text)

    def _fact_identity(self, scope: str, fact_type: str, semantic_key: str) -> dict[str, object]:
        return {"repository": self.repository_id, "scope": scope, "factType": fact_type, "semanticKey": semantic_key}

    def reserve_fact_text(self, text: str, *, rule_id: str, fact_type: str, scope: str,
                          semantic_key: str, additional_preview_bytes: int = 0,
                          preview_identity: object | None = None) -> bool:
        key = canonical_json_bytes(self._fact_identity(scope, fact_type, semantic_key))
        text_cost = 0 if key in self._literal_reservations else len(text.encode("utf-8"))
        preview_key = canonical_json_bytes(preview_identity) if preview_identity is not None else None
        preview_cost = 0 if preview_key is None or preview_key in self._reserved_previews else additional_preview_bytes
        if self._snippet + text_cost + preview_cost > self.policy.maxSnippetBytes:
            return False
        if text_cost:
            self._snippet += text_cost
            self._literal_reservations.add(key)
        if preview_key is not None and preview_cost:
            self._snippet += preview_cost
            self._reserved_previews[preview_key] = preview_cost
        return True

    def make_batch(self, reads: Sequence[ExtractionRead] = ()) -> ExtractionBatch:
        return ExtractionBatch(tuple(reads), tuple(self._evidence), tuple(self._facts), tuple(self._diagnostics), tuple(self._coverage))
