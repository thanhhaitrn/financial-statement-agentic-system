"""Every agent the planner selects must be dispatched and own a final section."""

import pytest

from agents import keyworder_runner, synth_runner
from agents.agent_registry import (
    ANALYSIS_AGENT_ORDER,
    ANALYSIS_ASPECT_LABELS,
    analysis_aspect_headings,
)
from graph import dispatch_nodes, workflow
from output_formatter import format_final_answer


def _hard_planner_plan(agents):
    return {
        "difficulty_level": "hard",
        "response_mode": "extractive",
        "analysis_axes": [
            {"axis": agent, "objective": f"Đánh giá {ANALYSIS_ASPECT_LABELS[agent]}."}
            for agent in agents
        ],
    }


def test_reconcile_materializes_missing_axis_from_the_planner_objective():
    planner_plan = _hard_planner_plan(["agent_profitability", "agent_efficiency"])
    reconciled, notes = keyworder_runner.reconcile_analysis_plan_coverage(
        [{"agent": "agent_profitability", "objective": "Đánh giá biên lợi nhuận."}],
        planner_plan,
    )

    assert [item["agent"] for item in reconciled] == [
        "agent_profitability",
        "agent_efficiency",
    ]
    assert reconciled[1]["objective"] == "Đánh giá Hiệu quả hoạt động."
    assert notes == ["materialized_from_axis:agent_efficiency"]


def test_reconcile_drops_agents_the_planner_did_not_choose():
    planner_plan = _hard_planner_plan(["agent_profitability"])
    reconciled, notes = keyworder_runner.reconcile_analysis_plan_coverage(
        [
            {"agent": "agent_profitability", "objective": "Đánh giá biên lợi nhuận."},
            {"agent": "agent_liquidity_solvency", "objective": "Tự thêm."},
        ],
        planner_plan,
    )

    assert [item["agent"] for item in reconciled] == ["agent_profitability"]
    assert notes == ["dropped_unplanned_agent:agent_liquidity_solvency"]


def test_reconcile_is_a_no_op_without_planner_axes():
    plan = [{"agent": "agent_efficiency", "objective": "Router tự chọn."}]
    reconciled, notes = keyworder_runner.reconcile_analysis_plan_coverage(plan, {})

    assert reconciled == plan
    assert notes == []


def test_four_axis_plan_dispatches_four_agents_in_planner_order():
    planner_plan = _hard_planner_plan(list(ANALYSIS_AGENT_ORDER))
    worker_plan = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [], "analysis_plan": [], "targets": []},
        planner_plan,
        user_query="Đánh giá toàn diện tình hình tài chính công ty",
    )

    assert [item["agent"] for item in worker_plan["analysis_plan"]] == list(
        ANALYSIS_AGENT_ORDER
    )

    updates = dispatch_nodes.prepare_analysis_dispatch_state(
        {"worker_plan": worker_plan, "trace": []}
    )

    assert updates["expected_workers"] == sorted(ANALYSIS_AGENT_ORDER)
    assert len(updates["analysis_dispatch_targets"]) == 4

    log = next(
        item
        for item in updates["trace"]
        if item.get("event") == "analysis_dispatch:prepare"
    )
    assert log["planned_agents"] == list(ANALYSIS_AGENT_ORDER)
    assert log["dispatched_agents"] == sorted(ANALYSIS_AGENT_ORDER)
    assert log["missing_agents"] == []


def test_four_completed_agents_need_four_ordered_sections_and_one_final_answer():
    answer = "**Tóm tắt đánh giá**\n\n- Tổng hợp bốn khía cạnh.\n"
    for position, agent in enumerate(ANALYSIS_AGENT_ORDER, start=1):
        answer += (
            f"\n**{position}. {ANALYSIS_ASPECT_LABELS[agent]}**\n\n"
            f"- Số liệu cho khía cạnh {position}.\n"
        )

    worker_results = {
        "analysis_outputs": {
            agent: {"answer": f"- Kết quả {agent}.", "requirements": []}
            for agent in ANALYSIS_AGENT_ORDER
        }
    }
    state = {
        "planner_plan": {"difficulty_level": "hard", "response_mode": "extractive"},
        "worker_plan": {
            "difficulty_level": "hard",
            "analysis_plan": [
                {"agent": agent, "objective": "x"} for agent in ANALYSIS_AGENT_ORDER
            ],
        },
    }
    decision = {"status": "answer", "answer": answer, "followups": []}

    assert (
        synth_runner._hard_analysis_contract_violations(state, decision, worker_results)
        == []
    )

    # Dropping any one aspect is a contract violation for that agent only.
    for agent in ANALYSIS_AGENT_ORDER:
        label = ANALYSIS_ASPECT_LABELS[agent]
        without = "\n".join(
            line for line in answer.splitlines() if label not in line
        )
        violations = synth_runner._hard_analysis_contract_violations(
            state, {**decision, "answer": without}, worker_results
        )
        assert f"missing_aspect:{agent}" in violations

    formatted = format_final_answer({"synth_decision": decision})
    assert "=== FINAL ANSWER ===" not in formatted
    assert "ANSWER:" not in formatted
    for heading in analysis_aspect_headings():
        assert heading in formatted


def test_workflow_nodes_must_cover_every_registered_analysis_agent(monkeypatch):
    monkeypatch.setitem(workflow.ANALYSIS_NODES, "agent_unwired", ())
    with pytest.raises(RuntimeError, match="unregistered nodes"):
        workflow.assert_analysis_nodes_match_registry()

    monkeypatch.delitem(workflow.ANALYSIS_NODES, "agent_unwired")
    monkeypatch.delitem(workflow.ANALYSIS_NODES, "agent_efficiency")
    with pytest.raises(RuntimeError, match="missing nodes"):
        workflow.assert_analysis_nodes_match_registry()
