from __future__ import annotations

from dataclasses import replace

from repo2skill.config import default_claim_policy
from repo2skill.ids import CanonicalIdContext, canonical_record_id
from repo2skill.models import Fact
from repo2skill.claims import derive_claims


REPO = "repo_0123456789abcdef01234567"
SNAPSHOT = "snapshot_0123456789abcdef01234567"


def _fact(*, fact_type: str, key: str, value: dict, derivation: str = "observed", revision: int = 1) -> Fact:
    return Fact(
        1, canonical_record_id("fact", {"key": key}), revision, SNAPSHOT, fact_type, key, ".", value,
        derivation, "generic.structural", "generic-allowlist-v1",
        ("evidence_0123456789abcdef01234567",),
    )


def test_derives_typed_deterministic_candidates_pending_and_omits_documentation() -> None:
    facts = (
        _fact(fact_type="artifact.presence", key="artifact.presence:package.v1:repo-root",
              value={"category": "package.v1", "name": "example@1.0", "path": "package.json"}),
        _fact(fact_type="command.invocation", key="command.invocation:test:pnpm:repo-root",
              value={"category": "test", "argv": ["pnpm", "test"]}, derivation="template"),
        _fact(fact_type="documentation.line", key="documentation:please run test",
              value={"category": "documentation", "text": "please run test"}),
    )

    result = derive_claims(facts, default_claim_policy(), repository_id=REPO)

    assert len(result.claims) == 2
    by_key = {claim.semanticKey: claim for claim in result.claims}
    package = by_key[facts[0].semanticKey]
    command = by_key[facts[1].semanticKey]
    assert package.kind == "observed" and package.origin == "deterministic"
    assert command.kind == "inferred" and command.origin == "deterministic"
    assert package.proposalStatus == command.proposalStatus == "pending"
    assert package.payload == facts[0].value and package.templateKey == "artifact-presence.v1"
    assert command.payload == facts[1].value and command.templateKey == "command-invocation.v1"
    assert package.factIds == (facts[0].factId,)
    assert package.evidenceIds == facts[0].evidenceIds


def test_claim_revision_uses_maximum_semantic_history_and_ignores_formatting() -> None:
    fact = _fact(fact_type="command.invocation", key="command.invocation:test:python:repo-root",
                 value={"category": "test", "argv": ["python", "-m", "pytest"]}, derivation="template")
    first = derive_claims((fact,), default_claim_policy(), repository_id=REPO).claims[0]
    old = replace(first, claimRevision=2)
    intervening = replace(first, claimRevision=5, payload={"category": "test", "argv": ["python", "-m", "pytest", "-q"]})
    changed_fact = replace(fact, value={"category": "test", "argv": ["python", "-m", "pytest", "--quiet"]})
    changed = derive_claims((changed_fact,), default_claim_policy(), repository_id=REPO,
                           prior_claims=(old, intervening)).claims[0]
    assert changed.claimRevision == 6

    format_only_history = replace(first, claimRevision=7, renderedText="Older formatting.", templateKey=None)
    unchanged = derive_claims((fact,), default_claim_policy(), repository_id=REPO,
                              prior_claims=(format_only_history,)).claims[0]
    assert unchanged.claimRevision == 7
    assert unchanged.proposalStatus == "pending"

    reverted = derive_claims((fact,), default_claim_policy(), repository_id=REPO,
                              prior_claims=(changed,)).claims[0]
    assert reverted.claimRevision == 7


def test_derive_keeps_all_candidates_even_when_budget_is_tiny() -> None:
    from dataclasses import replace as dc_replace

    facts = (
        _fact(fact_type="command.invocation", key="command.invocation:test:pnpm:repo-root",
              value={"category": "test", "argv": ["pnpm", "test"]}, derivation="template"),
        _fact(fact_type="command.invocation", key="command.invocation:test:go:repo-root",
              value={"category": "test", "argv": ["go", "test", "./..."]}, derivation="template"),
    )
    policy = dc_replace(default_claim_policy(), renderBudget=1)
    assert len(derive_claims(facts, policy, repository_id=REPO).claims) == 2


def test_typed_task_and_config_templates_preserve_only_approved_values() -> None:
    from repo2skill.claim_templates import format_claim_text

    facts = (
        _fact(fact_type="task.definition", key="task.definition:package-script.v1:repo-root:test",
              value={"category": "task", "runner": "package-script.v1", "name": "test",
                     "body": "vitest run --coverage"}),
        _fact(fact_type="config.structure", key="pyproject.toml:structure",
              value={"category": "structural-summary", "format": "toml",
                     "keys": ["project", "tool.pytest.ini_options"]}),
    )
    claims = derive_claims(facts, default_claim_policy(), repository_id=REPO).claims
    assert [claim.templateKey for claim in claims] == ["config-structure.v1", "task-definition.v1"]
    task = next(claim for claim in claims if claim.templateKey == "task-definition.v1")
    assert "vitest run --coverage" in format_claim_text(task.templateKey, task.payload)


def test_unknown_fact_template_fails_closed() -> None:
    import pytest

    unknown = _fact(fact_type="mystery.fact", key="mystery", value={"command": "unsafe"})
    with pytest.raises(ValueError, match="unsupported claim fact type"):
        derive_claims((unknown,), default_claim_policy(), repository_id=REPO)


def test_argv_formatter_quotes_placeholders_spaces_quotes_metacharacters_and_empty_args() -> None:
    from repo2skill.claim_templates import format_claim_text

    payload = {
        "category": "single-test.v1",
        "argv": ["runner", "<path>", "<TestName>", "two words", "it's", ";", ""],
    }
    assert format_claim_text("command-invocation.v1", payload) == (
        "Single-test command: `runner '<path>' '<TestName>' 'two words' "
        "'it'\"'\"'s' ';' ''`."
    )


def test_prior_claim_collision_identity_fails_closed(monkeypatch) -> None:
    import pytest

    import repo2skill.ids as ids

    monkeypatch.setattr(ids, "_digest_suffix", lambda _canonical: "0" * 24)
    old = _fact(fact_type="artifact.presence", key="artifact.presence:package.v1:old",
                value={"category": "package.v1", "name": "old", "path": "old.json"})
    current = _fact(fact_type="artifact.presence", key="artifact.presence:package.v1:new",
                    value={"category": "package.v1", "name": "new", "path": "new.json"})
    prior = derive_claims((old,), default_claim_policy(), repository_id=REPO).claims

    with pytest.raises(ids.IdCollisionError, match="collision"):
        derive_claims((current,), default_claim_policy(), repository_id=REPO, prior_claims=prior)


def test_id_context_is_explicitly_reusable_for_claim_collisions(monkeypatch) -> None:
    context = CanonicalIdContext()
    facts = (
        _fact(fact_type="artifact.presence", key="artifact.presence:package.v1:a",
              value={"category": "package.v1", "name": "a", "path": "a"}),
    )
    first = derive_claims(facts, default_claim_policy(), repository_id=REPO, id_context=context)
    second = derive_claims(facts, default_claim_policy(), repository_id=REPO, id_context=context)
    assert first.claims[0].claimId == second.claims[0].claimId
