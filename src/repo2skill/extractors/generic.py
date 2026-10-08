from __future__ import annotations

from collections.abc import Sequence
import json
import tomllib

from ..models import CoverageEntry, Diagnostic, EvidenceLocator, ExtractionBatch, ExtractionContext, InputSelector
from ..redact import redact_text
from .base import ExtractionSession
from .parsing import parse_selected, present_key_paths


_ROOT_CONFIGS = {
    "package.json": ("json", ("name", "scripts", "dependencies", "devDependencies", "packageManager", "engines", "workspaces", "type")),
    "pyproject.toml": ("toml", ("project", "build-system", "tool.pytest.ini_options", "tool.ruff", "tool.poetry")),
    "Cargo.toml": ("toml", ("package", "workspace", "dependencies", "dev-dependencies", "features")),
    "Taskfile.yml": ("yaml", ("version", "tasks")),
    "Taskfile.yaml": ("yaml", ("version", "tasks")),
    "compose.yml": ("yaml", ("services", "networks", "volumes", "secrets", "configs")),
    "compose.yaml": ("yaml", ("services", "networks", "volumes", "secrets", "configs")),
    "docker-compose.yml": ("yaml", ("services", "networks", "volumes", "secrets", "configs")),
    "docker-compose.yaml": ("yaml", ("services", "networks", "volumes", "secrets", "configs")),
}


class GenericExtractor:
    extractor_id = "generic"
    version = "1.0"
    rule_id = "generic.structural"
    rule_version = "generic-allowlist-v1"

    def __init__(self, session: ExtractionSession):
        self.session = session

    def discover(self, ctx: ExtractionContext) -> tuple[InputSelector, ...]:
        files = {item.path for item in ctx.inventory.entries if item.kind == "file"}
        selected: set[InputSelector] = set()
        for path in sorted(files):
            if path in _ROOT_CONFIGS or path in {"Makefile", "Justfile", "Dockerfile"}:
                selected.add(InputSelector("file", path, ".", False))
        for path in sorted(files):
            if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")) and "/" not in path[len(".github/workflows/"):]:
                selected.add(InputSelector("file", path, ".", False))
        selectors = set(selected)
        for candidate in ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts", "pom.xml"):
            if candidate in files:
                selectors.add(InputSelector("negative", f"filename-absent:{candidate}", ".", False))
        return tuple(sorted(selectors, key=lambda item: (item.scope, item.type, item.value, item.required)))

    def extract(self, ctx: ExtractionContext, selectors: Sequence[InputSelector]) -> ExtractionBatch:
        reads = []
        for selector in selectors:
            if selector.type == "negative":
                # Unsupported JVM presence was already established by inventory; this
                # selector records the named predicate and the selected path as a read.
                result = ctx.read(selector)
                reads.append(result.dependency)
                path = selector.value.split(":", 1)[1]
                self.session._coverage.append(CoverageEntry(1, selector, self.extractor_id, "unsupported", "jvm-native-unsupported", ()))
                continue
            result = ctx.read(selector)
            reads.append(result.dependency)
            if result.bytes is None:
                continue
            path = selector.value
            text = result.bytes.decode(result.encoding or "utf-8-sig")
            fmt_and_keys = _ROOT_CONFIGS.get(path)
            if fmt_and_keys:
                fmt, allowed = fmt_and_keys
                try:
                    document = parse_selected(text, fmt)
                except Exception:
                    self.session._diagnose("parse-invalid", path, self.extractor_id)
                    self.session._coverage.append(CoverageEntry(1, selector, self.extractor_id, "invalid", "parse-invalid", ()))
                    continue
                keys = present_key_paths(document, allowed)
                if keys:
                    evidence = self.session.add_evidence(extractor_id=self.extractor_id, path=path, content_hash=result.dependency.contentHash or "", semantic_key=f"config:{fmt}", media_type=f"application/{fmt}", encoding=result.encoding, locator=EvidenceLocator("key-path", list(keys), None), preview=None)
                    self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version, fact_type="config.structure", scope=".", semantic_key=f"{path}:structure", value={"category": "structural-summary", "format": fmt, "keys": list(keys)}, derivation="observed", evidence_ids=(evidence.evidenceId,))
            if path.startswith(".github/workflows/"):
                try:
                    document = parse_selected(text, "yaml")
                except Exception:
                    self.session._diagnose("parse-invalid", path, self.extractor_id)
                    self.session._coverage.append(CoverageEntry(1, selector, self.extractor_id, "invalid", "parse-invalid", ()))
                    continue
                jobs = document.get("jobs") if isinstance(document, dict) else None
                trigger_present = isinstance(document, dict) and "on" in document
                if trigger_present or isinstance(jobs, dict):
                    workflow = self.session.add_evidence(extractor_id=self.extractor_id, path=path, content_hash=result.dependency.contentHash or "", semantic_key="ci-workflow", media_type="application/yaml", encoding=result.encoding, locator=EvidenceLocator("key-path", "on" if trigger_present else "jobs", None), preview=None)
                    self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version, fact_type="artifact.presence", scope=".", semantic_key=f"workflow:{path}", value={"category": "ci-workflow", "name": path.rsplit("/", 1)[-1], "path": path}, derivation="observed", evidence_ids=(workflow.evidenceId,))
                if isinstance(jobs, dict):
                    for name in sorted(jobs):
                        item = self.session.add_evidence(extractor_id=self.extractor_id, path=path, content_hash=result.dependency.contentHash or "", semantic_key=f"ci-job:{name}", media_type="application/yaml", encoding=result.encoding, locator=EvidenceLocator("key-path", ["jobs", name], None), preview=None)
                        self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version, fact_type="artifact.presence", scope=".", semantic_key=f"job:{path}:{name}", value={"category": "ci-job", "name": name, "path": path}, derivation="observed", evidence_ids=(item.evidenceId,))
            if path == "Dockerfile" or path in {"compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml"}:
                evidence = self.session.add_evidence(extractor_id=self.extractor_id, path=path, content_hash=result.dependency.contentHash or "", semantic_key=f"container:{path}", media_type="text/plain" if path == "Dockerfile" else "application/yaml", encoding=result.encoding, locator=EvidenceLocator("file-presence", path, None), preview=None)
                self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version, fact_type="artifact.presence", scope=".", semantic_key=f"container:{path}", value={"category": "container", "name": path, "path": path}, derivation="observed", evidence_ids=(evidence.evidenceId,))
            if path.startswith("Taskfile."):
                try:
                    document = parse_selected(text, "yaml")
                except Exception:
                    self.session._diagnose("parse-invalid", path, self.extractor_id)
                    self.session._coverage.append(CoverageEntry(1, selector, self.extractor_id, "invalid", "parse-invalid", ()))
                    continue
                tasks = document.get("tasks") if isinstance(document, dict) else None
                if isinstance(tasks, dict):
                    for name in sorted(tasks):
                        task = tasks[name]
                        cmds = task.get("cmds") if isinstance(task, dict) else None
                        body = "\n".join(str(command) for command in cmds) if isinstance(cmds, list) else None
                        safe_body = self.session._bounded_text(body) if body is not None else None
                        task_key = f"task:{name}"
                        locator = EvidenceLocator("key-path", ["tasks", name], None)
                        evidence_key = {"repository": self.session.repository_id, "path": path, "locator": locator, "semanticKey": f"task:{name}"}
                        if safe_body is not None and not self.session.reserve_fact_text(safe_body, rule_id=self.rule_id, fact_type="task.definition", scope=".", semantic_key=task_key, additional_preview_bytes=len(safe_body.encode("utf-8")), preview_identity=evidence_key):
                            safe_body = None
                            self.session._coverage.append(CoverageEntry(1, selector, self.extractor_id, "capped", "task-body-budget", ()))
                        item = self.session.add_evidence(extractor_id=self.extractor_id, path=path, content_hash=result.dependency.contentHash or "", semantic_key=f"task:{name}", media_type="application/yaml", encoding=result.encoding, locator=EvidenceLocator("key-path", ["tasks", name], None), preview=safe_body)
                        self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version, fact_type="task.definition", scope=".", semantic_key=task_key, value={"category": "task", "runner": "task", "name": name, "body": safe_body}, derivation="observed", evidence_ids=(item.evidenceId,))
                        self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version, fact_type="command.invocation", scope=".", semantic_key=f"task-invoke:{name}", value={"category": "task", "argv": ["task", name]}, derivation="deterministic-derived", evidence_ids=(item.evidenceId,))
            if path in {"Makefile", "Justfile"}:
                runner = "make" if path == "Makefile" else "just"
                for target in sorted({match.group(1) for match in __import__("re").finditer(r"(?m)^([A-Za-z0-9_.-]+)\s*:(?:\s|$)", text) if match.group(1) not in {"include", "ifeq", "ifneq", "ifdef", "ifndef"}}):
                    evidence = self.session.add_evidence(extractor_id=self.extractor_id, path=path, content_hash=result.dependency.contentHash or "", semantic_key=f"{runner}-target:{target}", media_type="text/plain", encoding=result.encoding, locator=EvidenceLocator("key-path", target, None), preview=None)
                    self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version, fact_type="task.definition", scope=".", semantic_key=f"{runner}:{target}", value={"category": "task", "runner": runner, "name": target, "body": None}, derivation="observed", evidence_ids=(evidence.evidenceId,))
                    self.session.add_fact(extractor_id=self.extractor_id, rule_id=self.rule_id, rule_version=self.rule_version, fact_type="command.invocation", scope=".", semantic_key=f"{runner}-invoke:{target}", value={"category": "task", "argv": [runner, target]}, derivation="deterministic-derived", evidence_ids=(evidence.evidenceId,))
        batch = self.session.make_batch(reads)
        return ExtractionBatch(tuple(reads), batch.evidence, batch.facts, batch.diagnostics, batch.coverage)
