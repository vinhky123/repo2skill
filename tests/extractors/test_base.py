from pathlib import Path
from dataclasses import replace
import hashlib

from repo2skill.config import default_scan_policy
from repo2skill.identity import capture_repository
from repo2skill.inventory import collect_inventory
from repo2skill.models import EvidenceLocator, InputSelector
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


def test_control_handoff_rejects_nonrequired_aggregate(tmp_path: Path):
    from dataclasses import replace
    import pytest

    policy = default_scan_policy()
    observations = []
    inventory = collect_inventory(tmp_path, policy, control_sink=observations.append)
    observations[-1] = replace(observations[-1], selector=replace(observations[-1].selector, required=False))
    with pytest.raises(ValueError, match="aggregate"):
        ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=observations)


def test_control_handoff_rejects_unsupported_selector_and_outcome(tmp_path: Path):
    from dataclasses import replace
    import pytest

    policy = default_scan_policy()
    observations = []
    inventory = collect_inventory(tmp_path, policy, control_sink=observations.append)
    unsupported = observations + [replace(observations[0], selector=InputSelector("negative", "filename-absent:x", ".", False))]
    with pytest.raises(ValueError, match="unsupported selector"):
        ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=unsupported)
    invalid_outcome = [replace(observations[0], outcome="missing")]
    with pytest.raises(ValueError, match="invalid outcome"):
        ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=invalid_outcome)


def test_session_record_builders_fail_closed_on_truncated_id_collision(tmp_path: Path, monkeypatch):
    import repo2skill.ids as ids
    import pytest

    policy = default_scan_policy()
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    monkeypatch.setattr(ids, "_digest_suffix", lambda _canonical: "0" * 24)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        session.add_evidence(extractor_id="sample", path="a.txt", content_hash="sha256:" + "0" * 64,
            semantic_key="a", media_type="text/plain", encoding="utf-8", locator=EvidenceLocator("file-presence", "a.txt", None), preview=None)
        with pytest.raises(ids.IdCollisionError):
            session.add_evidence(extractor_id="sample", path="b.txt", content_hash="sha256:" + "0" * 64,
                semantic_key="b", media_type="text/plain", encoding="utf-8", locator=EvidenceLocator("file-presence", "b.txt", None), preview=None)


def test_unique_document_text_and_each_preview_charge_once(tmp_path: Path):
    policy = replace(default_scan_policy(), maxSnippetBytes=9)
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        for path, line in (("README.md", 1), ("docs/guide.md", 4)):
            locator = EvidenceLocator("line-range", [line, line], line)
            evidence_key = {"repository": session.repository_id, "path": path, "locator": locator, "semanticKey": "documentation-line:abc"}
            assert session.reserve_fact_text("abc", rule_id="docs.normalized-line", fact_type="documentation.line",
                scope=".", semantic_key="documentation:abc", additional_preview_bytes=3, preview_identity=evidence_key)
            session.add_evidence(extractor_id="docs", path=path, content_hash="sha256:" + "0" * 64,
                semantic_key="documentation-line:abc", media_type="text/markdown", encoding="utf-8",
                locator=locator, preview="abc")
        fact = session.add_fact(extractor_id="docs", rule_id="docs.normalized-line", rule_version="1",
            fact_type="documentation.line", scope=".", semantic_key="documentation:abc",
            value={"category": "documentation", "text": "abc"}, derivation="observed",
            evidence_ids=tuple(item.evidenceId for item in session._evidence))
        assert fact is not None
        assert session._snippet == 9
        assert not any(item.reasonCode == "snippet-budget" for item in session._coverage)


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
