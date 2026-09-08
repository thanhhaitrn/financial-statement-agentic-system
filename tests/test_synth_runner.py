"""Regression tests for test synth runner."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import json
import sys
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents import synth_runner


@pytest.mark.parametrize(
    "removed_renderer",
    [
        "_deterministic_decimal_decision",
        "_deterministic_lookup_decision",
        "_deterministic_grounded_interpretation",
        "_hard_analysis_contract_fallback",
        "_fact_ledger_contract_fallback",
        "_profitability_metric_fallback",
    ],
)
def test_answer_authoring_fallback_renderers_are_removed(removed_renderer):
    assert not hasattr(synth_runner, removed_renderer)


def test_run_synth_uses_heuristic_need_more_when_llm_errors(monkeypatch):
    def fake_invoke(_payload):
        return (
            {
                "status": "error",
                "answer": "Lỗi khi chạy synth: ngrok gateway error",
                "missing": [],
                "followups": [],
            },
            None,
            "structured",
        )

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)

    state = {
        "user_query": "Tính ROE",
        "planner_plan": {
            "difficulty_level": "medium",
            "analysis_axes": [
                {
                    "axis": "profitability",
                    "tables": [
                        "BẢNG CÂN ĐỐI KẾ TOÁN",
                        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                    ],
                    "objective": "Thu thập dữ liệu cần thiết để tính ROE",
                }
            ],
        },
        "worker_plan": {
            "targets": [
                {
                    "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                    "keywords": ["vốn chủ sở hữu"],
                },
                {
                    "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                    "keywords": ["lợi nhuận sau thuế thu nhập doanh nghiệp"],
                },
            ]
        },
        "synth_context": {
            "BẢNG CÂN ĐỐI KẾ TOÁN": {
                "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                "facts": [],
            },
            "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": {
                "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                "facts": [
                    {
                        "item_name": "Lợi nhuận sau thuế thu nhập doanh nghiệp",
                        "time_hint": "quý 2/2025",
                        "value": "1.000",
                        "source": "kb",
                    }
                ],
            },
        },
        "tool_results": [],
        "trace": [],
    }

    updates = synth_runner.run_synth(state)
    decision = updates["synth_decision"]

    assert decision["status"] == "error"
    assert decision["followups"] == []
    assert any(item["event"] == "synth:done" for item in updates["trace"])


def test_synth_payload_omits_allowed_keywords_json():
    payload = synth_runner._build_payload(
        {
            "user_query": "Tính ROE",
            "worker_plan": {"targets": []},
            "synth_context": {},
            "last_agent_response": "",
        },
        synth_runner.AGENT_PROFILES["agent_synth"],
        {},
    )

    assert payload["allowed_keywords_json"] == "{}"


def test_synth_payload_adds_easy_brief_answer_instruction():
    payload = synth_runner._build_payload(
        {
            "user_query": "Lợi nhuận sau thuế là bao nhiêu?",
            "planner_plan": {"difficulty_level": "easy"},
            "worker_plan": {"analysis_plan": [], "evidence_plan": []},
        },
        synth_runner.AGENT_PROFILES["agent_synth"],
        {},
    )

    plan = json.loads(payload["plan_json"])

    assert plan["difficulty_level"] == "easy"
    assert "QUY TẮC RIÊNG CHO DIFFICULTY EASY" in payload["system_instruction"]
    assert "Chỉ trả lời ngắn gọn" in payload["system_instruction"]
    assert "Không viết phân tích" in payload["system_instruction"]


def test_synth_payload_adds_medium_calculation_instruction():
    payload = synth_runner._build_payload(
        {
            "user_query": "Tính ROE",
            "planner_plan": {"difficulty_level": "medium"},
            "worker_plan": {"analysis_plan": [], "evidence_plan": []},
        },
        synth_runner.AGENT_PROFILES["agent_synth"],
        {},
    )

    plan = json.loads(payload["plan_json"])

    assert plan["difficulty_level"] == "medium"
    assert "QUY TẮC RIÊNG CHO DIFFICULTY MEDIUM" in payload["system_instruction"]
    assert "Tập trung tính toán" in payload["system_instruction"]
    assert "Không viết phân tích" in payload["system_instruction"]


def test_normalize_worker_result_does_not_fall_back_to_removed_retrieval_agent_table():
    item, kind = synth_runner._normalize_worker_result(
        {
            "facts": [
                {
                    "item_name": "Doanh thu thuần về bán hàng và cung cấp dịch vụ",
                    "value": "100",
                    "source": "kb",
                }
            ]
        },
        agent_name="removed_retrieval_agent",
    )

    assert kind == "structured"
    assert item["table"] == ""
    assert item["facts"][0]["table"] == ""


def test_prepare_synth_context_counts_facts_with_table_key():
    table = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
    worker_results, logs, context_mode, facts_n, requirements_n = synth_runner._prepare_synth_inputs(
        {
            "worker_results": {
                table: {
                    "table": table,
                    "facts": [
                        {
                            "item_name": "Doanh thu thuần về bán hàng và cung cấp dịch vụ",
                            "value": "100",
                            "source": "kb",
                        }
                    ]
                }
            },
            "trace": [],
        }
    )

    prepared_log = next(
        item for item in logs if item["event"] == "synth_context:prepared"
    )

    assert context_mode == "retrieval_fallback"
    assert prepared_log["synth_agents_n"] == 1
    assert prepared_log["facts_n_raw"] == 1
    assert facts_n == 1
    assert requirements_n == 0
    assert worker_results[table]["table"] == table


def test_prepare_synth_context_keeps_retrieval_fact_ledger_with_analysis_outputs():
    worker_results, logs, context_mode, facts_n, requirements_n = synth_runner._prepare_synth_inputs(
        {
            "worker_plan": {
                "targets": [
                    {
                        "agent": "agent_profitability",
                        "requirements": ["Đánh giá khả năng sinh lời"],
                    }
                ]
            },
            "worker_results": {
                "agent_profitability": {
                    "answer": "Biên lợi nhuận cải thiện nhờ lợi nhuận sau thuế tăng.",
                    "requirements": [],
                },
                "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH": {
                    "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                    "facts": [
                        {
                            "fact_id": "pat-2025",
                            "item_name": "Lợi nhuận sau thuế",
                            "value": "100",
                            "parsed_value": "100",
                            "unit": "VND",
                            "source": "report.md#pat-2025",
                        }
                    ]
                },
            },
            "trace": [],
        }
    )

    prepared_log = next(
        item for item in logs if item["event"] == "synth_context:prepared"
    )

    assert context_mode == "analysis"
    assert facts_n == 1
    assert requirements_n == 0
    assert prepared_log["facts_n_raw"] == 1
    assert prepared_log["facts_n_kept"] == 1
    assert prepared_log["facts_n_omitted_from_synth"] == 0
    assert "analysis_outputs" in worker_results
    assert "retrieval_facts" in worker_results
    assert worker_results["analysis_outputs"]["agent_profitability"]["answer"]
    fact = worker_results["retrieval_facts"][
        "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"
    ]["facts"][0]
    assert fact["fact_id"] == "pat-2025"
    assert fact["source"] == "report.md#pat-2025"
    assert fact["unit"] == "VND"
    assert fact["parsed_value"] == "100"


def test_prepare_synth_context_fails_closed_if_raw_facts_are_dropped(monkeypatch):
    def fake_compact(_normalized_results):
        return {}, {
            "agents_n": 1,
            "facts_n_raw": 1,
            "facts_n_kept": 0,
            "agents_trimmed": 0,
        }

    monkeypatch.setattr(synth_runner, "_build_compact_worker_results", fake_compact)

    with pytest.raises(RuntimeError, match="analysis payload contains zero facts"):
        synth_runner._prepare_synth_inputs(
            {
                "worker_plan": {
                    "targets": [
                        {
                            "agent": "agent_profitability",
                            "requirements": ["Đánh giá khả năng sinh lời"],
                        }
                    ]
                },
                "worker_results": {
                    "agent_profitability": {"answer": "Phân tích.", "requirements": []},
                    "BẢNG CÂN ĐỐI KẾ TOÁN": {
                        "facts": [{"item_name": "Tổng tài sản", "value": "100"}]
                    },
                },
            }
        )


def test_synth_system_instruction_enforces_fact_ledger_unit_and_cashflow_sign():
    payload = synth_runner._build_payload(
        {"worker_plan": {"targets": []}},
        synth_runner.AGENT_PROFILES["agent_synth"],
        {},
    )

    instruction = payload["system_instruction"]
    assert "analysis_outputs` và `retrieval_facts" in instruction
    assert "không đổi thành\n              triệu/tỷ/nghìn tỷ" in instruction
    assert "parsed_value > 0 là dòng tiền vào ròng" in instruction
    assert "parsed_value < 0 là dòng tiền ra ròng" in instruction
    assert "không được tuyên bố metric ấy còn thiếu" in instruction


def test_run_synth_repairs_missing_claim_when_fact_ledger_has_metric(monkeypatch):
    payloads = []

    def fake_invoke(payload):
        payloads.append(payload)
        if len(payloads) == 1:
            return (
                {
                    "status": "answer",
                    "answer": "Không có dữ liệu tổng tài sản nên chưa thể kết luận.",
                    "followups": [],
                },
                None,
                "structured",
            )
        return (
            {
                "status": "answer",
                "answer": (
                    "Tổng tài sản cuối năm 2025 là 19.717.684.156.562 VND "
                    "[fact-total-assets | report.md#assets]."
                ),
                "followups": [],
            },
            None,
            "structured",
        )

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)

    updates = synth_runner.run_synth(
        {
            "user_query": "Đánh giá tình hình tài chính năm 2025",
            "planner_plan": {"difficulty_level": "hard"},
            "worker_plan": {
                "targets": [
                    {
                        "agent": "agent_liquidity_solvency",
                        "requirements": ["Đánh giá thanh khoản và đòn bẩy"],
                    }
                ]
            },
            "worker_results": {
                "agent_liquidity_solvency": {
                    "answer": "Cần bổ sung tổng tài sản.",
                    "requirements": ["tổng tài sản"],
                },
                "BẢNG CÂN ĐỐI KẾ TOÁN": {
                    "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                    "facts": [
                        {
                            "fact_id": "fact-total-assets",
                            "item_name": "Tổng cộng tài sản",
                            "time_hint": "cuối năm 2025",
                            "value": "19.717.684.156.562",
                            "parsed_value": "19717684156562",
                            "unit": "VND",
                            "source": "report.md#assets",
                        }
                    ],
                },
            },
            "trace": [],
        }
    )

    assert len(payloads) == 2
    first_context = json.loads(payloads[0]["worker_results_json"])
    assert "analysis_outputs" in first_context
    assert "retrieval_facts" in first_context
    assert "fact_ledger:false_missing:tổng tài sản" in payloads[1]["system_instruction"]
    assert updates["synth_decision"]["status"] == "answer"
    assert "Không có dữ liệu" not in updates["synth_decision"]["answer"]
    quality = next(item for item in updates["trace"] if item["event"] == "synth:quality_check")
    assert quality["retry_attempted"] is True
    assert quality["accepted_candidate"] == "general_repair"


def test_run_synth_preserves_need_more_when_followup_limit_reached(monkeypatch):
    def fake_invoke(_payload):
        return (
            {
                "status": "need_more",
                "answer": "Trả lời tạm thời dựa trên dữ liệu hiện có.",
                "followups": [
                    {
                        "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                        "requirements": ["phải trả người bán ngắn hạn"],
                        "reason": "Cần để tính DPO.",
                    }
                ],
            },
            None,
            "structured",
        )

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)

    updates = synth_runner.run_synth(
        {
            "followup_rounds": synth_runner.MAX_FOLLOWUP_ROUNDS,
            "worker_plan": {"targets": []},
            "worker_results": {},
            "trace": [],
        }
    )

    decision = updates["synth_decision"]
    assert decision["status"] == "need_more"
    assert decision["followups"]
    assert updates["followup_requests"] == decision["followups"]
    assert decision["answer"] == "Trả lời tạm thời dựa trên dữ liệu hiện có."
    assert "Giới hạn dữ liệu" not in decision["answer"]


def test_run_synth_does_not_override_optional_followups(monkeypatch):
    def fake_invoke(_payload):
        return (
            {
                "status": "need_more",
                "answer": "Đã có thể đánh giá bằng doanh thu, lợi nhuận và tài sản.",
                "followups": [
                    {
                        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                        "requirements": ["chi phí bán hàng"],
                        "reason": "Cần để phân tích thêm cơ cấu chi phí.",
                    }
                ],
            },
            None,
            "structured",
        )

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)

    updates = synth_runner.run_synth(
        {
            "user_query": "Đánh giá khả năng sinh lời năm 2024",
            "followup_rounds": 0,
            "worker_plan": {"targets": []},
            "worker_results": {},
            "trace": [],
        }
    )

    decision = updates["synth_decision"]
    assert decision["status"] == "need_more"
    assert updates["followup_requests"] == decision["followups"]
    assert decision["answer"] == "Đã có thể đánh giá bằng doanh thu, lợi nhuận và tài sản."
    assert not any(
        item["event"] == "synth:optional_followups_answered"
        for item in updates["trace"]
    )


def test_run_synth_keeps_need_more_when_optional_item_is_explicitly_requested(monkeypatch):
    def fake_invoke(_payload):
        return (
            {
                "status": "need_more",
                "answer": "Cần chi phí bán hàng để trả lời đúng câu hỏi.",
                "followups": [
                    {
                        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                        "requirements": ["chi phí bán hàng"],
                        "reason": "Người dùng hỏi trực tiếp khoản mục này.",
                    }
                ],
            },
            None,
            "structured",
        )

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)

    updates = synth_runner.run_synth(
        {
            "user_query": "Phân tích chi phí bán hàng năm 2024",
            "followup_rounds": 0,
            "worker_plan": {"targets": []},
            "worker_results": {},
            "trace": [],
        }
    )

    assert updates["synth_decision"]["status"] == "need_more"
    assert updates["followup_requests"]


def test_run_synth_filters_invalid_data_followup_without_changing_status(monkeypatch):
    def fake_invoke(_payload):
        return (
            {
                "status": "need_more",
                "answer": "Có thể trả lời dựa trên phân tích sinh lời hiện có.",
                "followups": [
                    {
                        "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                        "requirements": ["chi phí bán hàng"],
                        "reason": "Muốn bổ sung dữ liệu chi phí.",
                    }
                ],
            },
            None,
            "structured",
        )

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)

    updates = synth_runner.run_synth(
        {
            "worker_plan": {
                "targets": [
                    {
                        "agent": "agent_profitability",
                        "requirements": ["Đánh giá khả năng sinh lời"],
                    }
                ]
            },
            "worker_results": {
                "agent_profitability": {
                    "answer": "Biên lợi nhuận cải thiện.",
                    "requirements": [],
                }
            },
            "trace": [],
        }
    )

    decision = updates["synth_decision"]
    assert decision["status"] == "need_more"
    assert decision["followups"] == []
    assert updates["followup_requests"] == []
    assert decision["answer"] == "Có thể trả lời dựa trên phân tích sinh lời hiện có."


def test_run_synth_preserves_new_analysis_agent_followups(monkeypatch):
    def fake_invoke(_payload):
        return (
            {
                "status": "need_more",
                "answer": "Cần thêm khía cạnh dòng tiền để kết luận đầy đủ.",
                "followups": [
                    {
                        "agent": "agent_cashflow_analysis",
                        "requirements": ["phân tích chất lượng dòng tiền"],
                        "reason": "Câu hỏi cần thêm khía cạnh dòng tiền.",
                    }
                ],
            },
            None,
            "structured",
        )

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)

    updates = synth_runner.run_synth(
        {
            "followup_rounds": 0,
            "worker_plan": {
                "targets": [
                    {
                        "agent": "agent_profitability",
                        "requirements": ["Đánh giá khả năng sinh lời"],
                    }
                ]
            },
            "worker_results": {
                "agent_profitability": {
                    "answer": "- ROE = 10 / 100 = 10%.\n\n*Nhận xét*:\n- Khả năng sinh lời ở mức tích cực.",
                    "requirements": [],
                }
            },
            "trace": [],
        }
    )

    assert updates["synth_decision"]["status"] == "need_more"
    assert updates["followup_requests"][0]["agent"] == "agent_cashflow_analysis"
    assert updates["followup_rounds"] == 1
    assert updates["planner_plan"]["difficulty_level"] == "hard"
    assert updates["planner_plan"]["analysis_axes"][0]["axis"] == "agent_cashflow_analysis"


def test_sanitize_followups_normalizes_requirements_before_dedupe():
    decision, _log = synth_runner._sanitize_followups(
        {},
        {
            "status": "need_more",
            "answer": "Cần thêm dữ liệu.",
            "followups": [
                {
                    "table": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
                    "requirements": [
                        "cần dữ liệu chi phí bán hàng",
                        "chi phí bán hàng",
                    ],
                    "reason": "Thiếu chi phí bán hàng.",
                }
            ],
        },
    )

    assert decision["followups"] == [
        {
            "requirements": ["chi phí bán hàng"],
            "reason": "Thiếu chi phí bán hàng.",
        }
    ]


def test_sanitize_followups_preserves_analysis_agent_without_table():
    decision, _log = synth_runner._sanitize_followups(
        {},
        {
            "status": "need_more",
            "answer": "Cần thêm khía cạnh dòng tiền.",
            "followups": [
                {
                    "agent": "agent_cashflow_analysis",
                    "requirements": ["phân tích chất lượng dòng tiền"],
                    "reason": "Cần thêm analysis agent dòng tiền.",
                }
            ],
        },
    )

    assert decision["followups"] == [
        {
            "agent": "agent_cashflow_analysis",
            "table": None,
            "requirements": ["phân tích chất lượng dòng tiền"],
            "reason": "Cần thêm analysis agent dòng tiền.",
        }
    ]


def test_invalid_quality_repair_returns_initial_model_answer(monkeypatch):
    initial = {
        "status": "answer",
        "answer": "Tổng tài sản là 0,1 tỷ VND.",
        "followups": [],
    }
    invalid_repair = {
        "status": "error",
        "answer": "Synth trả về sai schema.",
        "followups": [],
    }
    responses = iter(
        [
            (initial, None, "structured"),
            (invalid_repair, None, "structured"),
        ]
    )
    monkeypatch.setattr(synth_runner, "_invoke_synth", lambda _payload: next(responses))

    updates = synth_runner.run_synth(
        {
            "user_query": "Tổng tài sản là bao nhiêu?",
            "planner_plan": {"difficulty_level": "easy"},
            "worker_plan": {"difficulty_level": "easy", "analysis_plan": []},
            "worker_results": {
                "balance": {
                    "facts": [
                        {
                            "fact_id": "assets",
                            "item_name": "Tổng cộng tài sản",
                            "value": "100",
                            "unit": "VND",
                            "source": "report.md",
                        }
                    ]
                }
            },
            "trace": [],
        }
    )

    assert updates["synth_decision"] == initial
    quality = next(item for item in updates["trace"] if item["event"] == "synth:quality_check")
    assert quality["retry_attempted"] is True
    assert quality["accepted_candidate"] == "initial_after_invalid_repair"
    assert quality["remaining_violations"]


def test_two_invalid_synth_decisions_return_operational_error(monkeypatch):
    calls = []

    def fake_invoke(payload):
        calls.append(payload)
        return {
            "status": "error",
            "answer": "Synth không parse được JSON hợp lệ.",
            "followups": [],
        }, None, "plain_json"

    monkeypatch.setattr(synth_runner, "_invoke_synth", fake_invoke)

    updates = synth_runner.run_synth(
        {
            "user_query": "Doanh thu là bao nhiêu?",
            "planner_plan": {"difficulty_level": "easy"},
            "worker_plan": {"difficulty_level": "easy", "analysis_plan": []},
            "worker_results": {},
            "trace": [],
        }
    )

    assert len(calls) == 2
    assert updates["synth_decision"]["status"] == "error"
    quality = next(item for item in updates["trace"] if item["event"] == "synth:quality_check")
    assert quality["retry_attempted"] is True
    assert quality["accepted_candidate"] == "initial_after_invalid_repair"
    assert "invalid_synth_decision" in quality["remaining_violations"]
