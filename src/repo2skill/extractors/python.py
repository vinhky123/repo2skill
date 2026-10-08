"""Shallow Python project, manager, and test-target facts."""
from __future__ import annotations

import ast
from collections.abc import Sequence
import tomllib

from ..models import EvidenceLocator, ExtractionBatch, ExtractionContext, ExtractionRead, InputSelector
from ._ecosystem import finish, invalid_input, read_result, record_fact, selectors
from .base import ExtractionSession
from .parsing import parse_selected


_LOCKS = (("pixi.toml", "pixi"), ("uv.lock", "uv"), ("poetry.lock", "poetry"), ("Pipfile", "pipenv"), ("requirements.txt", "pip"))
_TEST_GLOB = "tests/**/test_*.py"


def _key(*parts: str) -> EvidenceLocator:
    return EvidenceLocator("key-path", list(parts), None)


def _selected_script(project: dict) -> tuple[str, object] | None:
    scripts = project.get("scripts")
    if not isinstance(scripts, dict):
        return None
    names = sorted(name for name in scripts if isinstance(name, str) and name.strip())
    if not names:
        return None
    name = project.get("name")
    selected = name if isinstance(name, str) and name in names else names[0]
    return selected, scripts[selected]


def _test_target(source: str) -> tuple[str, int, int] | None:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            return node.name, node.lineno, getattr(node, "end_lineno", node.lineno)
        if isinstance(node, ast.ClassDef) and node.name.startswith(("Test", "Quality")):
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test_"):
                    return f"{node.name}.{child.name}", child.lineno, getattr(child, "end_lineno", child.lineno)
    return None


class PythonExtractor:
    extractor_id = "python"
    version = "1.0"
    rule_id = "python.project"
    rule_version = "python-shallow-v1"

    def __init__(self, session: ExtractionSession):
        self.session = session

    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]:
        tests = [entry.path for entry in ctx.inventory.entries if entry.kind == "file" and entry.path.startswith("tests/") and entry.path.rsplit("/", 1)[-1].startswith("test_") and entry.path.endswith(".py")]
        return selectors(("pyproject.toml", *(name for name, _ in _LOCKS), *tests), (_TEST_GLOB,))

    def extract(self, ctx: ExtractionContext, selected: Sequence[InputSelector]) -> ExtractionBatch:
        reads: list[ExtractionRead] = []
        results = {selector.value: read_result(ctx, selector, reads) for selector in selected if selector.type == "file"}
        globs = [read_result(ctx, selector, reads) for selector in selected if selector.type == "glob"]
        manifest = results.get("pyproject.toml")
        if manifest is None or manifest.bytes is None:
            return finish(reads)
        try:
            project_data = parse_selected(manifest.bytes.decode(manifest.encoding or "utf-8-sig"), "toml")
            if not isinstance(project_data, dict):
                raise ValueError("pyproject must be a table")
        except Exception:
            invalid_input(self.session, self.extractor_id, manifest.selector, "pyproject.toml")
            return finish(reads)
        project = project_data.get("project")
        project = project if isinstance(project, dict) else {}
        record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                    result=manifest, path="pyproject.toml", fact_type="artifact.presence",
                    semantic_key="artifact.presence:ecosystem:python:pyproject.toml",
                    value={"category": "ecosystem", "name": "python", "path": "pyproject.toml"},
                    derivation="observed", locator=_key("project"))
        package_name = project.get("name")
        package_version = project.get("version")
        if isinstance(package_name, str) and package_name:
            identity = f"{package_name}@{package_version}" if isinstance(package_version, str) and package_version else package_name
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="pyproject.toml", fact_type="artifact.presence",
                        semantic_key="artifact.presence:package.v1:pyproject.toml",
                        value={"category": "package.v1", "name": identity, "path": "pyproject.toml"},
                        derivation="observed", locator=_key("project", "name"))
        runtime = project.get("requires-python")
        if isinstance(runtime, str):
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="pyproject.toml", fact_type="artifact.presence",
                        semantic_key="artifact.presence:runtime.v1:python:pyproject.toml",
                        value={"category": "runtime.v1", "name": f"python{runtime}", "path": "pyproject.toml"},
                        derivation="observed", locator=_key("project", "requires-python"))

        manager = None
        manager_result = None
        for lock_path, lock_manager in _LOCKS:
            result = results.get(lock_path)
            if result is None or result.bytes is None:
                continue
            locator = EvidenceLocator("file-presence", lock_path, None)
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=result, path=lock_path, fact_type="artifact.presence",
                        semantic_key=f"artifact.presence:package-manager.v1:lock:{lock_path}",
                        value={"category": "package-manager.v1", "name": lock_path, "path": lock_path},
                        derivation="observed", locator=locator)
            if lock_manager in {"uv", "poetry", "pixi"}:
                try:
                    lock_doc = tomllib.loads(result.bytes.decode(result.encoding or "utf-8-sig"))
                except Exception:
                    invalid_input(self.session, self.extractor_id, result.selector, lock_path)
                    continue
                if "version" not in lock_doc:
                    invalid_input(self.session, self.extractor_id, result.selector, lock_path)
                    continue
            if manager is None:
                manager, manager_result = lock_manager, result
        if manager is not None and manager_result is not None:
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manager_result, path=manager_result.selector.value, fact_type="artifact.presence",
                        semantic_key="artifact.presence:package-manager.v1:selected",
                        value={"category": "package-manager.v1", "name": manager, "path": manager_result.selector.value},
                        derivation="deterministic-derived", locator=EvidenceLocator("file-presence", manager_result.selector.value, None))
        elif project:
            manager = "pip"
            manager_result = manifest
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="pyproject.toml", fact_type="artifact.presence",
                        semantic_key="artifact.presence:package-manager.v1:selected",
                        value={"category": "package-manager.v1", "name": "pip", "path": "pyproject.toml"},
                        derivation="heuristic", locator=_key("project"))

        if manager == "uv":
            result = manager_result or manifest
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=result, path=result.selector.value, fact_type="command.invocation",
                        semantic_key="command.invocation:install:uv:repo-root",
                        value={"category": "install", "argv": ["uv", "sync"]}, derivation="template",
                        locator=EvidenceLocator("file-presence", result.selector.value, None))
        elif manager in {"poetry", "pixi", "pipenv", "pip"}:
            result = manager_result or manifest
            argv = {"poetry": ["poetry", "install"], "pixi": ["pixi", "install"], "pipenv": ["pipenv", "install"],
                    "pip": ["python", "-m", "pip", "install", "-e", "."]}[manager]
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=result, path=result.selector.value, fact_type="command.invocation",
                        semantic_key=f"command.invocation:install:{manager}:repo-root",
                        value={"category": "install", "argv": argv}, derivation="template",
                        locator=EvidenceLocator("file-presence", result.selector.value, None))

        script = _selected_script(project)
        if script is not None and manager == "uv":
            script_name, _script_value = script
            locator = _key("project", "scripts", script_name)
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="pyproject.toml", fact_type="command.invocation",
                        semantic_key="command.invocation:run:uv:project",
                        value={"category": "run", "argv": ["uv", "run", script_name]},
                        derivation="deterministic-derived", locator=locator)

        pytest_options = None
        tool = project_data.get("tool")
        if isinstance(tool, dict):
            pytest_options = tool.get("pytest")
        has_pytest = isinstance(pytest_options, dict) and "ini_options" in pytest_options
        if has_pytest:
            pytest_locator = _key("tool", "pytest", "ini_options")
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="pyproject.toml", fact_type="command.invocation",
                        semantic_key="command.invocation:test:python:repo-root",
                        value={"category": "test", "argv": ["python", "-m", "pytest"]}, derivation="template", locator=pytest_locator)
            target = None
            target_result = None
            for path in (globs[0].dependency.matchPaths if globs else ()):
                result = results.get(path)
                if result is None:
                    result = read_result(ctx, InputSelector("file", path, ".", False), reads)
                    results[path] = result
                if result.bytes is None:
                    continue
                try:
                    found = _test_target(result.bytes.decode(result.encoding or "utf-8-sig"))
                except (SyntaxError, UnicodeError):
                    invalid_input(self.session, self.extractor_id, result.selector, path)
                    continue
                if found is not None:
                    target, start, end = found
                    target_result = result
                    target_locator = EvidenceLocator("line-range", [start, end], start)
                    break
            if target is not None and target_result is not None:
                test_evidence = self.session.add_evidence(extractor_id=self.extractor_id, path=target_result.selector.value,
                    content_hash=target_result.dependency.contentHash or "", semantic_key="python-test-target",
                    media_type="text/x-python", encoding=target_result.encoding, locator=target_locator, preview=None)
                pytest_evidence = self.session.add_evidence(extractor_id=self.extractor_id, path="pyproject.toml",
                    content_hash=manifest.dependency.contentHash or "", semantic_key="python-pytest-configuration",
                    media_type="application/toml", encoding=manifest.encoding, locator=pytest_locator, preview=None)
                self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                    fact_type="command.invocation", scope=".", semantic_key="command.invocation:single-test.v1:python:one-test",
                    value={"category": "single-test.v1", "argv": ["python", "-m", "pytest", f"{target_result.selector.value}::{target}"]},
                    derivation="heuristic", evidence_ids=(test_evidence.evidenceId, pytest_evidence.evidenceId))
        return finish(reads)
