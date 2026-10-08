from importlib.util import find_spec


def test_go_dependency_and_test_commands_remain_templates(run_ecosystem_fixture):
    assert find_spec("repo2skill.extractors.go") is not None
    batch = run_ecosystem_fixture("go")
    assert any(item.value == {"category": "ecosystem", "name": "go", "path": "go.mod"} and item.derivation == "observed" for item in batch.facts)
    assert any(item.value.get("name") == "github.com/go-chi/chi/v5@v5.1.0" and item.derivation == "observed" for item in batch.facts)
    dependency = next(item for item in batch.facts if item.value.get("name") == "github.com/go-chi/chi/v5@v5.1.0")
    dependency_evidence = next(item for item in batch.evidence if item.evidenceId in dependency.evidenceIds)
    assert dependency_evidence.locator.kind == "line-range"
    assert dependency_evidence.locator.value == [5, 5]
    assert any(item.value == {"category": "test", "argv": ["go", "test", "./..."]} and item.derivation == "template" for item in batch.facts)
    assert any(item.value == {"category": "single-test.v1", "argv": ["go", "test", "./...", "-run", "<TestName>"]} and item.derivation == "template" for item in batch.facts)
    read = next(item for item in batch.reads if item.selector.value == "go.mod")
    assert read.outcome == "present"
    assert any(item.path == "go.mod" and item.contentHash == read.contentHash for item in batch.evidence)


def test_go_mod_without_supported_module_directive_is_visible_as_invalid(run_ecosystem_fixture):
    batch = run_ecosystem_fixture(
        "go", mutate=lambda root: (root / "go.mod").write_text("require example.invalid/pkg v1.0.0\n", encoding="utf-8")
    )
    assert any(item.reasonCode == "parse-invalid" and item.status == "invalid" for item in batch.coverage)
    assert not any(item.value.get("category") == "ecosystem" for item in batch.facts)
