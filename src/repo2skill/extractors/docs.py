from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
import re

from ..models import CoverageEntry, EvidenceLocator, ExtractionBatch, ExtractionContext, InputSelector
from ..redact import redact_text
from .base import ExtractionSession


class DocumentationExtractor:
    extractor_id = "docs"
    version = "1.0"
    rule_id = "docs.normalized-line"
    rule_version = "docs-lines-v1"

    def __init__(self, session: ExtractionSession):
        self.session = session

    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]:
        files = [item.path for item in ctx.inventory.entries if item.kind == "file" and item.path.endswith(".md") and (("/" not in item.path and item.path.rsplit("/", 1)[-1].lower().startswith("readme")) or item.path.startswith("docs/"))]
        selectors = [InputSelector("glob", "[Rr][Ee][Aa][Dd][Mm][Ee]*.md", ".", False), InputSelector("glob", "docs/**/*.md", ".", False)]
        selectors.extend(InputSelector("file", path, ".", False) for path in files)
        return tuple(sorted(set(selectors), key=lambda item: (item.scope, item.type, item.value, item.required)))

    def extract(self, ctx: ExtractionContext, selectors: Sequence[InputSelector]) -> ExtractionBatch:
        reads = [ctx.read(item).dependency for item in selectors if item.type == "glob"]
        evidence_by_text: dict[str, list[str]] = defaultdict(list)
        budget_coverage_selectors: set[InputSelector] = set()

        def record_budget_coverage(selector: InputSelector) -> None:
            if selector not in budget_coverage_selectors:
                budget_coverage_selectors.add(selector)
                self.session._coverage.append(CoverageEntry(
                    1, selector, self.extractor_id, "capped", "documentation-text-budget", (),
                ))

        for selector in selectors:
            if selector.type != "file":
                continue
            result = ctx.read(selector)
            reads.append(result.dependency)
            if result.bytes is None:
                continue
            text = result.bytes.decode(result.encoding or "utf-8-sig")
            for line_number, source_line in enumerate(text.splitlines(), 1):
                normalized = re.sub(r"\s+", " ", source_line).strip()
                if not normalized:
                    continue
                complete_redacted = redact_text(normalized)
                safe = self.session._bounded_text(normalized)
                if safe != complete_redacted:
                    record_budget_coverage(selector)
                    continue
                semantic_key = f"documentation:{safe}"
                locator = EvidenceLocator("line-range", [line_number, line_number], line_number)
                evidence_key = {"repository": self.session.repository_id, "path": selector.value, "locator": locator, "semanticKey": f"documentation-line:{safe}"}
                if not self.session.reserve_fact_text(safe, rule_id=self.rule_id, fact_type="documentation.line", scope=".", semantic_key=semantic_key, additional_preview_bytes=len(safe.encode("utf-8")), preview_identity=evidence_key):
                    record_budget_coverage(selector)
                    continue
                evidence = self.session.add_evidence(extractor_id=self.extractor_id, path=selector.value,
                    content_hash=result.dependency.contentHash or "", semantic_key=f"documentation-line:{safe}",
                    media_type="text/markdown", encoding=result.encoding,
                    locator=locator, preview=safe)
                if evidence.redactedPreview != safe:
                    continue
                evidence_by_text[safe].append(evidence.evidenceId)
        for text, evidence_ids in sorted(evidence_by_text.items()):
            unique = tuple(sorted(set(evidence_ids)))
            self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                fact_type="documentation.line", scope=".", semantic_key=f"documentation:{text}",
                value={"category": "documentation", "text": text}, derivation="observed", evidence_ids=unique)
        batch = self.session.make_batch(reads)
        return ExtractionBatch(tuple(reads), batch.evidence, batch.facts, batch.diagnostics, batch.coverage)
