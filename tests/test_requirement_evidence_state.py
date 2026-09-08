"""A top-k miss must not be mistaken for proof that a requirement is absent."""

from agents import agent_runner, keyworder_runner, synth_runner
from schemas.requirements import (
    REQUIREMENT_AMBIGUOUS,
    REQUIREMENT_EXHAUSTIVE_ABSENT,
    REQUIREMENT_MATCHED,
    REQUIREMENT_UNMATCHED_TOPK,
    requirement_evidence_state,
)
from schemas.table_names import TABLE_BS, TABLE_CF, TABLE_IS, TABLE_NOTE


def test_requirement_evidence_state_distinguishes_topk_from_exhaustive_absence():
    requirement = "chi phí bán hàng"
    topk_miss = {
        "table": TABLE_IS,
        "item_name": requirement,
        "value": "",
        "status": "not_found_after_search",
    }
    exhaustive_miss = {
        **topk_miss,
        "evidence_state": "exhaustive_absent",
        "search_exhaustive": True,
    }

    assert (
        requirement_evidence_state(requirement, [topk_miss], table=TABLE_IS)
        == REQUIREMENT_UNMATCHED_TOPK
    )
    assert (
        requirement_evidence_state(requirement, [exhaustive_miss], table=TABLE_IS)
        == REQUIREMENT_EXHAUSTIVE_ABSENT
    )


def test_requirement_evidence_state_preserves_matched_and_ambiguous():
    requirement = "doanh thu thuần"

    assert requirement_evidence_state(
        requirement,
        [
            {
                "table": TABLE_IS,
                "item_name": requirement,
                "value": "100",
                "status": "found",
            }
        ],
        table=TABLE_IS,
    ) == REQUIREMENT_MATCHED
    assert requirement_evidence_state(
        requirement,
        [
            {
                "table": TABLE_IS,
                "item_name": requirement,
                "value": "100 hoặc 101",
                "status": "ambiguous",
            }
        ],
        table=TABLE_IS,
    ) == REQUIREMENT_AMBIGUOUS


def test_same_complete_typed_slot_with_conflicting_values_is_ambiguous():
    base = {
        "table": TABLE_IS,
        "item_name": "Doanh thu | Năm nay",
        "row_label": "Doanh thu",
        "column_label": "Năm nay",
        "period": "cuối",
        "period_role": "current",
        "value_kind": "amount",
        "unit": "VND",
        "status": "found",
    }

    assert requirement_evidence_state(
        "doanh thu",
        [
            {**base, "value": "100", "parsed_value": "100"},
            {**base, "value": "120", "parsed_value": "120"},
        ],
        table=TABLE_IS,
    ) == REQUIREMENT_AMBIGUOUS


def test_equivalent_values_and_different_typed_slots_do_not_conflict():
    base = {
        "table": TABLE_IS,
        "item_name": "Doanh thu | Năm nay",
        "row_label": "Doanh thu",
        "column_label": "Năm nay",
        "period": "cuối",
        "period_role": "current",
        "value_kind": "amount",
        "unit": "VND",
        "status": "found",
    }

    assert requirement_evidence_state(
        "doanh thu",
        [
            {**base, "value": "100", "parsed_value": "100"},
            {**base, "value": "100,0", "parsed_value": "100.0"},
            {
                **base,
                "item_name": "Doanh thu | Năm trước",
                "column_label": "Năm trước",
                "period": "đầu",
                "period_role": "previous",
                "value": "80",
                "parsed_value": "80",
            },
        ],
        table=TABLE_IS,
    ) == REQUIREMENT_MATCHED


def test_two_reporting_period_requirement_needs_both_legs_in_one_block():
    query = "So sánh doanh thu năm hiện tại và năm trước"
    base = {
        "table": TABLE_IS,
        "company": "Công ty A",
        "fiscal_year": "2025",
        "block_id": "revenue-schedule",
        "metric_label": "Doanh thu",
        "row_label": "Doanh thu",
        "unit": "VND",
        "value_kind": "amount",
        "status": "found",
    }
    current = {
        **base,
        "fact_id": "revenue-current",
        "item_name": "Doanh thu | Năm nay",
        "column_label": "Năm nay",
        "period_label": "Năm nay",
        "value": "120",
    }
    previous = {
        **base,
        "fact_id": "revenue-previous",
        "item_name": "Doanh thu | Năm trước",
        "column_label": "Năm trước",
        "period_label": "Năm trước",
        "value": "100",
    }

    assert (
        requirement_evidence_state(query, [current], table=TABLE_IS)
        == REQUIREMENT_UNMATCHED_TOPK
    )
    assert (
        requirement_evidence_state(
            query,
            [current, {**previous, "block_id": "other-block"}],
            table=TABLE_IS,
        )
        == REQUIREMENT_UNMATCHED_TOPK
    )
    assert (
        requirement_evidence_state(
            query,
            [current, previous],
            table=TABLE_IS,
        )
        == REQUIREMENT_MATCHED
    )


def test_analysis_agent_does_not_close_requirement_on_plain_topk_miss(monkeypatch):
    state = {}
    facts = [
        {
            "table": TABLE_IS,
            "item_name": "chi phí bán hàng",
            "value": "",
            "status": "not_found_after_search",
        }
    ]
    monkeypatch.setattr(
        agent_runner,
        "_table_for_requirement",
        lambda *_args, **_kwargs: TABLE_IS,
    )

    assert not agent_runner._requirement_satisfied_by_evidence(
        state,
        "agent_profitability",
        "chi phí bán hàng",
        facts,
    )


def test_cashflow_net_change_routes_to_cashflow_despite_broad_balance_money_query():
    requirement = "lưu chuyển tiền thuần trong kỳ"
    target = {
        "agent": "agent_cashflow_analysis",
        "evidence_queries": [
            {"table": TABLE_BS, "query": "tiền và các khoản tương đương tiền"},
            {"table": TABLE_CF, "query": requirement},
        ],
    }
    state = {
        # This legacy/global list used to be searched first and incorrectly
        # collapsed the cash-flow requirement to the BS keyword "tiền".
        "evidence_queries": [
            {"table": TABLE_BS, "query": "tiền và các khoản tương đương tiền"},
        ],
        "dispatch_target": target,
        "worker_plan": {"analysis_plan": [target]},
    }

    assert agent_runner._table_for_requirement(
        state,
        requirement,
        agent_name="agent_cashflow_analysis",
    ) == TABLE_CF
    tool_call = agent_runner._deterministic_tool_call_for_missing_requirement(
        state,
        "agent_cashflow_analysis",
        requirement,
    )
    assert tool_call["tool_calls"][0]["name"] == "get_cashflow_info"
    assert tool_call["tool_calls"][0]["args"]["query"] == requirement


def test_cashflow_net_change_year_variant_uses_cashflow_semantics():
    requirement = "lưu chuyển tiền thuần trong năm"
    target = {
        "agent": "agent_cashflow_analysis",
        "evidence_queries": [
            # Reproduce the upstream mislabel from the audited run: semantic
            # cash-flow wording must repair this table instead of trusting it.
            {"table": TABLE_BS, "query": requirement},
        ],
    }
    state = {"dispatch_target": target}

    assert agent_runner._table_for_requirement(
        state,
        requirement,
        agent_name="agent_cashflow_analysis",
    ) == TABLE_CF
    assert agent_runner._deterministic_tool_call_for_missing_requirement(
        state,
        "agent_cashflow_analysis",
        requirement,
    )["tool_calls"][0]["name"] == "get_cashflow_info"


def test_analysis_requirement_scope_does_not_broadcast_other_dispatch_targets():
    profitability_target = {
        "agent": "agent_profitability",
        "requirements": ["doanh thu thuần", "lợi nhuận sau thuế"],
        "evidence_queries": [
            {"table": TABLE_IS, "query": "doanh thu thuần"},
            {"table": TABLE_IS, "query": "lợi nhuận sau thuế"},
            # Supporting context is not an additional active requirement when
            # the target has an explicit requirements contract.
            {"table": TABLE_BS, "query": "tổng cộng tài sản"},
        ],
    }
    cashflow_target = {
        "agent": "agent_cashflow_analysis",
        "requirements": ["lưu chuyển tiền thuần trong kỳ"],
        "evidence_queries": [
            {"table": TABLE_CF, "query": "lưu chuyển tiền thuần trong kỳ"},
        ],
    }
    state = {
        "dispatch_target": profitability_target,
        "evidence_queries": [
            {"table": TABLE_CF, "query": "lưu chuyển tiền thuần trong kỳ"},
            {"table": TABLE_BS, "query": "tổng cộng nguồn vốn"},
        ],
        "worker_plan": {
            "analysis_plan": [profitability_target, cashflow_target],
        },
    }

    assert agent_runner._assigned_requirements_for_agent(
        state,
        "agent_profitability",
    ) == ["doanh thu thuần", "lợi nhuận sau thuế"]
    assert agent_runner._assigned_requirements_for_agent(
        state,
        "agent_cashflow_analysis",
    ) == []
    prompt_plan = agent_runner._analysis_plan_payload_for_prompt(
        state,
        "agent_profitability",
    )
    assert prompt_plan["analysis_plan"][0]["requirements"] == [
        "doanh thu thuần",
        "lợi nhuận sau thuế",
    ]
    assert prompt_plan["analysis_plan"][0]["evidence_queries"] == [
        {"table": TABLE_IS, "query": "doanh thu thuần"},
        {"table": TABLE_IS, "query": "lợi nhuận sau thuế"},
        {"table": TABLE_BS, "query": "tổng cộng tài sản"},
    ]


def test_unscoped_evidence_is_inferred_for_relevant_axes_not_all_agents():
    analysis_targets = [
        {"agent": "agent_profitability"},
        {"agent": "agent_liquidity_solvency"},
        {"agent": "agent_cashflow_analysis"},
        {"agent": "agent_efficiency"},
    ]

    assert keyworder_runner._focused_needed_by(
        TABLE_CF,
        "lưu chuyển tiền thuần trong kỳ",
        analysis_targets,
    ) == ["agent_cashflow_analysis"]
    assert keyworder_runner._focused_needed_by(
        TABLE_IS,
        "doanh thu thuần",
        analysis_targets,
    ) == ["agent_profitability", "agent_efficiency"]
    assert keyworder_runner._focused_needed_by(
        TABLE_BS,
        "tài sản ngắn hạn",
        analysis_targets,
    ) == ["agent_liquidity_solvency"]


def test_hard_plan_builds_axis_scoped_dispatch_queries_without_broadcast():
    analysis_axes = [
        {"axis": "agent_profitability", "objective": "Đánh giá sinh lời"},
        {
            "axis": "agent_liquidity_solvency",
            "objective": "Đánh giá thanh khoản",
        },
        {"axis": "agent_cashflow_analysis", "objective": "Đánh giá dòng tiền"},
        {"axis": "agent_efficiency", "objective": "Đánh giá hiệu quả"},
    ]
    finalized = keyworder_runner._finalize_router_targets(
        {
            "targets": [
                {
                    "table": TABLE_CF,
                    "requirements": ["lưu chuyển tiền thuần trong kỳ"],
                },
                {
                    "table": TABLE_BS,
                    "requirements": ["tài sản ngắn hạn"],
                },
                {
                    "table": TABLE_IS,
                    "requirements": ["doanh thu thuần"],
                },
            ]
        },
        {"difficulty_level": "hard", "analysis_axes": analysis_axes},
        user_query="Đánh giá tình hình tài chính công ty",
    )
    queries_by_agent = {
        item["agent"]: [query["query"] for query in item["evidence_queries"]]
        for item in finalized["analysis_plan"]
    }

    assert any(
        "lưu chuyển tiền thuần từ hoạt động đầu tư" in query
        for query in queries_by_agent["agent_cashflow_analysis"]
    )
    assert any(
        "lưu chuyển tiền thuần từ hoạt động tài chính" in query
        for query in queries_by_agent["agent_cashflow_analysis"]
    )
    assert any(
        "tổng tài sản ngắn hạn" in query
        for query in queries_by_agent["agent_liquidity_solvency"]
    )
    assert any(
        "tổng nợ ngắn hạn" in query
        for query in queries_by_agent["agent_liquidity_solvency"]
    )
    assert any(
        "doanh thu thuần về bán hàng và cung cấp dịch vụ" in query
        for query in queries_by_agent["agent_profitability"]
    )
    assert any(
        "giá vốn hàng bán" in query
        for query in queries_by_agent["agent_efficiency"]
    )
    assert not any(
        "hoạt động đầu tư" in query
        for query in queries_by_agent["agent_efficiency"]
    )
    assert not any(
        "hàng tồn kho" in query
        for query in queries_by_agent["agent_cashflow_analysis"]
    )


def test_roll_forward_requirement_needs_every_leg_in_one_block():
    query = "Tình hình tăng giảm vay ngắn hạn trong năm?"
    base = {
        "table": TABLE_NOTE,
        "block_id": "loan-roll-forward",
        "company": "Công ty A",
        "fiscal_year": "2025",
        "source": "report.md",
        "metric_label": "Vay ngắn hạn",
        "unit": "VND",
        "value_kind": "amount",
        "status": "found",
    }
    facts = [
        {
            **base,
            "fact_id": "opening",
            "item_name": "Vay ngắn hạn | Số đầu năm",
            "row_label": "Số đầu năm",
            "column_label": "2025",
            "period": "đầu",
            "period_role": "previous",
            "value": "80",
        },
        {
            **base,
            "fact_id": "addition",
            "item_name": "Vay ngắn hạn | Vay thêm trong năm",
            "row_label": "Vay thêm trong năm",
            "column_label": "2025",
            "movement_type": "addition",
            "value": "40",
        },
        {
            **base,
            "fact_id": "reduction",
            "item_name": "Vay ngắn hạn | Hoàn trả trong năm",
            "row_label": "Hoàn trả trong năm",
            "column_label": "2025",
            "movement_type": "repayment",
            "value": "20",
        },
        {
            **base,
            "fact_id": "closing",
            "item_name": "Vay ngắn hạn | Số cuối năm",
            "row_label": "Số cuối năm",
            "column_label": "2025",
            "period": "cuối",
            "period_role": "current",
            "value": "100",
        },
    ]

    assert requirement_evidence_state(
        query,
        facts[:-1],
        table=TABLE_NOTE,
    ) == REQUIREMENT_UNMATCHED_TOPK
    assert requirement_evidence_state(
        query,
        facts,
        table=TABLE_NOTE,
    ) == REQUIREMENT_MATCHED


def test_composition_requirement_needs_explicit_closed_component_marker():
    query = "Phân tích cơ cấu của chi phí bán hàng"
    base = {
        "table": TABLE_NOTE,
        "block_id": "selling-expense-components",
        "company": "Công ty A",
        "fiscal_year": "2025",
        "source": "report.md",
        "metric_label": "Chi phí bán hàng",
        "unit": "VND",
        "value_kind": "amount",
        "status": "found",
    }
    facts = [
        {
            **base,
            "fact_id": "total",
            "item_name": "Tổng chi phí bán hàng",
            "row_label": "Tổng chi phí bán hàng",
            "column_label": "2025",
            "aggregation_level": "total",
            "value": "100",
        },
        {
            **base,
            "fact_id": "staff",
            "item_name": "Chi phí nhân viên",
            "row_label": "Chi phí nhân viên",
            "column_label": "2025",
            "aggregation_level": "component",
            "value": "60",
        },
        {
            **base,
            "fact_id": "services",
            "item_name": "Chi phí dịch vụ",
            "row_label": "Chi phí dịch vụ",
            "column_label": "2025",
            "aggregation_level": "component",
            "value": "40",
        },
    ]

    assert requirement_evidence_state(
        query,
        facts,
        table=TABLE_NOTE,
    ) == REQUIREMENT_UNMATCHED_TOPK

    closed = [
        {**fact, "coverage_complete_legs": ["components_closed"]}
        for fact in facts
    ]
    assert requirement_evidence_state(
        query,
        closed,
        table=TABLE_NOTE,
    ) == REQUIREMENT_MATCHED
