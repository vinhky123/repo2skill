"""Maintenance-only extractor for bounded fragments of committed source blobs."""
from __future__ import annotations

from collections.abc import Sequence

from .extractors.base import ExtractionSession
from .models import EvidenceLocator, ExtractionBatch, ExtractionContext, InputSelector


EXTRACTOR_ID = "maintenance.source-fragment"
EXTRACTOR_VERSION = "1"
RULE_ID = "maintenance.source-fragment"
RULE_VERSION = "1"
MAX_FRAGMENT_BYTES = 32 * 1024
MAX_FRAGMENT_LINES = 256


def maintenance_extractor_versions() -> dict[str, str]:
    """Return current P0 extractor versions plus this maintenance-only reader."""
    from .pipeline import extractor_versions

    return {**extractor_versions(), EXTRACTOR_ID: EXTRACTOR_VERSION}


class SourceFragmentExtractor:
    extractor_id = EXTRACTOR_ID
    version = EXTRACTOR_VERSION
    rule_id = RULE_ID
    rule_version = RULE_VERSION

    def __init__(self, session: ExtractionSession):
        self.session = session

    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]:
        return tuple(InputSelector("file", item.path, ".", True)
                     for item in ctx.inventory.entries
                     if item.kind == "file" and not item.ignored and not item.generated)

    def extract(self, ctx: ExtractionContext, selectors: Sequence[InputSelector]) -> ExtractionBatch:
        reads = []
        for selector in selectors:
            result = ctx.read(selector)
            reads.append(result.dependency)
            if result.bytes is None or result.dependency.contentHash is None:
                continue
            text = result.bytes.decode(result.encoding or "utf-8-sig", errors="strict")
            lines = []
            used_bytes = 0
            for line in text.splitlines():
                line_bytes = len(line.encode(result.encoding or "utf-8-sig")) + (1 if lines else 0)
                if len(lines) >= MAX_FRAGMENT_LINES or used_bytes + line_bytes > MAX_FRAGMENT_BYTES:
                    break
                lines.append(line)
                used_bytes += line_bytes
            if not lines:
                continue
            start, end = 1, len(lines)
            fragment = "\n".join(lines)
            path = selector.value
            semantic_key = f"{path}:{start}-{end}"
            evidence = self.session.add_evidence(
                extractor_id=self.extractor_id, path=path,
                content_hash=result.dependency.contentHash,
                semantic_key=f"source-fragment:{path}:{start}-{end}",
                media_type="text/plain", encoding=result.encoding,
                locator=EvidenceLocator("line-range", [start, end], start), preview=None,
            )
            self.session.add_fact(
                extractor_id=self.extractor_id, rule_id=self.rule_id,
                rule_version=self.rule_version, fact_type="source.fragment.v1",
                scope=".", semantic_key=semantic_key,
                value={"path": path, "start": start, "end": end, "text": fragment},
                derivation="observed", evidence_ids=(evidence.evidenceId,),
            )
        return self.session.make_batch(reads)
