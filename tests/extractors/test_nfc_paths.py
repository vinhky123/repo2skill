from pathlib import Path
import unicodedata

from repo2skill.config import default_scan_policy
from repo2skill.extractors.base import ExtractionSession
from repo2skill.identity import capture_repository
from repo2skill.inventory import collect_inventory
from repo2skill.models import InputSelector


def test_canonical_nfc_inventory_path_opens_decomposed_physical_name(tmp_path: Path):
    decomposed = "e\u0301.txt"
    canonical = unicodedata.normalize("NFC", decomposed)
    (tmp_path / decomposed).write_bytes(b"captured")
    policy = default_scan_policy()
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    assert any(entry.path == canonical for entry in inventory.entries)
    identity = capture_repository(tmp_path, inventory, "no-git", project_id="project_000000000000000000000000", policy=policy)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id=identity.repositoryId, prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        result = session.bind("test", "1", "test.nfc", "1").read(InputSelector("file", canonical, ".", True))
    assert result.dependency.outcome == "present"
    assert result.bytes == b"captured"
