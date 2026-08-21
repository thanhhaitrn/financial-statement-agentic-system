"""Regression tests for test followup flow."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from graph.dispatch_nodes import prepare_followup_dispatch_state
from output_formatter import format_final_answer


TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"
TABLE_IS = "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH"


def test_prepare_followup_dispatch_uses_requirements_without_keywords():
    state = {
        "followup_requests": [
            {
                "table": TABLE_BS,
                "requirements": ["cần dữ liệu vốn chủ sở hữu để tính ROE"],
                "reason": "Thiếu mẫu số để tính ROE.",
            }
        ],
        "followup_rounds": 0,
        "worker_plan": {"targets": []},
    }

    updates = prepare_followup_dispatch_state(state)

    assert updates["planner_plan"]["followup_mode"] is True
    assert updates["planner_plan"]["followup_requirements"] == [
        "vốn chủ sở hữu"
    ]
    assert updates["planner_plan"]["analysis_axes"] == [
        {
            "axis": "agent_profitability",
            "objective": "vốn chủ sở hữu",
        }
    ]


def test_prepare_followup_dispatch_preserves_table_grouping_for_router():
    state = {
        "followup_requests": [
            {
                "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
                "requirements": ["kỳ hạn vay và tài sản bảo đảm"],
                "reason": "Thiếu chi tiết nợ vay.",
            }
        ],
        "followup_rounds": 0,
        "worker_plan": {"targets": []},
    }

    updates = prepare_followup_dispatch_state(state)

    assert updates["planner_plan"]["followup_requests"] == [
        {
            "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
            "requirements": ["kỳ hạn vay và tài sản bảo đảm"],
            "reason": "Thiếu chi tiết nợ vay.",
        }
    ]


def test_prepare_followup_dispatch_supports_new_analysis_agent_followup():
    state = {
        "followup_requests": [
            {
                "agent": "agent_cashflow_analysis",
                "requirements": ["phân tích chất lượng dòng tiền"],
                "reason": "Cần thêm khía cạnh dòng tiền.",
            }
        ],
        "followup_rounds": 0,
        "worker_plan": {
            "analysis_plan": [
                {
                    "agent": "agent_profitability",
                    "objective": "Đánh giá khả năng sinh lời",
                }
            ]
        },
    }

    updates = prepare_followup_dispatch_state(state)

    assert updates["planner_plan"]["difficulty_level"] == "hard"
    assert updates["planner_plan"]["followup_mode"] is False
    assert updates["planner_plan"]["followup_requirements"] == []
    assert updates["planner_plan"]["analysis_axes"] == [
        {
            "axis": "agent_cashflow_analysis",
            "objective": "phân tích chất lượng dòng tiền",
        }
    ]
    assert updates["planner_plan"]["followup_requests"] == [
        {
            "agent": "agent_cashflow_analysis",
            "requirements": ["phân tích chất lượng dòng tiền"],
            "reason": "Cần thêm khía cạnh dòng tiền.",
        }
    ]


def test_prepare_followup_dispatch_preserves_route_metadata():
    state = {
        "followup_requests": [
            {
                "table": TABLE_BS,
                "requirements": ["vốn chủ sở hữu"],
                "reason": "Thiếu mẫu số ROE.",
                "time_hint": "31/12/2024",
                "period": "Năm 2024",
                "unit": "VND",
                "value_type": "Số cuối kỳ",
                "evidence_query": "vốn chủ sở hữu cuối kỳ",
                "source": "report.md",
            }
        ],
        "planner_plan": {
            "company": "APEC",
            "time_hint": "31/12/2024",
            "need_web": False,
        },
        "followup_rounds": 0,
        "worker_plan": {"targets": []},
    }

    updates = prepare_followup_dispatch_state(state)
    followup = updates["planner_plan"]["followup_requests"][0]

    assert updates["planner_plan"]["time_hint"] == "31/12/2024"
    assert updates["planner_plan"]["period"] == "Năm 2024"
    assert updates["planner_plan"]["unit"] == "VND"
    assert updates["planner_plan"]["value_type"] == "Số cuối kỳ"
    assert followup["evidence_query"] == "vốn chủ sở hữu cuối kỳ"
    assert followup["source"] == "report.md"


def test_format_final_answer_includes_not_found_after_search_messages():
    message = (
        "Không tìm thấy dòng chi phí bán hàng trong dữ liệu hiện có. "
        f"Có thể khoản này không phát sinh/không được trình bày riêng trong {TABLE_IS}, "
        "nhưng cần xác nhận từ báo cáo gốc."
    )
    formatted = format_final_answer(
        {
            "worker_results": {
                TABLE_IS: {
                    "table": TABLE_IS,
                    "facts": [
                        {
                            "item_name": "chi phí bán hàng",
                            "value": "",
                            "status": "not_found_after_search",
                            "interpretation_hint": message,
                        }
                    ]
                }
            },
            "synth_decision": {
                "status": "answer",
                "answer": "Chưa đủ dữ liệu để kết luận.",
            },
        }
    )

    assert message in formatted
