from __future__ import annotations

from pathlib import Path

from repo2skill.config import default_claim_policy, default_scan_policy, default_targets
from repo2skill.ledger import LedgerStore
from repo2skill.models import ProjectDocument, ProjectState
from repo2skill.ownership import inspect_target_ownership


def _store(root: Path) -> tuple[LedgerStore, tuple]:
    targets = default_targets(root)
    project = ProjectDocument(
        "1.0", "0.1.0", "project_0123456789abcdef01234567", "repo",
        ".repo2skill", "no-git", default_scan_policy(), default_claim_policy(), targets,
    )
    store = LedgerStore(root, root / ".repo2skill")
    with store.mutation():
        import json
        from repo2skill.ids import canonical_json_bytes
        store.write_immutable("project.json", json.loads(canonical_json_bytes(project)))
        store.read_state()
        store.replace_state(ProjectState("1.0", "0.1.0", project.projectId, None, None, (), {}, None, None))
    return store, targets


def test_inspect_target_ownership_is_clean_and_does_not_create_state(tmp_path: Path) -> None:
    store, targets = _store(tmp_path)
    assert inspect_target_ownership(targets[:1], store=store) == ()


def test_inspect_target_ownership_is_read_only_without_state_directory(tmp_path: Path) -> None:
    store = LedgerStore(tmp_path, tmp_path / ".repo2skill")
    assert inspect_target_ownership((), store=store) == ()
    assert not store.state_dir.exists()
