"""Exact medium-query calculations must not depend on binary floats or an LLM."""

from decimal import Decimal

from agents import synth_runner
from schemas.numbers import format_decimal_vi, parse_financial_decimal


def test_parse_financial_decimal_supports_statement_formats():
    assert parse_financial_decimal("14.592.618.979 VND") == Decimal("14592618979")
    assert parse_financial_decimal("(1.234.567)") == Decimal("-1234567")
    assert parse_financial_decimal("1.234,56") == Decimal("1234.56")
    assert parse_financial_decimal("12,5%") == Decimal("12.5")
    assert format_decimal_vi(Decimal("1234567.5000")) == "1.234.567,5"


def test_medium_two_period_change_uses_decimal_and_skips_model(monkeypatch):
    def fail_if_called(_payload):
        raise AssertionError("safe deterministic calculation must skip the model")

    monkeypatch.setattr(synth_runner, "_invoke_synth", fail_if_called)
    state = {
        "user_query": "So sánh lãi cho vay năm hiện tại và năm trước. Sự thay đổi là bao nhiêu?",
        "planner_plan": {"difficulty_level": "medium"},
        "worker_plan": {"difficulty_level": "medium", "analysis_plan": []},
        "worker_results": {
            "THUYẾT MINH BÁO CÁO TÀI CHÍNH": {
                "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                "facts": [
                    {
                        "item_name": "Lãi tiền gửi ngân hàng, Lãi cho vay",
                        "time_hint": "năm hiện tại",
                        "value": "14.592.618.979",
                        "unit": "VND",
                        "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    },
                    {
                        "item_name": "Lãi tiền gửi ngân hàng, Lãi cho vay",
                        "time_hint": "năm trước",
                        "value": "26.030.112.902",
                        "unit": "VND",
                        "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                    },
                ],
            }
        },
        "trace": [],
    }

    updates = synth_runner.run_synth(state)

    assert updates["synth_decision"]["status"] == "answer"
    assert "-11.437.493.923 VND" in updates["synth_decision"]["answer"]
    assert any(item["event"] == "synth:deterministic_decimal" for item in updates["trace"])


def test_ambiguous_equal_score_groups_do_not_choose_a_metric():
    calculation = synth_runner._deterministic_decimal_calculation(
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
