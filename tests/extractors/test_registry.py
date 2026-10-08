from pathlib import Path

import pytest

from repo2skill.config import default_scan_policy
from repo2skill.extractors.base import ExtractionSession
from repo2skill.extractors.registry import discover_extractors, run_extractors
from repo2skill.inventory import collect_inventory
from repo2skill.models import ExtractionBatch, InputSelector


class _Extractor:
    extractor_id = "z-test"
    version = "1"

    def discover(self, ctx):
        return ()

    def extract(self, ctx, selectors):
        raise AssertionError("no rules")


def test_registry_orders_extractors_by_id_and_version():
    assert [item.extractor_id for item in discover_extractors([_Extractor()])] == ["z-test"]


class _Rule:
    def __init__(self, name, session):
        self.rule_id = f"multi.{name}"
        self.rule_version = "1"
        self.name = name
        self.session = session

    def discover(self, ctx):
        return (InputSelector("file", f"{self.name}.txt", ".", False),)

    def extract(self, ctx, selectors):
        reads = tuple(ctx.read(selector).dependency for selector in selectors)
        return ExtractionBatch(reads, (), (), (), ())


class _MultiRuleExtractor:
    extractor_id = "multi"
    version = "1"

    def __init__(self, session, order):
        self.rules = tuple(_Rule(name, session) for name in order)


def _multi_rule_result(tmp_path: Path, order):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a.txt").write_bytes(b"a")
    (tmp_path / "b.txt").write_bytes(b"b")
    from dataclasses import replace
    from repo2skill.config import default_scan_policy
    from repo2skill.inventory import collect_inventory

    policy = replace(default_scan_policy(), maxOpenedFiles=1)
    controls = []
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        return run_extractors(session, [_MultiRuleExtractor(session, order)])


def test_registry_runs_rules_by_rule_id_independent_of_declared_order(tmp_path: Path):
    first = _multi_rule_result(tmp_path / "first", ["b", "a"])
    second = _multi_rule_result(tmp_path / "second", ["a", "b"])
    want = {"multi.a": "present", "multi.b": "capped"}
    assert {read.ruleId: read.outcome for read in first.reads if read.extractorId == "multi"} == want
    assert {read.ruleId: read.outcome for read in second.reads if read.extractorId == "multi"} == want


def test_registry_rejects_duplicate_extractor_ids(tmp_path: Path):
    controls = []
    policy = default_scan_policy()
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        with pytest.raises(ValueError, match="duplicate extractor"):
            run_extractors(session, [_Extractor(), _Extractor()])


class _SideEffectRule:
    def __init__(self, rule_id, events):
        self.rule_id = rule_id
        self.rule_version = "1"
        self.events = events

    def discover(self, ctx):
        self.events.append((self.rule_id, "discover"))
        return (InputSelector("file", "side-effect.txt", ".", False),)

    def extract(self, ctx, selectors):
        self.events.append((self.rule_id, "extract"))
        reads = tuple(ctx.read(selector).dependency for selector in selectors)
        return ExtractionBatch(reads, (), (), (), ())


class _RuleParent:
    def __init__(self, extractor_id, rules):
        self.extractor_id = extractor_id
        self.version = "1"
        self.rules = rules


def test_registry_preflights_duplicate_rule_ids_before_any_side_effect(tmp_path: Path):
    (tmp_path / "side-effect.txt").write_bytes(b"data")
    controls = []
    policy = default_scan_policy()
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    events = []
    extractors = [
        _RuleParent("a-parent", (_SideEffectRule("shared.rule", events),)),
        _RuleParent("b-parent", (_SideEffectRule("shared.rule", events),)),
    ]
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        with pytest.raises(ValueError, match="duplicate rule ID"):
            run_extractors(session, extractors)
        assert events == []
        assert session._logical == {}
        assert session._evidence == []
        assert session._facts == []


class _OrderRule:
    def __init__(self, rule_id, events):
        self.rule_id = rule_id
        self.rule_version = "1"
        self.events = events

    def discover(self, ctx):
        self.events.append((self.rule_id, "discover"))
        return ()

    def extract(self, ctx, selectors):
        self.events.append((self.rule_id, "extract"))
        return ExtractionBatch((), (), (), (), ())


def _nested_order_result(tmp_path: Path, reverse: bool):
    tmp_path.mkdir(parents=True, exist_ok=True)
    controls = []
    policy = default_scan_policy()
    inventory = collect_inventory(tmp_path, policy, control_sink=controls.append)
    events = []
    parents = [
        _RuleParent("a-parent", (_OrderRule("z.rule", events), _OrderRule("b.rule", events))),
        _RuleParent("b-parent", (_OrderRule("a.rule", events),)),
    ]
    if reverse:
        parents.reverse()
        parents[1].rules = tuple(reversed(parents[1].rules))
    with ExtractionSession(root=tmp_path, snapshot_id="snapshot_000000000000000000000000", inventory=inventory,
            policy=policy, repository_id="repository_000000000000000000000000", prior_evidence=(), prior_facts=(),
            excluded_paths=(), control_observations=controls) as session:
        run_extractors(session, parents)
    return events


def test_registry_order_is_extractor_then_rule_even_when_inputs_reverse(tmp_path: Path):
    first = _nested_order_result(tmp_path / "first", False)
    second = _nested_order_result(tmp_path / "second", True)
    assert first == second == [
        ("b.rule", "discover"), ("b.rule", "extract"),
        ("z.rule", "discover"), ("z.rule", "extract"),
        ("a.rule", "discover"), ("a.rule", "extract"),
    ]
