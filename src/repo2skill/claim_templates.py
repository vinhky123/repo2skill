"""Pure, versioned claim text formatters shared by deduplication and rendering."""
from __future__ import annotations

import json
import re
from typing import Final
import unicodedata

from .models import JsonValue


TEMPLATE_KEYS: Final = frozenset({
    "artifact-presence.v1", "task-definition.v1", "command-invocation.v1", "config-structure.v1",
})

_ARTIFACT_CATEGORIES = frozenset({
    "ci-workflow", "ci-job", "container", "ecosystem", "package.v1", "package-manager.v1",
    "runtime.v1", "dependency.v1", "convention.v1",
})
_RUNNERS = frozenset({"task", "make", "just", "package-script.v1"})
_COMMAND_CATEGORIES = frozenset({"test", "build", "run", "task", "install", "single-test.v1"})
_CONFIG_KEYS = {
    "json": frozenset({"name", "scripts", "dependencies", "devDependencies", "packageManager", "engines", "workspaces", "type"}),
    "toml": frozenset({"project", "build-system", "tool.pytest.ini_options", "tool.ruff", "tool.poetry", "package", "workspace", "dependencies", "dev-dependencies", "features"}),
    "yaml": frozenset({"version", "tasks", "services", "networks", "volumes", "secrets", "configs", "on", "jobs"}),
}
_PLAIN_ARG = re.compile(r"[A-Za-z0-9_./:@%+=,<>-]+\Z")


def template_key_for(fact_type: str, payload: object) -> str:
    """Return the exact P0 formatter key for an approved typed fact payload."""
    if not isinstance(payload, dict):
        raise ValueError("claim payload must be a typed object")
    if fact_type == "artifact.presence":
        if set(payload) != {"category", "name", "path"} or payload.get("category") not in _ARTIFACT_CATEGORIES:
            raise ValueError("unsupported artifact.presence payload")
        if not all(isinstance(payload.get(field), str) and payload[field] for field in ("name", "path")):
            raise ValueError("artifact presence name and path must be nonempty strings")
        return "artifact-presence.v1"
    if fact_type == "task.definition":
        if set(payload) != {"category", "runner", "name", "body"} or payload.get("category") != "task":
            raise ValueError("unsupported task.definition payload")
        if payload.get("runner") not in _RUNNERS or not isinstance(payload.get("name"), str) or not payload["name"]:
            raise ValueError("unsupported task runner or name")
        if payload.get("body") is not None and not isinstance(payload["body"], str):
            raise ValueError("task body must be text or null")
        return "task-definition.v1"
    if fact_type == "command.invocation":
        if set(payload) != {"category", "argv"} or payload.get("category") not in _COMMAND_CATEGORIES:
            raise ValueError("unsupported command.invocation payload")
        argv = payload.get("argv")
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("command argv must be a nonempty string array")
        if any(any(unicodedata.category(char) in {"Cc", "Cf", "Cs", "Zl", "Zp"} for char in item) for item in argv):
            raise ValueError("command argv contains an unsupported control character")
        return "command-invocation.v1"
    if fact_type == "config.structure":
        if set(payload) != {"category", "format", "keys"} or payload.get("category") != "structural-summary":
            raise ValueError("unsupported config.structure payload")
        fmt, keys = payload.get("format"), payload.get("keys")
        if fmt not in _CONFIG_KEYS or not isinstance(keys, list) or not keys:
            raise ValueError("config structure requires a supported format and keys")
        if any(not isinstance(key, str) or key not in _CONFIG_KEYS[fmt] for key in keys):
            raise ValueError("config structure contains an unknown selected key")
        if keys != sorted(set(keys)):
            raise ValueError("config structure keys must be sorted and unique")
        return "config-structure.v1"
    raise ValueError(f"unsupported claim fact type: {fact_type}")


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _display_argument(argument: str) -> str:
    if not argument:
        return "''"
    if _PLAIN_ARG.fullmatch(argument):
        return argument
    return "'" + argument.replace("'", "'\"'\"'") + "'"


def _inline_code(value: str) -> str:
    runs = re.findall(r"`+", value)
    fence = "`" * (max((len(run) for run in runs), default=0) + 1)
    padding = " " if value.startswith(("`", " ")) or value.endswith(("`", " ")) else ""
    return f"{fence}{padding}{value}{padding}{fence}"


def format_claim_text(template_key: str, payload: JsonValue) -> str:
    """Format approved payloads without executing or interpolating repository text."""
    if template_key not in TEMPLATE_KEYS:
        raise ValueError(f"unknown claim template: {template_key}")
    if template_key == "artifact-presence.v1":
        typed = template_key_for("artifact.presence", payload)
        assert typed == template_key
        category, name, path = payload["category"], payload["name"], payload["path"]  # type: ignore[index]
        label = {
            "ci-workflow": "CI workflow", "ci-job": "CI job", "container": "container artifact",
            "ecosystem": "ecosystem", "package.v1": "package", "package-manager.v1": "package manager",
            "runtime.v1": "runtime", "dependency.v1": "dependency", "convention.v1": "convention",
        }[category]  # type: ignore[index]
        return f"{label}: {_json_string(name)} ({_json_string(path)})."  # type: ignore[arg-type]
    if template_key == "task-definition.v1":
        template_key_for("task.definition", payload)
        body = payload["body"]  # type: ignore[index]
        suffix = f": {_inline_code(_json_string(body))}" if body is not None else ""
        return f"Task {_json_string(payload['name'])} is defined by {payload['runner']}{suffix}."  # type: ignore[index]
    if template_key == "command-invocation.v1":
        template_key_for("command.invocation", payload)
        argv = payload["argv"]  # type: ignore[index]
        command = " ".join(_display_argument(argument) for argument in argv)  # type: ignore[union-attr]
        return f"{payload['category'].replace('.v1', '').capitalize()} command: {_inline_code(command)}."  # type: ignore[index]
    template_key_for("config.structure", payload)
    keys = payload["keys"]  # type: ignore[index]
    rendered_keys = ", ".join(f"`{key}`" for key in keys)  # type: ignore[union-attr]
    return f"Configuration structure ({payload['format']}): {rendered_keys}."  # type: ignore[index]
