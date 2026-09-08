"""Regression coverage for diagnostic financial-answer consistency checks."""

from agents import synth_runner
from schemas.financial_validation import (
    cashflow_fact_values,
    cashflow_identity_violation,
    financial_answer_violations,
)


def _payload():
    facts = [
        {
            "item_name": "Lưu chuyển tiền thuần từ hoạt động kinh doanh | 2025 VND",
            "period_role": "current",
            "parsed_value": "7690701645261",
            "value": "7.690.701.645.261",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Lưu chuyển tiền thuần từ hoạt động đầu tư | 2025 VND",
            "period_role": "current",
            "parsed_value": "2608662395779",
            "value": "2.608.662.395.779",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Lưu chuyển thuần từ hoạt động tài chính | 2025 VND",
            "period_role": "current",
            "parsed_value": "-10660261630750",
            "value": "(10.660.261.630.750)",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Lưu chuyển tiền thuần trong năm | 2025 VND",
            "period_role": "current",
            "parsed_value": "-360897589710",
            "value": "(360.897.589.710)",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Doanh thu thuần về bán hàng và cung cấp dịch vụ | 2025 VND",
            "parsed_value": "52991496309263",
            "value": "52.991.496.309.263",
            "unit": "VND",
            "status": "found",
        },
        {
            "item_name": "Giá vốn hàng bán và dịch vụ cung cấp | 2025 VND",
            "parsed_value": "29430136161346",
            "value": "29.430.136.161.346",
            "unit": "VND",
            "status": "found",
        },
    ]
    return {
        "analysis_outputs": {"agent_cashflow_analysis": {"answer": "...", "requirements": []}},
        "retrieval_facts": {"cashflow": {"facts": facts}},
    }


def test_cashflow_values_and_identity_preserve_accounting_signs():
    values = cashflow_fact_values(_payload())

    assert values["cfi"] > 0
    assert values["cff"] < 0
    assert cashflow_identity_violation(_payload()) == ""


def test_answer_rejects_reversed_cfi_sign_and_unrequested_vnd_scaling():
    violations = financial_answer_violations(
        "CFI khoảng 2,61 tỷ VND là dòng tiền ra; CFO dương.",
        _payload(),
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert "unexpected_compact_vnd_unit" in violations
    assert "cashflow_sign:cfi:expected_positive" in violations


def test_answer_rejects_false_missing_metrics_already_in_fact_ledger():
    violations = financial_answer_violations(
        "Do thiếu dữ liệu doanh thu thuần và giá vốn hàng bán nên không thể tính biên gộp.",
        _payload(),
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert "false_missing:net_revenue" in violations
    assert "false_missing:cogs" in violations


def test_answer_accepts_exact_vnd_values_and_correct_cashflow_signs():
    violations = financial_answer_violations(
        (
            "CFO dương 7.690.701.645.261 VND; "
            "CFI dương 2.608.662.395.779 VND; "
            "CFF âm 10.660.261.630.750 VND."
        ),
        _payload(),
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert violations == []


def test_sign_validator_scopes_each_metric_in_a_shared_sentence():
    violations = financial_answer_violations(
        "CFO dương 7.690.701.645.261 VND, CFI dương và CFF âm.",
        _payload(),
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert violations == []


def test_shared_cashflow_sign_applies_to_each_joined_metric():
    violations = financial_answer_violations(
        "CFI và CFF âm lớn, gây áp lực lên tiền mặt.",
        _payload(),
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert "cashflow_sign:cfi:expected_positive" in violations


def test_cashflow_effect_direction_uses_algebraic_sum_of_named_flows():
    consistent = financial_answer_violations(
        "CFI dương nhưng CFF âm, làm giảm tiền mặt ròng.",
        _payload(),
        user_query="Đánh giá tình hình tài chính công ty",
    )
    assert "cashflow_effect_direction:expected_increase" not in consistent

    wrong = financial_answer_violations(
        "CFO, CFI và CFF làm tăng tiền mặt ròng.",
        _payload(),
        user_query="Đánh giá tình hình tài chính công ty",
    )
    assert "cashflow_effect_direction:expected_decrease" in wrong


def test_apec_positive_cfi_plus_cff_cannot_be_described_as_reducing_cash():
    payload = _payload()
    facts = payload["retrieval_facts"]["cashflow"]["facts"]
    values = {
        "Lưu chuyển tiền thuần từ hoạt động kinh doanh": "36158929436",
        "Lưu chuyển tiền thuần từ hoạt động đầu tư": "40218532929",
        "Lưu chuyển thuần từ hoạt động tài chính": "-38988400000",
        "Lưu chuyển tiền thuần trong năm": "37389062365",
    }
    for fact in facts:
        for label, value in values.items():
            if label in fact["item_name"]:
                fact["parsed_value"] = value
                fact["value"] = value

    violations = financial_answer_violations(
        "CFI dương nhưng CFF âm lớn, làm giảm tiền mặt ròng.",
        payload,
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert "cashflow_effect_direction:expected_increase" in violations


def test_negative_cff_that_becomes_less_negative_is_an_amount_increase():
    payload = _payload()
    payload["retrieval_facts"]["cashflow"]["facts"].append(
        {
            "item_name": "Lưu chuyển thuần từ hoạt động tài chính | 2024 VND",
            "period_role": "previous",
            "parsed_value": "-71573000000",
            "value": "(71.573.000.000)",
            "unit": "VND",
            "status": "found",
        }
    )
    current_cff = payload["retrieval_facts"]["cashflow"]["facts"][2]
    current_cff["parsed_value"] = "-38988400000"
    current_cff["value"] = "(38.988.400.000)"

    wrong = financial_answer_violations(
        (
            "CFF: (38.988.400.000) VND - giảm so với "
            "(71.573.000.000) VND năm trước."
        ),
        payload,
        user_query="Đánh giá tình hình tài chính công ty",
    )
    correct = financial_answer_violations(
        "CFF tăng từ âm 71.573.000.000 VND lên âm 38.988.400.000 VND.",
        payload,
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert "cashflow_comparison:cff_amount_expected_tang" in wrong
    assert "cashflow_comparison:cff_amount_expected_tang" not in correct


def test_hard_analysis_uses_at_least_one_linked_note_reference():
    payload = _payload()
    payload["retrieval_facts"]["cashflow"]["facts"].append(
        {
            "item_name": "Tiền gửi ngân hàng | Năm nay",
            "period_role": "current",
            "parsed_value": "90000000000",
            "value": "90.000.000.000",
            "unit": "VND",
            "status": "found",
            "note_ref": "VI.1",
            "evidence_role": "note_detail",
            "linked_parent_item": "Tiền và các khoản tương đương tiền",
        }
    )
    plan = {"difficulty_level": "hard"}

    without_note = financial_answer_violations(
        "Tiền và các khoản tương đương tiền phản ánh thanh khoản của công ty.",
        payload,
        plan_context=plan,
    )
    with_note = financial_answer_violations(
        (
            "Theo Thuyết minh VI.1, trong tiền và các khoản tương đương tiền "
            "có 90.000.000.000 VND tiền gửi ngân hàng."
        ),
        payload,
        plan_context=plan,
    )

    assert "note_ref_coverage:linked_detail_required" in without_note
    assert "note_ref_coverage:linked_detail_required" not in with_note


def test_main_balance_totals_cannot_be_reconstructed_from_incomplete_or_nested_parts():
    payload = _payload()
    payload["retrieval_facts"]["cashflow"]["facts"].extend(
        [
            {
                "item_name": "Tổng tài sản ngắn hạn | Số cuối năm",
                "metric_label": "Tổng tài sản ngắn hạn",
                "period_role": "current",
                "parsed_value": "984330724539",
                "value": "984.330.724.539",
                "unit": "VND",
                "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                "status": "found",
            },
            {
                "item_name": "Tổng nợ ngắn hạn | Số cuối năm",
                "metric_label": "Tổng nợ ngắn hạn",
                "period_role": "current",
                "parsed_value": "566994110452",
                "value": "566.994.110.452",
                "unit": "VND",
                "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                "status": "found",
            },
        ]
    )

    violations = financial_answer_violations(
        (
            "Current Assets = 934.875.076.365 VND = tiền + phải thu + tồn kho.\n"
            "Current Liabilities = 570.004.569.474 VND = nợ ngắn hạn + "
            "khoản phải trả khác trong thuyết minh."
        ),
        payload,
        user_query="Đánh giá tình hình tài chính công ty",
    )

    assert "balance_total_value_mismatch:current_assets" in violations
    assert "balance_total_value_mismatch:current_liabilities" in violations


def test_compact_unit_is_allowed_only_when_user_requests_it():
    violations = financial_answer_violations(
        "CFO là 7.690,70 tỷ đồng.",
        _payload(),
        user_query="CFO là bao nhiêu tỷ đồng?",
    )

    assert "unexpected_compact_vnd_unit" not in violations


def test_single_quality_cycle_repairs_sign_and_scale(monkeypatch):
    corrected = {
        "status": "answer",
        "answer": (
            "CFO dương 7.690.701.645.261 VND; "
            "CFI dương 2.608.662.395.779 VND; "
            "CFF âm 10.660.261.630.750 VND."
        ),
        "followups": [],
    }
    monkeypatch.setattr(
        synth_runner,
        "_invoke_synth",
        lambda _payload: (corrected, {"total_tokens": 10}, "structured"),
    )

    repaired, usage, mode, violations, remaining, retried, accepted, _diag = (
        synth_runner._retry_synth_quality_once(
            {},
            {
                "user_query": "Đánh giá tình hình tài chính công ty",
                "system_instruction": "rules",
                "plan_json": "{}",
            },
            _payload(),
            {
                "status": "answer",
                "answer": "CFI 2,61 tỷ VND là dòng tiền ra.",
                "followups": [],
            },
        )
    )

    assert repaired == corrected
    assert usage == {"total_tokens": 10}
    assert mode == "structured"
    assert "unexpected_compact_vnd_unit" in violations
    assert "cashflow_sign:cfi:expected_positive" in violations
    assert "unexpected_compact_vnd_unit" not in remaining
    assert "cashflow_sign:cfi:expected_positive" not in remaining
    assert "missing_bluf_summary" in remaining
    assert retried is True
    assert accepted == "general_repair"


def test_single_quality_cycle_returns_model_repair_even_if_still_wrong(monkeypatch):
    monkeypatch.setattr(
        synth_runner,
        "_invoke_synth",
        lambda _payload: (
            {
                "status": "answer",
                "answer": "CFI 2,61 tỷ VND là dòng tiền ra.",
                "followups": [],
            },
            None,
            "structured",
        ),
    )

    repaired, _usage, mode, _violations, remaining, retried, accepted, _diag = (
        synth_runner._retry_synth_quality_once(
            {},
            {
                "user_query": "Đánh giá tình hình tài chính công ty",
                "system_instruction": "rules",
                "plan_json": "{}",
            },
            _payload(),
            {
                "status": "answer",
                "answer": "CFI 2,61 tỷ VND là dòng tiền ra.",
                "followups": [],
            },
        )
    )

    assert mode == "structured"
    assert remaining
    assert repaired["status"] == "answer"
    assert repaired["answer"] == "CFI 2,61 tỷ VND là dòng tiền ra."
    assert retried is True
    assert accepted == "general_repair"
