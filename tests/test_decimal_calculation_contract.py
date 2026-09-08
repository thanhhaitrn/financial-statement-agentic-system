"""Typed calculation context must stay exact while Synth owns the answer."""

import json
from decimal import Decimal

from agents import keyworder_runner
from agents import synth_runner
from schemas.numbers import format_decimal_vi, parse_financial_decimal
from schemas.table_names import TABLE_BS, TABLE_IS, TABLE_NOTE


def test_parse_financial_decimal_supports_statement_formats():
    assert parse_financial_decimal("14.592.618.979 VND") == Decimal("14592618979")
    assert parse_financial_decimal("(1.234.567)") == Decimal("-1234567")
    assert parse_financial_decimal("1.234,56") == Decimal("1234.56")
    assert parse_financial_decimal("12,5%") == Decimal("12.5")
    assert format_decimal_vi(Decimal("1234567.5000")) == "1.234.567,5"
    assert format_decimal_vi(Decimal("1712869000000")) == "1.712.869.000.000"


def test_contrastive_accounting_semantics_reject_near_lexical_matches():
    cases = [
        (
            "vay ngắn hạn",
            {"item_name": "Phải thu về cho vay ngắn hạn"},
        ),
        (
            "vay",
            {"item_name": "Phải thu về cho vay"},
        ),
        (
            "tiền thu từ đi vay",
            {"item_name": "Số dư vay cuối kỳ"},
        ),
        (
            "lợi nhuận trước thuế",
            {"item_name": "Lợi nhuận sau thuế thu nhập doanh nghiệp"},
        ),
        (
            "cam kết thuê hoạt động",
            {"item_name": "Nợ thuê tài chính đã ghi nhận"},
        ),
        (
            "nguyên giá tài sản cố định",
            {"item_name": "Giá trị còn lại của tài sản cố định"},
        ),
        (
            "chi phí thuế thu nhập doanh nghiệp",
            {"item_name": "Tài sản thuế thu nhập hoãn lại cuối kỳ"},
        ),
        (
            "mua hàng hóa và dịch vụ với bên liên quan",
            {"item_name": "Doanh thu với các bên liên quan"},
        ),
        (
            "thay đổi tổng vốn chủ sở hữu",
            {"item_name": "Phân loại lại trong vốn chủ sở hữu"},
        ),
    ]

    for requested_slot, distractor in cases:
        assert not synth_runner._semantic_slot_compatible(requested_slot, distractor)


def test_contrastive_accounting_semantics_accept_exact_class():
    assert synth_runner._semantic_slot_compatible(
        "lợi nhuận trước thuế",
        {"item_name": "Lợi nhuận kế toán trước thuế năm 2025"},
    )
    assert synth_runner._semantic_slot_compatible(
        "cam kết thuê hoạt động",
        {"item_name": "Cam kết thanh toán tiền thuê hoạt động trong tương lai"},
    )


def test_derived_calculation_requires_complete_operand_provenance():
    state = {
        "user_query": "Giá trị kỳ hiện tại bằng bao nhiêu lần kỳ trước?",
        "planner_plan": {"difficulty_level": "medium"},
    }
    complete = [
        {
            "fact_id": "current",
            "source": "report.md#current",
            "item_name": "Cam kết thuê",
            "period_role": "current",
            "value": "2",
            "unit": "VND",
        },
        {
            "fact_id": "previous",
            "source": "report.md#previous",
            "item_name": "Cam kết thuê",
            "period_role": "previous",
            "value": "4",
            "unit": "VND",
        },
    ]

    for missing_field in ("fact_id", "source"):
        facts = [dict(item) for item in complete]
        facts[0].pop(missing_field)
        assert synth_runner._typed_decimal_calculation(
            state,
            {"note": {"facts": facts}},
        ) is None


def test_amount_calculation_requires_complete_equal_units():
    state = {
        "user_query": "Giá trị kỳ hiện tại bằng bao nhiêu lần kỳ trước?",
        "planner_plan": {"difficulty_level": "medium"},
    }
    base = [
        {
            "fact_id": "current",
            "source": "report.md#current",
            "item_name": "Cam kết thuê",
            "period_role": "current",
            "value": "2",
            "unit": "VND",
            "value_kind": "amount",
        },
        {
            "fact_id": "previous",
            "source": "report.md#previous",
            "item_name": "Cam kết thuê",
            "period_role": "previous",
            "value": "4",
            "unit": "VND",
            "value_kind": "amount",
        },
    ]

    missing_unit = [dict(item) for item in base]
    missing_unit[1]["unit"] = ""
    assert synth_runner._typed_decimal_calculation(
        state,
        {"note": {"facts": missing_unit}},
    ) is None

    mismatched_scale = [dict(item) for item in base]
    mismatched_scale[1]["unit"] = "Triệu VND"
    assert synth_runner._typed_decimal_calculation(
        state,
        {"note": {"facts": mismatched_scale}},
    ) is None


def test_ratio_rejects_cross_report_fiscal_year_operands():
    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": "Tỷ lệ lợi nhuận sau thuế trên tổng tài sản?",
            "planner_plan": {"difficulty_level": "medium"},
        },
        {
            "evidence": {
                "facts": [
                    {
                        "fact_id": "profit-2024",
                        "source": "report-2024.md#profit",
                        "company": "Công ty A",
                        "fiscal_year": "2024",
                        "item_name": "Lợi nhuận sau thuế",
                        "value": "100",
                        "unit": "VND",
                    },
                    {
                        "fact_id": "assets-2025",
                        "source": "report-2025.md#assets",
                        "company": "Công ty A",
                        "fiscal_year": "2025",
                        "item_name": "Tổng tài sản",
                        "value": "1000",
                        "unit": "VND",
                    },
                ]
            }
        },
    )

    assert calculation is None


def test_medium_two_period_change_passes_typed_decimal_context_to_model(monkeypatch):
    payloads = []

    def fake_invoke(payload):
        payloads.append(payload)
        return {
            "status": "answer",
            "answer": "Câu trả lời do model tạo.",
            "followups": [],
        }, None, "structured"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)
    retrieval_entry = {
        "kind": "retrieval_requirement",
        "requirement": "lãi cho vay",
        "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
        "parsed_query_slots": {"metric": "lãi cho vay"},
        "route_candidates": [
            {
                "rank": 1,
                "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                "confidence": 0.98,
                "reason": "explicit_note_semantics",
            }
        ],
        "requirement_state": {
            "before_retry": "matched",
            "after_retry": "matched",
        },
        "targeted_retry": {"performed": False, "query": ""},
        "selected_facts": [
            {"fact_id": "interest-current", "rank": 1},
            {"fact_id": "interest-previous", "rank": 2},
        ],
    }
    state = {
        "user_query": "So sánh lãi cho vay năm hiện tại và năm trước. Sự thay đổi là bao nhiêu?",
        "planner_plan": {"difficulty_level": "medium"},
        "worker_plan": {
            "difficulty_level": "medium",
            "analysis_plan": [],
            "evidence_plan": [
                {
                    "operation": "delta",
                    "operands": [
                        {
                            "role": "current",
                            "query": "lãi cho vay",
                            "period": "current",
                        },
                        {
                            "role": "previous",
                            "query": "lãi cho vay",
                            "period": "previous",
                        },
                    ],
                }
            ],
        },
        "worker_results": {
            "THUYẾT MINH BÁO CÁO TÀI CHÍNH": {
                "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                "facts": [
                        {
                            "fact_id": "interest-current",
                            "item_name": "Lãi tiền gửi ngân hàng, Lãi cho vay",
                            "time_hint": "năm hiện tại",
                            "value": "14.592.618.979",
                            "unit": "VND",
                            "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                            "source": "report.md#interest-current",
                        },
                        {
                            "fact_id": "interest-previous",
                            "item_name": "Lãi tiền gửi ngân hàng, Lãi cho vay",
                            "time_hint": "năm trước",
                            "value": "26.030.112.902",
                            "unit": "VND",
                            "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                            "source": "report.md#interest-previous",
                    },
                ],
            }
        },
        "evidence_ledger": {
            "schema_version": 1,
            "entries": [retrieval_entry],
        },
        "trace": [],
    }

    updates = synth_runner.run_synth(state)

    assert updates["synth_decision"]["answer"] == "Câu trả lời do model tạo."
    assert len(payloads) == 1
    context = json.loads(payloads[0]["worker_results_json"])
    calculation = context["typed_decimal_calculation"]
    assert calculation["difference"] == "-11437493923"
    assert [item["role"] for item in calculation["operands"]] == [
        "current",
        "previous",
    ]
    assert not any("deterministic" in item["event"] for item in updates["trace"])
    ledger = updates["evidence_ledger"]
    assert ledger["schema_version"] == 1
    assert ledger["entries"][0] == retrieval_entry
    assert ledger["entries"][1]["kind"] == "derived_calculation"
    assert ledger["entries"][1]["result"] == "-11437493923"
    assert [item["role"] for item in ledger["entries"][1]["operands"]] == [
        "current",
        "previous",
    ]


def test_ambiguous_equal_score_groups_do_not_choose_a_metric():
    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": "Sự thay đổi là bao nhiêu?",
            "planner_plan": {"difficulty_level": "medium"},
        },
        {
            "a": {
                "facts": [
                    {"item_name": "A", "time_hint": "năm nay", "value": "2"},
                    {"item_name": "A", "time_hint": "năm trước", "value": "1"},
                ]
            },
            "b": {
                "facts": [
                    {"item_name": "B", "time_hint": "năm nay", "value": "4"},
                    {"item_name": "B", "time_hint": "năm trước", "value": "2"},
                ]
            },
        },
    )

    assert calculation is None


def test_multiple_preserves_current_over_previous_operand_order():
    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": "Giá trị kỳ hiện tại bằng bao nhiêu lần kỳ trước?",
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": {
                "evidence_plan": [
                    {
                        "operation": "multiple",
                        "operands": [
                            {
                                "role": "current",
                                "query": "cam kết thuê",
                                "period": "current",
                            },
                            {
                                "role": "previous",
                                "query": "cam kết thuê",
                                "period": "previous",
                            },
                        ],
                    }
                ]
            },
        },
        {
            "note": {
                "facts": [
                    {
                        "fact_id": "current",
                        "item_name": "Cam kết thuê",
                            "period_role": "current",
                            "value": "2",
                            "unit": "VND",
                            "source": "report.md#current",
                    },
                    {
                        "fact_id": "previous",
                        "item_name": "Cam kết thuê",
                            "period_role": "previous",
                            "value": "4",
                            "unit": "VND",
                            "source": "report.md#previous",
                    },
                ]
            }
        },
    )

    assert calculation is not None
    assert calculation["operation"] == "multiple"
    assert Decimal(calculation["result"]) == Decimal("0.5")
    assert [operand["role"] for operand in calculation["operands"]] == [
        "current",
        "previous",
    ]


def test_ratio_binds_value_types_and_carries_fact_provenance():
    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": (
                "Tỷ lệ hao mòn lũy kế trên nguyên giá tài sản cố định hữu hình "
                "tại 31/12/2025 là bao nhiêu?"
            ),
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": {
                "evidence_plan": [
                    {
                        "operation": "ratio",
                        "operands": [
                            {
                                "role": "numerator",
                                "query": "hao mòn lũy kế tài sản cố định hữu hình",
                                "value_type": "hao mòn",
                                "aggregation": "total",
                                "period": "current",
                            },
                            {
                                "role": "denominator",
                                "query": "nguyên giá tài sản cố định hữu hình",
                                "value_type": "nguyên giá",
                                "aggregation": "total",
                                "period": "current",
                            },
                        ],
                    }
                ]
            },
        },
        {
            "note": {
                "facts": [
                    {
                        "fact_id": "gross",
                        "note_ref": "V.6",
                        "subheading": "Tài sản cố định hữu hình",
                        "item_name": "Số dư cuối năm | Tổng",
                        "period_role": "current",
                        "value_type": "nguyên giá",
                        "aggregation_level": "total",
                        "value": "20.218.660.492.456",
                        "unit": "VND",
                        "source": "report.md#page=31",
                    },
                    {
                        "fact_id": "depreciation",
                        "note_ref": "V.6",
                        "subheading": "Tài sản cố định hữu hình",
                        "item_name": "Số dư cuối năm | Tổng",
                        "period_role": "current",
                        "value_type": "hao mòn lũy kế",
                        "aggregation_level": "total",
                        "value": "14.809.390.958.929",
                        "unit": "VND",
                        "source": "report.md#page=31",
                    },
                    {
                        "fact_id": "net-distractor",
                        "note_ref": "V.6",
                        "subheading": "Tài sản cố định hữu hình",
                        "item_name": "Số dư cuối năm | Tổng",
                        "period_role": "current",
                        "value_type": "giá trị còn lại",
                        "aggregation_level": "total",
                        "value": "5.409.269.533.527",
                        "unit": "VND",
                    },
                ]
            }
        },
    )

    assert calculation is not None
    assert calculation["operation"] == "ratio"
    assert [operand["fact_id"] for operand in calculation["operands"]] == [
        "depreciation",
        "gross",
    ]
    assert Decimal(calculation["result"]).quantize(Decimal("0.01")) == Decimal("73.25")


def test_debt_equity_binding_uses_canonical_statement_sections():
    query = "Hệ số D/E (nợ phải trả trên vốn chủ sở hữu) là bao nhiêu?"
    worker_plan = keyworder_runner._finalize_router_targets(
        {"evidence_plan": [{"table": TABLE_BS, "query": query}]},
        {"difficulty_level": "medium"},
        user_query=query,
    )
    common = {
        "table": TABLE_BS,
        "company": "Công ty A",
        "fiscal_year": "2025",
        "aggregation_level": "total",
        "unit": "VND",
        "source": "report.md#balance-sheet",
    }
    facts = [
        {
            **common,
            "fact_id": "code-300",
            "item_name": "C - NỢ PHẢI TRẢ | 31/12/2025",
            "metric_label": "Nợ ngắn hạn",
            "section_key": "no_phai_tra",
            "value": "400",
        },
        {
            **common,
            "fact_id": "wrong-liability-section",
            "item_name": "C - NỢ PHẢI TRẢ | 31/12/2025",
            "metric_label": "Nợ phải trả",
            "section_key": "no_ngan_han",
            "value": "100",
        },
        {
            **common,
            "fact_id": "code-400",
            "item_name": "D - VỐN CHỦ SỞ HỮU | 31/12/2025",
            "metric_label": "Nguồn vốn",
            "section_key": "von_chu",
            "value": "200",
        },
        {
            **common,
            "fact_id": "wrong-equity-section",
            "item_name": "D - VỐN CHỦ SỞ HỮU | 31/12/2025",
            "metric_label": "Vốn chủ sở hữu",
            "section_key": "nguon_von",
            "value": "800",
        },
    ]

    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": query,
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": worker_plan,
        },
        {"balance-sheet": {"facts": facts}},
    )

    assert calculation is not None
    assert [operand["fact_id"] for operand in calculation["operands"]] == [
        "code-300",
        "code-400",
    ]
    assert [operand["section_key"] for operand in calculation["operands"]] == [
        "no_phai_tra",
        "von_chu",
    ]
    assert Decimal(calculation["result"]) == Decimal("2")


def test_keyworder_ratio_contract_binds_both_legs_with_distractors_and_provenance():
    cases = [
        {
            "query": "Tỷ trọng hàng tồn kho trên tổng tài sản?",
            "initial_table": TABLE_BS,
            "facts": [
                {
                    "fact_id": "inventory",
                    "table": TABLE_BS,
                    "item_name": "Hàng tồn kho",
                    "aggregation_level": "component",
                    "value": "250",
                    "unit": "VND",
                    "source": "report.md#inventory",
                },
                {
                    "fact_id": "total-assets",
                    "table": TABLE_BS,
                    "item_name": "Tổng cộng tài sản",
                    "aggregation_level": "total",
                    "value": "1000",
                    "unit": "VND",
                    "source": "report.md#assets",
                },
                {
                    "fact_id": "current-assets-distractor",
                    "table": TABLE_BS,
                    "item_name": "Tổng tài sản ngắn hạn",
                    "aggregation_level": "total",
                    "value": "600",
                    "unit": "VND",
                },
            ],
            "expected_ids": ["inventory", "total-assets"],
            "expected_result": Decimal("25"),
        },
        {
            "query": "Hệ số nợ phải trả trên vốn chủ sở hữu?",
            "initial_table": TABLE_BS,
            "facts": [
                {
                    "fact_id": "liabilities",
                    "table": TABLE_BS,
                    "item_name": "Nợ phải trả",
                    "aggregation_level": "total",
                    "value": "400",
                    "unit": "VND",
                    "source": "report.md#liabilities",
                },
                {
                    "fact_id": "equity",
                    "table": TABLE_BS,
                    "item_name": "Vốn chủ sở hữu",
                    "aggregation_level": "total",
                    "value": "200",
                    "unit": "VND",
                    "source": "report.md#equity",
                },
                {
                    "fact_id": "short-debt-distractor",
                    "table": TABLE_BS,
                    "item_name": "Nợ ngắn hạn",
                    "aggregation_level": "total",
                    "value": "100",
                    "unit": "VND",
                },
            ],
            "expected_ids": ["liabilities", "equity"],
            "expected_result": Decimal("2"),
        },
        {
            "query": (
                "Tỷ lệ lợi nhuận sau thuế thu nhập doanh nghiệp "
                "trên tổng tài sản?"
            ),
            "initial_table": TABLE_IS,
            "facts": [
                {
                    "fact_id": "net-profit",
                    "table": TABLE_IS,
                    "item_name": "Lợi nhuận sau thuế thu nhập doanh nghiệp",
                    "aggregation_level": "component",
                    "value": "100",
                    "unit": "VND",
                    "source": "report.md#profit",
                },
                {
                    "fact_id": "total-assets-cross-table",
                    "table": TABLE_BS,
                    "item_name": "Tổng cộng tài sản",
                    "aggregation_level": "total",
                    "value": "1000",
                    "unit": "VND",
                    "source": "report.md#assets",
                },
                {
                    "fact_id": "pre-tax-profit-distractor",
                    "table": TABLE_IS,
                    "item_name": "Tổng lợi nhuận kế toán trước thuế",
                    "aggregation_level": "component",
                    "value": "120",
                    "unit": "VND",
                },
            ],
            "expected_ids": ["net-profit", "total-assets-cross-table"],
            "expected_result": Decimal("10"),
        },
        {
            "query": "Tỷ lệ hao mòn / nguyên giá của tài sản cố định hữu hình?",
            "initial_table": TABLE_BS,
            "facts": [
                {
                    "fact_id": "depreciation",
                    "table": TABLE_NOTE,
                    "subheading": "Tài sản cố định hữu hình",
                    "item_name": "Tổng | Hao mòn lũy kế",
                    "value_type": "hao mòn",
                    "aggregation_level": "total",
                    "value": "75",
                    "unit": "VND",
                    "source": "report.md#depreciation",
                },
                {
                    "fact_id": "gross-cost",
                    "table": TABLE_NOTE,
                    "subheading": "Tài sản cố định hữu hình",
                    "item_name": "Tổng | Nguyên giá",
                    "value_type": "nguyên giá",
                    "aggregation_level": "total",
                    "value": "100",
                    "unit": "VND",
                    "source": "report.md#gross",
                },
                {
                    "fact_id": "net-book-distractor",
                    "table": TABLE_NOTE,
                    "subheading": "Tài sản cố định hữu hình",
                    "item_name": "Tổng | Giá trị còn lại",
                    "value_type": "giá trị còn lại",
                    "aggregation_level": "total",
                    "value": "25",
                    "unit": "VND",
                },
                {
                    "fact_id": "machinery-depreciation-distractor",
                    "table": TABLE_NOTE,
                    "subheading": "Tài sản cố định hữu hình",
                    "item_name": "Máy móc thiết bị | Hao mòn lũy kế",
                    "value_type": "hao mòn",
                    "aggregation_level": "component",
                    "value": "50",
                    "unit": "VND",
                },
                {
                    "fact_id": "machinery-cost-distractor",
                    "table": TABLE_NOTE,
                    "subheading": "Tài sản cố định hữu hình",
                    "item_name": "Máy móc thiết bị | Nguyên giá",
                    "value_type": "nguyên giá",
                    "aggregation_level": "component",
                    "value": "80",
                    "unit": "VND",
                },
            ],
            "expected_ids": ["depreciation", "gross-cost"],
            "expected_result": Decimal("75"),
        },
    ]

    for case in cases:
        query = case["query"]
        worker_plan = keyworder_runner._finalize_router_targets(
            keyworder_runner._sanitize_router_plan_payload(
                {
                    "evidence_plan": [
                        {
                            "table": case["initial_table"],
                            "query": query,
                        }
                    ]
                }
            ),
            {"difficulty_level": "medium"},
            user_query=query,
        )
        calculation = synth_runner._typed_decimal_calculation(
            {
                "user_query": query,
                "planner_plan": {"difficulty_level": "medium"},
                "worker_plan": worker_plan,
            },
            {"evidence": {"facts": case["facts"]}},
        )

        assert calculation is not None
        assert [
            operand["fact_id"] for operand in calculation["operands"]
        ] == case["expected_ids"]
        assert Decimal(calculation["result"]) == case["expected_result"]
        assert all(
            operand["source"]
            for operand in calculation["operands"]
        )


def _typed_ratio_state(query: str, table: str = TABLE_NOTE) -> dict:
    contract = keyworder_runner._typed_calculation_metadata(query, table)
    assert len(contract.get("operands", [])) == 2
    return {
        "user_query": query,
        "planner_plan": {"difficulty_level": "medium"},
        "worker_plan": {
            "evidence_plan": [
                {
                    "table": table,
                    "query": query,
                    **contract,
                }
            ]
        },
    }


def test_typed_ratio_binding_rejects_transaction_and_counterparty_distractors():
    query = (
        "Tỷ lệ mua hàng hóa và dịch vụ của Công ty Cổ phần Alpha trên "
        "doanh thu bán hàng và cung cấp dịch vụ của Công ty Cổ phần Beta "
        "trong giao dịch với bên liên quan?"
    )
    common = {
        "company": "Công ty báo cáo",
        "fiscal_year": "2025",
        "table": TABLE_NOTE,
        "scope_label": "Các giao dịch chủ yếu với bên liên quan",
        "unit": "VND",
        "source": "report.md#related-parties",
        "aggregation_level": "component",
    }
    facts = [
        {
            **common,
            "fact_id": "purchase-alpha",
            "item_name": "Mua hàng hóa và dịch vụ | Công ty Cổ phần Alpha",
            "metric_label": "Mua hàng hóa và dịch vụ",
            "entity_label": "Công ty Cổ phần Alpha",
            "counterparty": "Công ty Cổ phần Alpha",
            "transaction_type": "purchase",
            "value": "40",
        },
        {
            **common,
            "fact_id": "wrong-sale-alpha",
            # Lexically identical on purpose: the typed transaction dimension
            # must reject this candidate instead of letting rank choose it.
            "item_name": "Mua hàng hóa và dịch vụ | Công ty Cổ phần Alpha",
            "metric_label": "Mua hàng hóa và dịch vụ",
            "entity_label": "Công ty Cổ phần Alpha",
            "counterparty": "Công ty Cổ phần Alpha",
            "transaction_type": "sale",
            "value": "400",
        },
        {
            **common,
            "fact_id": "wrong-purchase-counterparty",
            "item_name": "Mua hàng hóa và dịch vụ | Công ty Cổ phần Alpha",
            "metric_label": "Mua hàng hóa và dịch vụ",
            "entity_label": "Công ty Cổ phần Gamma",
            "counterparty": "Công ty Cổ phần Gamma",
            "transaction_type": "purchase",
            "value": "800",
        },
        {
            **common,
            "fact_id": "sale-beta",
            "item_name": (
                "Doanh thu bán hàng và cung cấp dịch vụ | "
                "Công ty Cổ phần Beta"
            ),
            "metric_label": "Doanh thu bán hàng và cung cấp dịch vụ",
            "entity_label": "Công ty Cổ phần Beta",
            "counterparty": "Công ty Cổ phần Beta",
            "transaction_type": "sale",
            "value": "200",
        },
    ]

    calculation = synth_runner._typed_decimal_calculation(
        _typed_ratio_state(query),
        {"note": {"facts": facts}},
    )

    assert calculation is not None
    assert [item["fact_id"] for item in calculation["operands"]] == [
        "purchase-alpha",
        "sale-beta",
    ]
    assert Decimal(calculation["result"]) == Decimal("20")
    ledger_operands = synth_runner._calculation_evidence_ledger(calculation)[
        "entries"
    ][0]["operands"]
    assert ledger_operands[0]["scope_label"] == (
        "Các giao dịch chủ yếu với bên liên quan"
    )
    assert ledger_operands[0]["counterparty"] == "Công ty Cổ phần Alpha"
    assert ledger_operands[0]["transaction_type"] == "purchase"
    assert ledger_operands[0]["entity_label"] == "Công ty Cổ phần Alpha"


def test_typed_ratio_binding_rejects_geography_distractor():
    query = (
        "Tỷ trọng doanh thu bán hàng và cung cấp dịch vụ tại khu vực "
        "nước ngoài trên tổng doanh thu bán hàng và cung cấp dịch vụ?"
    )
    common = {
        "company": "Công ty báo cáo",
        "fiscal_year": "2025",
        "table": TABLE_NOTE,
        "note_ref": "V.2",
        "block_id": "geographic-segment",
        "metric_label": "Doanh thu bán hàng và cung cấp dịch vụ",
        "transaction_type": "sale",
        "unit": "VND",
        "source": "report.md#segments",
    }
    facts = [
        {
            **common,
            "fact_id": "foreign-revenue",
            "item_name": "Doanh thu | Nước ngoài",
            "entity_label": "Nước ngoài",
            "geography": "Nước ngoài",
            "aggregation_level": "component",
            "value": "30",
        },
        {
            **common,
            "fact_id": "wrong-geography",
            "item_name": "Doanh thu | Nước ngoài",
            "entity_label": "Nước ngoài",
            "geography": "Trong nước",
            "aggregation_level": "component",
            "value": "90",
        },
        {
            **common,
            "fact_id": "total-revenue",
            "item_name": "Tổng doanh thu",
            "entity_label": "",
            "geography": "",
            "aggregation_level": "total",
            "value": "100",
        },
    ]

    calculation = synth_runner._typed_decimal_calculation(
        _typed_ratio_state(query),
        {"note": {"facts": facts}},
    )

    assert calculation is not None
    assert [item["fact_id"] for item in calculation["operands"]] == [
        "foreign-revenue",
        "total-revenue",
    ]
    assert Decimal(calculation["result"]) == Decimal("30")
    assert calculation["operands"][0]["geography"] == "Nước ngoài"


def test_finished_goods_revenue_share_binds_typed_schedule_with_distractors():
    query = (
        "Tỷ trọng doanh thu bán thành phẩm trong tổng doanh thu "
        "năm 2025 là bao nhiêu?"
    )
    common = {
        "company": "Công ty báo cáo",
        "fiscal_year": "2025",
        "table": TABLE_NOTE,
        "note_ref": "V.1",
        "unit": "VND",
        "source": "report.md#revenue",
    }
    facts = [
        {
            **common,
            "fact_id": "finished-goods-2025",
            "block_id": "gross-revenue-schedule",
            "item_name": "- Bán thành phẩm | 2025 VND",
            "row_label": "- Bán thành phẩm",
            "metric_label": "Doanh thu bán thành phẩm",
            "transaction_type": "sale",
            "period_label": "2025 VND",
            "aggregation_level": "component",
            "value": "51.881.461.322.802",
        },
        {
            **common,
            "fact_id": "finished-goods-2024",
            "block_id": "gross-revenue-schedule",
            "item_name": "- Bán thành phẩm | 2024 VND",
            "row_label": "- Bán thành phẩm",
            "metric_label": "Doanh thu bán thành phẩm",
            "transaction_type": "sale",
            "period_label": "2024 VND",
            "aggregation_level": "component",
            "value": "49.000.000.000.000",
        },
        {
            **common,
            "fact_id": "related-party-sale",
            "block_id": "related-party-schedule",
            "item_name": "Doanh thu với Công ty Phạm Thành | 2025 VND",
            "row_label": "Công ty Phạm Thành",
            "metric_label": "Doanh thu với bên liên quan",
            "transaction_type": "sale",
            "period_label": "2025 VND",
            "aggregation_level": "component",
            "value": "999.000.000.000",
        },
        {
            **common,
            "fact_id": "gross-revenue-total",
            "block_id": "gross-revenue-schedule",
            "item_name": "Tổng doanh thu | 2025 VND",
            "row_label": "Tổng doanh thu",
            "metric_label": "Tổng doanh thu",
            "period_label": "2025 VND",
            "aggregation_level": "total",
            "value": "53.044.988.379.207",
        },
        {
            **common,
            "fact_id": "finance-income-total",
            "block_id": "finance-income-schedule",
            "item_name": "Tổng doanh thu hoạt động tài chính | 2025 VND",
            "row_label": "Tổng doanh thu hoạt động tài chính",
            "metric_label": "Tổng doanh thu hoạt động tài chính",
            "period_label": "2025 VND",
            "aggregation_level": "total",
            "value": "3.000.000.000.000",
        },
    ]

    calculation = synth_runner._typed_decimal_calculation(
        _typed_ratio_state(query),
        {"note": {"facts": facts}},
    )

    assert calculation is not None
    assert [item["fact_id"] for item in calculation["operands"]] == [
        "finished-goods-2025",
        "gross-revenue-total",
    ]
    assert Decimal(calculation["result"]) == (
        Decimal("51881461322802")
        / Decimal("53044988379207")
        * Decimal(100)
    )
    assert all(item["source"] for item in calculation["operands"])


def test_geographic_net_revenue_share_uses_same_block_net_total():
    query = (
        "Tỷ trọng doanh thu thuần từ thị trường nước ngoài trong "
        "tổng doanh thu thuần năm 2025 là bao nhiêu?"
    )
    common = {
        "company": "Công ty báo cáo",
        "fiscal_year": "2025",
        "table": TABLE_NOTE,
        "unit": "VND",
        "source": "report.md#revenue",
    }
    facts = [
        {
            **common,
            "fact_id": "foreign-net-revenue-2025",
            "note_ref": "V.2",
            "block_id": "geographic-net-revenue",
            "item_name": "Doanh thu thuần | Nước ngoài 2025 VND",
            "metric_label": "Doanh thu thuần",
            "entity_label": "Nước ngoài",
            "geography": "Nước ngoài",
            "period_label": "Nước ngoài 2025 VND",
            "aggregation_level": "component",
            "value": "7.105.418.295.238",
        },
        {
            **common,
            "fact_id": "domestic-net-revenue-2025",
            "note_ref": "V.2",
            "block_id": "geographic-net-revenue",
            "item_name": "Doanh thu thuần | Trong nước 2025 VND",
            "metric_label": "Doanh thu thuần",
            "entity_label": "Trong nước",
            "geography": "Trong nước",
            "period_label": "Trong nước 2025 VND",
            "aggregation_level": "component",
            "value": "45.886.078.013.025",
        },
        {
            **common,
            "fact_id": "foreign-net-revenue-2024",
            "note_ref": "V.2",
            "block_id": "geographic-net-revenue",
            "item_name": "Doanh thu thuần | Nước ngoài 2024 VND",
            "metric_label": "Doanh thu thuần",
            "entity_label": "Nước ngoài",
            "geography": "Nước ngoài",
            "period_label": "Nước ngoài 2024 VND",
            "aggregation_level": "component",
            "value": "6.000.000.000.000",
        },
        {
            **common,
            "fact_id": "net-revenue-total",
            "note_ref": "V.2",
            "block_id": "geographic-net-revenue",
            "item_name": "Doanh thu thuần | Tổng 2025 VND",
            "metric_label": "Doanh thu thuần",
            "period_label": "Tổng 2025 VND",
            "aggregation_level": "total",
            "value": "52.991.496.309.263",
        },
        {
            **common,
            "fact_id": "gross-revenue-total",
            "note_ref": "V.1",
            "block_id": "gross-revenue-schedule",
            "item_name": "Tổng doanh thu | 2025 VND",
            "metric_label": "Tổng doanh thu",
            "period_label": "2025 VND",
            "aggregation_level": "total",
            "value": "53.044.988.379.207",
        },
        {
            **common,
            "fact_id": "related-party-revenue-total",
            "note_ref": "V.1",
            "block_id": "related-party-schedule",
            "item_name": "Tổng doanh thu với bên liên quan | 2025 VND",
            "metric_label": (
                "Doanh thu bán hàng và cung cấp dịch vụ — "
                "Trong đó, doanh thu với khách hàng là các bên liên quan"
            ),
            "period_label": "2025 VND",
            "aggregation_level": "total",
            "value": "51.004.360",
        },
    ]

    calculation = synth_runner._typed_decimal_calculation(
        _typed_ratio_state(query),
        {"note": {"facts": facts}},
    )

    assert calculation is not None
    assert [item["fact_id"] for item in calculation["operands"]] == [
        "foreign-net-revenue-2025",
        "net-revenue-total",
    ]
    assert {
        item["block_id"] for item in calculation["operands"]
    } == {"geographic-net-revenue"}
    assert Decimal(calculation["result"]) == (
        Decimal("7105418295238")
        / Decimal("52991496309263")
        * Decimal(100)
    )
    assert all(item["source"] for item in calculation["operands"])


def test_typed_ratio_binding_rejects_movement_distractor():
    query = (
        "Tỷ lệ thanh lý tài sản cố định hữu hình trên "
        "mua sắm tài sản cố định hữu hình?"
    )
    common = {
        "company": "Công ty báo cáo",
        "fiscal_year": "2025",
        "table": TABLE_BS,
        "metric_label": "Tài sản cố định hữu hình",
        "unit": "VND",
        "source": "report.md#fixed-assets",
    }
    facts = [
        {
            **common,
            "fact_id": "disposal",
            "item_name": "Thanh lý tài sản cố định hữu hình",
            "movement_type": "disposal",
            "value": "10",
        },
        {
            **common,
            "fact_id": "wrong-disposal-movement",
            "item_name": "Thanh lý tài sản cố định hữu hình",
            "movement_type": "addition",
            "value": "90",
        },
        {
            **common,
            "fact_id": "addition",
            "item_name": "Mua sắm tài sản cố định hữu hình",
            "movement_type": "addition",
            "value": "20",
        },
    ]

    calculation = synth_runner._typed_decimal_calculation(
        _typed_ratio_state(query),
        {"note": {"facts": facts}},
    )

    assert calculation is not None
    assert [item["fact_id"] for item in calculation["operands"]] == [
        "disposal",
        "addition",
    ]
    assert Decimal(calculation["result"]) == Decimal("50")
    assert [item["movement_type"] for item in calculation["operands"]] == [
        "disposal",
        "addition",
    ]


def test_explicit_sum_contract_requires_every_named_component():
    state = {
        "user_query": "Tổng tác động của phải thu và hàng tồn kho là bao nhiêu?",
        "planner_plan": {"difficulty_level": "medium"},
        "worker_plan": {
            "evidence_plan": [
                {
                    "operation": "sum",
                    "operands": [
                        {
                            "role": "component",
                            "query": "biến động các khoản phải thu",
                        },
                        {
                            "role": "component",
                            "query": "biến động hàng tồn kho",
                        },
                    ],
                }
            ]
        },
    }
    facts = {
        "cashflow": {
            "facts": [
                {
                    "fact_id": "receivables",
                        "item_name": "Biến động các khoản phải thu",
                        "value": "(100)",
                        "unit": "VND",
                        "source": "report.md#receivables",
                },
                {
                    "fact_id": "inventory",
                        "item_name": "Biến động hàng tồn kho",
                        "value": "(25)",
                        "unit": "VND",
                        "source": "report.md#inventory",
                },
            ]
        }
    }

    calculation = synth_runner._typed_decimal_calculation(state, facts)

    assert calculation is not None
    assert calculation["operation"] == "sum"
    assert Decimal(calculation["result"]) == Decimal("-125")
    assert [operand["fact_id"] for operand in calculation["operands"]] == [
        "receivables",
        "inventory",
    ]


def test_explicit_sum_rejects_conflicting_typed_scope():
    state = {
        "user_query": "Tổng tác động của phải thu và hàng tồn kho là bao nhiêu?",
        "planner_plan": {"difficulty_level": "medium"},
        "worker_plan": {
            "evidence_plan": [
                {
                    "operation": "sum",
                    "operands": [
                        {
                            "role": "component",
                            "query": "biến động các khoản phải thu",
                        },
                        {
                            "role": "component",
                            "query": "biến động hàng tồn kho",
                        },
                    ],
                }
            ]
        },
    }
    facts = [
        {
            "fact_id": "receivables-a",
            "item_name": "Biến động các khoản phải thu",
            "scope_label": "Hoạt động kinh doanh",
            "geography": "Miền Bắc",
            "value": "(100)",
            "unit": "VND",
            "source": "report.md#cashflow",
        },
        {
            "fact_id": "inventory-b",
            "item_name": "Biến động hàng tồn kho",
            "scope_label": "Hoạt động đầu tư",
            "geography": "Miền Nam",
            "value": "(25)",
            "unit": "VND",
            "source": "report.md#cashflow",
        },
    ]

    assert synth_runner._typed_decimal_calculation(
        state,
        {"cashflow": {"facts": facts}},
    ) is None


def test_explicit_period_operand_rejects_fact_without_period_role():
    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": "Giá trị kỳ hiện tại bằng bao nhiêu lần kỳ trước?",
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": {
                "evidence_plan": [
                    {
                        "operation": "multiple",
                        "operands": [
                            {"role": "current", "query": "cam kết thuê"},
                            {"role": "previous", "query": "cam kết thuê"},
                        ],
                    }
                ]
            },
        },
        {
            "note": {
                "facts": [
                    {
                        "fact_id": "unknown-period",
                        "item_name": "Cam kết thuê",
                        "value": "2",
                        "unit": "VND",
                    },
                    {
                        "fact_id": "previous",
                        "item_name": "Cam kết thuê",
                        "period_role": "previous",
                        "value": "4",
                        "unit": "VND",
                    },
                ]
            }
        },
    )

    assert calculation is None


def test_explicit_two_period_contract_rejects_cross_entity_operands():
    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": "Chênh lệch doanh thu năm nay so với năm trước?",
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": {
                "evidence_plan": [
                    {
                        "operation": "delta",
                        "operands": [
                            {"role": "current", "query": "doanh thu"},
                            {"role": "previous", "query": "doanh thu"},
                        ],
                    }
                ]
            },
        },
        {
            "income": {
                "facts": [
                    {
                        "company": "Công ty A",
                        "item_name": "Doanh thu",
                        "period_role": "current",
                        "value": "200",
                        "unit": "VND",
                    },
                    {
                        "company": "Công ty B",
                        "item_name": "Doanh thu",
                        "period_role": "previous",
                        "value": "100",
                        "unit": "VND",
                    },
                ]
            }
        },
    )

    assert calculation is None


def test_explicit_two_period_contract_rejects_conflicting_typed_dimensions():
    state = {
        "user_query": "Chênh lệch doanh thu năm nay so với năm trước?",
        "planner_plan": {"difficulty_level": "medium"},
        "worker_plan": {
            "evidence_plan": [
                {
                    "operation": "delta",
                    "operands": [
                        {"role": "current", "query": "doanh thu"},
                        {"role": "previous", "query": "doanh thu"},
                    ],
                }
            ]
        },
    }
    conflicts = (
        ("entity_label", "Nhà máy Alpha", "Nhà máy Beta"),
        ("scope_label", "Hoạt động kinh doanh", "Hoạt động đầu tư"),
        ("counterparty", "Công ty Alpha", "Công ty Beta"),
        ("transaction_type", "purchase", "sale"),
        ("movement_type", "addition", "reversal"),
        ("geography", "Miền Bắc", "Miền Nam"),
        ("policy_topic", "recognition_criteria", "measurement_basis"),
    )

    for field, current_dimension, previous_dimension in conflicts:
        common = {
            "company": "Công ty A",
            "table": TABLE_IS,
            "item_name": "Doanh thu",
            "unit": "VND",
            "source": "report.md#income",
        }
        facts = [
            {
                **common,
                "fact_id": f"{field}-current",
                "period_role": "current",
                "value": "200",
                field: current_dimension,
            },
            {
                **common,
                "fact_id": f"{field}-previous",
                "period_role": "previous",
                "value": "100",
                field: previous_dimension,
            },
        ]
        assert synth_runner._typed_decimal_calculation(
            state,
            {"income": {"facts": facts}},
        ) is None, field


def test_explicit_two_period_contract_binds_same_scope_with_provenance():
    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": "Chênh lệch doanh thu năm nay so với năm trước?",
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": {
                "evidence_plan": [
                    {
                        "operation": "delta",
                        "operands": [
                            {"role": "current", "query": "doanh thu"},
                            {"role": "previous", "query": "doanh thu"},
                        ],
                    }
                ]
            },
        },
        {
            "income": {
                "facts": [
                    {
                        "fact_id": "revenue-current",
                        "company": "Công ty A",
                        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                        "item_name": "Doanh thu",
                        "period_role": "current",
                        "movement_type": "closing_balance",
                        "value": "200",
                        "unit": "VND",
                        "source": "report.md#page=10",
                    },
                    {
                        "fact_id": "revenue-previous",
                        "company": "Công ty A",
                        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                        "item_name": "Doanh thu",
                        "period_role": "previous",
                        "movement_type": "opening_balance",
                        "value": "100",
                        "unit": "VND",
                        "source": "report.md#page=10",
                    },
                ]
            }
        },
    )

    assert calculation is not None
    assert calculation["operation"] == "delta"
    assert calculation["difference"] == "100"
    assert [operand["fact_id"] for operand in calculation["operands"]] == [
        "revenue-current",
        "revenue-previous",
    ]


def test_fallback_two_period_binding_never_crosses_companies():
    calculation = synth_runner._typed_decimal_calculation(
        {
            "user_query": "Chênh lệch doanh thu năm nay so với năm trước?",
            "planner_plan": {"difficulty_level": "medium"},
        },
        {
            "income": {
                "facts": [
                    {
                        "company": "Công ty A",
                        "item_name": "Doanh thu",
                        "period_role": "current",
                        "value": "200",
                        "unit": "VND",
                    },
                    {
                        "company": "Công ty B",
                        "item_name": "Doanh thu",
                        "period_role": "previous",
                        "value": "100",
                        "unit": "VND",
                    },
                ]
            }
        },
    )

    assert calculation is None


def test_easy_unique_slot_lookup_is_context_and_model_owns_answer(monkeypatch):
    payloads = []

    def fake_invoke(payload):
        payloads.append(payload)
        return {
            "status": "answer",
            "answer": "Model trả lời doanh thu là 100 VND.",
            "followups": [],
        }, None, "structured"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)
    state = {
        "user_query": "Doanh thu bán hàng và cung cấp dịch vụ là bao nhiêu?",
        "planner_plan": {"difficulty_level": "easy"},
        "worker_plan": {"difficulty_level": "easy", "analysis_plan": []},
        "worker_results": {
            "income": {
                "facts": [
                    {
                        "fact_id": "revenue",
                        "item_name": "Doanh thu bán hàng và cung cấp dịch vụ",
                        "value": "100",
                        "unit": "VND",
                        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                        "source": "report.md",
                    }
                ]
            }
        },
        "trace": [],
    }

    updates = synth_runner.run_synth(state)

    assert updates["synth_decision"]["answer"] == "Model trả lời doanh thu là 100 VND."
    assert len(payloads) == 1
    context = json.loads(payloads[0]["worker_results_json"])
    assert context["canonical_lookup_candidate"]["fact_id"] == "revenue"
    assert context["canonical_lookup_candidate"]["value"] == "100"
    assert "answer" not in context["canonical_lookup_candidate"]


def test_easy_lookup_abstains_on_conflicting_values():
    decision = synth_runner._canonical_lookup_candidate(
        {
            "user_query": "Doanh thu bán hàng và cung cấp dịch vụ là bao nhiêu?",
            "planner_plan": {"difficulty_level": "easy"},
        },
        {
            "income": {
                "facts": [
                    {
                        "item_name": "Doanh thu bán hàng và cung cấp dịch vụ",
                        "value": "100",
                    },
                    {
                        "item_name": "Doanh thu bán hàng và cung cấp dịch vụ",
                        "value": "101",
                    },
                ]
            }
        },
    )

    assert decision is None


def test_incomplete_typed_calculation_is_context_for_model(monkeypatch):
    payloads = []

    def fake_invoke(payload):
        payloads.append(payload)
        return {
            "status": "answer",
            "answer": "Model tự xử lý phép tính chưa đủ toán hạng.",
            "followups": [],
        }, None, "structured"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)
    state = {
        "user_query": "Tỷ trọng hàng tồn kho trên tổng tài sản là bao nhiêu?",
        "planner_plan": {"difficulty_level": "medium"},
        "worker_plan": {
            "difficulty_level": "medium",
            "analysis_plan": [],
            "evidence_plan": [
                {
                    "table": TABLE_BS,
                    "query": "hàng tồn kho",
                    "operation": "share",
                    "operands": [
                        {
                            "role": "numerator",
                            "query": "hàng tồn kho",
                            "metric": "hàng tồn kho",
                            "table": TABLE_BS,
                        },
                        {
                            "role": "denominator",
                            "query": "tổng tài sản",
                            "metric": "tổng tài sản",
                            "aggregation_level": "total",
                            "table": TABLE_BS,
                        },
                    ],
                }
            ],
        },
        "worker_results": {
            "balance": {
                "facts": [
                    {
                        "fact_id": "inventory",
                        "item_name": "Hàng tồn kho",
                        "value": "250",
                        "unit": "VND",
                        "table": TABLE_BS,
                        "source": "report.md#inventory",
                    }
                ]
            }
        },
        "trace": [],
    }

    updates = synth_runner.run_synth(state)

    assert updates["synth_decision"]["answer"] == "Model tự xử lý phép tính chưa đủ toán hạng."
    context = json.loads(payloads[0]["worker_results_json"])
    operand_state = context["typed_operand_state"]
    assert operand_state["status"] == "incomplete"
    assert operand_state["matched_operands"][0]["role"] == "numerator"
    assert operand_state["unresolved_operands"][0]["role"] == "denominator"
    assert "answer" not in operand_state


def test_medium_calculation_without_operand_contract_is_exposed_to_model(
    monkeypatch,
):
    payloads = []

    def fake_invoke(payload):
        payloads.append(payload)
        return {
            "status": "need_more",
            "answer": "Model yêu cầu thêm toán hạng.",
            "followups": [],
        }, None, "structured"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)
    updates = synth_runner.run_synth(
        {
            "user_query": "Tỷ lệ lợi nhuận trên tổng tài sản là bao nhiêu?",
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": {
                "difficulty_level": "medium",
                "analysis_plan": [],
                "evidence_plan": [],
            },
            "worker_results": {},
            "trace": [],
        }
    )

    assert updates["synth_decision"]["answer"] == "Model yêu cầu thêm toán hạng."
    context = json.loads(payloads[0]["worker_results_json"])
    operand_state = context["typed_operand_state"]
    assert operand_state["status"] == "missing_contract"
    assert [item["role"] for item in operand_state["unresolved_operands"]] == [
        "numerator",
        "denominator",
    ]
