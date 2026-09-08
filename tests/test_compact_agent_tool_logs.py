"""Regression coverage for compact operational agent/tool traces."""

import json

import pytest

from agents import agent_runner
from schemas.table_names import TABLE_IS
from tools import tool_runner
from tools.evidence import clear_runtime_evidence_cache


@pytest.fixture(autouse=True)
def _reset_tool_runtime_state():
    clear_runtime_evidence_cache()
    tool_runner.set_collection(None)
    yield
    clear_runtime_evidence_cache()
    tool_runner.set_collection(None)


def _analysis_state(*, debug_trace: bool) -> dict:
    target = {
        "agent": "agent_profitability",
        "requirements": ["SECRET_MISSING_REQUIREMENT"],
    }
    return {
        "debug_trace": debug_trace,
        "user_query": "Phân tích khả năng sinh lời",
        "worker_plan": {"analysis_plan": [target]},
        "dispatch_target": target,
        "analysis_input_results": {},
        "worker_results": {},
        "tool_observations": [],
        "tool_results": [],
        "tool_call_counts": {},
        "force_collect_agents": {"agent_profitability": 0},
        "followup_rounds": 0,
    }


def test_analysis_operational_trace_uses_counts_without_payload_by_default(monkeypatch):
    parsed = {
        "answer": "SECRET_ANALYSIS_RESULT " + ("x" * 1_000),
        "requirements": [],
    }
    monkeypatch.setattr(
        agent_runner,
        "_run_analysis_once",
        lambda _payload: (parsed, json.dumps(parsed), "", "structured", {}),
    )

    compact = agent_runner.call_analysis_agent(
        _analysis_state(debug_trace=False),
        "agent_profitability",
    )["trace"]
    evidence_log = next(item for item in compact if item["event"] == "analysis:evidence_check")
    done_log = next(item for item in compact if item["event"] == "analysis:done")

    assert evidence_log["missing_requirements_n"] == 1
    assert "missing_requirements" not in evidence_log
    assert done_log["status"] == "ok"
    assert done_log["result_kind"] == "answer"
    assert done_log["requirements_n"] == 0
    assert done_log["answer_len"] > 1_000
    assert "result" not in done_log
    assert "SECRET_MISSING_REQUIREMENT" not in json.dumps(compact)
    assert "SECRET_ANALYSIS_RESULT" not in json.dumps(compact)

    detailed = agent_runner.call_analysis_agent(
        _analysis_state(debug_trace=True),
        "agent_profitability",
    )["trace"]
    detailed_evidence = next(
        item for item in detailed if item["event"] == "analysis:evidence_check"
    )
    detailed_done = next(item for item in detailed if item["event"] == "analysis:done")

    assert detailed_evidence["missing_requirements"] == ["SECRET_MISSING_REQUIREMENT"]
    assert detailed_done["result"] == parsed


def _tool_state(*, debug_trace: bool) -> dict:
    tool_call = {
        "name": "get_income_statement_info",
        "args": {"query": "SECRET_TOOL_QUERY"},
        "id": "tool-call-1",
        "type": "tool_call",
    }
    target = {
        "agent": "agent_profitability",
        "requirements": ["SECRET_TOOL_QUERY"],
    }
    return {
        "dataset_id": "dataset-a",
        "index_fingerprint": "index-v1",
        "user_query": "SECRET_USER_INTENT",
        "debug_trace": debug_trace,
        "followup_rounds": 0,
        "worker_plan": {"analysis_plan": [target]},
        "dispatch_target": target,
        "worker_messages": [
            {
                "agent": "agent_profitability",
                "kind": "agent_response",
                "round": 0,
                "tool_calls": [tool_call],
                "parsed_output": {"kind": "tool_calls", "tool_calls": [tool_call]},
            }
        ],
        "tool_call_counts": {},
        "tool_results": [],
        "evidence_cache": {},
    }


def test_tool_operational_trace_uses_short_cache_id_and_gates_previews(monkeypatch):
    def fake_tool(**_kwargs):
        return {
            "context": "SECRET_CONTEXT " + ("y" * 2_000),
            "source": "kb",
            "facts": [
                {
                    "table": TABLE_IS,
                    "item_name": "SECRET_FACT",
                    "value": "100",
                    "status": "found",
                }
            ],
        }

    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_income_statement_info",
        fake_tool,
    )
    tool_runner.set_collection(object())

    compact = tool_runner.call_tool_for_agent(
        _tool_state(debug_trace=False),
        "agent_profitability",
    )["trace"]
    compact_json = json.dumps(compact, ensure_ascii=False)
    cache_logs = [item for item in compact if item["event"].startswith("tool:cache_")]
    done_log = next(item for item in compact if item["event"] == "tool:done")

    assert cache_logs
    assert all(len(item["cache_id"]) == 12 for item in cache_logs)
    assert all("cache_key" not in item for item in cache_logs)
    assert done_log["status"] == "ok"
    assert done_log["facts_n"] == 1
    assert done_log["context_len"] > 2_000
    for field in (
        "args_preview",
        "context_preview",
        "facts_preview",
        "query",
        "query_redacted",
    ):
        assert all(field not in item for item in compact)
    for secret in (
        "SECRET_TOOL_QUERY",
        "SECRET_USER_INTENT",
        "SECRET_CONTEXT",
        "SECRET_FACT",
    ):
        assert secret not in compact_json

    clear_runtime_evidence_cache()
    detailed = tool_runner.call_tool_for_agent(
        _tool_state(debug_trace=True),
        "agent_profitability",
    )["trace"]
    detailed_cache_logs = [
        item for item in detailed if item["event"].startswith("tool:cache_")
    ]

    assert all(item.get("cache_key") for item in detailed_cache_logs)
    assert any(item.get("args_preview") for item in detailed)
    assert any(item.get("context_preview") for item in detailed)
    assert any(item.get("query") for item in detailed)
