from importlib.util import find_spec


def test_python_manifest_lock_defaults_and_source_test_target(run_ecosystem_fixture):
    assert find_spec("repo2skill.extractors.python") is not None
    batch = run_ecosystem_fixture("python")
    assert any(item.value.get("name") == "fixture-python-app@0.0.0" and item.derivation == "observed" for item in batch.facts)
    package = next(item for item in batch.facts if item.value.get("name") == "fixture-python-app@0.0.0")
    package_evidence = next(item for item in batch.evidence if item.evidenceId in package.evidenceIds)
    assert package_evidence.locator.kind == "key-path"
    assert package_evidence.locator.value == ["project", "name"]
    assert any(item.value.get("name") == "python>=3.11" and item.derivation == "observed" for item in batch.facts)
    assert any(item.value.get("name") == "uv.lock" and item.derivation == "observed" for item in batch.facts)
    assert any(item.value.get("name") == "uv" and item.derivation == "deterministic-derived" for item in batch.facts)
    assert any(item.value == {"category": "install", "argv": ["uv", "sync"]} and item.derivation == "template" for item in batch.facts)
    assert any(item.value == {"category": "run", "argv": ["uv", "run", "fixture-python-app"]} and item.derivation == "deterministic-derived" for item in batch.facts)
    target = next(item for item in batch.facts if item.value.get("category") == "single-test.v1")
    assert target.value["argv"] == ["python", "-m", "pytest", "tests/test_widget.py::test_widget_name"]
    assert target.derivation == "heuristic"
    read = next(item for item in batch.reads if item.selector.value == "tests/test_widget.py")
    assert read.outcome == "present"
    assert any(item.path == read.selector.value and item.contentHash == read.contentHash for item in batch.evidence)
    target_evidence = next(item for item in batch.evidence if item.evidenceId in target.evidenceIds and item.path == "tests/test_widget.py")
    assert target_evidence.locator.kind == "line-range"
    assert target_evidence.locator.value == [1, 2]


def test_invalid_pyproject_is_visible_as_invalid_coverage(run_ecosystem_fixture):
    batch = run_ecosystem_fixture(
        "python", mutate=lambda root: (root / "pyproject.toml").write_text("[project\nname =", encoding="utf-8")
    )
    assert any(item.reasonCode == "parse-invalid" and item.status == "invalid" for item in batch.coverage)
    assert not any(item.value.get("category") == "ecosystem" for item in batch.facts)


def test_python_run_script_does_not_assume_uv_without_uv_lock(run_ecosystem_fixture):
    def remove_uv_lock(root):
        (root / "uv.lock").unlink()

    batch = run_ecosystem_fixture("python", mutate=remove_uv_lock)
    assert any(item.value.get("name") == "pip" and item.derivation == "heuristic" for item in batch.facts)
    assert not any(item.value == {"category": "run", "argv": ["uv", "run", "fixture-python-app"]} for item in batch.facts)
