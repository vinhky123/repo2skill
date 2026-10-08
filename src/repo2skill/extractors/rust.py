"""Shallow Rust package facts and Cargo command templates."""
from __future__ import annotations

from collections.abc import Sequence

from ..models import EvidenceLocator, ExtractionBatch, ExtractionContext, ExtractionRead, InputSelector
from ._ecosystem import finish, invalid_input, line_locator, read_result, record_fact, selectors
from .base import ExtractionSession
from .parsing import parse_selected


class RustExtractor:
    extractor_id = "rust"
    version = "1.0"
    rule_id = "rust.cargo"
    rule_version = "rust-shallow-v1"

    def __init__(self, session: ExtractionSession):
        self.session = session

    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]:
        tests = [entry.path for entry in ctx.inventory.entries if entry.kind == "file" and entry.path.startswith("tests/") and entry.path.endswith(".rs")]
        return selectors(("Cargo.toml", *tests), ("tests/*.rs",))

    def extract(self, ctx: ExtractionContext, selected: Sequence[InputSelector]) -> ExtractionBatch:
        reads: list[ExtractionRead] = []
        results = {selector.value: read_result(ctx, selector, reads) for selector in selected if selector.type == "file"}
        test_glob = next((read_result(ctx, selector, reads) for selector in selected if selector.type == "glob"), None)
        cargo = results.get("Cargo.toml")
        if cargo is None or cargo.bytes is None:
            return finish(reads)
        text = cargo.bytes.decode(cargo.encoding or "utf-8-sig")
        try:
            document = parse_selected(text, "toml")
            package = document.get("package") if isinstance(document, dict) else None
            if not isinstance(package, dict):
                raise ValueError("Cargo manifest has no package table")
        except Exception:
            invalid_input(self.session, self.extractor_id, cargo.selector, "Cargo.toml")
            return finish(reads)
        package_locator = EvidenceLocator("key-path", ["package"], None)
        record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                    result=cargo, path="Cargo.toml", fact_type="artifact.presence",
                    semantic_key="artifact.presence:ecosystem:rust:Cargo.toml",
                    value={"category": "ecosystem", "name": "rust", "path": "Cargo.toml"}, derivation="observed",
                    locator=package_locator)
        name, version = package.get("name"), package.get("version")
        if isinstance(name, str) and name:
            identity = f"{name}@{version}" if isinstance(version, str) and version else name
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=cargo, path="Cargo.toml", fact_type="artifact.presence",
                        semantic_key="artifact.presence:package.v1:Cargo.toml",
                        value={"category": "package.v1", "name": identity, "path": "Cargo.toml"}, derivation="observed",
                        locator=EvidenceLocator("key-path", ["package", "name"], None))
        for category, argv in (("build", ["cargo", "build"]), ("test", ["cargo", "test"]),
                               ("single-test.v1", ["cargo", "test", "<test_name>"])):
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=cargo, path="Cargo.toml", fact_type="command.invocation",
                        semantic_key=f"command.invocation:{category}:cargo:repo-root",
                        value={"category": category, "argv": argv}, derivation="template", locator=package_locator)
        for path in (test_glob.dependency.matchPaths if test_glob else ()):
            result = results.get(path)
            if result is None:
                result = read_result(ctx, InputSelector("file", path, ".", False), reads)
                results[path] = result
            if result.bytes is None:
                continue
            source = result.bytes.decode(result.encoding or "utf-8-sig")
            if "#[test]" in source or "#[tokio::test]" in source:
                line_no, line = next((i, line) for i, line in enumerate(source.splitlines(), 1) if "#[test]" in line or "#[tokio::test]" in line)
                record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                            result=result, path=path, fact_type="artifact.presence",
                            semantic_key="artifact.presence:convention.v1:integration-tests-directory",
                            value={"category": "convention.v1", "name": "integration-tests-directory", "path": path},
                            derivation="heuristic", locator=EvidenceLocator("line-range", [line_no, line_no], line_no), preview=line)
                break
        return finish(reads)
