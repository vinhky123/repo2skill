from __future__ import annotations

from collections.abc import Iterable

from ..ids import canonical_json_bytes
from ..models import ExtractionBatch
from .base import ExtractionSession, Extractor


def discover_extractors(extractors: Iterable[Extractor]) -> tuple[Extractor, ...]:
    return tuple(sorted(extractors, key=lambda item: (item.extractor_id, item.version)))


def run_extractors(session: ExtractionSession, extractors: Iterable[Extractor]) -> ExtractionBatch:
    ordered_extractors = discover_extractors(extractors)
    ids: dict[str, str] = {}
    for extractor in ordered_extractors:
        previous = ids.setdefault(extractor.extractor_id, extractor.version)
        if previous != extractor.version:
            raise ValueError(f"duplicate extractor ID has conflicting versions: {extractor.extractor_id}")
        if sum(1 for item in ordered_extractors if item.extractor_id == extractor.extractor_id) > 1:
            raise ValueError(f"duplicate extractor ID: {extractor.extractor_id}")

    reads = list(session.control_reads)
    seen_rule_ids: set[str] = set()
    for extractor in ordered_extractors:
        declared_rules = getattr(extractor, "rules", None)
        rules = tuple(declared_rules) if declared_rules is not None else (extractor,)
        if not rules:
            continue
        work = []
        for rule in rules:
            rule_id = getattr(rule, "rule_id", f"{extractor.extractor_id}.extract")
            rule_version = getattr(rule, "rule_version", extractor.version)
            if rule_id in seen_rule_ids:
                raise ValueError(f"duplicate rule ID: {rule_id}")
            seen_rule_ids.add(rule_id)
            if not callable(getattr(rule, "discover", None)) or not callable(getattr(rule, "extract", None)):
                raise TypeError("each extractor rule must define discover(ctx) and extract(ctx, selectors)")
            work.append((rule_id, rule_version, rule))
        for rule_id, rule_version, rule in sorted(work, key=lambda item: item[0]):
            context = session.bind(extractor.extractor_id, extractor.version, rule_id, rule_version)
            selectors = tuple(sorted(rule.discover(context), key=lambda selector: (selector.scope, selector.type, selector.value, selector.required)))
            batch = rule.extract(context, selectors)
            reads.extend(batch.reads)
    unique_reads = {(item.extractorId, item.ruleId, canonical_json_bytes(item.selector)): item for item in reads}
    sorted_reads = tuple(sorted(unique_reads.values(), key=lambda item: (item.extractorId, item.ruleId, item.dependencyType, canonical_json_bytes(item.selector), item.selector.scope)))
    batch = session.make_batch(sorted_reads)
    return ExtractionBatch(batch.reads,
        tuple(sorted(batch.evidence, key=lambda item: item.evidenceId)),
        tuple(sorted(batch.facts, key=lambda item: item.factId)),
        tuple(sorted(batch.diagnostics, key=lambda item: (item.code, item.path or "", item.extractorId or ""))),
        tuple(sorted(batch.coverage, key=lambda item: (item.collector, item.selector.scope, item.selector.type, item.selector.value, item.status, item.reasonCode))))
