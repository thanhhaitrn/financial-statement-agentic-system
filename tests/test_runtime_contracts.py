"""Runtime contract tests for tool execution, isolation, and evidence caching."""

import sys
from contextvars import Context
from pathlib import Path

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from schemas.table_names import TABLE_BS, TABLE_IS
from tools import tool_runner
from tools.evidence import clear_runtime_evidence_cache


@pytest.fixture(autouse=True)
def _reset_tool_runtime_state():
    clear_runtime_evidence_cache()
    tool_runner.set_collection(None)
    yield
    clear_runtime_evidence_cache()
    tool_runner.set_collection(None)


def _native_call(tool_name: str, query: str, call_id: str) -> dict:
    return {
        "name": tool_name,
        "args": {"query": query},
        "id": call_id,
        "type": "tool_call",
    }


def _tool_state(tool_calls: list[dict], evidence_queries: list[dict]) -> dict:
    target = {
        "agent": "agent_profitability",
        "evidence_queries": evidence_queries,
    }
    return {
        "dataset_id": "dataset-a",
        "index_fingerprint": "index-v1",
        "user_query": "Phân tích doanh thu và tổng tài sản",
        "debug_trace": True,
        "followup_rounds": 0,
        "worker_plan": {"analysis_plan": [target]},
        "dispatch_target": target,
        "worker_messages": [
            {
                "agent": "agent_profitability",
                "kind": "agent_response",
                "round": 0,
                "tool_calls": tool_calls,
                "parsed_output": {"kind": "tool_calls", "tool_calls": tool_calls},
            }
        ],
        "tool_call_counts": {},
        "tool_results": [],
        "evidence_cache": {},
    }


def test_tool_runner_executes_multiple_native_calls_and_hides_internal_collection(monkeypatch):
    collection = object()
    tool_runner.set_collection(collection)
    calls = [
        _native_call("get_income_statement_info", "doanh thu thuần", "call-income"),
        _native_call("get_balance_sheet_info", "tổng cộng tài sản", "call-balance"),
    ]
    state = _tool_state(
        calls,
        [
            {"table": TABLE_IS, "query": "doanh thu thuần"},
            {"table": TABLE_BS, "query": "tổng cộng tài sản"},
        ],
    )
    executed = []

    def fake_tool(**kwargs):
        executed.append(dict(kwargs))
        return {
            "context": f"Item: {kwargs['query']}\nValue: 100",
            "source": "kb",
        }

    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_income_statement_info",
        fake_tool,
    )
    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_balance_sheet_info",
        fake_tool,
    )

    update = tool_runner.call_tool_for_agent(state, "agent_profitability")

    assert [item["query"] for item in executed] == [
        "doanh thu thuần về bán hàng và cung cấp dịch vụ",
        "tổng cộng tài sản",
    ]
    assert all(item["collection"] is collection for item in executed)
    assert [item["tool_call_id"] for item in update["tool_results"]] == [
        "call-income",
        "call-balance",
    ]
    assert all("collection" not in item["args"] for item in update["tool_results"])
    assert update["tool_call_counts"]["agent_profitability"] == {
        "round": 0,
        "count": 2,
    }


def test_collection_handle_is_context_local():
    parent_collection = object()
    child_collection = object()
    tool_runner.set_collection(parent_collection)

    isolated_context = Context()

    def set_and_read_child_collection():
        assert tool_runner.get_collection() is None
        tool_runner.set_collection(child_collection)
        return tool_runner.get_collection()

    assert isolated_context.run(set_and_read_child_collection) is child_collection
    assert isolated_context.run(tool_runner.get_collection) is child_collection
    assert tool_runner.get_collection() is parent_collection


def test_runtime_evidence_cache_is_separated_by_index_generation(monkeypatch):
    tool_runner.set_collection(object())
    executions = []

    def fake_tool(**kwargs):
        executions.append(kwargs["query"])
        return {
            "context": f"Item: {kwargs['query']}\nValue: 100",
            "source": "kb",
        }

    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_income_statement_info",
        fake_tool,
    )
    calls = [_native_call("get_income_statement_info", "doanh thu thuần", "call-1")]
    evidence_queries = [{"table": TABLE_IS, "query": "doanh thu thuần"}]

    first_state = _tool_state(calls, evidence_queries)
    first = tool_runner.call_tool_for_agent(first_state, "agent_profitability")

    next_generation_state = _tool_state(calls, evidence_queries)
    next_generation_state["index_fingerprint"] = "index-v2"
    second = tool_runner.call_tool_for_agent(next_generation_state, "agent_profitability")

    same_generation_state = _tool_state(calls, evidence_queries)
    third = tool_runner.call_tool_for_agent(same_generation_state, "agent_profitability")

    assert len(executions) == 2
    assert first["tool_results"][0]["kind"] == "primary"
    assert second["tool_results"][0]["kind"] == "primary"
    assert third["tool_results"][0]["kind"] == "cache_hit"
    first_key = next(iter(first["evidence_cache"]))
    second_key = next(iter(second["evidence_cache"]))
    assert first_key != second_key
    assert "index-v1" in first_key
    assert "index-v2" in second_key
