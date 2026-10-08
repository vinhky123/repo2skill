"""Manifest-backed shallow Go facts and fixed command templates."""
from __future__ import annotations

import re
from collections.abc import Sequence

from ..models import EvidenceLocator, ExtractionBatch, ExtractionContext, ExtractionRead, InputSelector
from ._ecosystem import finish, invalid_input, line_locator, read_result, record_fact, selectors
from .base import ExtractionSession


_MODULE = re.compile(r"^\s*module\s+([^\s]+)\s*$")
class GoExtractor:
    extractor_id = "go"
    version = "1.0"
    rule_id = "go.module"
    rule_version = "go-shallow-v1"

    def __init__(self, session: ExtractionSession):
        self.session = session

    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]:
        return selectors(("go.mod",))

    def extract(self, ctx: ExtractionContext, selected: Sequence[InputSelector]) -> ExtractionBatch:
        reads: list[ExtractionRead] = []
        selector = selected[0]
        result = read_result(ctx, selector, reads)
        if result.bytes is None:
            return finish(reads)
        text = result.bytes.decode(result.encoding or "utf-8-sig")
        lines = text.splitlines()
        module = None
        dependencies = []
        in_require = False
        recognized = False
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            if stripped == "require (":
                in_require = True
                recognized = True
                continue
            if in_require and stripped == ")":
                in_require = False
                continue
            match = _MODULE.match(line)
            if match:
                module = (match.group(1), number)
                recognized = True
                continue
            if in_require or stripped.startswith("require "):
                content = stripped.removeprefix("require ") if stripped.startswith("require ") else stripped
                match = re.match(r"^([^\s]+)\s+(v[^\s]+)(?:\s*//.*)?$", content)
                if match:
                    dependencies.append((match.group(1), match.group(2), number))
                    recognized = True
        if not module or not recognized:
            invalid_input(self.session, self.extractor_id, selector, "go.mod")
            return finish(reads)
        record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                    result=result, path="go.mod", fact_type="artifact.presence",
                    semantic_key="artifact.presence:ecosystem:go:go.mod",
                    value={"category": "ecosystem", "name": "go", "path": "go.mod"}, derivation="observed",
                    locator=line_locator(text, module[1]))
        # Module-path metadata is a direct projection of the module directive.
        record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                    result=result, path="go.mod", fact_type="artifact.presence",
                    semantic_key="artifact.presence:package.v1:go-module",
                    value={"category": "package.v1", "name": module[0], "path": "go.mod"}, derivation="observed",
                    locator=line_locator(text, module[1]))
        for dependency, version, number in dependencies:
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=result, path="go.mod", fact_type="artifact.presence",
                        semantic_key=f"artifact.presence:dependency.v1:{dependency}:go.mod",
                        value={"category": "dependency.v1", "name": f"{dependency}@{version}", "path": "go.mod"},
                        derivation="observed", locator=line_locator(text, number))
        for category, argv in (("test", ["go", "test", "./..."]),
                               ("single-test.v1", ["go", "test", "./...", "-run", "<TestName>"])):
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=result, path="go.mod", fact_type="command.invocation",
                        semantic_key=f"command.invocation:{category}:go:repo-root",
                        value={"category": category, "argv": argv}, derivation="template",
                        locator=line_locator(text, module[1]))
        return finish(reads)
