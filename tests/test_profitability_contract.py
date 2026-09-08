"""Focused regressions for profitability semantics and answer coverage."""

from copy import deepcopy
import json

from agents import synth_runner
from agents.profiles import AGENT_PROFILES
from schemas.financial_validation import (
    canonical_profitability_metrics,
    financial_answer_violations,
)


def _profitability_payload():
    section = (
        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH > "
        "Báo cáo kết quả hoạt động kinh doanh riêng cho năm 2025"
    )
    facts = [
        {
            "item_name": "Lợi nhuận sau thuế TNDN | 2025 VND",
            "section_path": section,
            "period_role": "current",
            "parsed_value": "9359349635629",
            "value": "9.359.349.635.629",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Doanh thu thuần về bán hàng và cung cấp dịch vụ | 2025 VND",
            "section_path": section,
            "period_role": "current",
            "parsed_value": "52991496309263",
            "value": "52.991.496.309.263",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Tổng tài sản | 31/12/2025 VND",
            "section_path": "Báo cáo tình hình tài chính riêng tại 31/12/2025",
            "period_role": "current",
            "parsed_value": "45952496972636",
            "value": "45.952.496.972.636",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Tổng tài sản | 1/1/2025 VND",
            "section_path": "Báo cáo tình hình tài chính riêng tại 31/12/2025",
            "period_role": "previous",
            "parsed_value": "47448528386601",
            "value": "47.448.528.386.601",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Vốn chủ sở hữu | 31/12/2025 VND",
            "section_path": "Báo cáo tình hình tài chính riêng tại 31/12/2025",
            "period_role": "current",
            "parsed_value": "29264932288220",
            "value": "29.264.932.288.220",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Vốn chủ sở hữu | 1/1/2025 VND",
            "section_path": "Báo cáo tình hình tài chính riêng tại 31/12/2025",
            "period_role": "previous",
            "parsed_value": "30977801524404",
            "value": "30.977.801.524.404",
            "unit": "VND",
            "status": "found",
        },
    ]
    for index, fact in enumerate(facts):
        fact.setdefault("fact_id", f"profitability-fact-{index}")
        fact.setdefault("company", "Công ty Cổ phần Sữa Việt Nam")
        fact.setdefault("fiscal_year", "2025")
        fact.setdefault("period", "2025" if fact["period_role"] == "current" else "1/1/2025")
        fact.setdefault(
            "table",
            (
                "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
                if index < 2
                else "BẢNG CÂN ĐỐI KẾ TOÁN"
            ),
        )
        fact.setdefault("source", "/data/vnm-separate-report.md")
        fact.setdefault("source_page", str(9 + index))
    return {"retrieval_facts": {"profitability": {"facts": facts}}}


def _hard_net_margin_plan():
    return {
        "difficulty_level": "hard",
        "analysis_plan": [
            {
                "agent": "agent_profitability",
                "objective": "Tính ROA, ROE và biên lợi nhuận ròng rồi đánh giá sinh lời",
            }
        ],
    }


def _comprehensive_profitability_plan():
    return {
        "difficulty_level": "hard",
        "analysis_plan": [
            {
                "agent": "agent_profitability",
                "objective": "Tính biên lợi nhuận ròng, ROA và ROE",
            },
            {
                "agent": "agent_cashflow_analysis",
                "objective": "Đánh giá chất lượng lợi nhuận qua CFO",
            },
            {
                "agent": "agent_efficiency",
                "objective": "Đánh giá hiệu quả sử dụng tài sản",
            },
        ],
    }


def _profitability_payload_with_cfo():
    payload = deepcopy(_profitability_payload())
    shared = {
        "company": "Công ty Cổ phần Sữa Việt Nam",
        "fiscal_year": "2025",
        "unit": "VND",
        "table": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
        "section_path": "Báo cáo lưu chuyển tiền tệ riêng cho năm 2025",
        "source": "/data/vnm-separate-report.md",
        "status": "found",
    }
    payload["retrieval_facts"]["profitability"]["facts"].extend(
        [
            {
                **shared,
                "fact_id": "cfo-2025",
                "item_name": "Lưu chuyển tiền thuần từ hoạt động kinh doanh | 2025 VND",
                "period": "2025",
                "period_role": "current",
                "parsed_value": "7690701645261",
                "value": "7.690.701.645.261",
            },
            {
                **shared,
                "fact_id": "cfo-2024",
                "item_name": "Lưu chuyển tiền thuần từ hoạt động kinh doanh | 2024 VND",
                "period": "2024",
                "period_role": "previous",
                "parsed_value": "8845818265750",
                "value": "8.845.818.265.750",
            },
        ]
    )
    return payload


def _profitability_payload_with_cfo_history():
    payload = _profitability_payload_with_cfo()
    facts = payload["retrieval_facts"]["profitability"]["facts"]
    shared = {
        "company": "Công ty Cổ phần Sữa Việt Nam",
        "fiscal_year": "2025",
        "period": "2024",
        "period_role": "previous",
        "unit": "VND",
        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "section_path": "Báo cáo kết quả hoạt động kinh doanh riêng năm 2025",
        "source": "/data/vnm-separate-report.md",
        "status": "found",
    }
    facts.extend(
        [
            {
                **shared,
                "fact_id": "pat-2024-cfo-history",
                "item_name": "Lợi nhuận sau thuế TNDN | 2024 VND",
                "parsed_value": "9262413822949",
                "value": "9.262.413.822.949",
            },
            {
                **shared,
                "fact_id": "net-revenue-2024-cfo-history",
                "item_name": (
                    "Doanh thu thuần về bán hàng và cung cấp dịch vụ | 2024 VND"
                ),
                "parsed_value": "50676707912192",
                "value": "50.676.707.912.192",
            },
        ]
    )
    return payload


def _apec_quarterly_payload():
    facts = []

    def add_flow(metric, current, previous, table, prefix):
        for role, value in (("current", current), ("previous", previous)):
            facts.append(
                {
                    "fact_id": f"{prefix}-{role}-cumulative",
                    "item_name": f"{metric} | Lũy kế đến quý IV năm 2024",
                    "period_role": role,
                    "reporting_basis": "cumulative",
                    "parsed_value": str(value),
                    "value": str(value),
                    "unit": "VND",
                    "table": table,
                    "status": "found",
                }
            )
            facts.append(
                {
                    "fact_id": f"{prefix}-{role}-quarter",
                    "item_name": f"{metric} | Quý IV năm 2024",
                    "period_role": role,
                    "reporting_basis": "quarter",
                    "parsed_value": "999999999999",
                    "value": "999999999999",
                    "unit": "VND",
                    "table": table,
                    "status": "found",
                }
            )

    add_flow(
        "Lợi nhuận sau thuế TNDN",
        -10849716013,
        -48187112561,
        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "pat",
    )
    add_flow(
        "Doanh thu thuần về bán hàng và cung cấp dịch vụ",
        209580465103,
        182250363494,
        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "revenue",
    )
    add_flow(
        "Lợi nhuận gộp về bán hàng và cung cấp dịch vụ",
        75877336939,
        52040652356,
        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "gross-profit",
    )
    add_flow(
        "Lợi nhuận thuần từ hoạt động kinh doanh",
        -12918933440,
        -43531303789,
        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "operating-profit",
    )
    add_flow(
        "Lưu chuyển tiền thuần từ hoạt động kinh doanh",
        36158929436,
        -4379663565,
        "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
        "cfo",
    )
    for fact_id, item, role, value in (
        ("assets-current", "Tổng tài sản", "current", 1662694128863),
        ("assets-opening", "Tổng tài sản", "previous", 1906094474649),
        ("equity-current", "Vốn chủ sở hữu", "current", 884244721299),
        ("equity-opening", "Vốn chủ sở hữu", "previous", 895094437312),
    ):
        facts.append(
            {
                "fact_id": fact_id,
                "item_name": item,
                "period_role": role,
                "reporting_basis": "point_in_time",
                "parsed_value": str(value),
                "value": str(value),
                "unit": "VND",
                "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                "status": "found",
            }
        )
    return {"retrieval_facts": {"profitability": {"facts": facts}}}


def _apec_payload_with_liquidity():
    payload = _apec_quarterly_payload()
    facts = payload["retrieval_facts"]["profitability"]["facts"]
    for fact_id, item, value in (
        ("current-assets", "Tổng tài sản ngắn hạn", 984330724539),
        ("current-liabilities", "Tổng nợ ngắn hạn", 566994110452),
        ("inventory-current", "Hàng tồn kho", 474117608966),
        ("cash-current", "Tiền và các khoản tương đương tiền", 97964405114),
        ("total-liabilities", "Tổng nợ phải trả", 778449407564),
    ):
        facts.append(
            {
                "fact_id": fact_id,
                "item_name": item,
                "metric_label": item,
                "period_role": "current",
                "reporting_basis": "point_in_time",
                "parsed_value": str(value),
                "value": str(value),
                "unit": "VND",
                "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                "status": "found",
            }
        )
    return payload


def _profitability_payload_with_operating_profit():
    payload = deepcopy(_profitability_payload())
    shared = {
        "company": "Công ty Cổ phần Sữa Việt Nam",
        "fiscal_year": "2025",
        "unit": "VND",
        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "section_path": "Báo cáo kết quả hoạt động kinh doanh riêng năm 2025",
        "source": "/data/vnm-separate-report.md",
        "source_page": "9",
        "status": "found",
    }
    payload["retrieval_facts"]["profitability"]["facts"].extend(
        [
            {
                **shared,
                "fact_id": "operating-profit-2025",
                "item_name": "Lợi nhuận thuần từ hoạt động kinh doanh | 2025 VND",
                "period": "2025",
                "period_role": "current",
                "parsed_value": "11376790113377",
                "value": "11.376.790.113.377",
            },
            {
                **shared,
                "fact_id": "operating-profit-2024",
                "item_name": "Lợi nhuận thuần từ hoạt động kinh doanh | 2024 VND",
                "period": "2024",
                "period_role": "previous",
                "parsed_value": "11154574287978",
                "value": "11.154.574.287.978",
            },
        ]
    )
    return payload


def _profitability_payload_with_prior_period():
    payload = deepcopy(_profitability_payload())
    facts = payload["retrieval_facts"]["profitability"]["facts"]
    shared = {
        "company": "Công ty Cổ phần Sữa Việt Nam",
        "fiscal_year": "2025",
        "period": "2024",
        "period_role": "previous",
        "unit": "VND",
        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "section_path": (
            "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH > "
            "Báo cáo kết quả hoạt động kinh doanh riêng cho năm 2025"
        ),
        "source": "/data/vnm-separate-report.md",
        "source_page": "9",
        "status": "found",
    }
    facts.extend(
        [
            {
                **shared,
                "fact_id": "pat-2024",
                "item_name": "Lợi nhuận sau thuế TNDN | 2024 VND",
                "parsed_value": "9262413822949",
                "value": "9.262.413.822.949",
            },
            {
                **shared,
                "fact_id": "net-revenue-2024",
                "item_name": (
                    "Doanh thu thuần về bán hàng và cung cấp dịch vụ | 2024 VND"
                ),
                "parsed_value": "50676707912192",
                "value": "50.676.707.912.192",
            },
        ]
    )
    return payload


def _profitability_payload_with_full_margin_history():
    payload = _profitability_payload_with_prior_period()
    facts = payload["retrieval_facts"]["profitability"]["facts"]
    operating_facts = _profitability_payload_with_operating_profit()[
        "retrieval_facts"
    ]["profitability"]["facts"]
    cfo_facts = _profitability_payload_with_cfo()["retrieval_facts"][
        "profitability"
    ]["facts"]
    facts.extend(
        fact
        for fact in [*operating_facts, *cfo_facts]
        if str(fact.get("fact_id", "")).startswith(("operating-profit-", "cfo-"))
    )
    shared = {
        "company": "Công ty Cổ phần Sữa Việt Nam",
        "fiscal_year": "2025",
        "unit": "VND",
        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "section_path": "Báo cáo kết quả hoạt động kinh doanh riêng năm 2025",
        "source": "/data/vnm-separate-report.md",
        "source_page": "9",
        "status": "found",
    }
    facts.extend(
        [
            {
                **shared,
                "fact_id": "gross-profit-2025",
                "item_name": "Lợi nhuận gộp về bán hàng và cung cấp dịch vụ | 2025 VND",
                "period": "2025",
                "period_role": "current",
                "parsed_value": "23561360147917",
                "value": "23.561.360.147.917",
            },
            {
                **shared,
                "fact_id": "gross-profit-2024",
                "item_name": "Lợi nhuận gộp về bán hàng và cung cấp dịch vụ | 2024 VND",
                "period": "2024",
                "period_role": "previous",
                "parsed_value": "23017365857504",
                "value": "23.017.365.857.504",
            },
        ]
    )
    return payload


def test_profit_after_tax_is_not_a_net_profit_proxy_in_separate_statements():
    violations = financial_answer_violations(
        "Lợi nhuận sau thuế TNDN có thể dùng làm proxy cho lãi ròng.",
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
    )

    assert "profitability_net_profit_equivalence:pat_is_net_profit" in violations


def test_profit_after_tax_fact_rejects_false_missing_net_profit():
    violations = financial_answer_violations(
        "Không tìm thấy số liệu lãi ròng trong báo cáo.",
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
    )

    assert "false_missing:net_profit" in violations


def test_complete_equity_ledger_rejects_false_missing_roe_claim():
    violations = financial_answer_violations(
        (
            "Thiếu dữ liệu tổng vốn chủ sở hữu đầu và cuối kỳ nên "
            "không thể tính ROE."
        ),
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
    )

    assert "false_missing:equity" in violations
    assert "false_missing:roe" in violations


def test_annual_roa_and_roe_reject_unlabeled_ending_balance_basis():
    violations = financial_answer_violations(
        (
            "ROA = 9.359.349.635.629 / 45.952.496.972.636 = 20,4%. "
            "ROE = 9.359.349.635.629 / 29.264.932.288.220 = 32,0%."
        ),
        _profitability_payload(),
        user_query="Tính ROA, ROE năm 2025",
    )

    assert (
        "profitability_ratio_basis:roa:ending_balance_must_be_labeled_simplified"
        in violations
    )
    assert (
        "profitability_ratio_basis:roe:ending_balance_must_be_labeled_simplified"
        in violations
    )


def test_annual_roa_and_roe_accept_average_opening_closing_basis():
    violations = financial_answer_violations(
        (
            "ROA dùng tổng tài sản bình quân đầu kỳ và cuối kỳ, đạt 20,04%. "
            "ROE dùng vốn chủ sở hữu bình quân đầu kỳ và cuối kỳ, đạt 31,07%."
        ),
        _profitability_payload(),
        user_query="Tính ROA, ROE năm 2025",
    )

    assert not any(item.startswith("profitability_ratio_basis:") for item in violations)


def test_summary_ratio_mentions_reuse_basis_disclosed_in_detail():
    violations = financial_answer_violations(
        (
            "Tóm tắt: ROA 20,04%; ROE 31,07%.\n"
            "Chi tiết ROA dùng tổng tài sản bình quân đầu kỳ và cuối kỳ.\n"
            "Chi tiết ROE dùng vốn chủ sở hữu bình quân đầu kỳ và cuối kỳ."
        ),
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời năm 2025",
    )

    assert not any(item.startswith("profitability_ratio_basis:") for item in violations)


def test_shared_average_basis_sentence_applies_to_both_roa_and_roe():
    violations = financial_answer_violations(
        "ROA và ROE được tính trên số bình quân đầu kỳ và cuối kỳ, lần lượt là 20,04% và 31,07%.",
        _profitability_payload(),
        user_query="Tính ROA và ROE năm 2025",
    )

    assert not any(item.startswith("profitability_ratio_basis:") for item in violations)


def test_ending_balance_ratio_is_allowed_only_when_explicitly_simplified():
    violations = financial_answer_violations(
        (
            "ROA theo cách tính đơn giản hóa dùng tài sản cuối kỳ là 20,4%. "
            "ROE theo cách tính đơn giản hóa dùng vốn chủ sở hữu cuối kỳ là 32,0%."
        ),
        _profitability_payload(),
        user_query="Tính ROA, ROE năm 2025",
    )

    assert not any(item.startswith("profitability_ratio_basis:") for item in violations)


def test_roa_and_roe_reject_operating_profit_as_explicit_numerator():
    violations = financial_answer_violations(
        (
            "ROA dùng tài sản bình quân đầu kỳ và cuối kỳ = "
            "Lợi nhuận thuần từ hoạt động kinh doanh / tài sản bình quân = 24,36%. "
            "ROE dùng vốn chủ sở hữu bình quân đầu kỳ và cuối kỳ = "
            "Lợi nhuận thuần từ hoạt động kinh doanh / vốn bình quân = 37,77%."
        ),
        _profitability_payload_with_operating_profit(),
        user_query="Tính ROA và ROE năm 2025",
    )

    assert "profitability_ratio_numerator:roa:must_use_net_profit" in violations
    assert "profitability_ratio_numerator:roe:must_use_net_profit" in violations


def test_roa_rejects_prose_claim_based_on_operating_profit():
    violations = financial_answer_violations(
        "ROA được tính dựa trên lợi nhuận thuần từ hoạt động kinh doanh.",
        _profitability_payload_with_operating_profit(),
        user_query="Tính ROA năm 2025",
    )

    assert "profitability_ratio_numerator:roa:must_use_net_profit" in violations


def test_roa_and_roe_reject_operating_profit_value_as_implicit_numerator():
    violations = financial_answer_violations(
        (
            "ROA theo tài sản bình quân = 11.376.790.113.377 / "
            "46.700.512.679.618,5 = 24,36%. "
            "ROE theo vốn chủ sở hữu bình quân = 11.376.790.113.377 / "
            "30.121.366.906.312 = 37,77%."
        ),
        _profitability_payload_with_operating_profit(),
        user_query="Tính ROA và ROE năm 2025",
    )

    assert "profitability_ratio_numerator:roa:must_use_net_profit" in violations
    assert "profitability_ratio_numerator:roe:must_use_net_profit" in violations


def test_logged_operating_profit_roa_roe_results_are_rejected_without_formula():
    violations = financial_answer_violations(
        "Tóm tắt: ROA 24,4%; ROE 37,8%.",
        _profitability_payload_with_operating_profit(),
        user_query="Đánh giá khả năng sinh lời năm 2025",
    )

    assert "profitability_ratio_numerator:roa:must_use_net_profit" in violations
    assert "profitability_ratio_numerator:roe:must_use_net_profit" in violations


def test_prior_period_roa_coinciding_with_operating_ratio_is_not_misread():
    violations = financial_answer_violations(
        "ROA năm 2024 là 24,4%. ROA năm 2025 dùng tài sản bình quân là 20,04%.",
        _profitability_payload_with_operating_profit(),
        user_query="So sánh ROA năm 2025 với 2024",
    )

    assert not any(
        item.startswith("profitability_ratio_numerator:") for item in violations
    )


def test_standard_roa_and_roe_accept_pat_and_can_disclaim_operating_profit():
    violations = financial_answer_violations(
        (
            "ROA không dùng lợi nhuận thuần từ hoạt động kinh doanh mà dùng PAT; "
            "ROA theo tài sản bình quân = 9.359.349.635.629 / "
            "46.700.512.679.618,5 = 20,04%. "
            "ROE theo vốn chủ sở hữu bình quân = 9.359.349.635.629 / "
            "30.121.366.906.312 = 31,07%."
        ),
        _profitability_payload_with_operating_profit(),
        user_query="Tính ROA và ROE năm 2025",
    )

    assert not any(
        item.startswith("profitability_ratio_numerator:") for item in violations
    )


def test_hard_profitability_objective_requires_net_margin_coverage():
    violations = financial_answer_violations(
        "ROA dùng tài sản bình quân là 20,04% và ROE dùng vốn bình quân là 31,07%.",
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
        plan_context=json.dumps(_hard_net_margin_plan(), ensure_ascii=False),
    )

    assert "profitability_coverage:net_margin_required_by_objective" in violations


def test_hard_profitability_objective_accepts_net_margin_coverage():
    violations = financial_answer_violations(
        (
            "ROA dùng tài sản bình quân là 20,04%; ROE dùng vốn chủ sở hữu bình quân "
            "là 31,07%; biên lợi nhuận ròng là 17,66%."
        ),
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
        plan_context=_hard_net_margin_plan(),
    )

    assert "profitability_coverage:net_margin_required_by_objective" not in violations


def test_net_margin_alias_inside_refusal_is_not_numeric_coverage():
    violations = financial_answer_violations(
        "Do thiếu số liệu doanh thu, không thể tính tỷ suất lợi nhuận ròng.",
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
        plan_context=_hard_net_margin_plan(),
    )

    assert "false_missing:net_revenue" in violations
    assert "profitability_coverage:net_margin_numeric_result_required" in violations


def test_asset_turnover_refusal_cannot_hide_available_net_revenue():
    violations = financial_answer_violations(
        "Không có doanh thu nên không thể tính vòng quay tài sản.",
        _profitability_payload(),
        user_query="Đánh giá hiệu quả sử dụng tài sản",
    )

    assert "false_missing:net_revenue" in violations


def test_post_repair_stale_multi_agent_layout_is_rejected():
    stale_layout = (
        "**Tóm tắt đánh giá**\n"
        "- Thiếu số liệu doanh thu, nên không thể tính biên lợi nhuận gộp, "
        "biên lợi nhuận hoạt động và tỷ suất lợi nhuận ròng.\n\n"
        "**1. Khả năng sinh lời**\n"
        "- Do thiếu số liệu doanh thu (hoặc doanh thu thuần), không thể tính net margin.\n"
        "- Lợi nhuận thuần từ hoạt động kinh doanh (EBIT): 11.376.790.113.377 VND.\n\n"
        "**2. Dòng tiền**\n"
        "- CFO 2025 (cuối kỳ): 7.690.701.645.261 VND.\n"
        "- CFO 2024 (đầu kỳ): 8.846.000.000.000 VND.\n"
        "- PAT 2025: 9.359.349.635.629 VND. Đánh giá chất lượng lợi nhuận cần "
        "so sánh CFO với PAT hoặc EBIT; do thiếu so sánh này nên chưa kết luận."
    )
    violations = financial_answer_violations(
        stale_layout,
        _profitability_payload_with_cfo(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert "false_missing:net_revenue" in violations
    assert "profitability_coverage:net_margin_numeric_result_required" in violations
    assert "profitability_coverage:cfo_pat_comparison_required" in violations
    assert "profitability_semantics:operating_profit_is_not_ebit" in violations
    assert "cashflow_period_label:cfo:annual_flow_not_ending_balance" in violations
    assert "cashflow_period_label:cfo:annual_flow_not_opening_balance" in violations


def test_numeric_net_margin_and_cfo_pat_comparison_satisfy_coverage():
    answer = (
        "Biên lợi nhuận ròng = 9.359.349.635.629 / 52.991.496.309.263 = 17,66%.\n"
        "CFO/PAT năm 2025 = 7.690.701.645.261 / 9.359.349.635.629 = 82,17%; "
        "CFO thấp hơn PAT, cho thấy lợi nhuận chưa chuyển đổi hoàn toàn thành tiền.\n"
        "Vòng quay tài sản = doanh thu thuần / tài sản bình quân = 1,13 lần.\n"
        "Lợi nhuận thuần từ hoạt động kinh doanh là 11.376.790.113.377 VND và "
        "không tự động đồng nhất với EBIT."
    )
    violations = financial_answer_violations(
        answer,
        _profitability_payload_with_cfo(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert not any(item.startswith("profitability_coverage:") for item in violations)
    assert "profitability_semantics:operating_profit_is_not_ebit" not in violations
    assert not any(item.startswith("cashflow_period_label:") for item in violations)


def test_operating_profit_amount_direction_is_not_replaced_by_margin_direction():
    violations = financial_answer_violations(
        (
            "Lợi nhuận thuần từ hoạt động kinh doanh giảm nhẹ "
            "(biên từ 22,0% xuống 21,5%)."
        ),
        _profitability_payload_with_operating_profit(),
        user_query="Đánh giá khả năng sinh lời năm 2025 so với 2024",
    )

    assert (
        "profitability_trend:operating_profit_amount_expected_increase"
        in violations
    )


def test_operating_profit_increase_and_operating_margin_decrease_are_distinct():
    violations = financial_answer_violations(
        (
            "Lợi nhuận thuần từ hoạt động kinh doanh tăng 1,99% so với năm 2024, "
            "trong khi biên lợi nhuận hoạt động giảm từ 22,0% xuống 21,5%."
        ),
        _profitability_payload_with_operating_profit(),
        user_query="Đánh giá khả năng sinh lời năm 2025 so với 2024",
    )

    assert not any(
        item.startswith("profitability_trend:operating_profit_amount")
        for item in violations
    )


def test_operating_profit_direction_guard_is_symmetric_for_a_decrease():
    payload = _profitability_payload_with_operating_profit()
    current = next(
        fact
        for fact in payload["retrieval_facts"]["profitability"]["facts"]
        if fact.get("fact_id") == "operating-profit-2025"
    )
    current["parsed_value"] = "10000000000000"
    current["value"] = "10.000.000.000.000"

    violations = financial_answer_violations(
        "Lợi nhuận thuần từ hoạt động kinh doanh tăng so với năm 2024.",
        payload,
        user_query="Đánh giá khả năng sinh lời năm 2025 so với 2024",
    )

    assert (
        "profitability_trend:operating_profit_amount_expected_decrease"
        in violations
    )


def test_comprehensive_profitability_requires_direction_when_prior_core_facts_exist():
    answer = (
        "Biên lợi nhuận ròng năm 2025 là 17,66%. "
        "ROA theo tài sản bình quân là 20,04% và ROE theo vốn bình quân là 31,07%."
    )

    violations = financial_answer_violations(
        answer,
        _profitability_payload_with_prior_period(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert "profitability_coverage:comparative_direction_required" in violations


def test_comprehensive_profitability_requires_numeric_asset_turnover():
    answer = (
        "Biên lợi nhuận ròng là 17,66%. CFO/PAT là 82,17%. "
        "Hiệu suất sử dụng tài sản cần được xem xét nhưng chưa có kết quả tính."
    )

    violations = financial_answer_violations(
        answer,
        _profitability_payload_with_cfo(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert "profitability_coverage:asset_turnover_numeric_result_required" in violations


def test_numeric_asset_turnover_satisfies_efficiency_coverage():
    answer = (
        "Biên lợi nhuận ròng là 17,66%. CFO/PAT 2025 = 82,17%. "
        "Vòng quay tài sản = doanh thu thuần / tài sản bình quân = 1,13 lần."
    )

    violations = financial_answer_violations(
        answer,
        _profitability_payload_with_cfo(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert not any("asset_turnover" in item for item in violations)


def test_asset_turnover_improvement_is_unsupported_without_prior_turnover():
    violations = financial_answer_violations(
        (
            "Vòng quay tài sản cải thiện so với năm trước vì doanh thu tăng "
            "trong khi tài sản giảm nhẹ khoảng 1,5%."
        ),
        _profitability_payload_with_prior_period(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert (
        "profitability_coverage:asset_turnover_comparison_unsupported"
        in violations
    )


def test_asset_turnover_comparison_accepts_a_direct_prior_turnover_fact():
    payload = _profitability_payload_with_prior_period()
    payload["retrieval_facts"]["profitability"]["facts"].append(
        {
            "fact_id": "asset-turnover-2024",
            "item_name": "Vòng quay tài sản | 2024",
            "period": "2024",
            "period_role": "previous",
            "parsed_value": "1.08",
            "value": "1,08",
            "unit": "x",
            "status": "found",
        }
    )

    violations = financial_answer_violations(
        "Vòng quay tài sản cải thiện từ 1,08 lần lên 1,13 lần.",
        payload,
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert (
        "profitability_coverage:asset_turnover_comparison_unsupported"
        not in violations
    )


def test_comparative_net_margin_direction_satisfies_comprehensive_coverage():
    answer = (
        "Biên lợi nhuận ròng giảm từ 18,28% năm 2024 xuống 17,66% năm 2025, "
        "tức thu hẹp 0,62 điểm phần trăm so với năm trước."
    )

    violations = financial_answer_violations(
        answer,
        _profitability_payload_with_prior_period(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert "profitability_coverage:comparative_direction_required" not in violations


def test_vague_three_margin_decline_requires_numeric_prior_and_delta():
    answer = (
        "Biên lợi nhuận gộp, hoạt động và ròng giảm nhẹ so với năm 2024, "
        "dù doanh thu tăng khoảng 4,7%."
    )

    violations = financial_answer_violations(
        answer,
        _profitability_payload_with_full_margin_history(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert (
        "profitability_coverage:gross_margin_comparison_quantification_required"
        in violations
    )
    assert (
        "profitability_coverage:operating_margin_comparison_quantification_required"
        in violations
    )
    assert (
        "profitability_coverage:net_margin_comparison_quantification_required"
        in violations
    )


def test_all_three_margin_prior_values_and_deltas_satisfy_quantitative_coverage():
    answer = (
        "Biên lợi nhuận gộp giảm từ 45,42% năm 2024 xuống 44,46% năm 2025, "
        "giảm 0,96 điểm phần trăm.\n"
        "Biên lợi nhuận hoạt động giảm từ 22,01% xuống 21,47%, "
        "giảm 0,54 điểm phần trăm.\n"
        "Biên lợi nhuận ròng giảm từ 18,28% xuống 17,66%, "
        "giảm 0,62 điểm phần trăm."
    )

    violations = financial_answer_violations(
        answer,
        _profitability_payload_with_full_margin_history(),
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=_comprehensive_profitability_plan(),
    )

    assert not any(
        item.endswith("margin_comparison_quantification_required")
        for item in violations
    )


def test_cashflow_effect_on_ending_cash_is_not_a_flow_period_mislabel():
    violations = financial_answer_violations(
        "CFO năm 2025 dương và góp phần làm tăng số dư tiền cuối kỳ.",
        _profitability_payload_with_cfo(),
        user_query="Đánh giá dòng tiền",
    )

    assert not any(item.startswith("cashflow_period_label:") for item in violations)


def test_cfo_change_percent_must_match_current_and_previous_facts():
    violations = financial_answer_violations(
        "CFO giảm khoảng 13,5% so với năm 2024.",
        _profitability_payload_with_cfo(),
        user_query="Đánh giá dòng tiền năm 2025 so với 2024",
    )

    assert "cashflow_comparison:cfo_change_percent_mismatch" in violations


def test_cfo_change_accepts_exact_or_reasonably_rounded_percent():
    exact = financial_answer_violations(
        "CFO giảm 13,06% so với năm 2024.",
        _profitability_payload_with_cfo(),
        user_query="Đánh giá dòng tiền năm 2025 so với 2024",
    )
    rounded = financial_answer_violations(
        "CFO giảm khoảng 13,1% so với năm 2024.",
        _profitability_payload_with_cfo(),
        user_query="Đánh giá dòng tiền năm 2025 so với 2024",
    )

    assert "cashflow_comparison:cfo_change_percent_mismatch" not in exact
    assert "cashflow_comparison:cfo_change_percent_mismatch" not in rounded


def test_annual_cashflow_rejects_opening_and_ending_year_balance_labels():
    violations = financial_answer_violations(
        "CFO 2025 (cuối năm): 7.690.701.645.261 VND; CFO 2024 (đầu năm): 8 VND.",
        _profitability_payload_with_cfo(),
        user_query="Đánh giá dòng tiền",
    )

    assert "cashflow_period_label:cfo:annual_flow_not_ending_balance" in violations
    assert "cashflow_period_label:cfo:annual_flow_not_opening_balance" in violations


def test_income_statement_flows_reject_opening_and_ending_balance_labels():
    answer = (
        "Doanh thu thuần 2025 (cuối kỳ): 52.991.496.309.263 VND.\n"
        "Lợi nhuận gộp 2024 (đầu kỳ): 23.017.365.857.504 VND.\n"
        "Lợi nhuận thuần từ hoạt động kinh doanh 2025 (cuối năm): "
        "11.376.790.113.377 VND.\n"
        "PAT 2024 (đầu năm): 9.262.413.822.949 VND."
    )

    violations = financial_answer_violations(
        answer,
        _profitability_payload_with_operating_profit(),
        user_query="Đánh giá kết quả kinh doanh năm 2025 so với 2024",
    )

    assert (
        "income_statement_period_label:net_revenue:annual_flow_not_ending_balance"
        in violations
    )
    assert (
        "income_statement_period_label:gross_profit:annual_flow_not_opening_balance"
        in violations
    )
    assert (
        "income_statement_period_label:operating_profit:annual_flow_not_ending_balance"
        in violations
    )
    assert (
        "income_statement_period_label:net_profit:annual_flow_not_opening_balance"
        in violations
    )


def test_income_flow_label_guard_handles_report_style_year_unit_surface():
    violations = financial_answer_violations(
        "Doanh thu bán hàng và cung cấp dịch vụ | 2025 VND (cuối kỳ): 1 VND.",
        _profitability_payload(),
        user_query="Doanh thu năm 2025",
    )

    assert (
        "income_statement_period_label:net_revenue:annual_flow_not_ending_balance"
        in violations
    )


def test_income_flow_can_explain_effect_on_an_ending_balance_without_mislabel():
    violations = financial_answer_violations(
        (
            "Lợi nhuận sau thuế TNDN năm 2025 làm tăng lợi nhuận sau thuế "
            "chưa phân phối cuối kỳ."
        ),
        _profitability_payload(),
        user_query="Giải thích biến động vốn chủ sở hữu",
    )

    assert not any(
        item.startswith("income_statement_period_label:") for item in violations
    )


def test_profitability_high_low_claim_requires_explicit_benchmark():
    unsupported = financial_answer_violations(
        "ROA 20,04% và ROE 31,07% cho thấy khả năng sinh lời ở mức cao.",
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
    )
    supported = financial_answer_violations(
        "So với kỳ trước, ROA tăng từ 18% lên 20,04% nên ở mức cao hơn kỳ trước.",
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
    )

    assert "profitability_level:explicit_benchmark_required" in unsupported
    assert "profitability_level:explicit_benchmark_required" not in supported


def test_unrelated_two_year_context_does_not_support_good_or_strong_claims():
    answer = (
        "ROA đạt khoảng 20% năm 2025, cho thấy khả năng sinh lời trên tài sản tốt. "
        "Doanh thu năm 2025 tăng so với năm 2024. "
        "ROA 20% cho thấy công ty tạo ra lợi nhuận ròng mạnh."
    )

    violations = financial_answer_violations(
        answer,
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
    )

    assert "profitability_level:explicit_benchmark_required" in violations


def test_good_profitability_claim_accepts_a_local_explicit_benchmark():
    violations = financial_answer_violations(
        "ROA 20,04% tốt hơn mức 18,00% của năm trước.",
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
    )

    assert "profitability_level:explicit_benchmark_required" not in violations


def test_high_cost_is_not_misread_as_high_profitability():
    violations = financial_answer_violations(
        "Lợi nhuận giảm do chi phí bán hàng cao.",
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
    )

    assert "profitability_level:explicit_benchmark_required" not in violations


def test_logged_profitability_failure_triggers_each_semantic_guard():
    violations = financial_answer_violations(
        (
            "Lợi nhuận sau thuế TNDN có thể dùng làm proxy cho lãi ròng. "
            "ROA = 9.359.349.635.629 / 45.952.496.972.636 = 20,4%. "
            "ROE = 9.359.349.635.629 / 29.264.932.288.220 = 32,0%. "
            "Các chỉ số cho thấy khả năng sinh lời ở mức cao. "
            "Không tìm thấy số liệu lãi ròng chính thức."
        ),
        _profitability_payload(),
        user_query="Đánh giá khả năng sinh lời",
        plan_context=_hard_net_margin_plan(),
    )

    assert "profitability_net_profit_equivalence:pat_is_net_profit" in violations
    assert "false_missing:net_profit" in violations
    assert any(item.startswith("profitability_ratio_basis:roa:") for item in violations)
    assert any(item.startswith("profitability_ratio_basis:roe:") for item in violations)
    assert "profitability_coverage:net_margin_required_by_objective" in violations
    assert "profitability_level:explicit_benchmark_required" in violations


def test_canonical_profitability_metrics_use_closed_average_basis():
    result = canonical_profitability_metrics(_profitability_payload())

    assert result["status"] == "complete"
    assert result["scope"] == "separate"
    assert result["period"] == "2025"
    assert result["amount_unit"] == "VND"
    assert result["metrics"]["net_margin"]["value"] == "17.66"
    assert result["metrics"]["roa"]["value"] == "20.04"
    assert result["metrics"]["roe"]["value"] == "31.07"
    assert result["metrics"]["asset_turnover"] == {
        "value": "1.13",
        "unit": "x",
        "formula": "net_revenue / average_total_assets",
        "basis": "average_opening_closing",
        "operand_keys": [
            "net_revenue",
            "total_assets_opening",
            "total_assets_current",
        ],
    }
    assert result["derived_denominators"]["average_total_assets"]["value"] == "46700512679618.5"
    assert result["derived_denominators"]["average_equity"]["value"] == "30121366906312"
    assert result["inputs"]["net_profit"]["fact_id"] == "profitability-fact-0"
    assert result["inputs"]["net_profit"]["source"] == "/data/vnm-separate-report.md"
    assert "trend" not in result


def test_synth_payload_exposes_calculation_ledger_without_prebuilt_prose():
    worker_results = _profitability_payload_with_full_margin_history()
    payload = synth_runner._build_payload(
        {
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "planner_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
            },
            "worker_plan": _comprehensive_profitability_plan(),
        },
        AGENT_PROFILES["agent_synth"],
        worker_results,
    )

    prompt_results = json.loads(payload["worker_results_json"])
    ledger = prompt_results["canonical_financial_metrics"]

    assert ledger["metrics"]["net_margin"]["value"] == "17.66"
    assert ledger["metrics"]["roa"]["basis"] == "average_opening_closing"
    assert ledger["metrics"]["roe"]["value"] == "31.07"
    assert ledger["comparatives"]["net_margin_change"]["direction"] == "decrease"
    assert "answer" not in ledger
    assert "source" not in ledger["inputs"]["net_profit"]
    assert "canonical_financial_metrics" in payload["system_instruction"]
    assert "context hỗ trợ" in payload["system_instruction"]


def test_quality_repair_keeps_full_context_and_model_owns_repaired_answer(
    monkeypatch,
):
    worker_results = _profitability_payload_with_full_margin_history()
    model_repair = {
        "status": "answer",
        "answer": "ROA cao nhưng chưa có dữ liệu ROE.",
        "followups": [],
    }
    captured = {}

    def fake_invoke(payload):
        captured.update(payload)
        return model_repair, None, "structured"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)
    payload = synth_runner._build_payload(
        {
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "planner_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
            },
            "worker_plan": _comprehensive_profitability_plan(),
        },
        AGENT_PROFILES["agent_synth"],
        worker_results,
    )

    repaired, _usage, mode, violations, remaining, retried, accepted, _diag = (
        synth_runner._retry_synth_quality_once(
            {
                "planner_plan": {
                    "difficulty_level": "hard",
                    "response_mode": "extractive",
                }
            },
            payload,
            worker_results,
            {
                "status": "answer",
                "answer": "ROA cao nhưng chưa có dữ liệu ROE.",
                "followups": [],
            },
        )
    )

    repair_context = json.loads(captured["worker_results_json"])
    assert "canonical_financial_metrics" in repair_context
    assert "retrieval_facts" in repair_context
    assert repair_context["canonical_financial_metrics"]["metrics"]["roe"][
        "value"
    ] == "31.07"
    assert "QUALITY REVIEW" in captured["system_instruction"]
    assert repaired == model_repair
    assert mode == "structured"
    assert violations
    assert remaining
    assert retried is True
    assert accepted == "general_repair"


def test_analysis_agents_receive_only_their_canonical_metric_scope():
    state = {
        "worker_results": _profitability_payload_with_full_margin_history(),
    }

    profitability = canonical_profitability_metrics(
        state["worker_results"]
    )
    assert profitability["status"] == "complete"

    from agents import agent_runner

    profit_ledger = agent_runner._canonical_metrics_for_analysis_agent(
        state,
        "agent_profitability",
    )
    cash_ledger = agent_runner._canonical_metrics_for_analysis_agent(
        state,
        "agent_cashflow_analysis",
    )
    efficiency_ledger = agent_runner._canonical_metrics_for_analysis_agent(
        state,
        "agent_efficiency",
    )

    assert {"net_margin", "roa", "roe"}.issubset(profit_ledger["metrics"])
    assert "cfo_to_net_profit" not in profit_ledger["metrics"]
    assert set(cash_ledger["metrics"]) == {"cfo_to_net_profit"}
    assert "equity_current" not in cash_ledger["inputs"]
    assert set(efficiency_ledger["metrics"]) == {"asset_turnover"}
    assert "roe" not in efficiency_ledger["metrics"]


def test_liquidity_agent_receives_canonical_ratio_context():
    from agents import agent_runner

    state = {"worker_results": _apec_payload_with_liquidity()}
    ledger = agent_runner._canonical_metrics_for_analysis_agent(
        state,
        "agent_liquidity_solvency",
    )

    assert set(ledger["metrics"]) == {
        "current_ratio",
        "quick_ratio",
        "cash_ratio",
        "debt_to_equity",
    }
    assert ledger["inputs"]["current_assets"]["fact_id"] == "current-assets"


def test_quality_repair_never_composes_specialist_prose(monkeypatch):
    worker_results = _profitability_payload_with_full_margin_history()
    worker_results["analysis_outputs"] = {
        "agent_profitability": {
            "answer": "Worker profitability prose.",
            "requirements": [],
        },
        "agent_cashflow_analysis": {
            "answer": "Worker cash-flow prose.",
            "requirements": [],
        },
        "agent_efficiency": {
            "answer": "Worker efficiency prose.",
            "requirements": [],
        },
    }
    model_repair = {
        "status": "answer",
        "answer": "ROA cao nhưng chưa có dữ liệu ROE.",
        "followups": [],
    }
    monkeypatch.setattr(
        synth_runner,
        "_invoke_synth",
        lambda _payload: (model_repair, None, "structured"),
    )
    state = {
        "planner_plan": {
            "difficulty_level": "hard",
            "response_mode": "extractive",
        }
    }
    payload = synth_runner._build_payload(
        {
            **state,
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "worker_plan": _comprehensive_profitability_plan(),
        },
        AGENT_PROFILES["agent_synth"],
        worker_results,
    )

    repaired, _usage, _mode, violations, remaining, retried, accepted, _diag = (
        synth_runner._retry_synth_quality_once(
            state,
            payload,
            worker_results,
            {
                "status": "answer",
                "answer": "ROA cao nhưng chưa có dữ liệu ROE.",
                "followups": [],
            },
        )
    )

    assert repaired == model_repair
    assert "Worker profitability prose" not in repaired["answer"]
    assert violations
    assert remaining
    assert retried is True
    assert accepted == "general_repair"


def test_canonical_profitability_metrics_add_optional_cfo_pat_comparison():
    result = canonical_profitability_metrics(_profitability_payload_with_cfo())

    assert result["inputs"]["cfo"]["fact_id"] == "cfo-2025"
    assert result["inputs"]["cfo"]["period_role"] == "current"
    assert result["metrics"]["cfo_to_net_profit"] == {
        "value": "82.17",
        "unit": "%",
        "formula": "cfo / net_profit * 100",
        "operand_keys": ["cfo", "net_profit"],
        "direction": "below_net_profit",
    }


def test_canonical_profitability_metrics_add_prior_cfo_pat_comparison():
    result = canonical_profitability_metrics(_profitability_payload_with_cfo_history())

    assert result["inputs"]["cfo_previous"]["fact_id"] == "cfo-2024"
    assert result["comparatives"]["cfo_to_net_profit_previous"] == {
        "value": "95.50",
        "unit": "%",
        "formula": "cfo_previous / net_profit_previous * 100",
        "operand_keys": ["cfo_previous", "net_profit_previous"],
    }
    assert result["comparatives"]["cfo_to_net_profit_change"] == {
        "value": "-13.33",
        "unit": "percentage_points",
        "formula": "cfo_to_net_profit - cfo_to_net_profit_previous",
        "direction": "decrease",
        "operand_keys": [
            "cfo",
            "net_profit",
            "cfo_previous",
            "net_profit_previous",
        ],
    }
    assert result["comparatives"]["cfo_growth"]["value"] == "-13.06"
    assert result["comparatives"]["net_profit_growth"]["value"] == "1.05"


def test_quarterly_report_metrics_use_cumulative_facts_and_explain_cfo_sign_crossing():
    result = canonical_profitability_metrics(_apec_quarterly_payload())

    assert result["inputs"]["net_profit"]["fact_id"] == "pat-current-cumulative"
    assert result["inputs"]["net_revenue"]["fact_id"] == "revenue-current-cumulative"
    assert result["metrics"]["net_margin"]["value"] == "-5.18"
    assert result["metrics"]["roa"]["value"] == "-0.61"
    assert result["metrics"]["roe"]["value"] == "-1.22"
    assert result["metrics"]["asset_turnover"]["value"] == "0.12"
    assert result["metrics"]["cfo_to_net_profit"]["interpretation_guard"] == (
        "net_profit_non_positive_do_not_label_earnings_conversion_quality"
    )
    assert "cfo_growth" not in result["comparatives"]
    assert result["comparatives"]["cfo_change_amount"] == {
        "value": "40538593001",
        "unit": "VND",
        "formula": "cfo - cfo_previous",
        "operand_keys": ["cfo", "cfo_previous"],
        "sign_transition": "negative_to_positive",
    }


def test_canonical_liquidity_metrics_use_main_totals_not_note_components():
    payload = _apec_payload_with_liquidity()
    result = canonical_profitability_metrics(payload)

    assert result["metrics"]["current_ratio"]["value"] == "1.74"
    assert result["metrics"]["quick_ratio"] == {
        "value": "0.90",
        "unit": "x",
        "formula": "(current_assets - inventory) / current_liabilities",
        "operand_keys": ["current_assets", "inventory", "current_liabilities"],
    }
    assert result["metrics"]["cash_ratio"]["value"] == "0.17"
    assert result["metrics"]["debt_to_equity"]["value"] == "0.88"

    violations = financial_answer_violations(
        (
            "Quick Ratio 0,82 = (tiền + phải thu + cấu phần note) / nợ ngắn hạn; "
            "Current Ratio 1,74 và Cash Ratio 0,17."
        ),
        payload,
        user_query="Đánh giá tình hình tài chính công ty",
    )
    assert "liquidity_ratio_value_mismatch:quick_ratio" in violations


def test_cfo_sign_crossing_rejects_percent_growth_headline():
    violations = financial_answer_violations(
        "CFO tăng 925,61% so với cùng kỳ.",
        _apec_quarterly_payload(),
        user_query="Đánh giá tình hình tài chính năm 2024",
    )

    assert "cashflow_comparison:cfo_sign_crossing_percent_misleading" in violations


def test_negative_pat_ratio_cannot_support_non_cash_conversion_claim():
    violations = financial_answer_violations(
        (
            "CFO/PAT là -333,27%; điều này cho thấy lợi nhuận kế toán không "
            "phản ánh đầy đủ khả năng sinh tiền và công ty đang dựa vào các "
            "yếu tố phi tiền mặt."
        ),
        _apec_quarterly_payload(),
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert (
        "cashflow_quality:cfo_pat_negative_profit_not_conversion_measure"
        in violations
    )


def test_quality_repair_prompt_explains_note_and_signed_cashflow_diagnostics():
    guidance = synth_runner._quality_repair_guidance(
        [
            "note_ref_coverage:linked_detail_required",
            "cashflow_comparison:cff_amount_expected_tang",
            "cashflow_quality:cfo_pat_negative_profit_not_conversion_measure",
        ]
    )

    assert "Theo Thuyết minh [note_ref]" in guidance
    assert "một số âm ít âm hơn" in guidance
    assert "khi PAT âm, bỏ tỷ lệ CFO/PAT" in guidance


def test_hard_comprehensive_cashflow_coverage_requires_prior_cfo_pat_direction():
    payload = _profitability_payload_with_cfo_history()
    plan = _comprehensive_profitability_plan()
    current_only = (
        "CFO/PAT năm 2025 = 7.690.701.645.261 / 9.359.349.635.629 = 82,17%."
    )
    violations = financial_answer_violations(
        current_only,
        payload,
        user_query="Đánh giá khả năng sinh lời của công ty",
        plan_context=plan,
    )
    assert "profitability_coverage:cfo_pat_comparative_direction_required" in violations

    narrow_violations = financial_answer_violations(
        current_only,
        payload,
        user_query="Đánh giá dòng tiền",
        plan_context=plan,
    )
    assert "profitability_coverage:cfo_pat_comparative_direction_required" not in narrow_violations


def test_profitability_context_includes_prior_cfo_pat_and_amount_changes():
    facts = _profitability_payload_with_cfo_history()
    context = synth_runner._canonical_profitability_metric_ledger(facts)

    assert context["metrics"]["cfo_to_net_profit"]["value"] == "82.17"
    assert context["comparatives"]["cfo_to_net_profit_previous"]["value"] == "95.50"
    assert context["comparatives"]["cfo_to_net_profit_change"]["value"] == "-13.33"
    assert context["comparatives"]["cfo_growth"]["value"] == "-13.06"
    assert context["comparatives"]["net_profit_growth"]["value"] == "1.05"
    assert "answer" not in context


def test_canonical_profitability_metrics_add_optional_prior_comparatives():
    result = canonical_profitability_metrics(
        _profitability_payload_with_prior_period()
    )

    assert result["inputs"]["net_profit_previous"]["fact_id"] == "pat-2024"
    assert result["inputs"]["net_revenue_previous"]["fact_id"] == "net-revenue-2024"
    assert result["comparatives"]["net_margin_previous"]["value"] == "18.28"
    assert result["comparatives"]["net_margin_change"] == {
        "value": "-0.62",
        "unit": "percentage_points",
        "formula": "net_margin - net_margin_previous",
        "direction": "decrease",
    }
    assert result["comparatives"]["net_profit_growth"]["value"] == "1.05"
    assert result["comparatives"]["net_revenue_growth"]["value"] == "4.57"


def test_canonical_profitability_metrics_add_all_three_margin_comparatives():
    result = canonical_profitability_metrics(
        _profitability_payload_with_full_margin_history()
    )

    assert result["inputs"]["gross_profit_current"]["fact_id"] == "gross-profit-2025"
    assert result["inputs"]["gross_profit_previous"]["fact_id"] == "gross-profit-2024"
    assert (
        result["inputs"]["operating_profit_current"]["fact_id"]
        == "operating-profit-2025"
    )
    assert (
        result["inputs"]["operating_profit_previous"]["fact_id"]
        == "operating-profit-2024"
    )
    assert result["metrics"]["gross_margin"]["value"] == "44.46"
    assert result["metrics"]["operating_margin"]["value"] == "21.47"
    assert result["comparatives"]["gross_margin_previous"]["value"] == "45.42"
    assert result["comparatives"]["gross_margin_change"] == {
        "value": "-0.96",
        "unit": "percentage_points",
        "formula": "gross_margin - gross_margin_previous",
        "direction": "decrease",
    }
    assert result["comparatives"]["operating_margin_previous"]["value"] == "22.01"
    assert result["comparatives"]["operating_margin_change"] == {
        "value": "-0.54",
        "unit": "percentage_points",
        "formula": "operating_margin - operating_margin_previous",
        "direction": "decrease",
    }


def test_optional_cfo_is_omitted_when_its_unit_is_incompatible():
    payload = _profitability_payload_with_cfo()
    payload["retrieval_facts"]["profitability"]["facts"][-1]["unit"] = "USD"

    result = canonical_profitability_metrics(payload)

    assert result["status"] == "complete"
    assert "cfo" not in result["inputs"]
    assert "cfo_to_net_profit" not in result["metrics"]


def test_canonical_profitability_metrics_fail_closed_when_opening_fact_is_missing():
    payload = deepcopy(_profitability_payload())
    facts = payload["retrieval_facts"]["profitability"]["facts"]
    facts[:] = [
        fact
        for fact in facts
        if not (
            fact["period_role"] == "previous"
            and "Vốn chủ sở hữu" in fact["item_name"]
        )
    ]

    assert canonical_profitability_metrics(payload) == {}


def test_deterministic_net_margin_does_not_substitute_gross_revenue():
    payload = deepcopy(_profitability_payload())
    facts = payload["retrieval_facts"]["profitability"]["facts"]
    net_revenue = next(fact for fact in facts if "Doanh thu thuần" in fact["item_name"])
    net_revenue["item_name"] = "Doanh thu bán hàng và cung cấp dịch vụ | 2025 VND"

    assert canonical_profitability_metrics(payload) == {}


def test_deterministic_metrics_ignore_retained_earnings_and_current_asset_distractors():
    payload = deepcopy(_profitability_payload())
    facts = payload["retrieval_facts"]["profitability"]["facts"]
    shared = {
        "company": "Công ty Cổ phần Sữa Việt Nam",
        "fiscal_year": "2025",
        "period": "2025",
        "period_role": "current",
        "unit": "VND",
        "status": "found",
        "source": "/data/vnm-separate-report.md",
    }
    facts.extend(
        [
            {
                **shared,
                "fact_id": "retained-earnings",
                "item_name": "Lợi nhuận sau thuế chưa phân phối | 2025 VND",
                "parsed_value": "8342152103924",
                "value": "8.342.152.103.924",
            },
            {
                **shared,
                "fact_id": "current-assets",
                "item_name": "Tổng tài sản ngắn hạn | 31/12/2025 VND",
                "parsed_value": "27309234148199",
                "value": "27.309.234.148.199",
            },
            {
                **shared,
                "fact_id": "domestic-net-revenue",
                "item_name": "Doanh thu thuần | Trong nước 2025 VND",
                "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                "parsed_value": "45886078014025",
                "value": "45.886.078.014.025",
            },
        ]
    )

    result = canonical_profitability_metrics(payload)

    assert result["metrics"]["net_margin"]["value"] == "17.66"
    assert result["metrics"]["roa"]["value"] == "20.04"


def test_profitability_prompt_states_accounting_and_benchmark_contracts():
    instruction = AGENT_PROFILES["agent_profitability"]["system_instruction"]

    assert "chính là lãi ròng/net profit" in instruction
    assert "BÌNH QUÂN đầu-cuối kỳ" in instruction
    assert "TỬ SỐ ROA/ROE" in instruction
    assert 'KHÔNG phải "Lợi nhuận thuần từ hoạt động kinh doanh"' in instruction
    assert "LNST / DOANH THU THUẦN" in instruction
    assert "biên lợi nhuận ròng hai kỳ" in instruction
    assert "biên gộp, biên hoạt động và biên ròng" in instruction
    assert "kèm thay đổi theo điểm phần trăm" in instruction
    assert "KHÔNG tự động là EBIT" in instruction
    assert "Tách rõ BIẾN ĐỘNG GIÁ TRỊ" in instruction
    assert "không dùng chiều của biên" in instruction
    assert "nêu rõ benchmark đối chiếu" in instruction
    assert "NGAY TRONG NHẬN ĐỊNH" in instruction
    assert "reporting_basis" in instruction
    assert "linked_parent_fact_id" in instruction
    assert 'chỉ diễn giải theo "trong đó"' in instruction


def test_cashflow_prompt_requires_cfo_pat_and_preserves_flow_period_labels():
    instruction = AGENT_PROFILES["agent_cashflow_analysis"]["system_instruction"]

    assert "so sánh trực tiếp CFO/PAT" in instruction
    assert "13,06%" in instruction
    assert "không thay PAT bằng EBIT" in instruction
    assert "CFO/CFI/CFF là FLOW" in instruction
    assert 'KHÔNG ghi "cuối kỳ/đầu kỳ"' in instruction
    assert "CFO đổi dấu giữa hai kỳ" in instruction
    assert "chênh lệch tuyệt đối" in instruction


def test_efficiency_prompt_uses_average_asset_turnover_contract():
    instruction = AGENT_PROFILES["agent_efficiency"]["system_instruction"]

    assert "doanh thu thuần / tổng tài sản bình quân đầu-cuối kỳ" in instruction
    assert "không được gọi thiếu doanh thu" in instruction
    assert "benchmark đối chiếu" in instruction
    assert 'Không kết luận vòng quay tài sản "cải thiện/suy giảm' in instruction


def test_planner_prompt_matches_canonical_profitability_axis_expansion():
    instruction = AGENT_PROFILES["agent_planner"]["system_instruction"]

    assert "ĐÁNH GIÁ/PHÂN TÍCH TỔNG HỢP KHẢ NĂNG SINH LỜI" in instruction
    assert "`agent_profitability`" in instruction
    assert "`agent_cashflow_analysis`" in instruction
    assert "`agent_efficiency`" in instruction
    assert "Chỉ thêm `agent_liquidity_solvency`" in instruction
    assert "KHÔNG áp dụng cho lookup" in instruction
