from __future__ import annotations

from collections.abc import Iterable

from ..ids import canonical_json_bytes
from ..models import ExtractionBatch
from .base import ExtractionSession, Extractor


def discover_extractors(extractors: Iterable[Extractor]) -> tuple[Extractor, ...]:
    return tuple(sorted(extractors, key=lambda item: (item.extractor_id, item.version)))


def run_extractors(session: ExtractionSession, extractors: Iterable[Extractor]) -> ExtractionBatch:
    reads = list(session.control_reads)
    for extractor in discover_extractors(extractors):
        rule_id = getattr(extractor, "rule_id", f"{extractor.extractor_id}.extract")
        rule_version = getattr(extractor, "rule_version", extractor.version)
        context = session.bind(extractor.extractor_id, extractor.version, rule_id, rule_version)
        selectors = tuple(sorted(extractor.discover(context), key=lambda selector: (selector.scope, selector.type, selector.value, selector.required)))
        batch = extractor.extract(context, selectors)
        reads.extend(batch.reads)
    unique_reads = {(item.extractorId, item.ruleId, canonical_json_bytes(item.selector)): item for item in reads}
    sorted_reads = tuple(sorted(unique_reads.values(), key=lambda item: (item.extractorId, item.ruleId, item.dependencyType, canonical_json_bytes(item.selector), item.selector.scope)))
    batch = session.make_batch(sorted_reads)
    return ExtractionBatch(batch.reads,
        tuple(sorted(batch.evidence, key=lambda item: item.evidenceId)),
        tuple(sorted(batch.facts, key=lambda item: item.factId)),
        tuple(sorted(batch.diagnostics, key=lambda item: (item.code, item.path or "", item.extractorId or ""))),
        tuple(sorted(batch.coverage, key=lambda item: (item.collector, item.selector.scope, item.selector.type, item.selector.value, item.status, item.reasonCode))))
