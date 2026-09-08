"""Regression tests for test output formatter."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import importlib
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

    assert formatted == "Doanh nghiệp có tín hiệu tích cực nhưng cần theo dõi vốn lưu động."
    assert "=== FINAL ANSWER ===" not in formatted
    assert "ANSWER:" not in formatted
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

    assert formatted == "Tổng hợp cuối."
    assert "Câu trả lời analysis round cuối." not in formatted
    assert "Câu trả lời analysis round 0." not in formatted


def test_format_final_answer_preserves_single_synth_answer_without_analysis():
    formatted = format_final_answer(
        {
            "synth_decision": {
                "status": "answer",
                "answer": "ROE khoảng 6,46%.",
            }
        }
    )

    assert formatted == "ROE khoảng 6,46%."


def test_format_final_answer_does_not_append_unrelated_worker_missing_facts():
    clean_answer = "ROA đạt 20,04% và ROE đạt 31,07%."
    missing_share_issuance = (
        "Không tìm thấy dòng tiền thu từ phát hành cổ phiếu, nhận vốn góp "
        "của chủ sở hữu trong dữ liệu hiện có."
    )
    formatted = format_final_answer(
        {
            "worker_results": {
                "BÁO CÁO LƯU CHUYỂN TIỀN TỆ": {
                    "facts": [
                        {
                            "item_name": "tiền thu từ phát hành cổ phiếu",
                            "status": "not_found_after_search",
                            "message": missing_share_issuance,
                        }
                    ]
                }
            },
            "synth_decision": {
                "status": "answer",
                "answer": clean_answer,
            },
        }
    )

    assert formatted == clean_answer
    assert missing_share_issuance not in formatted


def test_format_final_answer_keeps_explicit_need_more_requirements():
    formatted = format_final_answer(
        {
            "worker_results": {
                "BÁO CÁO LƯU CHUYỂN TIỀN TỆ": {
                    "facts": [
                        {
                            "item_name": "tiền thu từ phát hành cổ phiếu",
                            "status": "not_found_after_search",
                            "message": "Diagnostic không liên quan.",
                        }
                    ]
                }
            },
            "synth_decision": {
                "status": "need_more",
                "answer": "Cần bổ sung dữ liệu đầu kỳ để tính ROA.",
                "missing": ["Tổng tài sản đầu kỳ"],
            },
        }
    )

    assert formatted == (
        "Cần bổ sung dữ liệu đầu kỳ để tính ROA.\n\n"
        "**Còn thiếu dữ liệu**\n"
        "- Tổng tài sản đầu kỳ"
    )
    assert "Diagnostic không liên quan." not in formatted


def test_format_final_answer_strips_legacy_transport_labels_and_leading_boilerplate():
    formatted = format_final_answer(
        {
            "synth_decision": {
                "status": "answer",
                "answer": (
                    "=== FINAL ANSWER ===\n"
                    "ANSWER: Dựa trên số liệu hiện có: **Tóm tắt đánh giá**\n\n"
                    "- Khả năng sinh lời đang chịu áp lực."
                ),
            }
        }
    )

    assert formatted == (
        "**Tóm tắt đánh giá**\n\n"
        "- Khả năng sinh lời đang chịu áp lực."
    )


def test_format_final_answer_only_strips_evidence_boilerplate_at_the_start():
    answer = (
        "Kết luận trực tiếp.\n\n"
        "Cụm 'Dựa trên số liệu hiện có' bên trong nội dung được giữ nguyên."
    )

    assert format_final_answer({"synth_decision": {"answer": answer}}) == answer


def test_run_query_prints_clean_final_answer_without_transport_labels(monkeypatch, capsys):
    legacy_cli = importlib.import_module("test")
    final_state = {
        "synth_decision": {
            "status": "answer",
            "answer": "Tóm tắt khả năng sinh lời.",
        }
    }
    monkeypatch.setattr(legacy_cli, "describe_dataset", lambda _dataset: "dataset")
    monkeypatch.setattr(
        legacy_cli,
        "execute_query",
        lambda *_args, **_kwargs: final_state,
    )
    monkeypatch.setattr(legacy_cli, "extract_run_summary", lambda _state: {})

    returned = legacy_cli.run_query(object(), object(), "đánh giá sinh lời")

    output = capsys.readouterr().out
    assert "=== FINAL ANSWER ===" not in output
    assert "ANSWER:" not in output
    assert output.count("Tóm tắt khả năng sinh lời.") == 1
    assert returned is final_state
