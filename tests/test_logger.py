import json

from graph import logger


def _serialized_bytes(value: dict) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    )


def test_default_log_recursively_removes_payloads_and_redacts_secrets():
    entry = logger.make_log(
        {"run_id": "run-123", "debug_trace": False},
        "evidence_tool:done",
        status="ok",
        facts_n=1,
        facts_preview=[{"item": "Revenue", "value": "100"}],
        nested={
            "query": "private financial question",
            "evidence_queries": ["private supporting query"],
            "authorization": "Bearer secret-token",
            "result": {"answer": "private answer"},
            "analysis_outputs": {"agent": {"answer": "private analysis"}},
            "retrieval_facts": [{"parsed_value": "100"}],
            "worker_messages": [{"response": "private response"}],
            "requirements": ["private required metric"],
            "tool_calls": [{"args": {"query": "private tool query"}}],
            "children": [
                {
                    "raw_value": "19,717,000,000,000",
                    "source": "/private/reports/company.md",
                    "status": "matched",
                }
            ],
        },
    )

    assert entry["event"] == "evidence_tool:done"
    assert entry["run_id"] == "run-123"
    assert entry["status"] == "ok"
    assert entry["facts_n"] == 1
    assert entry["facts_preview_redacted"] is True
    assert "facts_preview" not in entry

    nested = entry["nested"]
    assert nested["query_redacted"] is True
    assert nested["evidence_queries_redacted"] is True
    assert nested["authorization"] == "<redacted>"
    assert nested["result_redacted"] is True
    assert nested["analysis_outputs_redacted"] is True
    assert nested["retrieval_facts_redacted"] is True
    assert nested["worker_messages_redacted"] is True
    assert nested["requirements_redacted"] is True
    assert nested["tool_calls_redacted"] is True
    assert nested["children"][0] == {
        "raw_value_redacted": True,
        "source_redacted": True,
        "status": "matched",
    }


def test_default_log_keeps_non_path_source_but_hides_source_paths():
    entry = logger.make_log(
        {},
        "source:test",
        retrieval={"source": "qdrant", "source_table": "balance_sheet"},
        file_metadata={"source": "report.pdf", "document_path": "/tmp/report.pdf"},
    )

    assert entry["retrieval"] == {
        "source": "qdrant",
        "source_table": "balance_sheet",
    }
    assert entry["file_metadata"] == {
        "source_redacted": True,
        "document_path_redacted": True,
    }


def test_empty_payload_fields_are_omitted_without_noisy_redaction_markers():
    entry = logger.make_log(
        {},
        "empty:payload",
        query="",
        canonical_query="",
        facts_preview=[],
        result=None,
        status="ok",
    )

    assert entry == {
        "event": "empty:payload",
        "timestamp": entry["timestamp"],
        "status": "ok",
    }


def test_debug_trace_is_explicit_payload_opt_in_but_never_exposes_secrets():
    entry = logger.make_log(
        {"debug_trace": True},
        "debug:payload",
        query="financial question",
        facts_preview=[
            {
                "parsed_value": "19717000000000",
                "source": "/private/reports/company.md",
                "api_key": "do-not-log",
            }
        ],
    )

    assert entry["query"] == "financial question"
    assert entry["facts_preview"][0]["parsed_value"] == "19717000000000"
    assert entry["facts_preview"][0]["source"] == "/private/reports/company.md"
    assert entry["facts_preview"][0]["api_key"] == "<redacted>"


def test_make_debug_log_remains_debug_only():
    assert logger.make_debug_log({}, "debug:event", query="hidden") is None

    entry = logger.make_debug_log(
        {"debug_trace": True},
        "debug:event",
        query="visible in explicit debug trace",
    )
    assert entry is not None
    assert entry["debug"] is True
    assert entry["query"] == "visible in explicit debug trace"


def test_default_log_caps_nested_collections_strings_and_total_event_size():
    entry = logger.make_log(
        {},
        "oversized:event",
        status="ok",
        duration_ms=125,
        giant="x" * 20_000,
        items=[{"label": f"item-{index}", "detail": "y" * 500} for index in range(200)],
    )

    assert entry["event"] == "oversized:event"
    assert entry["status"] == "ok"
    assert entry["duration_ms"] == 125
    assert entry["log_truncated"] is True
    assert entry["log_original_bytes"] > logger._DEFAULT_MAX_EVENT_BYTES
    assert _serialized_bytes(entry) <= logger._DEFAULT_MAX_EVENT_BYTES


def test_collection_caps_are_visible_without_dumping_the_omitted_items():
    entry = logger.make_log(
        {},
        "bounded:list",
        short_items=list(range(logger._DEFAULT_MAX_LIST_ITEMS + 8)),
    )

    assert entry["short_items"][-1] == {"_truncated_items": 8}
    assert len(entry["short_items"]) == logger._DEFAULT_MAX_LIST_ITEMS + 1
