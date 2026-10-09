from pathlib import Path
from dataclasses import replace
import hashlib
import json

import pytest

from repo2skill.config import default_scan_policy
from repo2skill.identity import capture_repository
from repo2skill.inventory import collect_inventory
from repo2skill.models import EvidenceLocator, InputSelector
from repo2skill.extractors.base import ExtractionSession


def test_prior_history_selects_max_revision_even_when_older_record_is_last(run_ecosystem_fixture):
    initial = run_ecosystem_fixture("javascript")
    evidence = tuple(initial.evidence)
    facts = tuple(initial.facts)
    prior_evidence = tuple(replace(item, revision=4) for item in evidence) + tuple(
        replace(item, revision=2) for item in evidence
    )
    prior_facts = tuple(replace(item, factRevision=7) for item in facts) + tuple(
        replace(item, factRevision=2) for item in facts
    )

    current = run_ecosystem_fixture("javascript", prior_evidence=prior_evidence, prior_facts=prior_facts)

    assert {item.revision for item in current.evidence} == {4}
    assert {item.factRevision for item in current.facts} == {7}


def test_prior_history_equal_maximum_conflict_fails_closed(run_ecosystem_fixture):
    initial = run_ecosystem_fixture("javascript")
    item = initial.facts[0]
    conflict = replace(item, factRevision=3, value={"conflict": True})
    maximum = replace(item, factRevision=3)

    with pytest.raises(ValueError, match="conflicting semantic history"):
        run_ecosystem_fixture("javascript", prior_facts=(maximum, conflict))


def test_prior_evidence_equal_maximum_conflict_fails_closed(run_ecosystem_fixture):
    initial = run_ecosystem_fixture("javascript")
    item = initial.evidence[0]
    maximum = replace(item, revision=3)
    conflict = replace(item, revision=3, contentHash="sha256:" + "f" * 64)

    with pytest.raises(ValueError, match="conflicting semantic history"):
        run_ecosystem_fixture("javascript", prior_evidence=(maximum, conflict))


def test_prior_evidence_and_fact_reappearance_revises_above_semantic_high_water(
    run_ecosystem_fixture, tmp_path: Path,
):
    initial = run_ecosystem_fixture("javascript")
    manifest_path = tmp_path / "javascript" / "package.json"
    original_bytes = manifest_path.read_bytes()
    changed_manifest = json.loads(original_bytes)
    changed_manifest["scripts"]["test"] = "vitest run --changed"
    changed_bytes = json.dumps(changed_manifest, indent=2).encode("utf-8")

    def write_changed(root: Path) -> None:
        (root / "package.json").write_bytes(changed_bytes)

    changed = run_ecosystem_fixture("javascript", mutate=write_changed)
    newer_evidence = {item.evidenceId: item for item in changed.evidence}
    newer_facts = {item.factId: item for item in changed.facts}
    old_evidence = next(item for item in initial.evidence
                        if newer_evidence[item.evidenceId].contentHash != item.contentHash)
    old_fact = next(item for item in initial.facts
                    if newer_facts[item.factId].value != item.value)
    evidence_at_two = replace(newer_evidence[old_evidence.evidenceId], revision=2)
    fact_at_two = replace(newer_facts[old_fact.factId], factRevision=2)
    evidence_at_one = replace(old_evidence, revision=1)
    fact_at_one = replace(old_fact, factRevision=1)

    unchanged_new = run_ecosystem_fixture(
        "javascript", mutate=write_changed,
        prior_evidence=(evidence_at_one, evidence_at_two), prior_facts=(fact_at_one, fact_at_two),
    )
    assert next(item for item in unchanged_new.evidence if item.evidenceId == old_evidence.evidenceId).revision == 2
    assert next(item for item in unchanged_new.facts if item.factId == old_fact.factId).factRevision == 2

    def restore_original(root: Path) -> None:
        (root / "package.json").write_bytes(original_bytes)

    reverted = run_ecosystem_fixture(
        "javascript", mutate=restore_original,
        prior_evidence=(evidence_at_two, evidence_at_one), prior_facts=(fact_at_two, fact_at_one),
    )
    assert next(item for item in reverted.evidence if item.evidenceId == old_evidence.evidenceId).revision == 3
    assert next(item for item in reverted.facts if item.factId == old_fact.factId).factRevision == 3


def test_prior_evidence_identity_collision_fails_before_builder_history_selection(
    run_ecosystem_fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    import repo2skill.ids as ids

    initial = run_ecosystem_fixture("javascript")
    source_root = tmp_path / "javascript"
    policy = default_scan_policy()
    inventory = collect_inventory(source_root, policy)
    identity = capture_repository(source_root, inventory, "no-git",
                                  project_id="project_000000000000000000000000", policy=policy)
    current = initial.evidence[0]
    rogue = replace(current, evidenceId="evidence_" + "0" * 24, path="other.json",
                    locator=EvidenceLocator("file-presence", "other.json", None))
    current_identity = {"repository": identity.repositoryId, "path": current.path,
                        "locator": current.locator, "semanticKey": current.semanticKey}
    rogue_identity = {"repository": identity.repositoryId, "path": rogue.path,
                      "locator": rogue.locator, "semanticKey": rogue.semanticKey}
    collision_identities = {ids.canonical_json_bytes(current_identity), ids.canonical_json_bytes(rogue_identity)}
    original_digest = ids._digest_suffix
    monkeypatch.setattr(ids, "_digest_suffix",
                        lambda canonical: "0" * 24 if canonical in collision_identities else original_digest(canonical))

    with pytest.raises(ids.IdCollisionError, match="collision"):
        run_ecosystem_fixture("javascript", prior_evidence=(rogue,))


def test_prior_fact_identity_collision_fails_before_builder_history_selection(
    run_ecosystem_fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    import repo2skill.ids as ids

    initial = run_ecosystem_fixture("javascript")
    source_root = tmp_path / "javascript"
    policy = default_scan_policy()
    inventory = collect_inventory(source_root, policy)
    identity = capture_repository(source_root, inventory, "no-git",
                                  project_id="project_000000000000000000000000", policy=policy)
    current = initial.facts[0]
    rogue = replace(current, factId="fact_" + "0" * 24, semanticKey="different-semantic-key")
    current_identity = {"repository": identity.repositoryId, "scope": current.scope,
                        "factType": current.factType, "semanticKey": current.semanticKey}
    rogue_identity = {"repository": identity.repositoryId, "scope": rogue.scope,
                      "factType": rogue.factType, "semanticKey": rogue.semanticKey}
    collision_identities = {ids.canonical_json_bytes(current_identity), ids.canonical_json_bytes(rogue_identity)}
    original_digest = ids._digest_suffix
    monkeypatch.setattr(ids, "_digest_suffix",
                        lambda canonical: "0" * 24 if canonical in collision_identities else original_digest(canonical))

    with pytest.raises(ids.IdCollisionError, match="collision"):
        run_ecosystem_fixture("javascript", prior_facts=(rogue,))


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
