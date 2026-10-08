"""Safe parsers for selected, already-captured immutable input bytes."""
from __future__ import annotations

import json
import tomllib
from typing import Any

import yaml


def _assert_acyclic(node: yaml.Node, active: set[int], complete: set[int]) -> None:
    identity = id(node)
    if identity in active:
        raise ValueError("recursive YAML alias")
    if identity in complete:
        return
    active.add(identity)
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            _assert_acyclic(key, active, complete)
            _assert_acyclic(value, active, complete)
    elif isinstance(node, yaml.SequenceNode):
        for child in node.value:
            _assert_acyclic(child, active, complete)
    active.remove(identity)
    complete.add(identity)


def parse_yaml(text: str) -> Any:
    node = yaml.compose(text, Loader=yaml.BaseLoader)
    if node is None:
        return None
    _assert_acyclic(node, set(), set())
    return yaml.load(text, Loader=yaml.BaseLoader)


def parse_selected(text: str, format: str) -> Any:
    if format == "json":
        def reject_constant(_value: str) -> None:
            raise ValueError("invalid JSON number")
        return json.loads(text, parse_constant=reject_constant)
    if format == "toml":
        return tomllib.loads(text)
    if format == "yaml":
        return parse_yaml(text)
    raise ValueError("unsupported structured format")


def present_key_paths(value: Any, allowed: tuple[str, ...]) -> tuple[str, ...]:
    present: list[str] = []
    for path in allowed:
        current = value
        for component in path.split("."):
            if not isinstance(current, dict) or component not in current:
                break
            current = current[component]
        else:
            present.append(path)
    return tuple(sorted(present))
