"""Regression tests for note scoped tool keyword guard."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path
import json


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from schemas.table_names import TABLE_NOTE
from tools import tool_runner


def _tool_call_payload(query: str) -> dict:
    tool_call = {
        "name": "get_note_info",
        "args": {"query": query},
        "id": "test-tool-call",
        "type": "tool_call",
    }
    return {
        "kind": "tool_calls",
        "tool_calls": [tool_call],
    }


def _note_tool_state(action_query: str, requirements: list[str]) -> dict:
    target = {
        "agent": "agent_profitability",
        "objective": "Lấy thuyết minh liên quan.",
        "evidence_queries": [
            {
                "table": TABLE_NOTE,
                "query": requirement,
            }
            for requirement in requirements
        ],
    }
    payload = _tool_call_payload(action_query)
    return {
        "debug_trace": True,
        "followup_rounds": 0,
        "worker_plan": {"analysis_plan": [target]},
        "worker_messages": [
            {
                "agent": "agent_profitability",
                "kind": "agent_response",
                "round": 0,
                "response": json.dumps(payload, ensure_ascii=False),
                "parsed_output": payload,
                "tool_calls": payload["tool_calls"],
            }
        ],
        "tool_call_counts": {},
        "tool_results": [],
    }


def test_note_scoped_tool_keeps_native_query_and_note_scope(monkeypatch):
    captured = {}

    def fake_get_note_info(**kwargs):
        captured.update(kwargs)
        return {"context": "Nội dung thuyết minh", "source": "kb"}

    tool_runner.set_collection(object())
    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_note_info",
        fake_get_note_info,
    )

    updates = tool_runner.call_tool_for_agent(
        _note_tool_state("hợp đồng thuê đất", ["tài sản thuê ngoài"]),
        "agent_profitability",
    )

    assert captured["query"] == "hợp đồng thuê đất"
    assert captured["table"] == TABLE_NOTE
    assert "query=hợp đồng thuê đất" in updates["tool_observations"][0]["text"]


def test_note_scoped_tool_does_not_override_query_across_multiple_targets(monkeypatch):
    captured = {}

    def fake_get_note_info(**kwargs):
        captured.update(kwargs)
        return {"context": "Nội dung thuyết minh", "source": "kb"}

    payload = _tool_call_payload("tự chọn thêm keyword")
    state = {
        "debug_trace": True,
        "followup_rounds": 0,
        "worker_plan": {
            "analysis_plan": [
                {
                    "agent": "agent_profitability",
                    "objective": "Lấy các thuyết minh liên quan.",
                    "evidence_queries": [
                        {"table": TABLE_NOTE, "query": "tài sản thuê ngoài"},
                        {"table": TABLE_NOTE, "query": "ngoại tệ các loại"},
                    ],
                },
            ]
        },
        "worker_messages": [
            {
                "agent": "agent_profitability",
                "kind": "agent_response",
                "round": 0,
                "response": json.dumps(payload, ensure_ascii=False),
                "parsed_output": payload,
                "tool_calls": payload["tool_calls"],
            }
        ],
        "tool_call_counts": {"agent_profitability": {"round": 0, "count": 1}},
        "tool_results": [],
    }

    tool_runner.set_collection(object())
    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_note_info",
        fake_get_note_info,
    )

    updates = tool_runner.call_tool_for_agent(state, "agent_profitability")

    assert captured["query"] == "tự chọn thêm keyword"
    assert "query=tự chọn thêm keyword" in updates["tool_observations"][0]["text"]


def test_note_scoped_tool_allows_native_query_without_assigned_requirement(monkeypatch):
    called = False

    def fake_get_note_info(**kwargs):
        nonlocal called
        called = True
        assert kwargs["query"] == "hợp đồng thuê kho"
        assert kwargs["table"] == TABLE_NOTE
        return {"context": "Nội dung thuyết minh", "source": "kb"}

    tool_runner.set_collection(object())
    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_note_info",
        fake_get_note_info,
    )

    updates = tool_runner.call_tool_for_agent(
        _note_tool_state("hợp đồng thuê kho", []),
        "agent_profitability",
    )

    assert called is True
    assert "query=hợp đồng thuê kho" in updates["tool_observations"][0]["text"]
