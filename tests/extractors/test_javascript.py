from importlib.util import find_spec
import json


def test_javascript_literals_manager_invocations_and_source_convention(run_ecosystem_fixture):
    assert find_spec("repo2skill.extractors.javascript") is not None
    batch = run_ecosystem_fixture("javascript")
    assert len({item.factId for item in batch.facts}) == len(batch.facts)
    facts = {(item.factType, item.semanticKey): item for item in batch.facts}
    assert any(item.value == {"category": "ecosystem", "name": "javascript", "path": "package.json"} and item.derivation == "observed" for item in batch.facts)
    script = next(item for item in batch.facts if item.factType == "task.definition")
    assert script.value == {
        "category": "task", "runner": "package-script.v1", "name": "test", "body": "vitest run --coverage"
    }
    assert script.derivation == "observed"
    script_evidence = next(item for item in batch.evidence if item.evidenceId in script.evidenceIds)
    assert script_evidence.path == "package.json"
    assert script_evidence.locator.kind == "json-pointer"
    assert script_evidence.locator.value == "/scripts/test"
    assert any(
        item.factType == "command.invocation"
        and item.value == {"category": "test", "argv": ["pnpm", "test"]}
        and item.derivation == "deterministic-derived"
        for item in batch.facts
    )
    assert any(item.value.get("name") == "pnpm@9.15.0" for item in batch.facts)
    assert any(item.value.get("name") == "vitest@2.1.0" for item in batch.facts)
    assert any(item.value == {"category": "single-test.v1", "argv": ["pnpm", "vitest", "run", "<path>", "-t", "<name>"]} and item.derivation == "template" for item in batch.facts)
    assert any(item.value.get("name") == "http-client-wrapper" for item in batch.facts)
    source_read = next(item for item in batch.reads if item.selector.value == "src/client.ts")
    assert source_read.outcome == "present"
    assert any(item.path == "src/client.ts" and item.contentHash == source_read.contentHash for item in batch.evidence)
    convention = next(item for item in batch.facts if item.value.get("name") == "http-client-wrapper")
    convention_evidence = next(item for item in batch.evidence if item.evidenceId in convention.evidenceIds)
    assert convention_evidence.locator.kind == "line-range"
    assert convention_evidence.locator.value == [1, 1]
    assert facts


def test_client_source_change_updates_digest_and_evidence_revision(run_ecosystem_fixture):
    first, first_snapshot = run_ecosystem_fixture("javascript", with_snapshot=True)

    def change_client(root):
        (root / "src/client.ts").write_text('export const getStatus = () => fetch("/health");\n', encoding="utf-8")

    second, second_snapshot = run_ecosystem_fixture(
        "javascript",
        mutate=change_client,
        with_snapshot=True,
        snapshot_id="snapshot_000000000000000000000001",
        prior_evidence=first.evidence,
        prior_facts=first.facts,
    )
    assert first_snapshot.inputDigest != second_snapshot.inputDigest
    old_evidence = next(item for item in first.evidence if item.path == "src/client.ts")
    new_evidence = next(item for item in second.evidence if item.path == "src/client.ts")
    assert new_evidence.evidenceId == old_evidence.evidenceId
    assert new_evidence.revision == old_evidence.revision + 1
    assert old_evidence.contentHash != new_evidence.contentHash


def test_invalid_package_json_is_visible_as_invalid_coverage(run_ecosystem_fixture):
    batch = run_ecosystem_fixture(
        "javascript", mutate=lambda root: (root / "package.json").write_text("{broken", encoding="utf-8")
    )
    assert any(item.reasonCode == "parse-invalid" and item.status == "invalid" for item in batch.coverage)
    assert not any(item.value.get("category") == "ecosystem" for item in batch.facts)


def test_dynamic_manifest_keys_are_json_pointer_escaped(run_ecosystem_fixture, tmp_path):
    def add_special_keys(root):
        path = root / "package.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["devDependencies"]["@scope/pkg~name"] = "1.2.3"
        document["scripts"]["test/path~unit"] = "node --test"
        path.write_text(json.dumps(document), encoding="utf-8")

    batch = run_ecosystem_fixture("javascript", mutate=add_special_keys)
    dependency = next(item for item in batch.facts if item.value.get("name") == "@scope/pkg~name@1.2.3")
    dependency_evidence = next(item for item in batch.evidence if item.evidenceId in dependency.evidenceIds)
    assert dependency_evidence.locator.value == "/devDependencies/@scope~1pkg~0name"
    manifest = json.loads((tmp_path / "javascript/package.json").read_text(encoding="utf-8"))
    script = next(item for item in batch.facts if item.factType == "task.definition" and item.value.get("name") == "test/path~unit")
    script_evidence = next(item for item in batch.evidence if item.evidenceId in script.evidenceIds)
    assert script_evidence.locator.value == "/scripts/test~1path~0unit"

    def resolve(pointer):
        value = manifest
        for segment in pointer[1:].split("/"):
            value = value[segment.replace("~1", "/").replace("~0", "~")]
        return value

    assert resolve(dependency_evidence.locator.value) == "1.2.3"
    assert resolve(script_evidence.locator.value) == "node --test"
