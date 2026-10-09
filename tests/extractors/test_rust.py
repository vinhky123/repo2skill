from importlib.util import find_spec


def test_rust_package_commands_and_test_directory_convention(run_ecosystem_fixture):
    assert find_spec("repo2skill.extractors.rust") is not None
    batch = run_ecosystem_fixture("rust")
    assert any(item.value == {"category": "ecosystem", "name": "rust", "path": "Cargo.toml"} and item.derivation == "observed" for item in batch.facts)
    assert any(item.value.get("name") == "fixture-rust-app@0.0.0" and item.derivation == "observed" for item in batch.facts)
    package = next(item for item in batch.facts if item.value.get("name") == "fixture-rust-app@0.0.0")
    package_evidence = next(item for item in batch.evidence if item.evidenceId in package.evidenceIds)
    assert package_evidence.locator.kind == "key-path"
    assert package_evidence.locator.value == ["package", "name"]
    for value in (
        {"category": "build", "argv": ["cargo", "build"]},
        {"category": "test", "argv": ["cargo", "test"]},
        {"category": "single-test.v1", "argv": ["cargo", "test", "<test_name>"]},
    ):
        assert any(item.value == value and item.derivation == "template" for item in batch.facts)
    single = next(item for item in batch.facts if item.value.get("category") == "single-test.v1")
    assert single.semanticKey == "command.invocation:single-test.v1:cargo:one-test"
    convention = next(item for item in batch.facts if item.value.get("name") == "integration-tests-directory")
    assert convention.derivation == "heuristic"
    assert any(item.selector.value == "tests/smoke.rs" and item.outcome == "present" for item in batch.reads)


def test_cargo_manifest_without_package_table_is_visible_as_invalid(run_ecosystem_fixture):
    batch = run_ecosystem_fixture(
        "rust", mutate=lambda root: (root / "Cargo.toml").write_text("[dependencies]\nserde = '1'\n", encoding="utf-8")
    )
    assert any(item.reasonCode == "parse-invalid" and item.status == "invalid" for item in batch.coverage)
    assert not any(item.value.get("category") == "ecosystem" for item in batch.facts)
