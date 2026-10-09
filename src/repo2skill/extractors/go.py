"""Manifest-backed shallow Go facts and fixed command templates."""
from __future__ import annotations

import re
from collections.abc import Sequence

from ..models import CoverageEntry, EvidenceLocator, ExtractionBatch, ExtractionContext, ExtractionRead, InputSelector
from ._ecosystem import finish, invalid_input, line_locator, read_result, record_fact, selectors
from .base import ExtractionSession


_MODULE = re.compile(r"^\s*module\s+([^\s]+)(?:\s+//.*)?$")
_GO_VERSION = re.compile(r"^\s*go\s+(\d+\.\d+(?:\.\d+)?)(?:\s+//.*)?$")
_REQUIRE = re.compile(r"^([^\s()]+)\s+(v[^\s]+)(?:\s+//\s*indirect)?$")


def _unsupported(session: ExtractionSession, selector: InputSelector, path: str) -> None:
    diagnostic_id = session._diagnose("go-mod-unsupported-directive", path, "go")
    session._coverage.append(CoverageEntry(1, selector, "go", "unsupported", "go-mod-unsupported-directive", (diagnostic_id,)))


def _invalid_syntax(session: ExtractionSession, selector: InputSelector) -> None:
    diagnostic_id = session._diagnose("go-mod-unsupported-syntax", "go.mod", "go")
    session._coverage.append(CoverageEntry(1, selector, "go", "invalid", "go-mod-unsupported-syntax", (diagnostic_id,)))


class GoExtractor:
    extractor_id = "go"
    version = "1.1"
    rule_id = "go.module"
    rule_version = "go-shallow-v2"

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
        seen_go = False
        invalid = False
        unsupported = False
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("//"):
                continue
            if in_require:
                if stripped == ")":
                    in_require = False
                    continue
                match = _REQUIRE.match(stripped)
                if match:
                    dependencies.append((match.group(1), match.group(2), number))
                else:
                    invalid = True
                    break
                continue
            match = _MODULE.match(line)
            if match:
                if module is not None:
                    invalid = True
                    break
                module = (match.group(1), number)
                continue
            if re.match(r"^module(?:\s|$)", stripped):
                invalid = True
                break
            match = _GO_VERSION.match(line)
            if match:
                if seen_go:
                    invalid = True
                    break
                seen_go = True
                continue
            if re.match(r"^go(?:\s|$)", stripped):
                invalid = True
                break
            if stripped in {"require (", "require("}:
                in_require = True
                continue
            if re.match(r"^require(?:\s|$)", stripped):
                content = re.sub(r"^require\s+", "", stripped, count=1)
                if content == "(":
                    in_require = True
                    continue
                match = _REQUIRE.match(content)
                if match:
                    dependencies.append((match.group(1), match.group(2), number))
                else:
                    invalid = True
                    break
                continue
            if stripped == ")":
                invalid = True
                break
            # Keep facts from the supported subset, but make every other Go
            # directive visible so it cannot look like a complete parse.
            unsupported = True
        if in_require:
            invalid = True
        if invalid:
            _invalid_syntax(self.session, selector)
            return finish(reads)
        if not module:
            invalid_input(self.session, self.extractor_id, selector, "go.mod")
            return finish(reads)
        if unsupported:
            _unsupported(self.session, selector, "go.mod")
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
                        semantic_key=f"command.invocation:{category}:go:{'one-test' if category == 'single-test.v1' else 'repo-root'}",
                        value={"category": category, "argv": argv}, derivation="template",
                        locator=line_locator(text, module[1]))
        return finish(reads)
