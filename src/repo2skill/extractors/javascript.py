"""Bounded shallow facts from the root JavaScript package manifest."""
from __future__ import annotations

import re
from collections.abc import Sequence

from ..models import CoverageEntry, EvidenceLocator, ExtractionBatch, ExtractionContext, ExtractionRead, InputSelector
from ._ecosystem import finish, invalid_input, read_result, record_fact, selectors
from .base import ExtractionSession
from .parsing import parse_selected


_LOCKS = (("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lock", "bun"), ("bun.lockb", "bun"), ("package-lock.json", "npm"))
_SOURCE_PATTERNS = ("**/*.js", "**/*.jsx", "**/*.ts", "**/*.tsx")


def _pointer(value: str) -> EvidenceLocator:
    return EvidenceLocator("json-pointer", value, None)


def _argv_for_script(manager: str, name: str) -> list[str]:
    if manager == "npm":
        return ["npm", name] if name in {"test", "start"} else ["npm", "run", name]
    if manager == "bun":
        return ["bun", "run", name]
    return [manager, name]


def _command_category(name: str) -> str | None:
    base = name.lower().split(":", 1)[0]
    if base in {"test", "check"}:
        return "test"
    if base in {"build", "compile"}:
        return "build"
    if base in {"start", "serve", "dev"}:
        return "run"
    return None


class JavaScriptExtractor:
    extractor_id = "javascript"
    version = "1.0"
    rule_id = "javascript.package"
    rule_version = "javascript-shallow-v1"

    def __init__(self, session: ExtractionSession):
        self.session = session

    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]:
        return selectors(("package.json", *(name for name, _ in _LOCKS)), _SOURCE_PATTERNS)

    def extract(self, ctx: ExtractionContext, selected: Sequence[InputSelector]) -> ExtractionBatch:
        reads: list[ExtractionRead] = []
        results = {selector.value: read_result(ctx, selector, reads) for selector in selected if selector.type == "file"}
        source_results = [read_result(ctx, selector, reads) for selector in selected if selector.type == "glob"]
        manifest = results.get("package.json")
        document = None
        if manifest and manifest.bytes is not None:
            try:
                document = parse_selected(manifest.bytes.decode(manifest.encoding or "utf-8-sig"), "json")
                if not isinstance(document, dict):
                    raise ValueError("package manifest must be an object")
            except Exception:
                invalid_input(self.session, self.extractor_id, manifest.selector, "package.json")
        if not isinstance(document, dict) or manifest is None:
            return finish(reads)

        presence_locator = _pointer("/scripts" if isinstance(document.get("scripts"), dict) else "/name")
        record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                    result=manifest, path="package.json", fact_type="artifact.presence",
                    semantic_key="artifact.presence:ecosystem:javascript:package.json",
                    value={"category": "ecosystem", "name": "javascript", "path": "package.json"},
                    derivation="observed", locator=presence_locator)

        manager_name: str | None = None
        manager_path = "package.json"
        manager_derivation = "template"
        manager_pointer: EvidenceLocator | None = None
        declared = document.get("packageManager")
        if isinstance(declared, str) and "@" in declared:
            manager_name = declared.split("@", 1)[0]
            manager_pointer = _pointer("/packageManager")
            manager_derivation = "observed"
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="package.json", fact_type="artifact.presence",
                        semantic_key="artifact.presence:package-manager.v1:declared:package.json",
                        value={"category": "package-manager.v1", "name": declared, "path": "package.json"},
                        derivation="observed", locator=manager_pointer)

        present_locks: list[tuple[str, str, object]] = []
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
            present_locks.append((lock_path, lock_manager, result))
        if manager_name is None and present_locks:
            manager_path, manager_name, manager_result = present_locks[0]
            manager_derivation = "deterministic-derived"
            manager_pointer = EvidenceLocator("file-presence", manager_path, None)
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manager_result, path=manager_path, fact_type="artifact.presence",
                        semantic_key="artifact.presence:package-manager.v1:selected",
                        value={"category": "package-manager.v1", "name": manager_name, "path": manager_path},
                        derivation=manager_derivation, locator=manager_pointer)
        if manager_name not in {"npm", "pnpm", "yarn", "bun"}:
            manager_name = "npm"
            manager_path = "package.json"
            manager_derivation = "template"
            manager_pointer = _pointer("/scripts")

        name = document.get("name")
        version = document.get("version")
        if isinstance(name, str) and name:
            identity = f"{name}@{version}" if isinstance(version, str) and version else name
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="package.json", fact_type="artifact.presence",
                        semantic_key="artifact.presence:package.v1:package.json",
                        value={"category": "package.v1", "name": identity, "path": "package.json"},
                        derivation="observed", locator=_pointer("/name"))
        engines = document.get("engines")
        if isinstance(engines, dict):
            for runtime, runtime_range in sorted(engines.items()):
                if isinstance(runtime, str) and isinstance(runtime_range, str):
                    record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                                result=manifest, path="package.json", fact_type="artifact.presence",
                                semantic_key=f"artifact.presence:runtime.v1:{runtime}:package.json",
                                value={"category": "runtime.v1", "name": f"{runtime}{runtime_range}", "path": "package.json"},
                                derivation="observed", locator=_pointer(f"/engines/{runtime}"))

        scripts = document.get("scripts")
        if isinstance(scripts, dict):
            for script_name, body in sorted(scripts.items()):
                if not isinstance(script_name, str) or not isinstance(body, str):
                    continue
                body = self.session._bounded_text(body)
                semantic_key = f"task.definition:package-script.v1:repo-root:{script_name}"
                locator = _pointer(f"/scripts/{script_name}")
                evidence_identity = {"repository": self.session.repository_id, "path": "package.json", "locator": locator, "semanticKey": semantic_key}
                if not self.session.reserve_fact_text(body, rule_id=self.rule_id, fact_type="task.definition", scope=".",
                                                     semantic_key=semantic_key,
                                                     additional_preview_bytes=len(body.encode("utf-8")),
                                                     preview_identity=evidence_identity):
                    body = None
                    self.session._coverage.append(CoverageEntry(1, manifest.selector, self.extractor_id, "capped", "task-body-budget", ()))
                fact = record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                                   result=manifest, path="package.json", fact_type="task.definition", semantic_key=semantic_key,
                                   value={"category": "task", "runner": "package-script.v1", "name": script_name, "body": body},
                                   derivation="observed", locator=locator, preview=body)
                category = _command_category(script_name)
                if category is not None:
                    target_scope = "repo-root" if script_name in {"test", "build", "start"} else f"script:{script_name}"
                    invocation_key = f"command.invocation:{category}:{manager_name}:{target_scope}"
                    record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                                result=manifest, path="package.json", fact_type="command.invocation", semantic_key=invocation_key,
                                value={"category": category, "argv": _argv_for_script(manager_name, script_name)},
                                derivation="template" if manager_derivation == "template" else "deterministic-derived",
                                locator=locator)

        dependencies: dict[str, tuple[str, str]] = {}
        for section in ("dependencies", "devDependencies"):
            raw = document.get(section)
            if isinstance(raw, dict):
                for package, package_version in raw.items():
                    if isinstance(package, str) and isinstance(package_version, str):
                        dependencies.setdefault(package, (package_version, section))
        for package, (package_version, section) in sorted(dependencies.items()):
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="package.json", fact_type="artifact.presence",
                        semantic_key=f"artifact.presence:dependency.v1:{package}:package.json",
                        value={"category": "dependency.v1", "name": f"{package}@{package_version}", "path": "package.json"},
                        derivation="observed", locator=_pointer(f"/{section}/{package}"))
        runner = next((candidate for candidate in ("vitest", "jest", "mocha") if candidate in dependencies), None)
        if runner:
            _, section = dependencies[runner]
            runner_prefix = "npx" if manager_name == "npm" else manager_name
            args = {"vitest": [runner_prefix, "vitest", "run", "<path>", "-t", "<name>"],
                    "jest": [runner_prefix, "jest", "<path>", "-t", "<name>"],
                    "mocha": [runner_prefix, "mocha", "<path>", "--grep", "<name>"]}[runner]
            record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                        result=manifest, path="package.json", fact_type="command.invocation",
                        semantic_key=f"command.invocation:single-test.v1:{manager_name}:one-test",
                        value={"category": "single-test.v1", "argv": args},
                        derivation="template", locator=_pointer(f"/{section}/{runner}"))

        if source_results:
            source_paths = sorted({path for item in source_results for path in item.dependency.matchPaths})
            if len(source_paths) > 160:
                self.session._coverage.append(CoverageEntry(1, source_results[0].selector, self.extractor_id, "capped", "source-convention-limit", ()))
            for path in source_paths[:160]:
                if not any(token in path.lower().split("/") for token in ("node_modules", ".git", "dist", "build", "vendor")):
                    result = results.get(path)
                    if result is None:
                        result = read_result(ctx, InputSelector("file", path, ".", False), reads)
                        results[path] = result
                    if result.bytes is None:
                        continue
                    text = result.bytes.decode(result.encoding or "utf-8-sig")
                    if re.search(r"\b(fetch|axios\.create|ky\.create|got\.extend)\s*\(", text) and any(token in path.lower().split("/")[-1] for token in ("api", "client", "http", "fetcher", "request")):
                        line_no, line = next((i, line) for i, line in enumerate(text.splitlines(), 1) if re.search(r"\b(fetch|axios\.create|ky\.create|got\.extend)\s*\(", line))
                        record_fact(self.session, extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version,
                                    result=result, path=path, fact_type="artifact.presence",
                                    semantic_key="artifact.presence:convention.v1:http-client-wrapper",
                                    value={"category": "convention.v1", "name": "http-client-wrapper", "path": path},
                                    derivation="heuristic", locator=EvidenceLocator("line-range", [line_no, line_no], line_no), preview=line)
                        break
        return finish(reads)
