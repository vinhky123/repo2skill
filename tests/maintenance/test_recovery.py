from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from repo2skill.cli import main


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", message],
                   check=True, capture_output=True)
    return _git(root, "rev-parse", "HEAD")


def _invoke(root: Path, capsys, *args: str):
    code = main(["--root", str(root), "--format", "json", "maintain", *args])
    output = capsys.readouterr()
    return code, json.loads(output.out.splitlines()[-1]), output.err


def _initialized(root: Path) -> None:
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / ".git" / "info" / "exclude").write_text(".repo2skill/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c",
                    "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "base"],
                   check=True, capture_output=True)
    code = main(["--root", str(root), "init", "--format", "json"])
    assert code == 0


def test_first_run_requires_explicit_bootstrap(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    target = _commit(root, "initial")

    code, status, _ = _invoke(root, capsys, "status")
    assert code == 4
    assert status["result"]["appliedBaseline"] is None
    assert "init" in " ".join(status["nextActions"])
    assert not (root / ".repo2skill").exists()

    code, result, _ = _invoke(root, capsys, "catch-up", "--rebuild")
    assert code == 4
    assert result["result"]["bootstrapRequired"] is True
    assert not (root / ".repo2skill").exists()

    code = main(["--root", str(root), "--format", "json", "init"])
    assert code == 0
    capsys.readouterr()
    code, _status, _ = _invoke(root, capsys, "status")
    assert code == 4
    assert not (root / ".repo2skill" / "maintenance" / "state.json").exists()
    assert _invoke(root, capsys, "setup", "--manual-hooks")[0] == 0
    assert _invoke(root, capsys, "catch-up")[0] == 4
    code, rebuilt, _ = _invoke(root, capsys, "catch-up", "--rebuild")
    assert code == 3
    assert rebuilt["result"]["jobs"]


@pytest.mark.parametrize("filename", ["SKILL.md", "repo2skill-provenance.json"])
def test_existing_unowned_skill_requires_adoption_or_conflict(tmp_path: Path, capsys, monkeypatch, filename: str) -> None:
    from repo2skill import maintenance_worker
    from repo2skill.maintenance_agent import AgentResponse
    worker_calls = []
    adapter_calls = []
    run_worker = maintenance_worker.run_worker

    def fake_probe(*args, **kwargs):
        adapter_calls.append("probe")
        return type("Capability", (), {"supported": False})()

    def fake_invoke(*args, **kwargs):
        adapter_calls.append("invoke")
        return AgentResponse("unavailable", b"", "fake adapter only")

    def isolated_worker(*args, **kwargs):
        worker_calls.append((args, kwargs))
        return run_worker(*args, probe=fake_probe, invoke=fake_invoke, **kwargs)

    # Keep every route to an installed adapter replaced, including on a broken
    # early-stop path. A known output collision must be caught before probing.
    monkeypatch.setattr(maintenance_worker, "run_worker", isolated_worker)
    root = tmp_path / "repo"
    _initialized(root)
    skill_dir = root / ".agents" / "skills" / root.name
    skill_dir.mkdir(parents=True)
    skill_bytes = b"# Human skill\n"
    output_path = skill_dir / filename
    output_path.write_bytes(skill_bytes)
    _commit(root, "add unowned skill")
    code, _setup, _ = _invoke(root, capsys, "setup", "--manual-hooks")
    assert code == 0

    code, payload, _ = _invoke(root, capsys, "catch-up", "--rebuild")
    assert code == 3
    assert payload["result"]["jobs"]
    job_id = payload["result"]["jobs"][0]["jobId"]
    code, approved, _ = _invoke(root, capsys, "approve", job_id)
    assert code == 5, approved["findings"]
    assert worker_calls == []
    assert adapter_calls == []
    assert output_path.read_bytes() == skill_bytes


def test_branch_switch_or_missing_object_requires_rebuild(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    _initialized(root)
    baseline = _git(root, "rev-parse", "HEAD")
    from repo2skill.ledger import LedgerStore
    from repo2skill.maintenance_store import MaintenanceStore
    from repo2skill.config import load_project_document

    _project, state_dir = load_project_document(root, ".repo2skill/project.json")
    store = LedgerStore(root, state_dir)
    maintenance = MaintenanceStore(root, store)
    with store.mutation():
        maintenance.replace_state(__import__("dataclasses").replace(
            maintenance.read_state(), appliedBaseline=baseline,
        ))
    _git(root, "checkout", "-qb", "side")
    (root / "side.py").write_text("side = True\n", encoding="utf-8")
    side_commit = _commit(root, "side branch")
    _git(root, "checkout", "--orphan", "rewritten")
    _git(root, "rm", "-rf", ".")
    (root / "rewritten.py").write_text("rewritten = True\n", encoding="utf-8")
    rewritten = _commit(root, "rewritten history")
    code, normal, _ = _invoke(root, capsys, "catch-up")
    assert code == 4
    assert normal["result"]["appliedBaseline"] == baseline
    code, rebuilt, _ = _invoke(root, capsys, "catch-up", "--rebuild")
    assert code == 3
    assert rebuilt["result"]["targetCommit"] == rewritten
    assert rebuilt["result"]["appliedBaseline"] == baseline
    assert maintenance.read_state().appliedBaseline == baseline
    assert side_commit != rewritten


def test_disable_preserves_history_and_outputs(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    _initialized(root)
    output = root / "AGENTS.md"
    output.write_bytes(b"human output\n")
    _invoke(root, capsys, "setup", "--manual-hooks")
    _invoke(root, capsys, "catch-up", "--rebuild")
    before = json.loads((root / ".repo2skill" / "maintenance" / "state.json").read_text(encoding="utf-8"))

    code, payload, _ = _invoke(root, capsys, "disable")

    after = json.loads((root / ".repo2skill" / "maintenance" / "state.json").read_text(encoding="utf-8"))
    assert code == 0 and payload["result"]["disabled"] is True
    assert before["jobs"] == after["jobs"]
    assert output.read_bytes() == b"human output\n"


def test_reopen_v10_state_with_maintenance_orphans(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    _initialized(root)
    from repo2skill.config import load_project_document
    from repo2skill.ledger import LedgerStore
    from repo2skill.maintenance_store import MaintenanceStore

    _project, state_dir = load_project_document(root, ".repo2skill/project.json")
    reopened = MaintenanceStore(root, LedgerStore(root, state_dir))
    state = reopened.read_state()
    assert state.schemaVersion == "1.0"
    assert state.appliedBaseline is None
    # Immutable orphan records do not become visible without a maintenance pointer.
    path = state_dir / "maintenance" / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    (path / "maintenance-job_0123456789abcdef01234567.json").write_text("{}", encoding="utf-8")
    assert reopened.read_state().jobs == {}


def test_reachable_events_coalesce_without_transferring_approval(tmp_path: Path) -> None:
    from repo2skill.config import load_project_document
    from repo2skill.ledger import LedgerStore
    from repo2skill.maintenance_git import compare_commits, enqueue_event
    from repo2skill.maintenance_models import GitEvent
    from repo2skill.maintenance_store import MaintenanceStore

    root = tmp_path / "repo"
    _initialized(root)
    baseline = _git(root, "rev-parse", "HEAD")
    _project, state_dir = load_project_document(root, ".repo2skill/project.json")
    ledger = LedgerStore(root, state_dir)
    maintenance = MaintenanceStore(root, ledger)
    with ledger.mutation():
        maintenance.replace_state(__import__("dataclasses").replace(
            maintenance.read_state(), appliedBaseline=baseline, enabled=True,
        ))
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("first = True\n", encoding="utf-8")
    first_target = _commit(root, "first source")
    first_event = GitEvent("git-event_222222222222222222222222", "post-commit", "cursor-first", first_target)
    first = enqueue_event(ledger, first_event, compare_commits(root, baseline, first_target))
    assert first is not None
    (root / "src" / "app.py").write_text("first = True\nsecond = True\n", encoding="utf-8")
    second_target = _commit(root, "second source")
    second_event = GitEvent("git-event_333333333333333333333333", "post-commit", "cursor-second", second_target)
    second = enqueue_event(ledger, second_event, compare_commits(root, baseline, second_target))

    assert second is not None and second.targetCommit == second_target
    assert second.eventIds == (first_event.eventId, second_event.eventId)
    state = maintenance.read_state()
    assert state.jobs[first.jobId].status == "stale"
    assert state.jobs[first.jobId].approvalSource is None
    assert state.jobs[second.jobId].status == "pending"


def test_manual_catch_up_after_merge_and_rewrite_rebuild(tmp_path: Path, capsys) -> None:
    root = tmp_path / "repo"
    _initialized(root)
    baseline = _git(root, "rev-parse", "HEAD")
    from repo2skill.config import load_project_document
    from repo2skill.ledger import LedgerStore
    from repo2skill.maintenance_store import MaintenanceStore

    _project, state_dir = load_project_document(root, ".repo2skill/project.json")
    store = LedgerStore(root, state_dir)
    maintenance = MaintenanceStore(root, store)
    with store.mutation():
        maintenance.replace_state(__import__("dataclasses").replace(
            maintenance.read_state(), appliedBaseline=baseline, enabled=True,
        ))
    base_branch = _git(root, "branch", "--show-current")
    _git(root, "checkout", "-qb", "feature")
    (root / "feature.py").write_text("feature = True\n", encoding="utf-8")
    _commit(root, "feature")
    _git(root, "checkout", "-q", base_branch)
    (root / "main.py").write_text("main = True\n", encoding="utf-8")
    _commit(root, "main")
    _git(root, "merge", "--no-ff", "-qm", "merge feature", "feature")
    merge_target = _git(root, "rev-parse", "HEAD")
    code, result, _ = _invoke(root, capsys, "catch-up")
    assert code == 3
    assert result["result"]["targetCommit"] == merge_target

    _git(root, "checkout", "--orphan", "rewritten")
    _git(root, "rm", "-rf", ".")
    (root / "fresh.py").write_text("fresh = True\n", encoding="utf-8")
    rewritten = _commit(root, "rewrite")
    code, result, _ = _invoke(root, capsys, "catch-up")
    assert code == 4
    assert result["result"]["appliedBaseline"] == baseline
    code, result, _ = _invoke(root, capsys, "catch-up", "--rebuild")
    assert code == 3
    assert result["result"]["targetCommit"] == rewritten
    assert maintenance.read_state().appliedBaseline == baseline


def test_queued_successor_refreshes_after_apply_with_new_binding(tmp_path: Path) -> None:
    from dataclasses import replace
    from repo2skill.config import load_project_document
    from repo2skill.ledger import LedgerStore
    from repo2skill.maintenance_git import compare_commits, enqueue_event, refresh_queued_successor
    from repo2skill.maintenance_models import GitEvent
    from repo2skill.maintenance_store import MaintenanceStore

    root = tmp_path / "repo"
    _initialized(root)
    baseline = _git(root, "rev-parse", "HEAD")
    _project, state_dir = load_project_document(root, ".repo2skill/project.json")
    ledger = LedgerStore(root, state_dir)
    maintenance = MaintenanceStore(root, ledger)
    with ledger.mutation():
        maintenance.replace_state(replace(maintenance.read_state(), appliedBaseline=baseline, enabled=True))
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("one = True\n", encoding="utf-8")
    first_target = _commit(root, "running endpoint")
    first_event = GitEvent("git-event_444444444444444444444444", "post-commit", "cursor-running", first_target)
    first = enqueue_event(ledger, first_event, compare_commits(root, baseline, first_target))
    assert first is not None
    with ledger.mutation():
        state = maintenance.read_state()
        jobs = dict(state.jobs)
        jobs[first.jobId] = replace(jobs[first.jobId], status="running")
        maintenance.replace_state(replace(state, jobs=jobs, runningJobId=first.jobId))
    (root / "src" / "app.py").write_text("one = True\ntwo = True\n", encoding="utf-8")
    queued_target = _commit(root, "queued endpoint")
    queued_event = GitEvent("git-event_555555555555555555555555", "post-commit", "cursor-queued", queued_target)
    queued = enqueue_event(ledger, queued_event, compare_commits(root, baseline, queued_target))
    assert queued is not None and queued.jobId != first.jobId
    with ledger.mutation():
        state = maintenance.read_state()
        jobs = dict(state.jobs)
        jobs[first.jobId] = replace(jobs[first.jobId], status="applied")
        maintenance.replace_state(replace(state, jobs=jobs, appliedBaseline=first_target, runningJobId=None))

    successor = refresh_queued_successor(ledger)

    assert successor is not None
    assert successor.baselineCommit == first_target
    assert successor.targetCommit == queued_target
    refreshed = maintenance.read_state()
    assert refreshed.jobs[queued.jobId].status == "stale"
    assert refreshed.jobs[successor.jobId].status == "pending"
    assert refreshed.jobs[successor.jobId].approvalBindingHash is None
    assert queued.eventIds[0] in successor.eventIds
