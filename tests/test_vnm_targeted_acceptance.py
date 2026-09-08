"""Local acceptance gates over the full parsed VNM fact pool.

The questions are regression fixtures only. Product code must recover them
through generic typed slots while every same-report distractor remains present.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from agents import keyworder_runner, planner_runner, synth_runner
from dataset_catalog.registry import get_dataset
from ingestion.pipeline import _parse_fact_rows, _resolve_ingestion_metadata
from kb.sqlite_repo import _FINANCIAL_FACT_COLUMNS
from schemas.table_names import TABLE_BS, TABLE_CF, TABLE_IS, TABLE_NOTE


@pytest.fixture(scope="module")
def vnm_facts():
    dataset = get_dataset("suavietnam")
    markdown = Path(dataset.file_path).read_text(encoding="utf-8")
    _tables, rows = _parse_fact_rows(
        dataset,
        markdown,
        _resolve_ingestion_metadata(dataset, markdown),
    )
    fields = list(_FINANCIAL_FACT_COLUMNS)
    facts = []
    for index, row in enumerate(rows):
        fact = dict(zip(fields, row))
        fact["table"] = fact.get("heading", "")
        # Persistent builds assign the stable hash ID during insertion. A
        # deterministic fixture ID is sufficient to exercise provenance gates.
        fact["fact_id"] = f"vnm-fixture-{index}"
        facts.append(fact)
    return facts


def _calculation(query, table, facts):
    contract = keyworder_runner._typed_calculation_metadata(query, table)
    state = {
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
    return synth_runner._typed_decimal_calculation(
        state,
        {"all-vnm-facts": {"facts": facts}},
    )


def test_textual_closing_date_selects_code_100_not_opening_or_code_270(vnm_facts):
    query = (
        "Tổng tài sản ngắn hạn tại ngày 31 tháng 12 năm 2025 "
        "là bao nhiêu VND?"
    )
    decision = synth_runner._canonical_lookup_candidate(
        {
            "user_query": query,
            "planner_plan": {"difficulty_level": "easy"},
        },
        {"all-vnm-facts": {"facts": vnm_facts}},
    )

    assert decision is not None
    assert decision["value"] == "27.309.234.148.199"
    assert decision["unit"] == "VND"
    assert "answer" not in decision


def test_total_assets_delta_binds_both_code_270_periods(vnm_facts):
    result = _calculation(
        (
            "Tổng tài sản của Công ty thay đổi như thế nào từ 1/1/2025 "
            "đến 31/12/2025 và mức thay đổi là bao nhiêu?"
        ),
        TABLE_BS,
        vnm_facts,
    )

    assert result is not None
    assert Decimal(result["current_value"]) == Decimal("45952496972636")
    assert Decimal(result["previous_value"]) == Decimal("47448528386601")
    assert Decimal(result["difference"]) == Decimal("-1496031413965")
    assert {
        operand["section_key"] for operand in result["operands"]
    } == {"tong_tai_san"}
    assert len({operand["block_id"] for operand in result["operands"]}) == 1


def test_debt_equity_ratio_binds_code_300_over_code_400(vnm_facts):
    result = _calculation(
        "Tính hệ số nợ trên vốn chủ sở hữu (D/E) của Công ty tại 31/12/2025.",
        TABLE_BS,
        vnm_facts,
    )

    assert result is not None
    assert [
        Decimal(operand["value"]) for operand in result["operands"]
    ] == [
        Decimal("16687564684416"),
        Decimal("29264932288220"),
    ]
    assert [
        operand["section_key"] for operand in result["operands"]
    ] == ["no_phai_tra", "von_chu"]
    assert Decimal(result["result"]).quantize(Decimal("0.001")) == Decimal("0.570")


def test_broad_profitability_source_contains_current_and_comparative_core_pairs(
    vnm_facts,
):
    expected = {
        (TABLE_IS, "Doanh thu thuần về bán hàng và cung cấp dịch vụ"): {
            "current": "52.991.496.309.263",
            "previous": "50.676.707.912.192",
        },
        (TABLE_IS, "Lợi nhuận gộp về bán hàng và cung cấp dịch vụ"): {
            "current": "23.561.360.147.917",
            "previous": "23.017.365.857.504",
        },
        (TABLE_IS, "Lợi nhuận thuần từ hoạt động kinh doanh"): {
            "current": "11.376.790.113.377",
            "previous": "11.154.574.287.978",
        },
        (TABLE_IS, "Lợi nhuận sau thuế TNDN"): {
            "current": "9.359.349.635.629",
            "previous": "9.262.413.822.949",
        },
        (TABLE_BS, "Tổng tài sản"): {
            "current": "45.952.496.972.636",
            "previous": "47.448.528.386.601",
        },
        (TABLE_BS, "Tổng vốn chủ sở hữu"): {
            "current": "29.264.932.288.220",
            "previous": "30.977.801.524.404",
        },
        (TABLE_CF, "Lưu chuyển tiền thuần từ hoạt động kinh doanh"): {
            "current": "7.690.701.645.261",
            "previous": "8.845.818.265.750",
        },
    }

    for (table, metric_prefix), values_by_role in expected.items():
        matched = {
            fact["period_role"]: fact["raw_value"]
            for fact in vnm_facts
            if fact.get("table") == table
            and (
                str(fact.get("metric_label", "") or "") == metric_prefix
                or str(fact.get("metric_label", "") or "").startswith(
                    metric_prefix + " ("
                )
                or str(fact.get("metric_label", "") or "").startswith(
                    metric_prefix + " {"
                )
            )
            and fact.get("period_role") in {"current", "previous"}
        }
        assert matched == values_by_role


@pytest.mark.parametrize(
    ("query", "initial_table", "numerator", "denominator", "expected_percent"),
    [
        (
            "Tỷ trọng doanh thu bán thành phẩm trong tổng doanh thu "
            "năm 2025 là bao nhiêu?",
            TABLE_NOTE,
            Decimal("51881461322802"),
            Decimal("53044988379207"),
            Decimal("97.8"),
        ),
        (
            "Tỷ trọng doanh thu thuần từ thị trường nước ngoài trong "
            "tổng doanh thu thuần năm 2025 là bao nhiêu?",
            TABLE_IS,
            Decimal("7105418295238"),
            Decimal("52991496309263"),
            Decimal("13.4"),
        ),
    ],
)
def test_revenue_shares_bind_component_and_total_in_one_note_block(
    vnm_facts,
    query,
    initial_table,
    numerator,
    denominator,
    expected_percent,
):
    result = _calculation(query, initial_table, vnm_facts)

    assert result is not None
    assert [
        Decimal(operand["value"]) for operand in result["operands"]
    ] == [numerator, denominator]
    assert [
        operand["aggregation_level"] for operand in result["operands"]
    ] == ["component", "total"]
    assert {
        operand["table"] for operand in result["operands"]
    } == {TABLE_NOTE}
    assert len({operand["block_id"] for operand in result["operands"]}) == 1
    assert Decimal(result["result"]).quantize(Decimal("0.1")) == expected_percent
    assert all(
        operand["fact_id"] and operand["source"]
        for operand in result["operands"]
    )


def test_narrative_timeline_covers_each_grounded_premise(
    vnm_facts,
):
    query = (
        "Ý nghĩa của việc Công ty chuyển đổi từ doanh nghiệp nhà nước sang "
        "công ty cổ phần niêm yết đối với quản trị doanh nghiệp là gì?"
    )
    premises = planner_runner._grounded_premise_requirements(query)
    state = {
        "user_query": query,
        "planner_plan": {
            "difficulty_level": "medium",
            "response_mode": "grounded_interpretation",
            "premise_requirements": premises,
        },
    }
    worker_results = {"all-vnm-facts": {"facts": vnm_facts}}

    bindings, missing = synth_runner._grounded_premise_bindings(
        state,
        worker_results,
    )
    assert len(premises) == 3
    assert missing == []
    assert set(bindings) == set(premises)
    assert all(bindings[premise] for premise in premises)
