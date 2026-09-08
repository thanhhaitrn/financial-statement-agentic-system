"""Regression tests for scoped-tool cache correctness and request coalescing."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

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


def _tool_state(call_id: str, **extra_args) -> dict:
    tool_call = {
        "name": "get_income_statement_info",
        "args": {"query": "doanh thu thuần", **extra_args},
        "id": call_id,
        "type": "tool_call",
    }
    target = {
        "agent": "agent_profitability",
        "evidence_queries": [{"table": TABLE_IS, "query": "doanh thu thuần"}],
    }
    return {
        "dataset_id": "dataset-a",
        "index_fingerprint": "index-v1",
        "user_query": "Phân tích doanh thu",
        "debug_trace": True,
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


def test_runtime_cache_separates_retrieval_variants(monkeypatch):
    executions = []

    def fake_tool(**kwargs):
        executions.append(dict(kwargs))
        return {"context": "Doanh thu thuần: 100", "source": "kb"}

    tool_runner.set_collection(object())
    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_income_statement_info",
        fake_tool,
    )

    variants = [
        {"strict_table": False},
        {"strict_table": True},
        {"retrieval_mode": "hybrid"},
        {"retrieval_mode": "structured_slots"},
    ]
    updates = [
        tool_runner.call_tool_for_agent(
            _tool_state(f"call-{index}", **variant),
            "agent_profitability",
        )
        for index, variant in enumerate(variants)
    ]

    repeated = tool_runner.call_tool_for_agent(
        _tool_state("call-repeat", retrieval_mode="hybrid"),
        "agent_profitability",
    )

    cache_keys = [next(iter(update["evidence_cache"])) for update in updates]
    assert len(executions) == 4
    assert len(set(cache_keys)) == 4
    assert repeated["tool_results"][0]["kind"] == "cache_hit"


def test_concurrent_same_key_misses_share_one_backend_call(monkeypatch):
    collection = object()
    backend_started = Event()
    allow_backend_return = Event()
    follower_waiting = Event()
    executions = []
    executions_lock = Lock()

    def fake_tool(**kwargs):
        with executions_lock:
            executions.append(dict(kwargs))
        backend_started.set()
        if not allow_backend_return.wait(timeout=5):
            raise TimeoutError("test did not release backend")
        return {"context": "Doanh thu thuần: 100", "source": "kb"}

    original_claim = tool_runner._claim_inflight_cache_call

    def observed_claim(cache_key):
        owns_flight, future = original_claim(cache_key)
        if not owns_flight:
            follower_waiting.set()
        return owns_flight, future

    monkeypatch.setitem(
        tool_runner.TOOLS_MAPPING_2_FUNCTIONS,
        "get_income_statement_info",
        fake_tool,
    )
    monkeypatch.setattr(tool_runner, "_claim_inflight_cache_call", observed_claim)

    def run_call(call_id):
        # ContextVars do not implicitly propagate into executor threads.
        tool_runner.set_collection(collection)
        return tool_runner.call_tool_for_agent(
            _tool_state(call_id),
            "agent_profitability",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader_future = executor.submit(run_call, "call-leader")
        assert backend_started.wait(timeout=2)
        follower_future = executor.submit(run_call, "call-follower")
        try:
            assert follower_waiting.wait(timeout=2)
        finally:
            allow_backend_return.set()
        updates = [leader_future.result(timeout=5), follower_future.result(timeout=5)]

    result_kinds = {update["tool_results"][0]["kind"] for update in updates}
    assert len(executions) == 1
    assert result_kinds == {"primary", "cache_hit"}
    assert any(
        item.get("event") == "tool:cache_coalesced"
        for update in updates
        for item in update.get("trace", [])
    )
    assert tool_runner._INFLIGHT_CACHE_CALLS == {}
