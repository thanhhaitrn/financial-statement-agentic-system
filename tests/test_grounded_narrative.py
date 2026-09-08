"""Contracts for qualitative interpretation grounded in report premises."""

from agents import keyworder_runner, planner_runner, synth_runner
from graph.dispatch_nodes import prepare_followup_dispatch_state
from schemas.agent_outputs import PlannerEvidencePlan
from schemas.table_names import TABLE_BS, TABLE_CF, TABLE_IS


NARRATIVE_QUERY = (
    "Ý nghĩa của việc Công ty chuyển đổi từ doanh nghiệp nhà nước sang "
    "công ty cổ phần niêm yết đối với quản trị doanh nghiệp là gì?"
)


def _planner_result(payload: dict) -> dict:
    return {
        "parsed": payload,
        "raw": None,
        "mode": "structured",
    }


def test_planner_response_mode_defaults_and_normalizes():
    assert PlannerEvidencePlan().response_mode == "extractive"
    assert (
        PlannerEvidencePlan(response_mode="grounded-interpretation").response_mode
        == "grounded_interpretation"
    )
    assert PlannerEvidencePlan(response_mode="unknown").response_mode == "extractive"


def test_narrative_interpretation_uses_medium_without_financial_axes(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: _planner_result(
            {
                "difficulty_level": "hard",
                "analysis_axes": [
                    {
                        "axis": "agent_profitability",
                        "objective": "Đánh giá tác động đến doanh nghiệp.",
                    }
                ],
                "company": "",
                "time_hint": "",
                "need_web": False,
            }
        ),
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": NARRATIVE_QUERY,
            "dataset_id": "",
            "debug_trace": True,
        }
    )

    assert updates["planner_plan"]["difficulty_level"] == "medium"
    assert updates["planner_plan"]["response_mode"] == "grounded_interpretation"
    assert updates["planner_plan"]["analysis_axes"] == []
    assert updates["planner_plan"]["premise_requirements"] == [
        "trạng thái doanh nghiệp nhà nước trước chuyển đổi",
        "sự kiện cổ phần hóa và đăng ký công ty cổ phần",
        "sự kiện cấp phép và niêm yết cổ phiếu",
    ]
    assert any(
        log.get("event") == "planner:qualitative_concept_no_financial_axes"
        and log.get("response_mode") == "grounded_interpretation"
        for log in updates["trace"]
    )


def test_profitability_assessment_overrides_grounded_and_dispatches_supporting_axes(
    monkeypatch,
):
    """Covers the heuristic axis expansion itself, so it pins the gate on.

    The default is model_first, where a successful planner call owns its axes;
    this heuristic is the fallback path and is exercised explicitly.
    """

    from config.runtime_policy import (
        DEFAULT_POLICY,
        RoutingPolicy,
        set_active_policy,
    )

    set_active_policy(
        DEFAULT_POLICY.with_overrides(
            routing=RoutingPolicy(planner_axis_expansion=True)
        )
    )
    monkeypatch.setattr(
        planner_runner, "shadow_routing", lambda: False, raising=False
    )
    try:
        _run_profitability_axis_expansion_case(monkeypatch)
    finally:
        set_active_policy(DEFAULT_POLICY)


def _run_profitability_axis_expansion_case(monkeypatch):
    query = "Đánh giá khả năng sinh lời của công ty"
    profitability_axis = {
        "axis": "agent_profitability",
        "objective": "Tính và đánh giá ROA, ROE và biên lợi nhuận ròng.",
    }
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: _planner_result(
            {
                "difficulty_level": "hard",
                "response_mode": "grounded_interpretation",
                "premise_requirements": [
                    "lợi nhuận sau thuế",
                    "doanh thu thuần",
                    "tổng tài sản",
                    "vốn chủ sở hữu",
                ],
                "analysis_axes": [profitability_axis],
                "company": "",
                "time_hint": "",
                "need_web": False,
            }
        ),
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": query,
            "dataset_id": "",
            "debug_trace": True,
        }
    )
    plan = updates["planner_plan"]
    axis_agents = [axis["axis"] for axis in plan["analysis_axes"]]

    assert plan["difficulty_level"] == "hard"
    assert plan["response_mode"] == "extractive"
    assert plan["premise_requirements"] == []
    assert axis_agents == [
        "agent_profitability",
        "agent_cashflow_analysis",
        "agent_efficiency",
    ]
    assert plan["analysis_axes"][0]["axis"] == profitability_axis["axis"]
    assert plan["analysis_axes"][0]["objective"].startswith(
        profitability_axis["objective"]
    )
    assert "doanh thu thuần" in plan["analysis_axes"][0]["objective"]
    assert "lợi nhuận sau thuế" in plan["analysis_axes"][0]["objective"]
    assert "kỳ so sánh" in plan["analysis_axes"][0]["objective"]

    done_logs = [
        log for log in updates["trace"] if log.get("event") == "planner:done"
    ]
    assert len(done_logs) == 1
    assert done_logs[0]["difficulty_level"] == "hard"
    assert done_logs[0]["response_mode"] == "extractive"
    assert done_logs[0]["premise_requirements_n"] == 0
    assert [axis["axis"] for axis in done_logs[0]["analysis_axes"]] == axis_agents
    assert any(
        log.get("event") == "planner:response_mode_invariants"
        and "axes_or_hard->extractive" in log.get("corrections", [])
        for log in updates["trace"]
    )
    assert any(
        log.get("event") == "planner:profitability_axes_expanded"
        and log.get("added_axes")
        == ["agent_cashflow_analysis", "agent_efficiency"]
        for log in updates["trace"]
    )

    routed = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [], "analysis_plan": [], "targets": []},
        plan,
        user_query=query,
    )
    assert [item["agent"] for item in routed["analysis_plan"]] == axis_agents
    profitability_queries = routed["analysis_plan"][0]["evidence_queries"]
    cashflow_queries = routed["analysis_plan"][1]["evidence_queries"]
    assert [
        (item["table"], item.get("period_role", ""), item.get("period", ""))
        for item in profitability_queries
    ] == [
        (TABLE_IS, "both", ""),
        (TABLE_IS, "both", ""),
        (TABLE_IS, "both", ""),
        (TABLE_IS, "both", ""),
        (TABLE_BS, "", "both"),
        (TABLE_BS, "", "both"),
    ]
    assert cashflow_queries[-1]["table"] == TABLE_CF
    assert cashflow_queries[-1]["period_role"] == "both"
    assert routed["targets"] == routed["analysis_plan"]


def test_profitability_axis_expansion_is_narrow_and_sustainability_aware():
    profitability_axis = {
        "axis": "agent_profitability",
        "objective": "Đánh giá ROA.",
    }
    base_plan = {
        "difficulty_level": "hard",
        "response_mode": "extractive",
        "analysis_axes": [profitability_axis],
    }

    ratio_plan, added = planner_runner._expand_broad_profitability_axes(
        {"user_query": "Đánh giá ROA của công ty"},
        base_plan,
    )
    assert ratio_plan["analysis_axes"] == [profitability_axis]
    assert added == []

    sustainable_plan, added = planner_runner._expand_broad_profitability_axes(
        {
            "user_query": (
                "Đánh giá tính bền vững của khả năng sinh lời của công ty"
            )
        },
        base_plan,
    )
    assert [axis["axis"] for axis in sustainable_plan["analysis_axes"]] == [
        "agent_profitability",
        "agent_liquidity_solvency",
        "agent_cashflow_analysis",
        "agent_efficiency",
    ]
    assert set(added) == {
        "agent_liquidity_solvency",
        "agent_cashflow_analysis",
        "agent_efficiency",
    }


def test_canonical_narrative_premises_are_minimum_when_model_is_generic():
    generic_model_premise = "quá trình hình thành/chuyển đổi/niêm yết"

    plan, _log = planner_runner._apply_planner_difficulty_heuristics(
        {"user_query": NARRATIVE_QUERY},
        {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": [generic_model_premise],
            "analysis_axes": [],
        },
    )

    assert plan["premise_requirements"][:3] == [
        "trạng thái doanh nghiệp nhà nước trước chuyển đổi",
        "sự kiện cổ phần hóa và đăng ký công ty cổ phần",
        "sự kiện cấp phép và niêm yết cổ phiếu",
    ]
    assert generic_model_premise in plan["premise_requirements"]


def test_literal_front_matter_lookup_remains_extractive(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: _planner_result(
            {
                "difficulty_level": "hard",
                "response_mode": "grounded_interpretation",
                "analysis_axes": [],
                "company": "",
                "time_hint": "",
                "need_web": False,
            }
        ),
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": "Công ty được cổ phần hóa vào ngày nào?",
            "dataset_id": "",
            "debug_trace": False,
        }
    )

    assert updates["planner_plan"]["difficulty_level"] == "easy"
    assert updates["planner_plan"]["response_mode"] == "extractive"
    assert updates["planner_plan"]["analysis_axes"] == []
    assert updates["planner_plan"]["premise_requirements"] == []


def test_financial_metric_interpretation_does_not_use_narrative_mode():
    assert planner_runner._is_grounded_interpretation_query(
        "Ý nghĩa của mức thay đổi doanh thu thuần đối với biên lợi nhuận là gì?"
    ) is False


def test_narrative_classifier_covers_routes_and_rejects_numeric_governance():
    assert planner_runner._is_grounded_interpretation_query(
        "Vai trò của việc cổ phần hóa và niêm yết đối với quản trị là gì?"
    )
    assert planner_runner._is_grounded_interpretation_query(
        "Ảnh hưởng của việc niêm yết đến quản trị doanh nghiệp là gì?"
    )
    assert planner_runner._is_grounded_interpretation_query(
        "Ý nghĩa của chính sách không khấu hao quyền sử dụng đất là gì?"
    )
    assert not planner_runner._is_grounded_interpretation_query(
        "Đánh giá mức thay đổi thù lao HĐQT 2025 so với 2024"
    )
    assert not planner_runner._is_grounded_interpretation_query(
        "Đánh giá nợ vay ngắn hạn của Công ty"
    )
    assert not planner_runner._is_grounded_interpretation_query(
        "Phân tích chi phí khấu hao năm 2025"
    )


def test_complete_calculation_contract_is_promoted_to_medium():
    for query in (
        "Tính hệ số nợ trên vốn chủ sở hữu (D/E) tại 31/12/2025.",
        "Tỷ trọng doanh thu bán thành phẩm trong tổng doanh thu năm 2025?",
        (
            "Tổng tài sản thay đổi từ 1/1/2025 đến 31/12/2025 "
            "và mức thay đổi là bao nhiêu?"
        ),
    ):
        plan, _log = planner_runner._apply_planner_difficulty_heuristics(
            {"user_query": query},
            {
                "difficulty_level": "easy",
                "response_mode": "extractive",
                "analysis_axes": [],
            },
        )

        assert plan["difficulty_level"] == "medium"
        assert plan["analysis_axes"] == []


def test_planner_fallback_still_applies_grounded_interpretation_heuristic(monkeypatch):
    monkeypatch.setattr(
        planner_runner,
        "invoke_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("invalid output")),
    )
    monkeypatch.setattr(planner_runner, "get_dataset", lambda dataset_id: None)

    updates = planner_runner.run_planner(
        {
            "user_query": NARRATIVE_QUERY,
            "dataset_id": "",
            "debug_trace": False,
        }
    )

    assert updates["planner_plan"]["difficulty_level"] == "medium"
    assert updates["planner_plan"]["response_mode"] == "grounded_interpretation"
    assert updates["planner_plan"]["analysis_axes"] == []
    assert len(updates["planner_plan"]["premise_requirements"]) == 3


def test_router_restores_each_planned_narrative_premise():
    premises = [
        "trạng thái doanh nghiệp nhà nước trước chuyển đổi",
        "sự kiện cổ phần hóa và đăng ký công ty cổ phần",
        "sự kiện cấp phép và niêm yết cổ phiếu",
    ]
    plan = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [], "analysis_plan": []},
        {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": premises,
            "analysis_axes": [],
        },
        user_query=NARRATIVE_QUERY,
    )
    routed_queries = {
        query
        for item in plan["evidence_plan"]
        for query in item.get("queries", [item.get("query", "")])
        if query
    }

    assert set(premises).issubset(routed_queries)
    assert plan["analysis_plan"] == []
    routes_by_query = {
        premise: {
            item["table"]
            for item in plan["evidence_plan"]
            if premise in item.get("queries", [item.get("query", "")])
        }
        for premise in premises
    }
    assert {
        "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    }.issubset(routes_by_query["sự kiện cổ phần hóa và đăng ký công ty cổ phần"])
    assert {
        "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        "PHẦN ĐẦU BÁO CÁO TÀI CHÍNH",
    }.issubset(routes_by_query["sự kiện cấp phép và niêm yết cổ phiếu"])


def test_followup_preserves_grounded_mode_and_premises():
    premises = ["sự kiện cổ phần hóa", "sự kiện niêm yết"]
    updates = prepare_followup_dispatch_state(
        {
            "planner_plan": {
                "difficulty_level": "medium",
                "response_mode": "grounded_interpretation",
                "premise_requirements": premises,
                "company": "",
                "need_web": False,
            },
            "worker_plan": {"analysis_plan": []},
            "followup_requests": [
                {
                    "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    "requirements": ["sự kiện niêm yết"],
                    "reason": "thiếu premise",
                }
            ],
            "followup_rounds": 0,
        }
    )

    assert updates["planner_plan"]["response_mode"] == "grounded_interpretation"
    assert updates["planner_plan"]["premise_requirements"] == premises


def test_synth_plan_and_instruction_propagate_grounded_mode():
    premises = ["sự kiện cổ phần hóa", "sự kiện niêm yết"]
    state = {
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": premises,
        },
        "worker_plan": {"difficulty_level": "medium", "analysis_plan": []},
    }

    payload = synth_runner._synth_plan_payload(state)
    instruction = synth_runner._synth_difficulty_instruction(state)

    assert payload["response_mode"] == "grounded_interpretation"
    assert payload["premise_requirements"] == premises
    assert "GROUNDED INTERPRETATION" in instruction
    assert "Dữ liệu trích xuất" in instruction
    assert "Suy luận đánh giá" in instruction
    assert "Suy luận có giới hạn" not in instruction
    assert "theo hai phần" in instruction
    assert "theo ba phần" not in instruction


def test_grounded_refusal_gets_one_constrained_repair(monkeypatch):
    compliant = {
        "status": "answer",
        "answer": (
            "**Dữ liệu trích xuất**: Công ty đã cổ phần hóa và niêm yết.\n\n"
            "**Suy luận đánh giá**: Từ các dữ kiện này có thể suy ra khuôn "
            "khổ quản trị của doanh nghiệp đã thay đổi."
        ),
        "followups": [],
    }
    calls = []

    def fake_invoke(payload):
        calls.append(payload)
        return compliant, None, "structured"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)
    state = {
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
        }
    }
    worker_results = {
        "agent_note": {
            "facts": [
                {
                    "item_name": "Hình thức sở hữu vốn",
                    "value": "Cổ phần hóa năm 2003; niêm yết năm 2006.",
                    "fact_id": "fact-history",
                    "source": "report.md#page=12",
                }
            ]
        }
    }

    repaired, _usage, mode, violations, remaining, retried, accepted, _diag = synth_runner._retry_synth_quality_once(
        state,
        {
            "system_instruction": "base",
            "last_agent_response": "",
            "user_query": "",
            "plan_json": "{}",
        },
        worker_results,
        {
            "status": "answer",
            "answer": "Không có thông tin về ý nghĩa của sự chuyển đổi.",
            "followups": [],
        },
    )

    assert retried is True
    assert mode == "structured"
    assert repaired == compliant
    assert violations == ["grounded_interpretation_contract"]
    assert remaining == []
    assert accepted == "general_repair"
    assert len(calls) == 1
    assert "QUALITY REVIEW" in calls[0]["system_instruction"]
    assert synth_runner._grounded_interpretation_contract_passes(repaired)


def test_grounded_contract_does_not_retry_when_already_satisfied(monkeypatch):
    decision = {
        "status": "answer",
        "answer": (
            "Dữ liệu trích xuất: doanh nghiệp đã niêm yết. "
            "Suy luận đánh giá: Từ các dữ kiện này có thể suy ra cơ chế "
            "giám sát có thể thay đổi."
        ),
        "followups": [],
    }
    monkeypatch.setattr(
        synth_runner,
        "_invoke_synth",
        lambda payload: (_ for _ in ()).throw(AssertionError("unexpected retry")),
    )

    repaired, usage, mode, violations, remaining, retried, accepted, _diag = synth_runner._retry_synth_quality_once(
        {
            "planner_plan": {
                "difficulty_level": "medium",
                "response_mode": "grounded_interpretation",
            }
        },
        {"system_instruction": "base", "user_query": "", "plan_json": "{}"},
        {
            "agent_note": {
                "facts": [
                    {
                        "item_name": "Sự kiện niêm yết",
                        "value": "Công ty đã niêm yết.",
                        "fact_id": "fact-listing",
                        "source": "report.md#page=12",
                    }
                ]
            }
        },
        decision,
    )

    assert repaired == decision
    assert usage is None
    assert mode == ""
    assert retried is False
    assert violations == []
    assert remaining == []
    assert accepted == "initial"


def test_legacy_three_section_grounded_answer_gets_repaired(monkeypatch):
    compliant = {
        "status": "answer",
        "answer": (
            "Dữ liệu trích xuất: Công ty đã niêm yết. "
            "Suy luận đánh giá: Từ các dữ kiện này có thể suy ra cơ chế "
            "giám sát có thể thay đổi."
        ),
        "followups": [],
    }
    calls = []

    def fake_invoke(payload):
        calls.append(payload)
        return compliant, None, "structured"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)
    state = {
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
        }
    }
    worker_results = {
        "agent_note": {
            "facts": [
                {
                    "item_name": "Sự kiện niêm yết",
                    "value": "Công ty đã niêm yết.",
                    "fact_id": "fact-listing",
                    "source": "report.md#page=12",
                }
            ]
        }
    }
    legacy = {
        "status": "answer",
        "answer": (
            "Fact được báo cáo xác nhận: Công ty đã niêm yết. "
            "Suy luận có giới hạn: Từ dữ kiện này có thể suy ra cơ chế "
            "giám sát có thể thay đổi. "
            "Giới hạn bằng chứng: Báo cáo chưa chứng minh mức độ tác động."
        ),
        "followups": [],
    }

    repaired, _usage, mode, violations, remaining, retried, accepted, _diag = synth_runner._retry_synth_quality_once(
        state,
        {"system_instruction": "base", "user_query": "", "plan_json": "{}"},
        worker_results,
        legacy,
    )

    assert retried is True
    assert mode == "structured"
    assert repaired == compliant
    assert violations == ["grounded_interpretation_contract"]
    assert remaining == []
    assert accepted == "general_repair"
    assert len(calls) == 1
    assert not synth_runner._grounded_interpretation_contract_passes(legacy)
    assert synth_runner._grounded_interpretation_contract_passes(repaired)


def test_previous_two_section_headings_no_longer_pass_grounded_contract():
    decision = {
        "status": "answer",
        "answer": (
            "Fact được báo cáo xác nhận: Công ty đã niêm yết. "
            "Suy luận dựa trên fact: Từ dữ kiện này có thể suy ra cơ chế "
            "giám sát có thể thay đổi."
        ),
        "followups": [],
    }

    assert not synth_runner._grounded_interpretation_contract_passes(decision)


def test_grounded_contract_rejects_refusal_inside_inference_section():
    decision = {
        "status": "answer",
        "answer": (
            "Dữ liệu trích xuất: Công ty đã cổ phần hóa và niêm yết. "
            "Suy luận đánh giá: Không thể đánh giá ý nghĩa của các sự kiện."
        ),
        "followups": [],
    }

    assert not synth_runner._grounded_interpretation_contract_passes(decision)


def test_grounded_contract_rejects_marker_only_answer():
    decision = {
        "status": "answer",
        "answer": (
            "Dữ liệu trích xuất. Suy luận đánh giá."
        ),
        "followups": [],
    }

    assert not synth_runner._grounded_interpretation_contract_passes(decision)


def test_grounded_contract_rejects_unsupported_claim_as_reported_fact():
    state = {
        "user_query": NARRATIVE_QUERY,
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
        },
    }
    worker_results = {
        "agent_note": {
            "facts": [
                {
                    "item_name": "Hình thức sở hữu vốn",
                    "value": "Cổ phần hóa năm 2003; niêm yết năm 2006.",
                    "fact_id": "fact-history",
                    "source": "report.md#page=12",
                }
            ]
        }
    }
    decision = {
        "status": "answer",
        "answer": (
            "Dữ liệu trích xuất: Công ty đã cổ phần hóa, niêm yết và "
            "đã tạo kênh huy động vốn. "
            "Suy luận đánh giá: Từ các dữ kiện này có thể suy ra khuôn khổ "
            "quản trị thay đổi."
        ),
        "followups": [],
    }

    assert not synth_runner._grounded_interpretation_contract_passes(
        decision,
        state=state,
        worker_results_payload=worker_results,
    )


def test_grounded_contract_requires_every_planned_premise():
    premises = [
        "trạng thái doanh nghiệp nhà nước trước chuyển đổi",
        "sự kiện cổ phần hóa và đăng ký công ty cổ phần",
        "sự kiện cấp phép và niêm yết cổ phiếu",
    ]
    state = {
        "user_query": NARRATIVE_QUERY,
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": premises,
        },
    }
    worker_results = {
        "agent_note": {
            "facts": [
                {
                    "item_name": "Sự kiện niêm yết",
                    "value": "Cổ phiếu được cấp phép và niêm yết năm 2006.",
                    "fact_id": "fact-listing",
                    "source": "report.md#page=12",
                }
            ]
        }
    }
    decision = {
        "status": "answer",
        "answer": (
            "Dữ liệu trích xuất: Công ty từng là doanh nghiệp nhà nước, "
            "đã cổ phần hóa, đăng ký công ty cổ phần và được cấp phép niêm yết. "
            "Suy luận đánh giá: Từ các dữ kiện này có thể suy ra khuôn khổ "
            "quản trị thay đổi."
        ),
        "followups": [],
    }

    assert not synth_runner._grounded_interpretation_contract_passes(
        decision,
        state=state,
        worker_results_payload=worker_results,
    )


def test_grounded_premise_binding_rejects_listing_policy_route_hint():
    premises = [
        "trạng thái doanh nghiệp nhà nước trước chuyển đổi",
        "sự kiện cổ phần hóa và đăng ký công ty cổ phần",
        "sự kiện cấp phép và niêm yết cổ phiếu",
    ]
    state = {
        "user_query": NARRATIVE_QUERY,
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": premises,
        },
    }
    worker_results = {
        "agent_note": {
            "facts": [
                {
                    "value": (
                        "Năm 1993 Công ty hoạt động theo loại hình "
                        "Doanh nghiệp Nhà Nước."
                    ),
                    "fact_id": "fact-state-owned",
                    "source": "report.md#page=2",
                    "status": "found",
                },
                {
                    "value": (
                        "Năm 2003 Công ty được cổ phần hóa và đăng ký trở "
                        "thành công ty cổ phần."
                    ),
                    "fact_id": "fact-corporatization",
                    "source": "report.md#page=2",
                    "status": "found",
                },
                {
                    # The route hint and label repeat the requested premise,
                    # but the evidence payload is only a valuation policy.
                    "evidence_query": premises[2],
                    "item_name": "Sự kiện cấp phép và niêm yết cổ phiếu",
                    "value": (
                        "Đối với chứng khoán niêm yết trên Sở Giao dịch "
                        "Chứng khoán, giá đóng cửa được dùng để xác định "
                        "giá trị hợp lý."
                    ),
                    "fact_id": "fact-listing-policy",
                    "source": "report.md#page=18",
                    "status": "found",
                },
            ]
        }
    }

    bindings, missing = synth_runner._grounded_premise_bindings(
        state,
        worker_results,
    )

    assert set(bindings) == set(premises[:2])
    assert missing == [premises[2]]


def test_grounded_listing_event_contrastive_fact_binds():
    premise = "sự kiện cấp phép và niêm yết cổ phiếu"
    state = {
        "planner_plan": {
            "response_mode": "grounded_interpretation",
            "premise_requirements": [premise],
        }
    }
    worker_results = {
        "agent_note": {
            "facts": [
                {
                    "value": (
                        "Cổ phiếu của Công ty được cấp phép và niêm yết "
                        "trên Sở Giao dịch Chứng khoán năm 2006."
                    ),
                    "fact_id": "fact-listing-event",
                    "source": "report.md#page=2",
                    "status": "found",
                }
            ]
        }
    }

    bindings, missing = synth_runner._grounded_premise_bindings(
        state,
        worker_results,
    )

    assert missing == []
    assert bindings[premise][0]["fact_id"] == "fact-listing-event"


def test_invalid_grounded_repair_is_returned_with_diagnostics(monkeypatch):
    state = {
        "user_query": "Ý nghĩa của việc niêm yết đối với quản trị là gì?",
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": ["sự kiện cấp phép và niêm yết cổ phiếu"],
        },
    }
    worker_results = {
        "agent_note": {
            "facts": [
                {
                    "item_name": "Cấp phép niêm yết",
                    "value": "Cổ phiếu được cấp phép và niêm yết năm 2006.",
                    "fact_id": "fact-listing",
                    "source": "report.md#page=12",
                    "source_page": "12",
                }
            ]
        }
    }
    monkeypatch.setattr(
        synth_runner,
        "_invoke_synth",
        lambda payload: (
            {
                "status": "answer",
                "answer": "Không có thông tin để trả lời.",
                "followups": [],
            },
            None,
            "structured",
        ),
    )

    repaired, _usage, mode, violations, remaining, retried, accepted, _diag = synth_runner._retry_synth_quality_once(
        state,
        {"system_instruction": "base", "user_query": state["user_query"], "plan_json": "{}"},
        worker_results,
        {
            "status": "answer",
            "answer": "Không có thông tin để trả lời.",
            "followups": [],
        },
    )

    assert retried is True
    assert mode == "structured"
    assert accepted == "general_repair"
    assert violations == ["grounded_interpretation_contract"]
    assert remaining == ["grounded_interpretation_contract"]
    assert repaired["answer"] == "Không có thông tin để trả lời."


def test_grounded_quality_retry_does_not_turn_placeholders_into_prose(monkeypatch):
    monkeypatch.setattr(
        synth_runner,
        "_invoke_synth",
        lambda payload: (
            {
                "status": "answer",
                "answer": "Không có thông tin để đánh giá ý nghĩa.",
                "followups": [],
            },
            None,
            "structured",
        ),
    )
    state = {
        "user_query": NARRATIVE_QUERY,
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
        },
    }
    placeholders = {
        "agent_note": {
            "facts": [
                {
                    "item_name": "Sự kiện niêm yết",
                    "value": "",
                    "status": "not_found_after_search",
                },
                {
                    "item_name": "Cổ phần hóa",
                    "value": "Có nhắc đến",
                    "status": "found",
                },
            ]
        }
    }

    decision, usage, mode, violations, remaining, retried, accepted, _diag = synth_runner._retry_synth_quality_once(
        state,
        {"system_instruction": "base", "user_query": state["user_query"], "plan_json": "{}"},
        placeholders,
        {
            "status": "answer",
            "answer": "Không có thông tin để đánh giá ý nghĩa.",
            "followups": [],
        },
    )

    assert decision["answer"].startswith("Không có thông tin")
    assert usage is None
    assert mode == "structured"
    assert retried is True
    assert accepted == "general_repair"
    assert violations == ["grounded_interpretation_contract"]
    assert remaining == ["grounded_interpretation_contract"]


def test_run_synth_repairs_grounded_answer_for_recovered_fallback_modes(monkeypatch):
    calls = []
    compliant = {
        "status": "answer",
        "answer": (
            "Dữ liệu trích xuất: Công ty đã cổ phần hóa năm 2003 và "
            "niêm yết năm 2006. "
            "Suy luận đánh giá: Từ các dữ kiện này có thể suy ra khuôn khổ "
            "quản trị đã thay đổi."
        ),
        "followups": [],
    }

    def fake_invoke(payload):
        calls.append(payload)
        if len(calls) == 1:
            return (
                {
                    "status": "answer",
                    "answer": "Không có thông tin về ý nghĩa của quá trình này.",
                    "followups": [],
                },
                None,
                "structured_raw_recovered",
            )
        return compliant, None, "plain_json_after_structured_parse_error"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)
    updates = synth_runner.run_synth(
        {
            "user_query": NARRATIVE_QUERY,
            "planner_plan": {
                "difficulty_level": "medium",
                "response_mode": "grounded_interpretation",
                "premise_requirements": [
                    "sự kiện cổ phần hóa",
                    "sự kiện niêm yết",
                ],
            },
            "worker_plan": {"analysis_plan": [], "evidence_plan": []},
            "worker_results": {
                "agent_note": {
                    "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    "facts": [
                        {
                            "item_name": "Hình thức sở hữu vốn",
                            "value": (
                                "Công ty được cổ phần hóa năm 2003 và cổ phiếu "
                                "được niêm yết năm 2006."
                            ),
                            "fact_id": "fact-history",
                            "source": "report.md#page=12",
                            "status": "found",
                        }
                    ],
                }
            },
            "trace": [],
        }
    )

    assert len(calls) == 2
    assert updates["synth_decision"] == compliant
    quality = next(item for item in updates["trace"] if item.get("event") == "synth:quality_check")
    assert quality["retry_attempted"] is True
    assert quality["accepted_candidate"] == "general_repair"
    assert quality["remaining_violations"] == []
