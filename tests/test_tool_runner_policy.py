import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from tools import tool_runner


TABLE_BS = "BẢNG CÂN ĐỐI KẾ TOÁN"


def _make_state(
    *,
    question_type: str,
    user_query: str,
    objective: str,
    seed_keywords: list[str],
    action_query: str,
    followup_rounds: int = 0,
):
    return {
        "user_query": user_query,
        "followup_rounds": followup_rounds,
        "planner_plan": {
            "question_type": question_type,
            "analysis_axes": [
                {
                    "axis": "axis_1",
                    "tables": [TABLE_BS],
                    "objective": objective,
                }
            ],
        },
        "worker_plan": {
            "targets": [
                {
                    "table": TABLE_BS,
                    "keywords": seed_keywords,
                }
            ]
        },
        "worker_messages": [
            {
                "agent": "agent_bs",
                "kind": "agent_response",
                "round": followup_rounds,
                "response": '{"kind":"action","action":"get_related_info","arguments":{"query":"%s"}}' % action_query,
                "parsed_output": {
                    "kind": "action",
                    "action": "get_related_info",
                    "arguments": {
                        "query": action_query,
                    },
                },
            }
        ],
        "tool_results": [],
        "tool_observations": [],
        "tool_call_counts": {},
        "force_collect_agents": {},
        "trace": [],
    }


def test_lookup_uses_planner_fallback_only_when_primary_is_empty(monkeypatch):
    calls = []

    def fake_get_related_info(query: str, table: str, collection):
        calls.append(query)
        if query == "nợ phải trả":
            return {"context": "", "source": "kb"}
        if query == "vốn chủ sở hữu":
            return {"context": "matched", "source": "kb"}
        return {"context": "", "source": "kb"}

    monkeypatch.setitem(tool_runner.TOOLS_MAPPING_2_FUNCTIONS, "get_related_info", fake_get_related_info)
    tool_runner.set_collection(object())

    state = _make_state(
        question_type="lookup",
        user_query="Vốn chủ sở hữu là bao nhiêu?",
        objective="Xác định vốn chủ sở hữu",
        seed_keywords=["nợ phải trả"],
        action_query="nợ phải trả",
    )

    updates = tool_runner.call_tool_for_agent(state, "agent_bs")

    assert calls == ["nợ phải trả", "vốn chủ sở hữu"]
    assert len(updates["tool_results"]) == 2
    assert updates["tool_results"][0]["args"]["query"] == "nợ phải trả"
    assert updates["tool_results"][1]["args"]["query"] == "vốn chủ sở hữu"

    refine_log = next(item for item in updates["trace"] if item["event"] == "tool:keyword_refined")
    assert refine_log["question_type"] == "lookup"
    assert refine_log["expansion_enabled"] is False
    assert refine_log["planner_hints"] == []
    assert refine_log["empty_primary_fallbacks"] == ["vốn chủ sở hữu"]
    assert "planner_queries" not in refine_log

    followup_log = next(item for item in updates["trace"] if item["event"] == "tool:followup_done")
    assert followup_log["trigger"] == "empty_primary"


def test_lookup_does_not_run_planner_fallback_when_primary_has_context(monkeypatch):
    calls = []

    def fake_get_related_info(query: str, table: str, collection):
        calls.append(query)
        return {"context": "matched", "source": "kb"}

    monkeypatch.setitem(tool_runner.TOOLS_MAPPING_2_FUNCTIONS, "get_related_info", fake_get_related_info)
    tool_runner.set_collection(object())

    state = _make_state(
        question_type="lookup",
        user_query="Vốn chủ sở hữu là bao nhiêu?",
        objective="Xác định vốn chủ sở hữu",
        seed_keywords=["nợ phải trả"],
        action_query="nợ phải trả",
    )

    updates = tool_runner.call_tool_for_agent(state, "agent_bs")

    assert calls == ["nợ phải trả"]
    assert len(updates["tool_results"]) == 1


def test_analysis_question_allows_keyword_expansion_followups(monkeypatch):
    calls = []

    def fake_get_related_info(query: str, table: str, collection):
        calls.append(query)
        return {"context": "matched", "source": "kb"}

    monkeypatch.setitem(tool_runner.TOOLS_MAPPING_2_FUNCTIONS, "get_related_info", fake_get_related_info)
    tool_runner.set_collection(object())

    state = _make_state(
        question_type="evaluation",
        user_query="Đánh giá cấu trúc tài sản và vốn",
        objective="Đánh giá tổng cộng tài sản và vốn chủ sở hữu",
        seed_keywords=["tổng cộng tài sản"],
        action_query="tổng cộng tài sản",
    )

    updates = tool_runner.call_tool_for_agent(state, "agent_bs")

    assert calls == ["tổng cộng tài sản", "vốn chủ sở hữu"]
    assert len(updates["tool_results"]) == 2

    refine_log = next(item for item in updates["trace"] if item["event"] == "tool:keyword_refined")
    assert refine_log["question_type"] == "evaluation"
    assert refine_log["expansion_enabled"] is True
    assert refine_log["planner_hints"] == ["tổng cộng tài sản", "vốn chủ sở hữu"]

    followup_log = next(item for item in updates["trace"] if item["event"] == "tool:followup_done")
    assert followup_log["trigger"] == "standard"


def test_lookup_does_not_fallback_to_raw_planner_objective_text():
    state = {
        "user_query": "Tổng tài sản của Hòa Phát tại ngày 30/06/2025 là bao nhiêu?",
        "followup_rounds": 0,
        "planner_plan": {
            "question_type": "lookup",
            "analysis_axes": [
                {
                    "axis": "total_assets",
                    "tables": [TABLE_BS],
                    "objective": "Xác định giá trị tổng tài sản tại thời điểm được yêu cầu",
                }
            ],
        },
        "worker_plan": {
            "targets": [
                {
                    "table": TABLE_BS,
                    "keywords": ["tổng cộng tài sản"],
                }
            ]
        },
    }

    refined = tool_runner._refine_keywords_for_table(
        state,
        "agent_bs",
        TABLE_BS,
        requested_query="tổng cộng tài sản",
    )

    assert refined[1] == []
    assert refined[3] == ["tổng cộng tài sản"]
    assert refined[4] == []


def test_followup_prefers_untried_queries_before_repeating_previous_round_query():
    state = {
        "user_query": "Tính hiệu suất sử dụng tài sản",
        "followup_rounds": 1,
        "planner_plan": {
            "question_type": "calculation",
            "analysis_axes": [
                {
                    "axis": "capital_efficiency",
                    "tables": [TABLE_BS],
                    "objective": "Thu thập dữ liệu để xác định thời điểm tổng tài sản phù hợp với doanh thu",
                }
            ],
        },
        "worker_plan": {
            "targets": [
                {
                    "table": TABLE_BS,
                    "keywords": [
                        "tổng cộng tài sản",
                        "ngày kết thúc kỳ báo cáo",
                        "thời điểm tương ứng doanh thu",
                    ],
                    "source": "followup",
                }
            ]
        },
        "tool_results": [
            {
                "agent": "agent_bs",
                "tool": "get_related_info",
                "args": {
                    "table": TABLE_BS,
                    "query": "tổng cộng tài sản",
                },
                "round": 0,
            }
        ],
    }

    refined = tool_runner._refine_keywords_for_table(
        state,
        "agent_bs",
        TABLE_BS,
        requested_query="tổng cộng tài sản",
    )

    assert refined[3][:2] == ["ngày kết thúc kỳ báo cáo", "thời điểm tương ứng doanh thu"]
    assert refined[3][-1] == "tổng cộng tài sản"
