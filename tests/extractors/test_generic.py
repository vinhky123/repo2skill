from dataclasses import replace
from pathlib import Path

from repo2skill.config import default_scan_policy
from repo2skill.extractors.base import ExtractionSession
from repo2skill.extractors.docs import DocumentationExtractor
from repo2skill.extractors.generic import GenericExtractor
from repo2skill.extractors.registry import run_extractors
from repo2skill.identity import capture_repository
from repo2skill.inventory import collect_inventory


def test_generic_taskfile_ci_jvm_and_docs_keep_source_provenance(tmp_path: Path):
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / ".github/workflows/ci.yml").write_text("on: push\njobs:\n  verify:\n    steps: []\n", encoding="utf-8")
    (tmp_path / "Taskfile.yml").write_text("version: '3'\ntasks:\n  test:\n    cmds:\n      - echo unsafe\n", encoding="utf-8")
    (tmp_path / "build.gradle.kts").write_text("plugins {}\n", encoding="utf-8")
    sentence = "Run `task test` to check the fixture."
    (tmp_path / "README.md").write_text(sentence + "\napi_token: top-secret-value\n", encoding="utf-8")
    (tmp_path / "docs/checks.md").write_text(sentence + "\n", encoding="utf-8")
    policy = default_scan_policy()
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    identity = capture_repository(tmp_path, inventory, "no-git", project_id="project_000000000000000000000000", policy=policy)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id=identity.repositoryId, prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        batch = run_extractors(session, [DocumentationExtractor(session), GenericExtractor(session)])
    values = [fact.value for fact in batch.facts]
    assert {value["name"] for value in values if value.get("category") == "ci-job"} == {"verify"}
    assert any(value.get("name") == "test" and value.get("body") == "echo unsafe" for value in values)
    docs = [fact for fact in batch.facts if fact.factType == "documentation.line"]
    assert len(docs) == 2
    duplicate = next(fact for fact in docs if fact.value["text"] == sentence)
    assert len(duplicate.evidenceIds) == 2
    assert "top-secret-value" not in repr(batch.facts)
    assert "top-secret-value" not in repr(batch.evidence)
    assert any(fact.value["text"] == "api_token: [REDACTED:secret-key]" for fact in docs)
    assert any(item.reasonCode == "jvm-native-unsupported" for item in batch.coverage)
    assert all("unsafe" not in str(fact.value) for fact in batch.facts if fact.factType != "task.definition")
