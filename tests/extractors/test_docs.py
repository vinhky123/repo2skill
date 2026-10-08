from pathlib import Path

from repo2skill.config import default_scan_policy
from repo2skill.extractors.base import ExtractionSession
from repo2skill.extractors.docs import DocumentationExtractor
from repo2skill.inventory import collect_inventory


def test_docs_discovery_uses_case_sensitive_markdown_extension(tmp_path: Path):
    (tmp_path / "README.md").write_text("lower", encoding="utf-8")
    (tmp_path / "README.MD").write_text("upper", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "guide.md").write_text("guide", encoding="utf-8")
    (tmp_path / "docs" / "guide.MD").write_text("upper guide", encoding="utf-8")
    policy = default_scan_policy()
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        context = session.bind("docs", "1", "docs.normalized-line", "1")
        selectors = DocumentationExtractor(session).discover(context)
    selected_files = {selector.value for selector in selectors if selector.type == "file"}
    assert selected_files == {"README.md", "docs/guide.md"}
