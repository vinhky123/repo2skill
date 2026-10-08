from pathlib import Path
from dataclasses import replace
import hashlib

from repo2skill.config import default_scan_policy
from repo2skill.identity import capture_repository
from repo2skill.inventory import collect_inventory
from repo2skill.models import InputSelector
from repo2skill.extractors.base import ExtractionSession


def test_read_finalizes_raw_hash_and_parses_captured_buffer_after_mutation(tmp_path: Path):
    source = tmp_path / "config.json"
    source.write_bytes(b'{"name":"before"}')
    policy = default_scan_policy()
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    identity = capture_repository(tmp_path, inventory, "no-git", project_id="project_000000000000000000000000", policy=policy)
    with ExtractionSession(
        root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory, policy=policy,
        repository_id=identity.repositoryId, prior_evidence=(), prior_facts=(),
        excluded_paths=(), control_observations=controls,
    ) as session:
        context = session.bind("generic", "1", "generic.config", "1")
        result = context.read(InputSelector("file", "config.json", ".", True))
        source.write_bytes(b'{"name":"after"}')
    assert result.dependency.outcome == "present"
    assert result.dependency.contentHash == "sha256:" + hashlib.sha256(b'{"name":"before"}').hexdigest()
    assert result.bytes == b'{"name":"before"}'


def test_unique_capture_cache_shares_one_open_across_rule_reads_and_cap_is_visible(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    policy = replace(default_scan_policy(), maxOpenedFiles=1)
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        first = session.bind("a", "1", "a.rule", "1").read(InputSelector("file", "a.txt", ".", True))
        repeated = session.bind("b", "1", "b.rule", "1").read(InputSelector("file", "a.txt", ".", True))
        capped = session.bind("c", "1", "c.rule", "1").read(InputSelector("file", "b.txt", ".", True))
        empty = session.bind("c", "1", "c.glob", "1").read(InputSelector("glob", "nothing/*.json", ".", False))
        assert first.dependency.outcome == "present"
        assert repeated.dependency.contentHash == first.dependency.contentHash
        assert capped.dependency.outcome == "capped"
        assert empty.dependency.outcome == "no-match"
        assert empty.dependency.matchSetHash == "sha256:" + hashlib.sha256(b"[]").hexdigest()
        assert session.opened_file_count == 1
        assert session.decoded_byte_count == 1


def test_inventory_control_observations_become_reads_without_reopening(tmp_path: Path):
    (tmp_path / ".gitignore").write_text("private/\n", encoding="utf-8")
    policy = default_scan_policy()
    observations = []
    inventory = collect_inventory(tmp_path, policy, control_sink=observations.append)
    identity = capture_repository(tmp_path, inventory, "no-git", project_id="project_000000000000000000000000", policy=policy)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id=identity.repositoryId, prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=observations) as session:
        control_file = next(read for read in session.control_reads if read.selector.type == "file")
        result = session.bind("inventory", "1.0", "inventory.ignore-control", "1.0").read(control_file.selector)
        assert control_file.contentHash == result.dependency.contentHash
        assert result.bytes is None
        assert session.opened_file_count == observations[0].openedFileCount


def test_incomplete_inventory_never_proves_missing_file_or_empty_glob(tmp_path: Path):
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    policy = replace(default_scan_policy(), maxInventoryEntries=1)
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        context = session.bind("test", "1", "test.incomplete", "1")
        missing = context.read(InputSelector("file", "b.txt", ".", True)).dependency
        glob = context.read(InputSelector("glob", "*.json", ".", False)).dependency
    assert missing.outcome == "capped"
    assert glob.outcome == "capped" and glob.matchSetHash is None
