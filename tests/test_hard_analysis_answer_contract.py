"""Regression coverage for summary-first multi-agent financial answers."""

import json

from agents import planner_runner, synth_runner
from schemas.financial_validation import financial_answer_violations


def _state(*, response_mode="extractive"):
    return {
        "planner_plan": {
            "difficulty_level": "hard",
            "response_mode": response_mode,
        }
    }


def _worker_results():
    return {
        "analysis_outputs": {
            "agent_profitability": {
                "answer": "- Biên lợi nhuận ròng giảm 0,62 điểm phần trăm.",
                "requirements": [],
            },
            "agent_cashflow_analysis": {
                "answer": "- CFO dương và hỗ trợ chất lượng lợi nhuận.",
                "requirements": [],
            },
            "agent_efficiency": {
                "answer": "- Vòng quay tài sản cần được đối chiếu theo hai kỳ.",
                "requirements": [],
            },
        }
    }


def _profitability_facts():
    section = "Báo cáo kết quả hoạt động kinh doanh riêng năm 2025"
    values = (
        ("Lợi nhuận sau thuế TNDN", "current", "9359349635629"),
        ("Doanh thu thuần về bán hàng và cung cấp dịch vụ", "current", "52991496309263"),
        ("Tổng tài sản", "current", "45952496972636"),
        ("Tổng tài sản", "previous", "47448528386601"),
        ("Vốn chủ sở hữu", "current", "29264932288220"),
        ("Vốn chủ sở hữu", "previous", "30977801524404"),
    )
    return {
        "retrieval_facts": {
            "profitability": {
                "facts": [
                    {
                        "fact_id": f"fact-{index}",
                        "item_name": label,
                        "section_path": section,
                        "period_role": role,
                        "period_label": "2025" if role == "current" else "đầu năm 2025",
                        "parsed_value": value,
                        "value": value,
                        "unit": "VND",
                        "status": "found",
                        "source": "/data/vnm-congtyme.md",
                    }
                    for index, (label, role, value) in enumerate(values)
                ]
            }
        }
    }


def _profitability_facts_with_cfo():
    payload = _profitability_facts()
    payload["retrieval_facts"]["profitability"]["facts"].extend(
        [
            {
                "fact_id": "fact-net-profit-previous",
                "item_name": "Lợi nhuận sau thuế TNDN | 2024 VND",
                "section_path": "Báo cáo kết quả hoạt động kinh doanh riêng năm 2025",
                "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                "period_role": "previous",
                "period_label": "2024",
                "fiscal_year": "2025",
                "parsed_value": "9262413822949",
                "value": "9262413822949",
                "unit": "VND",
                "status": "found",
                "source": "/data/vnm-congtyme.md",
            },
            {
                "fact_id": "fact-net-revenue-previous",
                "item_name": "Doanh thu thuần về bán hàng và cung cấp dịch vụ | 2024 VND",
                "section_path": "Báo cáo kết quả hoạt động kinh doanh riêng năm 2025",
                "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                "period_role": "previous",
                "period_label": "2024",
                "fiscal_year": "2025",
                "parsed_value": "50676707912192",
                "value": "50676707912192",
                "unit": "VND",
                "status": "found",
                "source": "/data/vnm-congtyme.md",
            },
        ]
    )
    payload["retrieval_facts"]["cashflow"] = {
        "facts": [
            {
                "fact_id": "fact-cfo-current",
                "item_name": "Lưu chuyển tiền thuần từ hoạt động kinh doanh | 2025 VND",
                "section_path": "Báo cáo lưu chuyển tiền tệ riêng năm 2025",
                "table": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
                "period_role": "current",
                "period_label": "2025",
                "fiscal_year": "2025",
                "parsed_value": "7690701645261",
                "value": "7690701645261",
                "unit": "VND",
                "status": "found",
                "source": "/data/vnm-congtyme.md",
            }
        ]
    }
    return payload


def _profitability_facts_full():
    payload = _profitability_facts_with_cfo()
    facts = payload["retrieval_facts"]["profitability"]["facts"]
    shared = {
        "section_path": "Báo cáo kết quả hoạt động kinh doanh riêng năm 2025",
        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
        "fiscal_year": "2025",
        "unit": "VND",
        "status": "found",
        "source": "/data/vnm-congtyme.md",
    }
    facts.extend(
        [
            {
                **shared,
                "fact_id": "fact-gross-profit-current",
                "item_name": "Lợi nhuận gộp về bán hàng và cung cấp dịch vụ | 2025 VND",
                "period_role": "current",
                "period_label": "2025",
                "parsed_value": "23561360147917",
                "value": "23561360147917",
            },
            {
                **shared,
                "fact_id": "fact-gross-profit-previous",
                "item_name": "Lợi nhuận gộp về bán hàng và cung cấp dịch vụ | 2024 VND",
                "period_role": "previous",
                "period_label": "2024",
                "parsed_value": "23017365857504",
                "value": "23017365857504",
            },
            {
                **shared,
                "fact_id": "fact-operating-profit-current",
                "item_name": "Lợi nhuận thuần từ hoạt động kinh doanh | 2025 VND",
                "period_role": "current",
                "period_label": "2025",
                "parsed_value": "11376790113377",
                "value": "11376790113377",
            },
            {
                **shared,
                "fact_id": "fact-operating-profit-previous",
                "item_name": "Lợi nhuận thuần từ hoạt động kinh doanh | 2024 VND",
                "period_role": "previous",
                "period_label": "2024",
                "parsed_value": "11154574287978",
                "value": "11154574287978",
            },
        ]
    )
    return payload


def test_hard_contract_requires_bluf_and_every_completed_agent_aspect():
    violations = synth_runner._hard_analysis_contract_violations(
        _state(),
        {
            "status": "answer",
            "answer": "**Khả năng sinh lời**\n\nBiên ròng giảm.",
            "followups": [],
        },
        _worker_results(),
    )

    assert "missing_bluf_summary" in violations
    assert "missing_aspect:agent_cashflow_analysis" in violations
    assert "missing_aspect:agent_efficiency" in violations


def test_hard_contract_accepts_summary_first_multi_aspect_answer():
    answer = """**Tóm tắt đánh giá**

- Biên lợi nhuận thu hẹp nhưng CFO vẫn dương.

**1. Khả năng sinh lời**

- Biên ròng giảm.

**2. Dòng tiền**

- CFO dương.

**3. Hiệu quả hoạt động**

- Cần đối chiếu vòng quay tài sản.
- Giới hạn bằng chứng hiện tại chỉ ảnh hưởng nhận xét về xu hướng.
"""

    assert synth_runner._hard_analysis_contract_violations(
        _state(),
        {"status": "answer", "answer": answer, "followups": []},
        _worker_results(),
    ) == []


def test_hard_contract_allows_report_period_but_not_financial_values_in_summary():
    answer = """**Tóm tắt đánh giá**

- Trong năm 2025, khả năng sinh lời suy yếu nhưng dòng tiền kinh doanh vẫn hỗ trợ hoạt động.

**1. Khả năng sinh lời**

- Biên lợi nhuận ròng năm 2025 là 17,66%.

**2. Dòng tiền**

- CFO năm 2025 đạt 7.690.701.645.261 VND.

**3. Hiệu quả hoạt động**

- Vòng quay tài sản là 1,13 lần.
"""

    assert synth_runner._hard_analysis_contract_violations(
        _state(),
        {"status": "answer", "answer": answer, "followups": []},
        _worker_results(),
    ) == []


def test_hard_contract_flags_financial_number_and_formula_in_summary_only():
    answer = """**Tóm tắt đánh giá**

- Biên lợi nhuận ròng năm 2025 là 17,66% theo công thức 9.359 / 52.991.

**1. Khả năng sinh lời**

- Biên lợi nhuận ròng năm 2025 là 17,66%.

**2. Dòng tiền**

- CFO năm 2025 đạt 7.690.701.645.261 VND.

**3. Hiệu quả hoạt động**

- Vòng quay tài sản = 52.991 / 46.700 = 1,13 lần.
"""

    violations = synth_runner._hard_analysis_contract_violations(
        _state(),
        {"status": "answer", "answer": answer, "followups": []},
        _worker_results(),
    )

    assert "summary_contains_financial_number" in violations
    assert "summary_contains_formula" in violations


def test_synth_quality_flags_leading_evidence_boilerplate():
    answer = """Dựa trên số liệu hiện có:

**Tóm tắt đánh giá**

- Khả năng sinh lời đang chịu áp lực.

**1. Khả năng sinh lời**

- Biên ròng giảm.

**2. Dòng tiền**

- CFO dương.

**3. Hiệu quả hoạt động**

- Hiệu suất sử dụng tài sản cần theo dõi.
"""

    violations = synth_runner._collect_synth_quality_violations(
        _state(),
        {"user_query": "", "plan_json": "{}"},
        _worker_results(),
        {"status": "answer", "answer": answer, "followups": []},
    )

    assert "boilerplate_evidence_lead_in" in violations


def test_hard_contract_requires_real_aspect_headings_not_summary_mentions():
    answer = """**Tóm tắt đánh giá**

- Khả năng sinh lời, Dòng tiền và Hiệu quả hoạt động đều đã được xem xét.

Phần nội dung tổng hợp không chia thành các khía cạnh chuyên gia.
"""

    violations = synth_runner._hard_analysis_contract_violations(
        _state(),
        {"status": "answer", "answer": answer, "followups": []},
        _worker_results(),
    )

    assert "missing_aspect:agent_profitability" in violations
    assert "missing_aspect:agent_cashflow_analysis" in violations
    assert "missing_aspect:agent_efficiency" in violations


def test_hard_contract_rejects_internal_agent_labels():
    answer = """**Tóm tắt đánh giá**

- Ba khía cạnh đã được tổng hợp.

**1. Khả năng sinh lời**

- Biên ròng được phân tích bởi agent_profitability.

**2. Dòng tiền**

- CFO dương.

**3. Hiệu quả hoạt động**

- Vòng quay tài sản đạt 1,13 lần.
"""

    violations = synth_runner._hard_analysis_contract_violations(
        _state(),
        {"status": "answer", "answer": answer, "followups": []},
        _worker_results(),
    )

    assert "internal_agent_label_exposed" in violations


def test_run_synth_returns_financial_repair_without_code_layout_composition(
    monkeypatch,
):
    stale_worker_phrase = (
        "không có dữ liệu doanh thu thuần nên chưa tính được biên lợi nhuận ròng"
    )
    analysis_outputs = _worker_results()["analysis_outputs"]
    analysis_outputs["agent_profitability"]["answer"] = f"- {stale_worker_phrase}."

    initial_answer = f"""**Tóm tắt đánh giá**

- {stale_worker_phrase}.

**1. Khả năng sinh lời**

- {stale_worker_phrase}.

**2. Dòng tiền**

- CFO/PAT năm 2025 = 7.690.701.645.261 / 9.359.349.635.629 = 82,17%.

**3. Hiệu quả hoạt động**

- Vòng quay tài sản cần được đối chiếu theo hai kỳ.
"""
    repaired_answer = """**Tóm tắt đánh giá**

- Biên lợi nhuận ròng giảm từ 18,28% năm 2024 xuống 17,66% năm 2025.
- CFO tương đương 82,17% PAT năm 2025.

**1. Khả năng sinh lời**

- Biên lợi nhuận ròng = LNST / doanh thu thuần = **17,66%**.
- ROA theo tài sản bình quân đầu kỳ và cuối kỳ là **20,04%**; ROE theo vốn chủ sở hữu bình quân đầu kỳ và cuối kỳ là **31,07%**.
- So với năm 2024, doanh thu thuần tăng 4,57%, PAT tăng 1,05%; biên lợi nhuận ròng giảm từ 18,28% xuống 17,66%, tương ứng 0,62 điểm phần trăm.
- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.

**2. Dòng tiền**

- CFO/PAT năm 2025 = 7.690.701.645.261 / 9.359.349.635.629 = **82,17%**; CFO thấp hơn PAT.
"""
    # Targeted repair keeps the corrected figures AND restores the dropped section.
    targeted_answer = """**Tóm tắt đánh giá**

- Trong năm 2025, biên sinh lời thu hẹp so với năm 2024, còn dòng tiền kinh doanh chưa chuyển đổi toàn bộ lợi nhuận kế toán.
- Hiệu suất sử dụng tài sản đã được lượng hóa nhưng chưa có cơ sở để kết luận xu hướng.

**1. Khả năng sinh lời**

- Biên lợi nhuận ròng = LNST / doanh thu thuần = **17,66%**.
- ROA theo tài sản bình quân đầu kỳ và cuối kỳ là **20,04%**; ROE theo vốn chủ sở hữu bình quân đầu kỳ và cuối kỳ là **31,07%**.
- So với năm 2024, doanh thu thuần tăng 4,57%, PAT tăng 1,05%; biên lợi nhuận ròng giảm từ 18,28% xuống 17,66%, tương ứng 0,62 điểm phần trăm.

**2. Dòng tiền**

- CFO/PAT năm 2025 = 7.690.701.645.261 / 9.359.349.635.629 = **82,17%**; CFO thấp hơn PAT.

**3. Hiệu quả hoạt động**

- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.
"""
    responses = iter(
        [
            ({"status": "answer", "answer": initial_answer, "followups": []}, None, "structured"),
            ({"status": "answer", "answer": repaired_answer, "followups": []}, None, "structured"),
            ({"status": "answer", "answer": targeted_answer, "followups": []}, None, "structured"),
        ]
    )
    monkeypatch.setattr(synth_runner, "_invoke_synth", lambda _payload: next(responses))

    retrieval_payload = _profitability_facts_with_cfo()["retrieval_facts"]
    updates = synth_runner.run_synth(
        {
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "planner_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
            },
            "worker_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
                "analysis_plan": [
                    {
                        "agent": "agent_profitability",
                        "objective": (
                            "So sánh kỳ hiện tại với kỳ trước; tính biên lợi nhuận "
                            "ròng, ROA và ROE."
                        ),
                    },
                    {
                        "agent": "agent_cashflow_analysis",
                        "objective": "Đánh giá chất lượng lợi nhuận qua CFO/PAT.",
                    },
                    {
                        "agent": "agent_efficiency",
                        "objective": "Đánh giá hiệu quả sử dụng tài sản.",
                    },
                ],
            },
            "worker_results": {
                **retrieval_payload,
                **analysis_outputs,
            },
            "trace": [],
        }
    )

    answer = updates["synth_decision"]["answer"]
    assert stale_worker_phrase not in answer.lower()
    assert "**17,66%**" in answer
    assert "**1. Khả năng sinh lời**" in answer
    assert "**2. Dòng tiền**" in answer
    assert "**3. Hiệu quả hoạt động**" in answer
    assert "agent_" not in answer
    quality = next(
        item for item in updates["trace"] if item.get("event") == "synth:quality_check"
    )
    # The general repair fixed the numbers but dropped a section; one targeted
    # repair recovers both without any code-composed prose.
    assert quality["accepted_candidate"] == "targeted_repair"
    assert quality["repair_rounds"] == 2
    assert "summary_contains_financial_number" not in quality["remaining_violations"]
    assert "summary_contains_formula" not in quality["remaining_violations"]
    assert not any(
        str(item).startswith("missing_aspect:")
        for item in quality["remaining_violations"]
    )
    assert not any("fallback" in item.get("event", "") for item in updates["trace"])


def test_run_synth_keeps_unstructured_model_repair_and_only_reports_diagnostics(
    monkeypatch,
):
    stale_phrase = "không có dữ liệu doanh thu thuần nên chưa tính được biên ròng"
    initial_answer = f"""**Tóm tắt đánh giá**

- {stale_phrase}.

**1. Khả năng sinh lời**

- {stale_phrase}.

**2. Dòng tiền**

- CFO khoảng 7,69 nghìn tỷ VND.

**3. Hiệu quả hoạt động**

- Doanh thu khoảng 52,99 nghìn tỷ VND.
"""
    financially_repaired_but_unstructured = """### Đánh giá khả năng sinh lời (agent_profitability)

- Biên lợi nhuận ròng = 9.359.349.635.629 / 52.991.496.309.263 = **17,66%**.
- ROA = 9.359.349.635.629 / tài sản bình quân đầu kỳ và cuối kỳ 46.700.512.679.619 = **20,04%**.
- ROE = 9.359.349.635.629 / vốn chủ sở hữu bình quân đầu kỳ và cuối kỳ 30.121.867.406.312 = **31,07%**.
- So với năm 2024, doanh thu thuần tăng 4,57%, PAT tăng 1,05% và biên ròng giảm 0,62 điểm phần trăm.
- CFO/PAT năm 2025 = 7.690.701.645.261 / 9.359.349.635.629 = **82,17%**; CFO thấp hơn PAT.
- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.
"""
    responses = iter(
        [
            ({"status": "answer", "answer": initial_answer, "followups": []}, None, "structured"),
            (
                {
                    "status": "answer",
                    "answer": financially_repaired_but_unstructured,
                    "followups": [],
                },
                None,
                "structured",
            ),
            # The targeted repair is attempted (the rewrite dropped two sections)
            # but comes back unusable, so the general repair stays the best
            # scoring candidate and is returned verbatim.
            ({"status": "answer", "answer": "", "followups": []}, None, "structured"),
        ]
    )
    monkeypatch.setattr(synth_runner, "_invoke_synth", lambda _payload: next(responses))

    analysis_outputs = _worker_results()["analysis_outputs"]
    analysis_outputs["agent_profitability"]["answer"] = f"- {stale_phrase}."
    analysis_outputs["agent_cashflow_analysis"]["answer"] = (
        "- CFO khoảng 7,69 nghìn tỷ VND; cần đối chiếu với PAT."
    )
    analysis_outputs["agent_efficiency"]["answer"] = (
        "- Doanh thu khoảng 52,99 nghìn tỷ VND; vòng quay tài sản 1,13 lần."
    )
    retrieval_payload = _profitability_facts_with_cfo()["retrieval_facts"]
    worker_plan = {
        "difficulty_level": "hard",
        "response_mode": "extractive",
        "analysis_plan": [
            {
                "agent": "agent_profitability",
                "objective": "So sánh hai kỳ; tính biên ròng, ROA và ROE.",
            },
            {
                "agent": "agent_cashflow_analysis",
                "objective": "Đánh giá chất lượng lợi nhuận qua CFO/PAT.",
            },
            {
                "agent": "agent_efficiency",
                "objective": "Đánh giá hiệu quả sử dụng tài sản.",
            },
        ],
    }

    updates = synth_runner.run_synth(
        {
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "planner_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
            },
            "worker_plan": worker_plan,
            "worker_results": {**retrieval_payload, **analysis_outputs},
            "trace": [],
        }
    )

    decision = updates["synth_decision"]
    answer = decision["answer"]
    assert decision["status"] == "answer"
    assert answer == financially_repaired_but_unstructured
    assert "agent_profitability" in answer
    assert stale_phrase not in answer.lower()
    quality = next(
        item for item in updates["trace"] if item.get("event") == "synth:quality_check"
    )
    assert quality["accepted_candidate"] == "general_repair"
    assert quality["repair_rounds"] == 2
    assert "missing_bluf_summary" in quality["remaining_violations"]
    assert "internal_agent_label_exposed" in quality["remaining_violations"]


def test_run_synth_does_not_fail_closed_for_remaining_hard_violations(monkeypatch):
    responses = iter(
        [
            (
                {
                    "status": "answer",
                    "answer": "Không có dữ liệu chi tiết; CFO khoảng 7,69 nghìn tỷ VND.",
                    "followups": [],
                },
                None,
                "structured",
            ),
            (
                {
                    "status": "answer",
                    "answer": (
                        "CFO năm 2025 là 7.690.701.645.261 VND; vòng quay tài "
                        "sản là 1,13 lần."
                    ),
                    "followups": [],
                },
                None,
                "structured",
            ),
        ]
    )
    monkeypatch.setattr(synth_runner, "_invoke_synth", lambda _payload: next(responses))

    facts = _profitability_facts_with_cfo()["retrieval_facts"]
    updates = synth_runner.run_synth(
        {
            "user_query": "Đánh giá dòng tiền và hiệu quả hoạt động",
            "planner_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
            },
            "worker_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
                "analysis_plan": [
                    {
                        "agent": "agent_cashflow_analysis",
                        "objective": "Đánh giá CFO.",
                    },
                    {
                        "agent": "agent_efficiency",
                        "objective": "Đánh giá vòng quay tài sản.",
                    },
                ],
            },
            "worker_results": {
                **facts,
                "agent_cashflow_analysis": {
                    "answer": "- CFO khoảng 7,69 nghìn tỷ VND.",
                    "requirements": [],
                },
                "agent_efficiency": {
                    "answer": "- Doanh thu khoảng 52,99 nghìn tỷ VND.",
                    "requirements": [],
                },
            },
            "trace": [],
        }
    )

    assert updates["synth_decision"]["status"] == "answer"
    assert updates["synth_decision"]["answer"] == (
        "CFO năm 2025 là 7.690.701.645.261 VND; vòng quay tài sản là 1,13 lần."
    )
    quality = next(
        item for item in updates["trace"] if item.get("event") == "synth:quality_check"
    )
    assert quality["accepted_candidate"] == "general_repair"
    assert quality["remaining_violations"]


def test_run_synth_prefers_initial_over_repair_that_adds_false_missing_claims(
    monkeypatch,
):
    """The rewrite restores every section but claims equity/ROE are missing when
    both are in the facts.  Correctness-first selection keeps the earlier model
    answer verbatim; code still never composes prose of its own."""

    initial_answer = """**Tóm tắt đánh giá**

- Biên lợi nhuận năm 2025 thu hẹp.

**1. Khả năng sinh lời**

- Biên lợi nhuận ròng năm 2025 là 17,66%.
"""
    invalid_e2e3_answer = """**Tóm tắt đánh giá**

- Các biên lợi nhuận đều giảm so với năm trước.
- ROA xấp xỉ 20%, cho thấy hiệu quả sử dụng tài sản tốt và lợi nhuận mạnh.
- CFO/PAT giảm từ 95,5% xuống 82,2%.
- Thiếu dữ liệu tổng vốn chủ sở hữu đầu và cuối kỳ nên không thể tính ROE.

**1. Khả năng sinh lời**

- Biên lợi nhuận ròng = 9.359.349.635.629 / 52.991.496.309.263 = **17,66%**.
- ROA = 9.359.349.635.629 / tài sản bình quân đầu kỳ và cuối kỳ 46.700.512.679.619 = **20,04%**; ROA 20% cho thấy khả năng sinh lời mạnh.
- Thiếu dữ liệu vốn chủ sở hữu bình quân nên không thể tính ROE.

**2. Dòng tiền**

- CFO/PAT năm 2025 = 7.690.701.645.261 / 9.359.349.635.629 = **82,17%**, giảm so với mức 95,5% năm 2024.

**3. Hiệu quả hoạt động**

- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.
- Vòng quay tài sản cải thiện so với năm trước vì doanh thu tăng trong khi tài sản giảm nhẹ.

**Điểm cần theo dõi**

- Cần bổ sung vốn chủ sở hữu để tính ROE.
"""
    responses = iter(
        [
            ({"status": "answer", "answer": initial_answer, "followups": []}, None, "structured"),
            ({"status": "answer", "answer": invalid_e2e3_answer, "followups": []}, None, "structured"),
            ({"status": "answer", "answer": invalid_e2e3_answer, "followups": []}, None, "structured"),
        ]
    )
    monkeypatch.setattr(synth_runner, "_invoke_synth", lambda _payload: next(responses))

    analysis_outputs = _worker_results()["analysis_outputs"]
    facts = _profitability_facts_full()["retrieval_facts"]
    worker_plan = {
        "difficulty_level": "hard",
        "response_mode": "extractive",
        "analysis_plan": [
            {
                "agent": "agent_profitability",
                "objective": "So sánh hai kỳ; tính biên ròng, ROA và ROE.",
            },
            {
                "agent": "agent_cashflow_analysis",
                "objective": "Đánh giá chất lượng lợi nhuận qua CFO/PAT.",
            },
            {
                "agent": "agent_efficiency",
                "objective": "Đánh giá hiệu quả sử dụng tài sản.",
            },
        ],
    }
    updates = synth_runner.run_synth(
        {
            "user_query": "Đánh giá khả năng sinh lời của công ty",
            "planner_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
            },
            "worker_plan": worker_plan,
            "worker_results": {**facts, **analysis_outputs},
            "trace": [],
        }
    )

    decision = updates["synth_decision"]
    answer = decision["answer"]
    assert decision["status"] == "answer"
    # Verbatim model output, never a code-composed blend of the two candidates.
    assert answer == initial_answer
    assert "không thể tính ROE" not in answer
    quality = next(
        item for item in updates["trace"] if item.get("event") == "synth:quality_check"
    )
    assert quality["accepted_candidate"] == "initial"
    assert quality["selection_reason"] == "correctness_first_lexicographic"
    scores = quality["candidate_scores"]
    assert scores["general_repair"][0] > scores["initial"][0]
    assert quality["remaining_violations"]


def test_grounded_interpretation_does_not_use_hard_analysis_contract():
    assert synth_runner._hard_analysis_contract_violations(
        _state(response_mode="grounded_interpretation"),
        {"status": "answer", "answer": "Dữ liệu trích xuất", "followups": []},
        _worker_results(),
    ) == []


def test_profitability_context_exposes_closed_average_basis_without_prose():
    facts = _profitability_facts()
    context = synth_runner._canonical_profitability_metric_ledger(facts)

    assert context["status"] == "complete"
    assert context["metrics"]["net_margin"]["value"] == "17.66"
    assert context["metrics"]["roa"]["value"] == "20.04"
    assert context["metrics"]["roe"]["value"] == "31.07"
    assert context["metrics"]["roa"]["basis"] == "average_opening_closing"
    assert "answer" not in context


def test_profitability_context_includes_cfo_pat_and_comparatives_without_prose():
    facts = _profitability_facts_with_cfo()
    context = synth_runner._canonical_profitability_metric_ledger(facts)

    assert context["metrics"]["cfo_to_net_profit"]["value"] == "82.17"
    assert context["comparatives"]["net_revenue_growth"]["value"] == "4.57"
    assert context["comparatives"]["net_profit_growth"]["value"] == "1.05"
    assert context["comparatives"]["net_margin_change"]["value"] == "-0.62"
    assert context["metrics"]["asset_turnover"]["value"] == "1.13"
    assert "answer" not in context


def test_broad_profitability_plan_normalizes_primary_metric_coverage():
    plan, added = planner_runner._expand_broad_profitability_axes(
        {"user_query": "Đánh giá khả năng sinh lời của công ty"},
        {
            "difficulty_level": "hard",
            "response_mode": "extractive",
            "analysis_axes": [
                {
                    "axis": "agent_profitability",
                    "objective": "Đánh giá kết quả lợi nhuận.",
                }
            ],
        },
    )

    objective = plan["analysis_axes"][0]["objective"].lower()
    assert "biên lợi nhuận ròng" in objective
    assert "roa" in objective
    assert "roe" in objective
    assert added == ["agent_cashflow_analysis", "agent_efficiency"]


def test_run_synth_keeps_initial_when_every_repair_drops_required_aspects(monkeypatch):
    """A rewrite that loses required sections while only fixing a presentation
    problem must not become final.  The targeted repair gets one chance to
    restore them; when it fails too, the earlier model answer is kept."""

    initial_answer = """**Tóm tắt đánh giá**

- CFO năm 2025 đạt 7.690.701.645.261 VND và vòng quay tài sản 1,13 lần.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND (nguồn agent_cashflow_analysis).

*Nhận xét*:
- Dòng tiền kinh doanh dương.

**2. Hiệu quả hoạt động**

- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.

*Nhận xét*:
- Hiệu suất sử dụng tài sản ổn định.
"""
    # The repair strips the internal agent label but collapses to one aspect and
    # resolves no content-level finding.
    repaired_answer = """**Tóm tắt đánh giá**

- CFO năm 2025 đạt 7.690.701.645.261 VND.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND.

*Nhận xét*:
- Dòng tiền kinh doanh dương.
"""
    responses = iter(
        [
            ({"status": "answer", "answer": initial_answer, "followups": []}, None, "structured"),
            ({"status": "answer", "answer": repaired_answer, "followups": []}, None, "structured"),
            # Targeted repair also fails to bring the second aspect back.
            ({"status": "answer", "answer": repaired_answer, "followups": []}, None, "structured"),
        ]
    )
    monkeypatch.setattr(synth_runner, "_invoke_synth", lambda _payload: next(responses))

    facts = _profitability_facts_with_cfo()["retrieval_facts"]
    updates = synth_runner.run_synth(
        {
            "user_query": "Đánh giá dòng tiền và hiệu quả hoạt động",
            "planner_plan": {"difficulty_level": "hard", "response_mode": "extractive"},
            "worker_plan": {
                "difficulty_level": "hard",
                "response_mode": "extractive",
                "analysis_plan": [
                    {"agent": "agent_cashflow_analysis", "objective": "Đánh giá CFO."},
                    {"agent": "agent_efficiency", "objective": "Đánh giá vòng quay tài sản."},
                ],
            },
            "worker_results": {
                **facts,
                "agent_cashflow_analysis": {
                    "answer": "- CFO năm 2025 = 7.690.701.645.261 VND.",
                    "requirements": [],
                },
                "agent_efficiency": {
                    "answer": "- Vòng quay tài sản = 1,13 lần.",
                    "requirements": [],
                },
            },
            "trace": [],
        }
    )

    quality = next(
        item for item in updates["trace"] if item.get("event") == "synth:quality_check"
    )
    assert quality["accepted_candidate"] == "initial"
    assert quality["repair_rounds"] == 2

    answer = updates["synth_decision"]["answer"]
    assert "**1. Dòng tiền**" in answer
    assert "**2. Hiệu quả hoạt động**" in answer
    assert not any(
        str(item).startswith("missing_aspect:")
        for item in quality["remaining_violations"]
    )


def _two_aspect_state(answer_facts, *, trace=None):
    return {
        "user_query": "Đánh giá dòng tiền và hiệu quả hoạt động",
        "planner_plan": {"difficulty_level": "hard", "response_mode": "extractive"},
        "worker_plan": {
            "difficulty_level": "hard",
            "response_mode": "extractive",
            "analysis_plan": [
                {"agent": "agent_cashflow_analysis", "objective": "Đánh giá CFO."},
                {"agent": "agent_efficiency", "objective": "Đánh giá vòng quay tài sản."},
            ],
        },
        "worker_results": {
            **answer_facts,
            "agent_cashflow_analysis": {
                "answer": "- CFO năm 2025 = 7.690.701.645.261 VND.",
                "requirements": [],
            },
            "agent_efficiency": {
                "answer": "- Vòng quay tài sản = 1,13 lần.",
                "requirements": [],
            },
        },
        "trace": list(trace or []),
    }


_TWO_ASPECT_COMPLETE_ANSWER = """**Tóm tắt đánh giá**

- Trong năm 2025, dòng tiền kinh doanh dương và hiệu quả sử dụng tài sản được duy trì.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND.

**2. Hiệu quả hoạt động**

- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.
"""


def test_run_synth_does_not_call_targeted_repair_when_general_keeps_aspects(monkeypatch):
    """The third Synth call exists only to undo an aspect regression, so a
    general repair that keeps every section must end the cycle at two calls."""

    initial_answer = """**Tóm tắt đánh giá**

- CFO năm 2025 đạt 7.690.701.645.261 VND và vòng quay tài sản 1,13 lần.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND (nguồn agent_cashflow_analysis).

**2. Hiệu quả hoạt động**

- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.
"""
    responses = iter(
        [
            ({"status": "answer", "answer": initial_answer, "followups": []}, None, "structured"),
            (
                {"status": "answer", "answer": _TWO_ASPECT_COMPLETE_ANSWER, "followups": []},
                None,
                "structured",
            ),
        ]
    )
    calls = {"n": 0}

    def _fake_invoke(_payload):
        calls["n"] += 1
        return next(responses)

    monkeypatch.setattr(synth_runner, "_invoke_synth", _fake_invoke)

    updates = synth_runner.run_synth(
        _two_aspect_state(_profitability_facts_with_cfo()["retrieval_facts"])
    )

    assert calls["n"] == 2
    quality = next(
        item for item in updates["trace"] if item.get("event") == "synth:quality_check"
    )
    assert quality["repair_rounds"] == 1
    assert quality["accepted_candidate"] == "general_repair"
    assert "targeted_repair" not in quality["candidate_scores"]
    assert updates["synth_decision"]["answer"] == _TWO_ASPECT_COMPLETE_ANSWER


def test_run_synth_keeps_general_repair_when_targeted_reintroduces_false_missing(
    monkeypatch,
):
    """Restoring a dropped section does not buy the right to re-assert that data
    already in the facts is missing: the section-complete targeted answer loses
    to the shorter but factually clean general repair."""

    stale_phrase = "không có dữ liệu doanh thu thuần nên chưa tính được vòng quay tài sản"
    initial_answer = f"""**Tóm tắt đánh giá**

- CFO năm 2025 đạt 7.690.701.645.261 VND.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND.

**2. Hiệu quả hoạt động**

- {stale_phrase}.
"""
    general_answer = """**Tóm tắt đánh giá**

- CFO năm 2025 đạt 7.690.701.645.261 VND; doanh thu thuần 52.991.496.309.263 VND.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND.
- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.
"""
    targeted_answer = f"""**Tóm tắt đánh giá**

- CFO năm 2025 đạt 7.690.701.645.261 VND.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND.

**2. Hiệu quả hoạt động**

- {stale_phrase}.
"""
    responses = iter(
        [
            ({"status": "answer", "answer": initial_answer, "followups": []}, None, "structured"),
            ({"status": "answer", "answer": general_answer, "followups": []}, None, "structured"),
            ({"status": "answer", "answer": targeted_answer, "followups": []}, None, "structured"),
        ]
    )
    monkeypatch.setattr(synth_runner, "_invoke_synth", lambda _payload: next(responses))

    updates = synth_runner.run_synth(
        _two_aspect_state(_profitability_facts_with_cfo()["retrieval_facts"])
    )

    quality = next(
        item for item in updates["trace"] if item.get("event") == "synth:quality_check"
    )
    scores = quality["candidate_scores"]
    assert quality["repair_rounds"] == 2
    assert scores["targeted_repair"][0] > scores["general_repair"][0]
    assert scores["targeted_repair"][1] < scores["general_repair"][1]
    assert quality["accepted_candidate"] == "general_repair"
    assert updates["synth_decision"]["answer"] == general_answer


def test_run_synth_sums_usage_across_all_three_synth_calls(monkeypatch):
    """Token accounting must cover the targeted repair as well, otherwise the
    third call is invisible in cost reporting."""

    initial_answer = """**Tóm tắt đánh giá**

- CFO năm 2025 đạt 7.690.701.645.261 VND và vòng quay tài sản 1,13 lần.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND (nguồn agent_cashflow_analysis).

**2. Hiệu quả hoạt động**

- Vòng quay tài sản = 52.991.496.309.263 / 46.700.512.679.619 = **1,13 lần**.
"""
    dropped_answer = """**Tóm tắt đánh giá**

- CFO năm 2025 đạt 7.690.701.645.261 VND.

**1. Dòng tiền**

- CFO năm 2025 = 7.690.701.645.261 VND.
"""
    responses = iter(
        [
            (
                {"status": "answer", "answer": initial_answer, "followups": []},
                {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "model": "m"},
                "structured",
            ),
            (
                {"status": "answer", "answer": dropped_answer, "followups": []},
                {"input_tokens": 20, "output_tokens": 7, "total_tokens": 27, "model": "m"},
                "structured",
            ),
            (
                {"status": "answer", "answer": _TWO_ASPECT_COMPLETE_ANSWER, "followups": []},
                {"input_tokens": 30, "output_tokens": 9, "total_tokens": 39, "model": "m"},
                "structured",
            ),
        ]
    )
    monkeypatch.setattr(synth_runner, "_invoke_synth", lambda _payload: next(responses))

    updates = synth_runner.run_synth(
        _two_aspect_state(_profitability_facts_with_cfo()["retrieval_facts"])
    )

    quality = next(
        item for item in updates["trace"] if item.get("event") == "synth:quality_check"
    )
    assert quality["repair_rounds"] == 2
    assert quality["accepted_candidate"] == "targeted_repair"

    done = next(item for item in updates["trace"] if item.get("event") == "synth:done")
    assert done["input_tokens"] == 60
    assert done["output_tokens"] == 21
    assert done["total_tokens"] == 81
