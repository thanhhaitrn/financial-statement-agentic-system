"""ROUTING_MODE decides who owns a semantic decision: the model or a heuristic."""

import pytest

from agents import keyworder_runner, planner_runner
from config.runtime_policy import (
    DEFAULT_POLICY,
    RoutingPolicy,
    RuntimePolicy,
    set_active_policy,
)

BROAD_QUERY = "Đánh giá khả năng sinh lời của công ty"


@pytest.fixture(autouse=True)
def _restore_policy():
    yield
    set_active_policy(DEFAULT_POLICY)


def _use_mode(mode: str) -> RuntimePolicy:
    policy = DEFAULT_POLICY.with_overrides(routing=RoutingPolicy(mode=mode))
    set_active_policy(policy)
    return policy


def _planner_state(monkeypatch, *, model_succeeds=True):
    payload = {
        "difficulty_level": "hard",
        "response_mode": "extractive",
        "premise_requirements": [],
        "analysis_axes": [
            {
                "axis": "agent_profitability",
                "objective": "Tính và đánh giá ROA, ROE và biên lợi nhuận ròng.",
            }
        ],
        "company": "",
        "time_hint": "",
        "need_web": False,
    }

    def _invoke(*_args, **_kwargs):
        if not model_succeeds:
            raise RuntimeError("planner unavailable")
        return {"parsed": payload, "raw": None, "mode": "structured"}

    monkeypatch.setattr(planner_runner, "invoke_prompt", _invoke)
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)
    return {"user_query": BROAD_QUERY, "dataset_id": "", "debug_trace": True}


def _axes(updates):
    return [axis["axis"] for axis in updates["planner_plan"]["analysis_axes"]]


def test_legacy_mode_still_expands_the_planner_axes(monkeypatch):
    _use_mode("legacy")
    updates = planner_runner.run_planner(_planner_state(monkeypatch))

    assert _axes(updates) == [
        "agent_profitability",
        "agent_cashflow_analysis",
        "agent_efficiency",
    ]
    assert not [
        log for log in updates["trace"] if log.get("event") == "routing:shadow_diff"
    ]


def test_model_first_leaves_a_successful_planner_plan_alone(monkeypatch):
    _use_mode("model_first")
    updates = planner_runner.run_planner(_planner_state(monkeypatch))

    assert _axes(updates) == ["agent_profitability"]
    assert not [
        log
        for log in updates["trace"]
        if log.get("event") == "planner:profitability_axes_expanded"
    ]


def test_model_first_falls_back_to_the_heuristic_path_when_the_call_failed(monkeypatch):
    """A heuristic is a fallback, not an override: with no model decision to
    respect, model_first must behave exactly like legacy."""

    _use_mode("legacy")
    legacy = planner_runner.run_planner(
        _planner_state(monkeypatch, model_succeeds=False)
    )
    _use_mode("model_first")
    model_first = planner_runner.run_planner(
        _planner_state(monkeypatch, model_succeeds=False)
    )

    assert model_first["planner_plan"] == legacy["planner_plan"]
    assert any(log.get("event") == "planner:error" for log in model_first["trace"])
    assert not [
        log
        for log in model_first["trace"]
        if log.get("event") == "routing:model_first_planner_axes"
    ]


def test_shadow_keeps_legacy_output_and_reports_the_difference(monkeypatch):
    _use_mode("shadow")
    updates = planner_runner.run_planner(_planner_state(monkeypatch))

    assert _axes(updates) == [
        "agent_profitability",
        "agent_cashflow_analysis",
        "agent_efficiency",
    ]
    diff = next(
        log
        for log in updates["trace"]
        if log.get("event") == "routing:shadow_diff"
        and log.get("stage") == "planner_axes"
    )
    assert diff["heuristic_added_axes"] == [
        "agent_cashflow_analysis",
        "agent_efficiency",
    ]
    assert diff["model_axes_n"] == 1


def _direct_bypass_query():
    """An easy line-item query the deterministic router plan can answer."""

    plan = {"difficulty_level": "easy", "response_mode": "extractive"}
    for query in (
        "Tổng cộng tài sản là bao nhiêu",
        "Lợi nhuận sau thuế thu nhập doanh nghiệp là bao nhiêu",
        "Tiền và các khoản tương đương tiền là bao nhiêu",
    ):
        if keyworder_runner._direct_router_plan_from_query(plan, query) is not None:
            return plan, query
    pytest.skip("no direct line-item query available in this build")


def test_router_bypass_applies_in_legacy_and_is_skipped_in_model_first(monkeypatch):
    plan, query = _direct_bypass_query()
    calls = {"n": 0}

    def _invoke(*_args, **_kwargs):
        calls["n"] += 1
        raise RuntimeError("router unavailable")

    monkeypatch.setattr(keyworder_runner, "invoke_prompt", _invoke)
    state = {"user_query": query, "planner_plan": plan, "trace": []}

    _use_mode("legacy")
    legacy = keyworder_runner.run_keyworder(dict(state))
    assert calls["n"] == 0
    assert any(
        log.get("event") == "router:done"
        and log.get("mode") == "heuristic_direct_line_item"
        for log in legacy["trace"]
    )

    _use_mode("model_first")
    model_first = keyworder_runner.run_keyworder(dict(state))
    assert calls["n"] == 1
    assert not any(
        log.get("mode") == "heuristic_direct_line_item"
        for log in model_first["trace"]
    )


def test_shadow_reports_the_router_bypass_it_would_have_dropped(monkeypatch):
    plan, query = _direct_bypass_query()
    _use_mode("shadow")

    updates = keyworder_runner.run_keyworder(
        {"user_query": query, "planner_plan": plan, "trace": []}
    )

    diff = next(
        log
        for log in updates["trace"]
        if log.get("event") == "routing:shadow_diff"
        and log.get("stage") == "router_direct_bypass"
    )
    assert diff["bypassed"] is True


# ---------------------------------------------------------------------------
# Gate independence: model_first is the default, but recall-first retrieval is
# a separate switch because the A/B measured it as load-bearing.
# ---------------------------------------------------------------------------


def test_model_first_is_the_default_but_keeps_recall_first_retrieval():
    from config.runtime_policy import RuntimePolicy

    routing = RuntimePolicy.from_env({}).routing

    assert routing.mode == "model_first"
    # The two gates that let a heuristic override the model are off...
    assert routing.planner_axis_expansion_enabled is False
    assert routing.router_direct_bypass_enabled is False
    # ...but retrieval recall is not an override and stays on.
    assert routing.evidence_augmentation_enabled is True


def test_each_gate_can_be_pinned_independently_of_the_mode():
    from config.runtime_policy import RuntimePolicy

    pinned_on = RuntimePolicy.from_env(
        {"ROUTING_MODE": "model_first", "ROUTING_PLANNER_AXIS_EXPANSION": "1"}
    ).routing
    assert pinned_on.planner_axis_expansion_enabled is True
    assert pinned_on.router_direct_bypass_enabled is False

    pinned_off = RuntimePolicy.from_env(
        {"ROUTING_MODE": "legacy", "ROUTING_DIRECT_BYPASS": "0"}
    ).routing
    assert pinned_off.router_direct_bypass_enabled is False
    assert pinned_off.planner_axis_expansion_enabled is True

    pure = RuntimePolicy.from_env(
        {"ROUTING_EVIDENCE_AUGMENTATION": "0"}
    ).routing
    assert pure.evidence_augmentation_enabled is False


def test_invalid_gate_override_fails_fast():
    from config.runtime_policy import RuntimePolicy, RuntimePolicyError

    with pytest.raises(RuntimePolicyError):
        RuntimePolicy.from_env({"ROUTING_EVIDENCE_AUGMENTATION": "maybe"})


def test_default_mode_leaves_a_successful_planner_plan_alone(monkeypatch):
    """Without any env override the planner model now owns its axes."""

    set_active_policy(DEFAULT_POLICY)
    updates = planner_runner.run_planner(_planner_state(monkeypatch))

    assert _axes(updates) == ["agent_profitability"]
