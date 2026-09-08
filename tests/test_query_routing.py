"""Contracts for centralized multi-label routing and typed retrieval slots."""

from unittest.mock import patch

import pytest

from agents import keyworder_runner
from graph.evidence import _ensure_report_section_target, _normalized_retrieval_targets
from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
)
from tools.evidence import scoped_tool_name_for_query
from tools.query_routing import (
    fact_reporting_basis,
    fact_matches_required_slots,
    fact_sibling_group_key,
    fact_slot_score,
    parse_query_slots,
    query_reporting_basis,
    route_candidates,
    targeted_retry_query,
)


def test_full_period_query_prefers_cumulative_pair_over_quarter_pair():
    slots = parse_query_slots("doanh thu thuần năm nay và năm trước")
    quarter = {
        "heading": TABLE_IS,
        "fiscal_year": "2024",
        "metric_label": "Doanh thu thuần",
        "item_name": "Doanh thu thuần | Quý IV năm 2024",
        "period_label": "Quý IV năm 2024",
        "period_role": "current",
    }
    cumulative = {
        **quarter,
        "item_name": "Doanh thu thuần | Lũy kế đến quý IV năm 2024",
        "period_label": "Lũy kế đến quý IV năm 2024",
    }

    assert slots.reporting_basis == "full_period"
    assert fact_reporting_basis(quarter) == "quarter"
    assert fact_reporting_basis(cumulative) == "cumulative"
    assert fact_slot_score(slots, cumulative) > fact_slot_score(slots, quarter)
    assert fact_matches_required_slots(slots, cumulative)
    assert not fact_matches_required_slots(slots, quarter)


def test_accumulated_depreciation_is_not_a_cumulative_flow_basis():
    assert query_reporting_basis("hao mòn lũy kế tài sản cố định") == ""
    assert query_reporting_basis("lũy kế đến quý IV năm 2024") == "cumulative"
from tools import tools as retrieval_tools
from vectorstore.qdrant_store import _ensure_payload_indexes


def _tables(query: str) -> list[str]:
    return [candidate.table for candidate in route_candidates(query)]


def test_exclusive_front_matter_signal_is_primary():
    candidates = route_candidates(
        "Số báo cáo kiểm toán và đơn vị kiểm toán của báo cáo là gì?"
    )
    assert candidates[0].table == TABLE_REPORT_SECTION
    assert candidates[0].confidence >= 0.90
    assert scoped_tool_name_for_query(
        "Số báo cáo kiểm toán là gì?"
    ) == "get_report_section_info"


def test_accounting_policy_routes_to_note_before_front_matter():
    candidates = route_candidates(
        "Chính sách kế toán và điều kiện ghi nhận thuế hoãn lại là gì?"
    )
    assert candidates[0].table == TABLE_NOTE
    assert candidates[0].confidence >= 0.90


def test_bare_governance_titles_keep_front_route_but_financial_context_prefers_note():
    for title in (
        "Tổng Giám đốc",
        "Giám đốc điều hành",
        "Giám đốc tài chính",
        "Phó Tổng Giám đốc",
        "Chủ tịch",
    ):
        bare_tables = _tables(f"{title} của công ty là ai?")
        assert TABLE_REPORT_SECTION in bare_tables
        assert bare_tables[0] == TABLE_REPORT_SECTION

        financial_tables = _tables(f"Thù lao của {title} trong năm là bao nhiêu?")
        assert financial_tables[0] == TABLE_NOTE
        assert TABLE_REPORT_SECTION in financial_tables


def test_headquarters_scope_distinguishes_company_front_from_subsidiary_note():
    assert _tables("Trụ sở công ty ở đâu?")[0] == TABLE_REPORT_SECTION
    assert _tables("Trụ sở của công ty con ở đâu?")[0] == TABLE_NOTE
    assert _tables("Trụ sở của chi nhánh ở đâu?")[0] == TABLE_NOTE


def test_entity_boundary_keeps_viet_nam_inside_legal_company_name():
    slots = parse_query_slots(
        "Trụ sở đăng ký của Công ty Cổ phần Sữa Việt Nam ở đâu?"
    )

    assert slots.entity == "cong ty co phan sua viet nam"
    assert fact_matches_required_slots(
        slots,
        {
            "company": "Công ty Cổ phần Sữa Việt Nam",
            "metric_label": "Trụ sở đăng ký",
            "item_name": "Địa chỉ trụ sở đăng ký",
        },
    )


def test_asset_detail_vocabulary_routes_to_notes():
    for query in (
        "Thời gian khấu hao tài sản cố định là bao lâu?",
        "Nguyên giá nhãn hiệu cuối năm là bao nhiêu?",
        "Chi tiết quyền sử dụng đất cuối kỳ là bao nhiêu?",
    ):
        assert _tables(query)[0] == TABLE_NOTE


def test_policy_duration_and_asset_first_phrasings_bind_canonical_slots():
    duration_cases = (
        (
            "Thời gian khấu hao nhãn hiệu là bao lâu?",
            "nhan hieu",
        ),
        (
            "Phần mềm máy vi tính được khấu hao trong bao lâu?",
            "phan mem may vi tinh",
        ),
    )
    for query, entity in duration_cases:
        slots = parse_query_slots(query)
        assert slots.metric == "thời gian khấu hao"
        assert slots.entity == entity
        assert slots.policy_topic == "depreciation_period"
        assert slots.scope_label == "chính sách kế toán"
        assert slots.value_type == ()
        assert _tables(query)[0] == TABLE_NOTE


def test_broad_policy_scope_accepts_a_scope_specific_policy_atom():
    slots = parse_query_slots(
        "Thời gian khấu hao nhãn hiệu là bao lâu?"
    )
    policy_atom = {
        "metric_label": "Thời gian hữu dụng",
        "entity_label": "Nhãn hiệu",
        "scope_label": "c) Nhãn hiệu",
        "policy_topic": "depreciation_period",
        "aggregation_level": "component",
        "raw_value": "5 - 10 năm",
    }

    assert fact_matches_required_slots(slots, policy_atom)
    assert not fact_matches_required_slots(
        slots,
        {**policy_atom, "scope_label": ""},
    )


def test_asset_first_policy_method_preserves_land_use_term_modifier():
    indefinite = parse_query_slots(
        "Quyền sử dụng đất lâu dài được khấu hao như thế nào?"
    )
    finite = parse_query_slots(
        "Quyền sử dụng đất có thời hạn được khấu hao như thế nào?"
    )

    for slots in (indefinite, finite):
        assert slots.metric == "phương pháp khấu hao"
        assert slots.policy_topic == "depreciation_method"
        assert slots.scope_label == "chính sách kế toán"
        assert slots.value_type == ()
    assert indefinite.entity == "quyen su dung dat lau dai"
    assert finite.entity == "quyen su dung dat co thoi han"

    indefinite_fact = {
        "metric_label": "Quyền sử dụng đất lâu dài không khấu hao",
        "entity_label": "Quyền sử dụng đất lâu dài",
        "scope_label": "Chính sách kế toán",
        "policy_topic": "non_depreciation",
        "aggregation_level": "component",
    }
    finite_fact = {
        **indefinite_fact,
        "metric_label": "Quyền sử dụng đất có thời hạn được khấu hao",
        "entity_label": "Quyền sử dụng đất có thời hạn",
        "policy_topic": "depreciation_method",
    }
    assert fact_matches_required_slots(indefinite, indefinite_fact)
    assert not fact_matches_required_slots(indefinite, finite_fact)

    non_depreciation_query = parse_query_slots(
        "Quyền sử dụng đất lâu dài có được khấu hao không?"
    )
    assert fact_matches_required_slots(
        non_depreciation_query,
        indefinite_fact,
    )
    assert not fact_matches_required_slots(
        non_depreciation_query,
        {**indefinite_fact, "policy_topic": "depreciation_method"},
    )


def test_policy_asset_entity_drops_process_suffix():
    slots = parse_query_slots(
        "Quyền sử dụng đất lâu dài được xử lý khấu hao như thế nào?"
    )

    assert slots.entity == "quyen su dung dat lau dai"


def test_ambiguous_corporate_structure_retains_note_and_report():
    candidates = route_candidates(
        "Công ty có những công ty con, chi nhánh và nhà máy nào?"
    )
    assert [candidate.table for candidate in candidates] == [
        TABLE_NOTE,
        TABLE_REPORT_SECTION,
    ]
    assert all(candidate.confidence < 0.80 for candidate in candidates)

    guarded = _ensure_report_section_target(
        [],
        "Công ty có những công ty con, chi nhánh và nhà máy nào?",
    )
    assert [target["table"] for target in guarded] == [
        TABLE_NOTE,
        TABLE_REPORT_SECTION,
    ]


def test_financial_metric_can_outrank_ambiguous_organization_markers():
    tables = _tables("Giá trị đầu tư vào công ty con cuối kỳ là bao nhiêu?")
    assert tables[0] == TABLE_BS
    assert TABLE_NOTE in tables
    assert TABLE_REPORT_SECTION in tables


def test_cashflow_semantics_beat_generic_balance_sheet_money_metric():
    candidates = route_candidates(
        "Tiền thu hồi cho vay trên báo cáo lưu chuyển tiền tệ là bao nhiêu?"
    )
    assert candidates[0].table == TABLE_CF


def test_graph_guard_retains_compound_high_confidence_routes():
    query = (
        "Số báo cáo kiểm toán là gì và lưu chuyển tiền thuần "
        "từ hoạt động kinh doanh là bao nhiêu?"
    )
    guarded = _ensure_report_section_target([], query)
    tables = [target["table"] for target in guarded]
    assert tables[:2] == [TABLE_CF, TABLE_REPORT_SECTION] or tables[:2] == [
        TABLE_REPORT_SECTION,
        TABLE_CF,
    ]


def test_parse_query_slots_for_two_period_delta():
    query = (
        "Chênh lệch dự phòng phải thu ngắn hạn khó đòi của CTCP ABC "
        "giữa cuối kỳ và đầu năm là bao nhiêu?"
    )
    slots = parse_query_slots(query)
    assert slots.metric == "dự phòng phải thu ngắn hạn khó đòi"
    assert slots.entity == "ctcp abc"
    assert slots.period == "both"
    assert slots.value_type == ("dự phòng",)
    assert slots.aggregation == "component"
    assert slots.operation == "delta"


def test_flow_period_roles_stay_distinct_from_stock_opening_closing():
    query = "So sánh lãi cho vay năm hiện tại và năm trước"
    slots = parse_query_slots(query)

    assert slots.period == ""
    assert slots.period_role == "both"
    assert targeted_retry_query(query).endswith("năm nay năm trước")

    flow_current = {
        "metric_label": "Lãi tiền gửi, lãi cho vay",
        "item_name": "Lãi tiền gửi, lãi cho vay | Năm nay",
        "period_label": "Năm nay",
        "transaction_type": "lending",
    }
    stock_closing = {
        **flow_current,
        "item_name": "Dự thu lãi cho vay | Số cuối kỳ",
        "period": "cuối",
        "period_label": "Số cuối kỳ",
        "period_role": "current",
    }

    assert fact_matches_required_slots(slots, flow_current)
    assert not fact_matches_required_slots(slots, stock_closing)


def test_redundant_current_phrase_does_not_turn_closing_stock_into_flow_role():
    slots = parse_query_slots(
        "Số dư lợi nhuận giữ lại cuối năm hiện tại là bao nhiêu?"
    )
    closing = {
        "heading": TABLE_BS,
        "metric_label": "Lợi nhuận sau thuế chưa phân phối",
        "item_name": "Lợi nhuận sau thuế chưa phân phối | Số cuối năm",
        "period": "cuối",
        "period_label": "Số cuối năm",
    }
    wrong_component = {
        **closing,
        "metric_label": "LNST chưa phân phối kỳ này",
        "item_name": "- LNST chưa phân phối kỳ này | Số cuối năm",
    }

    assert slots.period == "cuối"
    assert slots.period_role == ""
    assert fact_matches_required_slots(slots, closing)
    assert not fact_matches_required_slots(slots, wrong_component)


@pytest.mark.parametrize(
    ("query", "entity", "policy_topic"),
    [
        (
            "Thời gian khấu hao ước tính của máy móc và thiết bị "
            "là bao nhiêu năm?",
            "may moc va thiet bi",
            "depreciation_period",
        ),
        (
            "Phương pháp khấu hao nhãn hiệu và thời gian khấu hao",
            "nhan hieu",
            "depreciation_period",
        ),
        (
            "Bất động sản đầu tư - quyền sử dụng đất lâu dài được xử lý "
            "khấu hao như thế nào?",
            "quyen su dung dat lau dai",
            "depreciation_method",
        ),
    ],
)
def test_policy_entity_ignores_qualifiers_compound_tail_and_scope_prefix(
    query,
    entity,
    policy_topic,
):
    slots = parse_query_slots(query)

    assert slots.entity == entity
    assert slots.policy_topic == policy_topic


def test_value_type_comparison_at_one_period_does_not_request_period_sibling():
    slots = parse_query_slots(
        "So sánh nguyên giá và hao mòn tài sản cố định hữu hình cuối kỳ"
    )
    assert slots.operation == "compare"
    assert slots.period == "cuối"
    assert slots.value_type == ("nguyên giá", "hao mòn")
    cost = {
        "heading": TABLE_NOTE,
        "note_ref": "V.6",
        "subheading": "Tài sản cố định hữu hình — Nguyên giá",
        "item_name": "Nguyên giá | Máy móc và thiết bị | Cuối kỳ",
        "value_type": "nguyên giá",
    }
    depreciation = {
        **cost,
        "subheading": "Tài sản cố định hữu hình — Giá trị hao mòn lũy kế",
        "item_name": "Giá trị hao mòn lũy kế | Máy móc và thiết bị | Cuối kỳ",
        "value_type": "hao mòn",
    }
    assert fact_sibling_group_key(
        cost, ignore_value_type=True
    ) == fact_sibling_group_key(depreciation, ignore_value_type=True)


def test_statement_sibling_group_normalizes_period_with_attached_vnd_unit():
    base = {
        "heading": TABLE_IS,
        "company": "Công ty Cổ phần Sữa Việt Nam",
        "fiscal_year": "2025",
        "block_id": "income-statement",
        "metric_label": "Lợi nhuận sau thuế TNDN (60 = 50 - 51 - 52)",
        "item_code": "60",
        "unit": "VND",
    }
    current = {
        **base,
        "item_name": (
            "Lợi nhuận sau thuế TNDN (60 = 50 - 51 - 52) | 2025VND"
        ),
        "period_role": "current",
    }
    previous = {
        **base,
        "item_name": (
            "Lợi nhuận sau thuế TNDN (60 = 50 - 51 - 52) | 2024VND"
        ),
        "period_role": "previous",
    }

    assert fact_sibling_group_key(current) == fact_sibling_group_key(previous)


def test_parse_query_slots_uses_calculation_contract_operations():
    assert parse_query_slots(
        "Doanh thu năm nay gấp bao nhiêu lần năm trước?"
    ).operation == "multiple"
    assert parse_query_slots(
        "Tỷ lệ thay đổi doanh thu năm nay so với năm trước?"
    ).operation == "percent_change"
    assert parse_query_slots(
        "Tỷ trọng hàng tồn kho trên tổng tài sản?"
    ).operation == "share"
    assert parse_query_slots(
        "Hệ số nợ phải trả trên vốn chủ sở hữu?"
    ).operation == "ratio"


@pytest.mark.parametrize(
    ("query", "operation"),
    [
        (
            "Chênh lệch doanh thu năm 2024 và năm 2023 là bao nhiêu?",
            "delta",
        ),
        (
            "So sánh lãi cho vay năm hiện tại và năm trước",
            "compare",
        ),
        (
            "Tỷ lệ thay đổi doanh thu năm nay so với năm trước?",
            "percent_change",
        ),
        (
            "Doanh thu năm nay gấp bao nhiêu lần năm trước?",
            "multiple",
        ),
    ],
)
def test_targeted_retry_preserves_calculation_operation(query, operation):
    original = parse_query_slots(query)
    retried = parse_query_slots(targeted_retry_query(query))

    assert original.operation == operation
    assert retried.operation == operation
    assert retried.period == original.period
    assert retried.period_role == original.period_role
    assert retried.period_labels == original.period_labels


@pytest.mark.parametrize(
    "query",
    [
        "Tỷ trọng hàng tồn kho trên tổng tài sản?",
        "Hệ số nợ phải trả trên vốn chủ sở hữu?",
        "Tỷ lệ hao mòn / nguyên giá của tài sản cố định hữu hình?",
    ],
)
def test_targeted_retry_preserves_directional_ratio_operands(query):
    original = parse_query_slots(query)
    retried = parse_query_slots(targeted_retry_query(query))

    assert retried.operation == original.operation
    assert [
        (
            operand.role,
            operand.metric,
            operand.entity,
            operand.value_type,
            operand.aggregation,
        )
        for operand in retried.operands
    ] == [
        (
            operand.role,
            operand.metric,
            operand.entity,
            operand.value_type,
            operand.aggregation,
        )
        for operand in original.operands
    ]


def test_ratio_parser_emits_directional_typed_operands_only_for_explicit_syntax():
    cases = [
        (
            "Tỷ trọng hàng tồn kho trên tổng tài sản?",
            "share",
            ("hàng tồn kho", "tổng cộng tài sản"),
            ("", ""),
            ("", "total"),
        ),
        (
            "Hệ số nợ phải trả trên vốn chủ sở hữu?",
            "ratio",
            ("nợ phải trả", "vốn chủ sở hữu"),
            ("", ""),
            ("total", "total"),
        ),
        (
            "Tỷ lệ hao mòn / nguyên giá của tài sản cố định hữu hình?",
            "ratio",
            ("tài sản cố định hữu hình", "tài sản cố định hữu hình"),
            ("hao mòn", "nguyên giá"),
            ("total", "total"),
        ),
    ]
    for query, operation, metrics, value_types, aggregations in cases:
        slots = parse_query_slots(query)
        assert slots.operation == operation
        assert [operand.role for operand in slots.operands] == [
            "numerator",
            "denominator",
        ]
        assert tuple(operand.metric for operand in slots.operands) == metrics
        assert tuple(operand.value_type for operand in slots.operands) == value_types
        assert tuple(operand.aggregation for operand in slots.operands) == aggregations

    # "so với" is comparison syntax too; a period name must never be promoted
    # to a denominator merely because the question also says "tỷ lệ".
    ambiguous = parse_query_slots(
        "Tỷ lệ doanh thu thuần năm hiện tại so với năm trước là bao nhiêu?"
    )
    assert ambiguous.operation == "compare"
    assert ambiguous.operands == ()


def test_ratio_operands_keep_independent_semantic_dimensions_per_leg():
    query = (
        "Tỷ lệ mua hàng hóa và dịch vụ của Công ty Cổ phần Alpha trên "
        "doanh thu bán hàng và cung cấp dịch vụ của Công ty Cổ phần Beta "
        "trong giao dịch với bên liên quan?"
    )

    slots = parse_query_slots(query)

    assert len(slots.operands) == 2
    numerator, denominator = slots.operands
    assert (
        numerator.scope_label,
        numerator.counterparty,
        numerator.transaction_type,
    ) == (
        "bên liên quan",
        "cong ty co phan alpha",
        "purchase",
    )
    assert (
        denominator.scope_label,
        denominator.counterparty,
        denominator.transaction_type,
    ) == (
        "bên liên quan",
        "cong ty co phan beta",
        "sale",
    )

    contract = keyworder_runner._typed_calculation_metadata(query, TABLE_NOTE)
    assert [
        (
            operand["role"],
            operand["scope_label"],
            operand["counterparty"],
            operand["transaction_type"],
        )
        for operand in contract["operands"]
    ] == [
        ("numerator", "bên liên quan", "cong ty co phan alpha", "purchase"),
        ("denominator", "bên liên quan", "cong ty co phan beta", "sale"),
    ]


def test_ratio_legs_with_same_metric_survive_when_movement_dimensions_differ():
    slots = parse_query_slots(
        "Tỷ lệ thanh lý tài sản cố định hữu hình trên "
        "mua sắm tài sản cố định hữu hình?"
    )

    assert [operand.metric for operand in slots.operands] == [
        "tài sản cố định hữu hình",
        "tài sản cố định hữu hình",
    ]
    assert [operand.movement_type for operand in slots.operands] == [
        "disposal",
        "addition",
    ]


def test_keyworder_ratio_plan_survives_compaction_with_both_operand_queries():
    query = "Tỷ trọng hàng tồn kho trên tổng tài sản?"
    sanitized = keyworder_runner._sanitize_router_plan_payload(
        {"evidence_plan": [{"table": TABLE_BS, "query": query}]}
    )
    worker_plan = keyworder_runner._finalize_router_targets(
        sanitized,
        {"difficulty_level": "medium"},
        user_query=query,
    )

    assert len(worker_plan["evidence_plan"]) == 1
    item = worker_plan["evidence_plan"][0]
    assert item["table"] == TABLE_BS
    assert item["queries"] == ["hàng tồn kho", "tổng tài sản"]
    assert {
        query_text: metadata["operand_role"]
        for query_text, metadata in item["query_metadata"].items()
    } == {
        "hàng tồn kho": "numerator",
        "tổng tài sản": "denominator",
    }
    assert all(
        metadata["operation"] == "share"
        and len(metadata["operands"]) == 2
        for metadata in item["query_metadata"].values()
    )

    retrieval_target = _normalized_retrieval_targets(worker_plan)[0]
    assert retrieval_target["requirements"] == ["hàng tồn kho", "tổng tài sản"]
    assert retrieval_target["metadata_by_query"]["hàng tồn kho"]["operand_role"] == "numerator"
    assert retrieval_target["metadata_by_query"]["tổng tài sản"]["operand_role"] == "denominator"


def test_keyworder_multi_value_type_ratio_adds_bounded_note_schedule_routes():
    query = "Tỷ lệ hao mòn / nguyên giá của tài sản cố định hữu hình?"
    worker_plan = keyworder_runner._finalize_router_targets(
        keyworder_runner._sanitize_router_plan_payload(
            {"evidence_plan": [{"table": TABLE_BS, "query": query}]}
        ),
        {"difficulty_level": "medium"},
        user_query=query,
    )

    assert [item["table"] for item in worker_plan["evidence_plan"]] == [
        TABLE_NOTE,
        TABLE_BS,
    ]
    for item in worker_plan["evidence_plan"]:
        assert item["queries"] == [
            "tổng hao mòn tài sản cố định hữu hình",
            "tổng nguyên giá của tài sản cố định hữu hình",
        ]
        assert {
            metadata["value_type"]
            for metadata in item["query_metadata"].values()
        } == {"hao mòn", "nguyên giá"}
        assert all(
            operand["table"] == TABLE_NOTE
            for metadata in item["query_metadata"].values()
            for operand in metadata["operands"]
        )


def test_generic_organization_list_does_not_become_strict_entity_slot():
    for query in (
        "Công ty có những công ty con nào?",
        "Công ty có các nhà máy nào?",
        "Doanh nghiệp có những chi nhánh nào?",
    ):
        slots = parse_query_slots(query)
        assert slots.operation == "list"
        assert slots.aggregation == "list"
        assert slots.entity == ""


def test_exact_slot_candidate_beats_period_value_type_and_entity_distractors():
    query = (
        "Dự phòng phải thu ngắn hạn khó đòi của CTCP ABC cuối kỳ "
        "là bao nhiêu?"
    )
    slots = parse_query_slots(query)
    exact = {
        "item_name": "Dự phòng phải thu ngắn hạn khó đòi | CTCP ABC",
        "period": "cuối",
        "value_type": "dự phòng",
    }
    wrong_period = {**exact, "period": "đầu"}
    wrong_entity = {
        **exact,
        "item_name": "Dự phòng phải thu ngắn hạn khó đòi | CTCP XYZ",
    }
    wrong_type = {**exact, "value_type": "nguyên giá"}

    exact_score = fact_slot_score(slots, exact, "10.000 VND")
    assert exact_score > fact_slot_score(slots, wrong_period, "9.000 VND")
    assert exact_score > fact_slot_score(slots, wrong_entity, "10.000 VND")
    assert exact_score > fact_slot_score(slots, wrong_type, "10.000 VND")
    assert fact_matches_required_slots(slots, exact, "10.000 VND")
    assert not fact_matches_required_slots(slots, wrong_period, "9.000 VND")


def test_canonical_section_key_beats_noisy_balance_sheet_header():
    slots = parse_query_slots(
        "Tổng tài sản ngắn hạn cuối kỳ là bao nhiêu?"
    )
    exact = {
        "section_key": "ts_ngan_han",
        "metric_label": "Tài sản dài hạn",
        "column_label": "Tài sản dài hạn",
        "item_name": "A - TÀI SẢN NGẮN HẠN | 31/12/2025",
        "period": "cuối",
        # A canonical section key is itself proof that this is an aggregate,
        # even when reading legacy metadata produced before the level was
        # populated explicitly.
        "aggregation_level": "component",
    }
    wrong_section = {
        **exact,
        "section_key": "ts_dai_han",
        "item_name": "B - TÀI SẢN DÀI HẠN | 31/12/2025",
    }

    assert slots.metric == "tổng tài sản ngắn hạn"
    assert fact_matches_required_slots(slots, exact)
    assert not fact_matches_required_slots(slots, wrong_section)
    assert fact_slot_score(slots, exact) > fact_slot_score(slots, wrong_section)
    assert retrieval_tools._structured_slot_filters(slots, TABLE_BS) == [
        {
            "heading": TABLE_BS,
            "period": "cuối",
            "section_key": "ts_ngan_han",
        }
    ]
    assert retrieval_tools._structured_slot_filters(slots, TABLE_NOTE) == [
        {
            "heading": TABLE_NOTE,
            "period": "cuối",
            "aggregation_level": "total",
        }
    ]


def test_section_key_separates_otherwise_identical_sibling_groups():
    base = {
        "company": "Công ty A",
        "heading": TABLE_BS,
        "block_id": "balance-sheet",
        "item_code": "270",
        "item_name": "Tổng | 31/12/2025",
        "period_role": "current",
        "aggregation_level": "total",
    }

    assert fact_sibling_group_key(
        {**base, "section_key": "tong_tai_san"}
    ) != fact_sibling_group_key(
        {**base, "section_key": "ts_dai_han"}
    )


@pytest.mark.parametrize(
    ("query", "expected_metric"),
    [
        (
            "Tổng tài sản cố định hữu hình cuối kỳ là bao nhiêu?",
            "tài sản cố định hữu hình",
        ),
        (
            "Tổng tài sản thuế thu nhập doanh nghiệp hoãn lại là bao nhiêu?",
            "tài sản thuế thu nhập doanh nghiệp hoãn lại",
        ),
        (
            "Tổng tài sản ngắn hạn khác cuối kỳ là bao nhiêu?",
            "tài sản ngắn hạn khác",
        ),
        (
            "Tổng nợ phải trả người bán cuối kỳ là bao nhiêu?",
            "phải trả người bán",
        ),
    ],
)
def test_line_item_totals_do_not_collapse_to_statement_sections(
    query,
    expected_metric,
):
    slots = parse_query_slots(query)

    assert slots.metric == expected_metric
    assert slots.section_key == ""
    filters = retrieval_tools._structured_slot_filters(slots, TABLE_BS)
    assert filters
    assert all("section_key" not in item for item in filters)


def test_directional_slot_rejects_borrowing_lending_lookalike():
    slots = parse_query_slots("Vay ngắn hạn cuối kỳ là bao nhiêu?")
    borrowing = {
        "metric_label": "Vay ngắn hạn",
        "item_name": "Vay ngắn hạn | Cuối kỳ",
        "period": "cuối",
    }
    lending = {
        "metric_label": "Phải thu về cho vay ngắn hạn",
        "item_name": "Phải thu về cho vay ngắn hạn | Cuối kỳ",
        "period": "cuối",
    }

    assert fact_matches_required_slots(slots, borrowing)
    assert not fact_matches_required_slots(slots, lending)
    assert fact_slot_score(slots, borrowing) > fact_slot_score(slots, lending)


def test_explicit_semantic_dimensions_reject_nearby_typed_distractors():
    purchase_slots = parse_query_slots(
        "Mua hàng hóa từ bên liên quan Công ty Cổ phần APIS là bao nhiêu?"
    )
    assert purchase_slots.transaction_type == "purchase"
    purchase = {
        "metric_label": "Mua hàng hóa",
        "entity_label": "Công ty Cổ phần APIS",
        "scope_label": "Bên liên quan",
        "counterparty": "Công ty Cổ phần APIS",
        "transaction_type": "purchase",
    }
    sale = {**purchase, "metric_label": "Bán hàng hóa", "transaction_type": "sale"}
    assert fact_matches_required_slots(purchase_slots, purchase)
    assert not fact_matches_required_slots(purchase_slots, sale)

    geography_slots = parse_query_slots("Doanh thu tại khu vực nước ngoài?")
    assert geography_slots.geography == "nuoc ngoai"
    foreign = {
        "metric_label": "Doanh thu",
        "entity_label": "Nước ngoài",
        "geography": "Nước ngoài",
    }
    domestic = {**foreign, "entity_label": "Trong nước", "geography": "Trong nước"}
    assert fact_matches_required_slots(geography_slots, foreign)
    assert not fact_matches_required_slots(geography_slots, domestic)

    movement_slots = parse_query_slots("Khoản hoàn nhập trong năm?")
    assert movement_slots.movement_type == "reversal"
    reversal = {
        "metric_label": "Hoàn nhập trong năm",
        "movement_type": "reversal",
    }
    addition = {
        "metric_label": "Trích lập dự phòng trong năm",
        "movement_type": "provision_charge",
    }
    assert fact_matches_required_slots(movement_slots, reversal)
    assert not fact_matches_required_slots(movement_slots, addition)

    policy_slots = parse_query_slots("Phương pháp khấu hao là gì?")
    assert policy_slots.policy_topic == "depreciation_method"
    method = {
        "metric_label": "Phương pháp khấu hao",
        "scope_label": "Chính sách kế toán",
        "policy_topic": "depreciation_method",
    }
    duration = {
        "metric_label": "Thời gian khấu hao",
        "policy_topic": "depreciation_period",
    }
    assert fact_matches_required_slots(policy_slots, method)
    assert not fact_matches_required_slots(policy_slots, duration)


def test_metric_matching_does_not_assemble_finished_goods_from_rendered_prose():
    slots = parse_query_slots("doanh thu bán thành phẩm 2025")
    target = {
        "metric_label": "Doanh thu bán thành phẩm",
        "row_label": "- Bán thành phẩm",
        "period_label": "2025 VND",
        "transaction_type": "sale",
    }
    unrelated_sale = {
        "metric_label": "Doanh thu với bên liên quan",
        "row_label": "Công ty Cổ phần Alpha",
        "period_label": "2025 VND",
        "transaction_type": "sale",
    }
    rendered_noise = (
        "Công ty Phạm Thành. Phạm vi bảng: giao dịch bán sản phẩm. "
        "Doanh thu với bên liên quan năm 2025."
    )

    assert fact_matches_required_slots(slots, target)
    assert not fact_matches_required_slots(
        slots,
        unrelated_sale,
        rendered_noise,
    )


def test_coverage_contracts_survive_keyworder_compaction():
    query = "Tình hình tăng giảm vay ngắn hạn trong năm?"
    slots = parse_query_slots(query)
    assert slots.coverage_template == "roll_forward"
    assert slots.required_legs == (
        "opening",
        "additions",
        "reductions",
        "closing",
    )

    normalized = keyworder_runner._normalize_evidence_plan_payloads(
        [{"table": TABLE_NOTE, "query": query}]
    )
    item = next(candidate for candidate in normalized if candidate["table"] == TABLE_NOTE)
    assert item["coverage_template"] == "roll_forward"
    assert item["required_legs"] == [
        "opening",
        "additions",
        "reductions",
        "closing",
    ]


def test_rerank_pairs_only_same_logical_row_without_raising_limit():
    query = "So sánh các khoản phải thu ngắn hạn cuối kỳ và đầu năm"
    docs = ["a-current", "b-current", "a-previous"]
    metas = [
        {
            "heading": TABLE_BS,
            "subheading": "Các khoản phải thu ngắn hạn",
            "item_name": "Các khoản phải thu ngắn hạn | CTCP A | Cuối kỳ",
            "period": "cuối",
        },
        {
            "heading": TABLE_BS,
            "subheading": "Các khoản phải thu ngắn hạn",
            "item_name": "Các khoản phải thu ngắn hạn | CTCP B | Cuối kỳ",
            "period": "cuối",
        },
        {
            "heading": TABLE_BS,
            "subheading": "Các khoản phải thu ngắn hạn",
            "item_name": "Các khoản phải thu ngắn hạn | CTCP A | Đầu năm",
            "period": "đầu",
        },
    ]
    scores = {"a-current": 100.0, "b-current": 90.0, "a-previous": 80.0}

    with patch.object(
        retrieval_tools,
        "_item_match_score",
        side_effect=lambda _q, _m, doc, intent=None: scores[doc],
    ):
        chosen_docs, _ = retrieval_tools._rerank_matches(
            query,
            docs,
            metas,
            limit=2,
            intent=query,
        )

    assert chosen_docs == ["a-current", "a-previous"]
    assert len(chosen_docs) == 2


def test_keyworder_preserves_and_infers_typed_calculation_contract():
    query = (
        "Chênh lệch dự phòng phải thu ngắn hạn khó đòi cuối kỳ "
        "so với đầu năm là bao nhiêu?"
    )
    normalized = keyworder_runner._normalize_evidence_plan_payloads(
        [{"table": TABLE_BS, "query": query}]
    )
    primary = next(item for item in normalized if item["table"] == TABLE_BS)
    assert primary["operation"] == "delta"
    assert [operand["role"] for operand in primary["operands"]] == [
        "current",
        "previous",
    ]
    assert [operand["period"] for operand in primary["operands"]] == [
        "cuối",
        "đầu",
    ]

    compacted = keyworder_runner._compact_evidence_plan_by_table_needby(
        keyworder_runner._merge_evidence_plans(normalized)
    )
    compact_primary = next(item for item in compacted if item["table"] == TABLE_BS)
    assert compact_primary["operation"] == "delta"
    assert len(compact_primary["operands"]) == 2


def test_keyworder_multiple_uses_period_roles_and_does_not_guess_ratio_operands():
    multiple_query = (
        "Doanh thu thuần năm hiện tại gấp bao nhiêu lần năm trước?"
    )
    multiple = keyworder_runner._typed_calculation_metadata(
        multiple_query, TABLE_IS
    )
    assert multiple["operation"] == "multiple"
    assert [operand["role"] for operand in multiple["operands"]] == [
        "current",
        "previous",
    ]

    ratio_query = (
        "Tỷ lệ doanh thu thuần năm hiện tại so với năm trước là bao nhiêu?"
    )
    assert (
        keyworder_runner._typed_calculation_metadata(ratio_query, TABLE_IS) == {}
    )


def test_qdrant_indexes_fields_used_by_structured_slot_filters():
    class FakeClient:
        def __init__(self):
            self.fields = []

        def create_payload_index(self, *, collection_name, field_name, field_schema):
            assert collection_name == "facts"
            self.fields.append(field_name)

    client = FakeClient()
    _ensure_payload_indexes(client, "facts")
    assert {
        "heading",
        "period",
        "period_role",
        "value_type",
        "aggregation_level",
        "counterparty",
        "transaction_type",
        "movement_type",
        "geography",
        "policy_topic",
        "section_key",
    }.issubset(client.fields)


def test_structured_slot_filters_use_flow_period_role():
    current = parse_query_slots(
        "Doanh thu bán bất động sản năm hiện tại là bao nhiêu?"
    )
    both = parse_query_slots(
        "So sánh doanh thu bán bất động sản năm hiện tại và năm trước"
    )

    assert retrieval_tools._structured_slot_filters(current, TABLE_NOTE) == [
        {
            "heading": TABLE_NOTE,
            "period_role": "current",
            "transaction_type": "sale",
        }
    ]
    assert retrieval_tools._structured_slot_filters(both, TABLE_NOTE) == [
        {
            "heading": TABLE_NOTE,
            "period_role": "current",
            "transaction_type": "sale",
        },
        {
            "heading": TABLE_NOTE,
            "period_role": "previous",
            "transaction_type": "sale",
        },
    ]


def test_structured_policy_filter_includes_non_depreciation_terminal_answer():
    slots = parse_query_slots(
        "Quyền sử dụng đất lâu dài được khấu hao như thế nào?"
    )

    assert retrieval_tools._structured_slot_filters(slots, TABLE_NOTE) == [
        {
            "heading": TABLE_NOTE,
            "aggregation_level": "component",
            "policy_topic": "depreciation_method",
        },
        {
            "heading": TABLE_NOTE,
            "aggregation_level": "component",
            "policy_topic": "non_depreciation",
        },
    ]


def test_explicit_date_delta_keeps_specific_total_and_targeted_period_legs():
    query = (
        "Tổng tài sản của Công ty thay đổi như thế nào từ 1/1/2025 "
        "đến 31/12/2025 và mức thay đổi là bao nhiêu?"
    )

    slots = parse_query_slots(query)
    assert slots.operation == "delta"
    assert slots.metric == "tổng cộng tài sản"
    assert slots.period_labels == ("01/01/2025", "31/12/2025")

    contract = keyworder_runner._typed_calculation_metadata(query, TABLE_BS)
    assert contract["operation"] == "delta"
    assert [
        (
            operand["role"],
            operand["query"],
            operand["period_role"],
            operand["aggregation_level"],
        )
        for operand in contract["operands"]
    ] == [
        ("current", "tổng cộng tài sản 31/12/2025", "current", "total"),
        ("previous", "tổng cộng tài sản 01/01/2025", "previous", "total"),
    ]

    # Even when an LLM has reduced the evidence request to one lookup, the
    # deterministic contract compiled from the original query must survive.
    plan = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [{"table": TABLE_BS, "query": "tổng cộng tài sản"}]},
        {"difficulty_level": "medium"},
        user_query=query,
    )
    item = next(entry for entry in plan["evidence_plan"] if entry["table"] == TABLE_BS)
    assert item["operation"] == "delta"
    assert {
        "tổng cộng tài sản 31/12/2025",
        "tổng cộng tài sản 01/01/2025",
    }.issubset(set(item["queries"]))
    assert {
        metadata["operand_role"]
        for metadata in item["query_metadata"].values()
        if metadata.get("operand_role")
    } == {"current", "previous"}


def test_qualitative_change_does_not_invent_a_delta_contract():
    slots = parse_query_slots("Tổng tài sản thay đổi như thế nào?")
    assert slots.operation == "lookup"
    assert keyworder_runner._typed_calculation_metadata(
        "Tổng tài sản thay đổi như thế nào?",
        TABLE_BS,
    ) == {}


@pytest.mark.parametrize(
    "query",
    [
        "Tính hệ số nợ trên vốn chủ sở hữu (D/E) của Công ty tại 31/12/2025.",
        "Nợ trên vốn chủ sở hữu tại 31/12/2025 là bao nhiêu?",
    ],
)
def test_debt_equity_formula_binds_ordered_total_statement_operands(query):
    slots = parse_query_slots(query)

    assert slots.operation == "ratio"
    assert [
        (operand.role, operand.metric, operand.aggregation)
        for operand in slots.operands
    ] == [
        ("numerator", "nợ phải trả", "total"),
        ("denominator", "vốn chủ sở hữu", "total"),
    ]
    assert all("31/12/2025" in operand.query for operand in slots.operands)

    contract = keyworder_runner._typed_calculation_metadata(query, TABLE_BS)
    assert [
        (operand["role"], operand["metric"], operand["table"])
        for operand in contract["operands"]
    ] == [
        ("numerator", "nợ phải trả", TABLE_BS),
        ("denominator", "vốn chủ sở hữu", TABLE_BS),
    ]


def test_bare_debt_alias_is_bounded_to_explicit_debt_equity_formula():
    slots = parse_query_slots("Nợ vay ngắn hạn tại 31/12/2025 là bao nhiêu?")
    assert slots.operation == "lookup"
    assert slots.operands == ()
    assert slots.metric != "nợ phải trả"


def test_ratio_over_two_explicit_periods_does_not_collapse_to_one_operand_pair():
    slots = parse_query_slots(
        "Hệ số nợ trên vốn chủ sở hữu năm 2025 và năm 2024 là bao nhiêu?"
    )
    assert slots.operation == "ratio"
    assert slots.period_labels == ("2025", "2024")
    assert slots.operands == ()


def test_share_contract_is_restored_after_router_drops_original_expression():
    query = (
        "Tỷ trọng doanh thu bán thành phẩm trong tổng doanh thu "
        "năm 2025 là bao nhiêu?"
    )
    plan = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [{"table": TABLE_IS, "query": "doanh thu"}]},
        {"difficulty_level": "medium"},
        user_query=query,
    )

    note_item = next(
        item for item in plan["evidence_plan"] if item["table"] == TABLE_NOTE
    )
    assert note_item["operation"] == "share"
    assert note_item["queries"] == [
        "doanh thu bán thành phẩm 2025",
        "tổng doanh thu năm 2025",
    ]
    assert note_item["canonical_queries"]["tổng doanh thu năm 2025"] in {
        "tổng doanh thu",
        "tong doanh thu",
    }
    assert (
        note_item["canonical_queries"]["tổng doanh thu năm 2025"]
        != "các khoản giảm trừ doanh thu"
    )
    assert [
        operand["role"]
        for operand in note_item["operands"]
    ] == ["numerator", "denominator"]
    # A low-confidence fallback is a retrieval route, not proof that both
    # semantic operands belong to the income statement.
    assert all(
        operand.get("table", "") in {"", TABLE_NOTE}
        for operand in note_item["operands"]
    )


def test_geographic_share_binds_both_legs_to_the_note_schedule():
    query = (
        "Tỷ trọng doanh thu thuần từ thị trường nước ngoài trong "
        "tổng doanh thu thuần năm 2025 là bao nhiêu?"
    )
    contract = keyworder_runner._typed_calculation_metadata(query, TABLE_IS)
    numerator, denominator = contract["operands"]

    assert numerator["role"] == "numerator"
    assert numerator["geography"] == "nuoc ngoai"
    assert numerator["aggregation_level"] == "component"
    assert numerator["period_label"] == "2025"
    assert numerator["table"] == TABLE_NOTE
    assert denominator["role"] == "denominator"
    assert denominator["geography"] == ""
    assert denominator["aggregation_level"] == "total"
    assert denominator["period_label"] == "2025"
    assert denominator["table"] == TABLE_NOTE

    plan = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [{"table": TABLE_IS, "query": "doanh thu thuần"}]},
        {"difficulty_level": "medium"},
        user_query=query,
    )
    by_table = {item["table"]: item for item in plan["evidence_plan"]}
    assert {TABLE_IS, TABLE_NOTE}.issubset(by_table)
    assert (
        "doanh thu thuần từ thị trường nước ngoài 2025"
        in by_table[TABLE_NOTE]["queries"]
    )
    assert (
        "tổng doanh thu thuần năm 2025"
        in by_table[TABLE_NOTE]["queries"]
    )
    assert all(
        metadata["operation"] == "share"
        for item in (by_table[TABLE_IS], by_table[TABLE_NOTE])
        for metadata in item.get("query_metadata", {}).values()
        if metadata.get("operand_role")
    )


def test_total_geographic_lookup_does_not_invent_share_operands():
    query = "Tổng doanh thu thuần tại thị trường nước ngoài năm 2025 là bao nhiêu?"
    slots = parse_query_slots(query)
    assert slots.operation == "lookup"
    assert slots.operands == ()
