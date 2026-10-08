from importlib.util import find_spec

from repo2skill.inventory import INVENTORY_VERSION


def test_mixed_repository_retains_all_ecosystem_commands(run_ecosystem_fixture):
    assert all(find_spec(f"repo2skill.extractors.{name}") for name in ("javascript", "python", "go", "rust"))
    batch = run_ecosystem_fixture("mixed")
    assert {item.selector.value for item in batch.reads} >= {"package.json", "pyproject.toml", "go.mod", "Cargo.toml"}
    inventory_control = next(item for item in batch.reads if item.extractorId == "inventory" and item.selector.value == "**/.gitignore")
    assert inventory_control.extractorVersion == INVENTORY_VERSION
    commands = [item.value for item in batch.facts if item.factType == "command.invocation"]
    assert {"category": "test", "argv": ["npm", "test"]} in commands
    assert {"category": "install", "argv": ["python", "-m", "pip", "install", "-e", "."]} in commands
    assert {"category": "test", "argv": ["go", "test", "./..."]} in commands
    assert {"category": "build", "argv": ["cargo", "build"]} in commands
    literal = next(item for item in batch.facts if item.factType == "task.definition")
    assert literal.value == {"category": "task", "runner": "package-script.v1", "name": "test", "body": "node --test"}
    assert literal.derivation == "observed"
