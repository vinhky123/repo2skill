from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import dataclasses
import hashlib
import json
import re
from typing import Literal, TypeVar, cast

from .models import ClaimPolicy, JsonValue, ScanPolicy, TargetSpec, validate_json_value


T = TypeVar("T")


class IdCollisionError(ValueError):
    """A truncated stable ID was produced for two different identities."""


def _json_compatible(value: object) -> JsonValue:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_compatible(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, tuple):
        return [_json_compatible(item) for item in value]
    if isinstance(value, list):
        return [_json_compatible(item) for item in value]
    if isinstance(value, Mapping):
        converted: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("canonical JSON object keys must be strings")
            converted[key] = _json_compatible(item)
        return converted
    validate_json_value(value)
    return cast(JsonValue, value)


def canonical_json_bytes(value: object) -> bytes:
    compatible = _json_compatible(value)
    validate_json_value(compatible)
    return json.dumps(
        compatible,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest_suffix(canonical: bytes) -> str:
    return hashlib.sha256(canonical).hexdigest()[:24]


def canonical_record_id(prefix: str, identity: JsonValue) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9-]*", prefix):
        raise ValueError("ID prefix must be lowercase and URL-safe")
    return f"{prefix}_{_digest_suffix(canonical_json_bytes(identity))}"


class CanonicalIdContext:
    """Collision index with an explicit lifetime chosen by the caller."""

    def __init__(self) -> None:
        self._identities: dict[str, bytes] = {}

    def record_id(self, prefix: str, identity: JsonValue) -> str:
        canonical = canonical_json_bytes(identity)
        record_id = canonical_record_id(prefix, identity)
        previous = self._identities.setdefault(record_id, canonical)
        if previous != canonical:
            raise IdCollisionError(f"truncated canonical ID collision: {record_id}")
        return record_id


def revise_record(
    previous: T | None,
    new: T,
    *,
    semantic_payload: Callable[[T], JsonValue],
    revision_field: Literal["revision", "factRevision", "claimRevision"],
) -> T:
    if not dataclasses.is_dataclass(new) or isinstance(new, type):
        raise TypeError("new must be a dataclass instance")
    id_fields = {
        "revision": "evidenceId",
        "factRevision": "factId",
        "claimRevision": "claimId",
    }
    id_field = id_fields[revision_field]
    if not hasattr(new, revision_field) or not hasattr(new, id_field):
        raise TypeError(f"record does not contain {revision_field} and {id_field}")
    if previous is None:
        revision = 1
    else:
        if type(previous) is not type(new):
            raise TypeError("previous and new records must have the same type")
        if getattr(previous, id_field) != getattr(new, id_field):
            raise ValueError("previous record has a different stable ID")
        prior_revision = getattr(previous, revision_field)
        if canonical_json_bytes(semantic_payload(previous)) == canonical_json_bytes(semantic_payload(new)):
            revision = prior_revision
        else:
            revision = prior_revision + 1
    return cast(T, dataclasses.replace(new, **{revision_field: revision}))


def effective_policy_revision(
    scan_policy: ScanPolicy,
    claim_policy: ClaimPolicy,
    configured_targets: Sequence[TargetSpec],
    *,
    generation_options: Mapping[str, JsonValue],
) -> str:
    if generation_options:
        raise ValueError("generation_options must be empty in P0")
    projection = {
        "scanPolicy": _json_compatible(scan_policy),
        "claimPolicy": _json_compatible(claim_policy),
        "configuredTargets": [
            _json_compatible(target)
            for target in sorted(configured_targets, key=lambda item: item.targetId)
        ],
        "generationOptions": {},
    }
    return "sha256:" + hashlib.sha256(canonical_json_bytes(projection)).hexdigest()
