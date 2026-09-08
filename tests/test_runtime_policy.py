"""Runtime limits come from one validated policy, not scattered constants."""

import pytest

from config.runtime_policy import (
    DEFAULT_POLICY,
    EvidencePolicy,
    RuntimePolicy,
    RuntimePolicyError,
    active_policy,
    set_active_policy,
)
from graph import conditions, dispatch_nodes
from graph import evidence as evidence_node
from graph.state import WorkflowServices
from graph.workflow import _bind_services
from schemas.table_names import TABLE_IS


@pytest.fixture(autouse=True)
def _restore_active_policy():
    yield
    set_active_policy(DEFAULT_POLICY)


def test_env_overrides_are_parsed_once_and_typed():
    policy = RuntimePolicy.from_env(
        {
            "EVIDENCE_FACTS_LIMIT": "14",
            "MAX_FOLLOWUP_ROUNDS": "1",
            "ROUTING_MODE": "shadow",
            "ROUTING_FALLBACK_CONFIDENCE": "0.75",
        }
    )

    assert policy.evidence.default_facts_limit == 14
    assert policy.execution.max_followup_rounds == 1
    assert policy.routing.mode == "shadow"
    assert policy.routing.fallback_confidence_threshold == 0.75
    # Untouched groups keep their defaults rather than becoming zero/None.
    assert policy.synth.max_facts_per_agent == 20


@pytest.mark.parametrize(
    "env",
    [
        {"EVIDENCE_FACTS_LIMIT": "abc"},
        {"EVIDENCE_FACTS_LIMIT": "0"},
        {"EVIDENCE_MAX_CONCURRENCY": "16"},
        {"ROUTING_MODE": "model-first"},
        {"ROUTING_FALLBACK_CONFIDENCE": "1.5"},
        {"MAX_FOLLOWUP_ROUNDS": "9"},
    ],
)
def test_invalid_overrides_fail_fast_instead_of_silently_defaulting(env):
    with pytest.raises(RuntimePolicyError):
        RuntimePolicy.from_env(env)


def test_fingerprint_changes_with_any_limit_and_is_stable_otherwise():
    baseline = RuntimePolicy.from_env({})
    same = RuntimePolicy.from_env({})
    changed = RuntimePolicy.from_env({"NOTE_FACTS_LIMIT": "13"})

    assert baseline.fingerprint() == same.fingerprint()
    assert baseline.fingerprint() != changed.fingerprint()
    assert len(baseline.fingerprint()) == 16


def test_run_identity_changes_when_the_effective_policy_changes():
    """An env-only cap change leaves the git tree untouched, so the run
    fingerprint has to carry the policy or a resume would reuse answers that
    were produced under different limits."""

    from evaluation.run_identity import build_runtime_fingerprints

    set_active_policy(DEFAULT_POLICY)
    baseline = build_runtime_fingerprints()["config"]
    assert build_runtime_fingerprints()["config"] == baseline

    set_active_policy(
        DEFAULT_POLICY.with_overrides(
            evidence=EvidencePolicy(hard_analysis_main_facts_limit=24)
        )
    )
    assert active_policy().evidence.hard_analysis_main_facts_limit == 24
    assert build_runtime_fingerprints()["config"] != baseline


def test_two_graphs_do_not_share_each_others_policy():
    seen = []
    node = _bind_services(
        lambda state: seen.append(active_policy().evidence.max_concurrency) or {},
        WorkflowServices(
            policy=DEFAULT_POLICY.with_overrides(
                evidence=EvidencePolicy(max_concurrency=1)
            )
        ),
    )
    other = _bind_services(
        lambda state: seen.append(active_policy().evidence.max_concurrency) or {},
        WorkflowServices(policy=DEFAULT_POLICY),
    )

    node({})
    other({})
    node({})

    assert seen == [1, 4, 1]


def test_hard_analysis_facts_are_not_truncated_below_the_policy_cap():
    facts = [
        {"item_name": f"row-{i}", "value": str(i), "table": TABLE_IS}
        for i in range(80)
    ]
    state = {
        "planner_plan": {"difficulty_level": "hard"},
        "worker_plan": {"difficulty_level": "hard"},
    }

    kept = dispatch_nodes._limit_facts_for_analysis_prompt(TABLE_IS, facts, state=state)

    assert len(kept) == DEFAULT_POLICY.evidence.hard_analysis_main_facts_limit == 32
    assert len(kept) == evidence_node.HARD_ANALYSIS_MAIN_FACTS_LIMIT


def test_followup_edge_uses_the_same_ceiling_as_synth():
    from agents import synth_runner

    state = {
        "synth_decision": {"status": "need_more"},
        "followup_requests": [{"agent": "agent_efficiency", "requirements": ["x"]}],
    }

    assert conditions.synth_route({**state, "followup_rounds": 1}) == "followup"
    assert (
        conditions.synth_route(
            {**state, "followup_rounds": synth_runner.MAX_FOLLOWUP_ROUNDS + 1}
        )
        == "end"
    )

    set_active_policy(
        DEFAULT_POLICY.with_overrides(
            execution=type(DEFAULT_POLICY.execution)(max_followup_rounds=1)
        )
    )
    assert conditions.synth_route({**state, "followup_rounds": 2}) == "end"
