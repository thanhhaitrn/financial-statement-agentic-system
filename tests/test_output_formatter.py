"""Regression tests for test output formatter."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from output_formatter import format_final_answer


def test_format_final_answer_prints_only_synth_answer_once():
    state = {
        "worker_plan": {
            "targets": [
                {"agent": "agent_cashflow_analysis", "requirements": ["đánh giá dòng tiền"]},
                {"agent": "agent_profitability", "requirements": ["đánh giá sinh lời"]},
            ]
        },
        "worker_results": {
            "agent_profitability": {
                "answer": "Biên lợi nhuận cải thiện nhờ doanh thu tăng nhanh hơn chi phí.",
                "requirements": [],
            },
            "agent_cashflow_analysis": {
                "answer": "**3. Dòng tiền**\nDòng tiền kinh doanh dương, hỗ trợ chất lượng lợi nhuận.",
                "requirements": [],
            },
        },
        "synth_decision": {
            "status": "answer",
            "answer": "Doanh nghiệp có tín hiệu tích cực nhưng cần theo dõi vốn lưu động.",
            "followups": [],
        },
    }

    formatted = format_final_answer(state)

    assert formatted == (
        "=== FINAL ANSWER ===\n"
        "ANSWER: Doanh nghiệp có tín hiệu tích cực nhưng cần theo dõi vốn lưu động."
    )
    assert formatted.count("=== FINAL ANSWER ===") == 1
    assert "Agent Cashflow Analysis" not in formatted
    assert "Agent Profitability" not in formatted
    assert "**3. Dòng tiền**" not in formatted


def test_format_final_answer_does_not_repeat_stale_or_latest_worker_answers():
    state = {
        "worker_plan": {
            "targets": [
                {"agent": "agent_profitability", "requirements": ["đánh giá sinh lời"]},
            ]
        },
        "worker_results": {
            "agent_profitability": {
                "answer": "Câu trả lời analysis round 0.",
                "requirements": [],
                "round": 0,
            },
        },
        "worker_messages": [
            {
                "agent": "agent_profitability",
                "kind": "agent_response",
                "round": 0,
                "parsed_output": {
                    "answer": "Câu trả lời analysis round 0.",
                    "requirements": [],
                },
            },
            {
                "agent": "agent_profitability",
                "kind": "agent_response",
                "round": 1,
                "parsed_output": {
                    "answer": "Câu trả lời analysis round cuối.",
                    "requirements": [],
                },
            },
        ],
        "synth_decision": {
            "status": "answer",
            "answer": "Tổng hợp cuối.",
            "followups": [],
        },
    }

    formatted = format_final_answer(state)

    assert formatted == "=== FINAL ANSWER ===\nANSWER: Tổng hợp cuối."
    assert "ANSWER: Câu trả lời analysis round cuối." not in formatted
    assert "ANSWER: Câu trả lời analysis round 0." not in formatted


def test_format_final_answer_preserves_single_synth_answer_without_analysis():
    formatted = format_final_answer(
        {
            "synth_decision": {
                "status": "answer",
                "answer": "ROE khoảng 6,46%.",
            }
        }
    )

    assert formatted == "=== FINAL ANSWER ===\nANSWER: ROE khoảng 6,46%."
