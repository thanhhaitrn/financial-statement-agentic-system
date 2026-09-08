import json
import re

from graph import evidence as evidence_node
from schemas.table_names import TABLE_IS


def _install_fake_retrieval(monkeypatch, *, misses_first: bool = False):
    runtime_cache = {}
    calls = []

    def fake_get_related_info(**kwargs):
        calls.append(kwargs)
        missed = misses_first and len(calls) == 1
        item_name = "Chi phí khác" if missed else "Doanh thu thuần"
        raw_value = "987654321" if missed else "123456789"
        source = "/private/confidential/customer/report-2025.md"
        return {
            "context": f"{item_name}: {raw_value}",
            "source": source,
            "documents": [f"{item_name}: {raw_value}"],
            "metadatas": [
                {
                    "heading": TABLE_IS,
                    "item_name": item_name,
                    "raw_value": raw_value,
                    "source": source,
                }
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(evidence_node, "get_related_info", fake_get_related_info)
    monkeypatch.setattr(
        evidence_node,
        "get_runtime_cache_item",
        lambda key: runtime_cache.get(key),
    )
    monkeypatch.setattr(
        evidence_node,
        "set_runtime_cache_item",
        lambda key, value: runtime_cache.__setitem__(key, value),
    )
    return calls


def _state(*, debug_trace: bool = False) -> dict:
    return {
        "run_id": "run-compact-evidence-log",
        "debug_trace": debug_trace,
        "dataset_id": "dataset-compact-evidence-log",
        "user_query": "Doanh thu thuần là bao nhiêu?",
        "worker_plan": {
            "evidence_plan": [
                {"table": TABLE_IS, "query": "doanh thu thuần"},
            ],
        },
    }


def test_default_evidence_trace_has_one_compact_completion_per_call(monkeypatch):
    calls = _install_fake_retrieval(monkeypatch)

    first = evidence_node.build_evidence_pack(_state())
    second_state = _state()
    second_state["evidence_cache"] = first["evidence_cache"]
    second = evidence_node.build_evidence_pack(second_state)

    assert len(calls) == 1
    first_done = [
        entry for entry in first["trace"] if entry["event"] == "evidence_tool:done"
    ]
    second_done = [
        entry for entry in second["trace"] if entry["event"] == "evidence_tool:done"
    ]
    assert len(first_done) == len(second_done) == 1
    assert first_done[0]["cache_hit"] is False
    assert second_done[0]["cache_hit"] is True
    assert first_done[0]["cache_id"] == second_done[0]["cache_id"]
    assert re.fullmatch(r"ec_[0-9a-f]{12}", first_done[0]["cache_id"])
    assert not any(
        entry["event"] == "evidence_tool:start" for entry in first["trace"]
    )

    trace_text = json.dumps(first["trace"], ensure_ascii=False)
    full_cache_key = next(iter(first["evidence_cache"]))
    assert full_cache_key not in trace_text
    assert "/private/confidential/customer/report-2025.md" not in trace_text
    assert "123456789" not in trace_text
    for entry in [*first_done, *second_done]:
        assert "cache_key" not in entry
        assert "facts_preview" not in entry
        assert "source" not in entry
        assert "duration_ms" in entry
        assert "facts_n" in entry


def test_debug_trace_keeps_start_but_uses_compact_cache_identifier(monkeypatch):
    _install_fake_retrieval(monkeypatch)

    updates = evidence_node.build_evidence_pack(_state(debug_trace=True))

    starts = [
        entry for entry in updates["trace"] if entry["event"] == "evidence_tool:start"
    ]
    assert len(starts) == 1
    assert starts[0]["debug"] is True
    assert re.fullmatch(r"ec_[0-9a-f]{12}", starts[0]["cache_id"])
    assert "cache_key" not in starts[0]
    done = next(
        entry for entry in updates["trace"] if entry["event"] == "evidence_tool:done"
    )
    assert done["query"] == "doanh thu thuần"
    assert done["source"] == "/private/confidential/customer/report-2025.md"
    assert done["facts_preview"]


def test_targeted_retry_default_trace_omits_start_and_fact_payload(monkeypatch):
    _install_fake_retrieval(monkeypatch, misses_first=True)

    updates = evidence_node.build_evidence_pack(_state())

    assert not any(
        entry["event"] == "evidence_tool:targeted_retry_start"
        for entry in updates["trace"]
    )
    retry_done = next(
        entry
        for entry in updates["trace"]
        if entry["event"] == "evidence_tool:targeted_retry_done"
    )
    assert re.fullmatch(r"ec_[0-9a-f]{12}", retry_done["cache_id"])
    assert retry_done["retrieval_status"] == "matched"
    assert "cache_key" not in retry_done
    assert "facts_preview" not in retry_done
    assert "source" not in retry_done


def test_unsupported_web_trace_uses_compact_cache_identifier(monkeypatch):
    monkeypatch.setattr(evidence_node, "get_collection", lambda: None)
    query = "tin tức bí mật của khách hàng"
    state = {
        "run_id": "run-compact-web-log",
        "dataset_id": "dataset-compact-web-log",
        "user_query": query,
        "worker_plan": {
            "need_web": True,
            "evidence_plan": [{"table": "", "query": query}],
        },
    }

    updates = evidence_node.build_evidence_pack(state)

    unsupported = next(
        entry
        for entry in updates["trace"]
        if entry["event"] == "evidence_tool:unsupported"
    )
    full_cache_key = evidence_node.evidence_cache_key(
        dataset_id=state["dataset_id"],
        table="",
        query=query,
        mode="web",
        generation="",
    )
    trace_text = json.dumps(updates["trace"], ensure_ascii=False)
    assert re.fullmatch(r"ec_[0-9a-f]{12}", unsupported["cache_id"])
    assert unsupported["query_redacted"] is True
    assert "cache_key" not in unsupported
    assert full_cache_key not in trace_text
    assert query not in trace_text
