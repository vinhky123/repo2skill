"""Shared typed-record helpers for shallow ecosystem rules."""
from __future__ import annotations

from collections.abc import Sequence

from ..models import CoverageEntry, EvidenceLocator, ExtractionBatch, ExtractionContext, ExtractionRead, InputSelector, ReadResult
from .base import ExtractionSession


def file_selector(path: str) -> InputSelector:
    return InputSelector("file", path, ".", False)


def glob_selector(pattern: str) -> InputSelector:
    return InputSelector("glob", pattern, ".", False)


def selectors(paths: Sequence[str], patterns: Sequence[str] = ()) -> tuple[InputSelector, ...]:
    return tuple(sorted({*(file_selector(path) for path in paths), *(glob_selector(pattern) for pattern in patterns)},
                        key=lambda item: (item.scope, item.type, item.value, item.required)))


def record_fact(
    session: ExtractionSession,
    *, extractor_id: str,
    rule_id: str,
    rule_version: str,
    result: ReadResult,
    path: str,
    fact_type: str,
    semantic_key: str,
    value: dict[str, object],
    derivation: str,
    locator: EvidenceLocator,
    preview: str | None = None,
):
    if result.bytes is None or result.dependency.contentHash is None:
        return None
    evidence = session.add_evidence(
        extractor_id=extractor_id,
        path=path,
        content_hash=result.dependency.contentHash,
        semantic_key=semantic_key,
        media_type="application/json" if path.endswith(".json") else "application/toml" if path.endswith(".toml") else "text/plain",
        encoding=result.encoding,
        locator=locator,
        preview=preview,
    )
    return session.add_fact(
        extractor_id=extractor_id,
        rule_id=rule_id,
        rule_version=rule_version,
        fact_type=fact_type,
        scope=".",
        semantic_key=semantic_key,
        value=value,
        derivation=derivation,
        evidence_ids=(evidence.evidenceId,),
    )


def finish(reads: Sequence[ExtractionRead]) -> ExtractionBatch:
    return ExtractionBatch(tuple(reads), (), (), (), ())


def invalid_input(session: ExtractionSession, extractor_id: str, selector: InputSelector, path: str) -> None:
    diagnostic_id = session._diagnose("parse-invalid", path, extractor_id)
    session._coverage.append(CoverageEntry(1, selector, extractor_id, "invalid", "parse-invalid", (diagnostic_id,)))


def read_result(ctx: ExtractionContext, selector: InputSelector, reads: list[ExtractionRead]) -> ReadResult:
    result = ctx.read(selector)
    reads.append(result.dependency)
    return result


def line_locator(text: str, line_number: int) -> EvidenceLocator:
    return EvidenceLocator("line-range", [line_number, line_number], line_number)
