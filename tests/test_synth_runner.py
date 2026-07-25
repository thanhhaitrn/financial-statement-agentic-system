"""Regression tests for test synth runner."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agents import synth_runner


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
        "web_summary": "",
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
            "web_summary": "",
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
            "web_summary": "",
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


def test_prepare_synth_context_omits_retrieval_facts_with_analysis_outputs():
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
                            "item_name": "Lợi nhuận sau thuế",
                            "value": "100",
                            "source": "kb",
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
    assert facts_n == 0
    assert requirements_n == 0
    assert prepared_log["facts_n_raw"] == 1
    assert prepared_log["facts_n_kept"] == 0
    assert prepared_log["facts_n_omitted_from_synth"] == 1
    assert "analysis_outputs" in worker_results
    assert "retrieval_facts" not in worker_results
    assert worker_results["analysis_outputs"]["agent_profitability"]["answer"]


def test_analysis_requirement_followups_drop_requirements_already_in_retrieval_facts():
    followups = synth_runner._build_followups_from_analysis_requirements(
        {
            "agent_liquidity_solvency": {
                "answer": "Còn thiếu tài sản.",
                "requirements": ["tổng tài sản", "tài sản lưu động"],
            }
        },
        {
            "BẢNG CÂN ĐỐI KẾ TOÁN": {
                "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
                "facts": [
                    {
                        "item_name": "tổng cộng tài sản",
                        "time_hint": "2025",
                        "value": "100",
                        "source": "kb",
                    },
                    {
                        "item_name": "tài sản ngắn hạn",
                        "time_hint": "2025",
                        "value": "40",
                        "source": "kb",
                    },
                ],
            }
        },
    )

    assert followups == []


def test_analysis_requirement_followups_do_not_override_synth_answer():
    decision, log = synth_runner._merge_analysis_requirement_followups(
        {"debug_trace": True, "worker_results": {}},
        {
            "status": "answer",
            "answer": "Có đủ dữ liệu để trả lời chính.",
            "followups": [],
        },
        {
            "agent_efficiency": {
                "answer": "Đã tính được vòng quay tài sản và DSO.",
                "requirements": ["phải trả người bán ngắn hạn"],
            }
        },
    )

    assert decision["status"] == "answer"
    assert decision["followups"] == []
    assert log["event"] == "synth:auto_followups_skipped_for_answer"


def test_run_synth_converts_need_more_to_answer_when_followup_limit_reached(monkeypatch):
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
    assert decision["status"] == "answer"
    assert decision["followups"] == []
    assert updates["followup_requests"] == []
    assert "Giới hạn dữ liệu" in decision["answer"]
    assert "phải trả người bán ngắn hạn" in decision["answer"]


def test_run_synth_answers_when_only_optional_followups_are_missing(monkeypatch):
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
    assert decision["status"] == "answer"
    assert updates["followup_requests"] == []
    assert "chỉ số phụ" in decision["answer"]
    assert any(
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


def test_run_synth_drops_data_followups_in_analysis_context(monkeypatch):
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
    assert decision["status"] == "answer"
    assert decision["followups"] == []
    assert updates["followup_requests"] == []


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
