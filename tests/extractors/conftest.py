from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from repo2skill.config import default_scan_policy
from repo2skill.dependencies import build_snapshot
from repo2skill.extractors.base import ExtractionSession
from repo2skill.extractors.registry import run_extractors
from repo2skill.identity import capture_repository
from repo2skill.inventory import INVENTORY_VERSION, collect_inventory


@pytest.fixture
def run_ecosystem_fixture(tmp_path: Path):
    fixture_root = Path(__file__).parents[1] / "fixtures" / "ecosystems"

    def run(name: str, *, mutate=None, with_snapshot: bool = False,
            snapshot_id: str = "snapshot_000000000000000000000000", prior_evidence=(), prior_facts=()):
        root = tmp_path / name
        if not root.exists():
            shutil.copytree(fixture_root / name, root)
        if mutate is not None:
            mutate(root)
        policy = default_scan_policy()
        controls = []
        inventory = collect_inventory(root, policy, control_sink=controls.append)
        identity = capture_repository(
            root, inventory, "no-git", project_id="project_000000000000000000000000", policy=policy
        )
        from repo2skill.extractors.registry import ecosystem_extractors

        with ExtractionSession(
            root=root,
            snapshot_id=snapshot_id,
            inventory=inventory,
            policy=policy,
            repository_id=identity.repositoryId,
            prior_evidence=prior_evidence,
            prior_facts=prior_facts,
            excluded_paths=(),
            control_observations=controls,
        ) as session:
            batch = run_extractors(session, ecosystem_extractors(session))
        evidence_by_id = {item.evidenceId: item for item in batch.evidence}
        reads_by_path = {item.selector.value: item for item in batch.reads if item.selector.type == "file"}
        for fact in batch.facts:
            assert fact.evidenceIds
            for evidence_id in fact.evidenceIds:
                evidence = evidence_by_id[evidence_id]
                dependency = reads_by_path[evidence.path]
                assert dependency.outcome == "present"
                assert dependency.contentHash == evidence.contentHash
        if with_snapshot:
            versions = {"javascript": "1.0", "python": "1.0", "go": "1.0", "rust": "1.0", "inventory": INVENTORY_VERSION}
            snapshot = build_snapshot(snapshot_id, identity, inventory, versions, batch.reads,
                                      policy_revision="sha256:" + "0" * 64, root_label=name)
            return batch, snapshot
        return batch

    return run
