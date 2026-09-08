"""Bounded-concurrency contracts for the evidence executor."""

from __future__ import annotations

import threading
import time

from graph import evidence as evidence_node
from schemas.table_names import TABLE_IS, TABLE_NOTE
from tools.evidence import clear_runtime_evidence_cache


def _raw_fact(table: str, item_name: str, *, note_ref: str = "") -> dict:
    metadata = {
        "heading": table,
        "item_name": item_name,
        "raw_value": "100",
        "fact_id": f"{table}:{item_name}",
        "source": "report.md",
    }
    if note_ref:
        metadata["note_ref"] = note_ref
    return {
        "context": f"{item_name}: 100",
        "source": "report.md",
        "documents": [f"{item_name}: 100"],
        "metadatas": [metadata],
    }


def _medium_state(dataset_id: str, queries: list[str]) -> dict:
    return {
        "dataset_id": dataset_id,
        "user_query": "Các chỉ tiêu kết quả kinh doanh là bao nhiêu?",
        "debug_trace": True,
        "worker_plan": {
            "difficulty_level": "medium",
            "evidence_plan": [{"table": TABLE_IS, "queries": queries}],
            "analysis_plan": [],
        },
    }


def test_primary_evidence_chains_are_bounded_parallel_and_merge_in_plan_order(
    monkeypatch,
):
    clear_runtime_evidence_cache()
    queries = [
        "doanh thu bán hàng và cung cấp dịch vụ",
        "giá vốn hàng bán",
        "lợi nhuận gộp",
        "doanh thu hoạt động tài chính",
        "chi phí tài chính",
        "lợi nhuận sau thuế thu nhập doanh nghiệp",
    ]
    delays = dict(zip(queries, (0.12, 0.10, 0.08, 0.06, 0.04, 0.02)))
    lock = threading.Lock()
    first_wave_ready = threading.Event()
    active = 0
    max_active = 0
    completion_order: list[str] = []

    def fake_get_related_info(*, query, table, **_kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == evidence_node.EVIDENCE_MAX_CONCURRENCY:
                first_wave_ready.set()
        assert first_wave_ready.wait(timeout=1.0)
        time.sleep(delays[query])
        with lock:
            active -= 1
            completion_order.append(query)
        return _raw_fact(table, query)

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )

    started_at = time.perf_counter()
    updates = evidence_node.build_evidence_pack(
        _medium_state("parallel-plan-order", queries)
    )
    elapsed = time.perf_counter() - started_at

    assert evidence_node.EVIDENCE_MAX_CONCURRENCY == 4
    assert 2 <= max_active <= evidence_node.EVIDENCE_MAX_CONCURRENCY
    # The Event makes the regression deterministic: serial execution cannot
    # release its first call, while a four-worker first wave proceeds at once.
    assert elapsed < 0.50
    assert completion_order != queries
    assert [item["query"] for item in updates["evidence_pack"]["items"]] == queries
    assert [
        item["requirement"]
        for item in updates["evidence_ledger"]["entries"]
    ] == queries
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 6
    assert updates["evidence_pack"]["stats"]["targeted_retries_n"] == 0


def test_each_parallel_chain_keeps_initial_before_targeted_retry(monkeypatch):
    clear_runtime_evidence_cache()
    queries = ["doanh thu thuần", "lợi nhuận sau thuế"]
    calls_by_thread: dict[int, list[bool]] = {}
    lock = threading.Lock()

    def fake_get_related_info(*, query, table, strict_table=False, **_kwargs):
        thread_id = threading.get_ident()
        with lock:
            calls_by_thread.setdefault(thread_id, []).append(strict_table)
        time.sleep(0.02)
        if not strict_table:
            return _raw_fact(table, "dòng không liên quan")
        item_name = (
            "doanh thu thuần"
            if "doanh thu" in query
            else "lợi nhuận sau thuế"
        )
        return _raw_fact(table, item_name)

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )

    updates = evidence_node.build_evidence_pack(
        _medium_state("parallel-retry-order", queries)
    )

    assert sorted(calls_by_thread.values()) == [[False, True], [False, True]]
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 4
    assert updates["evidence_pack"]["stats"]["targeted_retries_n"] == 2
    assert all(
        entry["targeted_retry"]["performed"]
        for entry in updates["evidence_ledger"]["entries"]
    )


def test_note_ref_stage_starts_only_after_all_primary_chains_finish(monkeypatch):
    clear_runtime_evidence_cache()
    queries = ["doanh thu thuần", "lợi nhuận sau thuế"]
    note_refs = {queries[0]: "23", queries[1]: "24"}
    lock = threading.Lock()
    primary_active = 0
    primary_completed = 0
    note_stage_observations: list[tuple[int, int]] = []

    def fake_get_related_info(*, query, table, **_kwargs):
        nonlocal primary_active, primary_completed
        if table != TABLE_NOTE:
            with lock:
                primary_active += 1
            time.sleep(0.04)
            with lock:
                primary_active -= 1
                primary_completed += 1
            return _raw_fact(table, query, note_ref=note_refs[query])

        with lock:
            note_stage_observations.append(
                (primary_active, primary_completed)
            )
        note_ref = "23" if "23" in query else "24"
        return _raw_fact(
            TABLE_NOTE,
            f"Thuyết minh {note_ref} chi tiết",
        )

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )
    state = _medium_state("note-ref-stage-barrier", queries)
    state["planner_plan"] = {"difficulty_level": "hard"}
    state["worker_plan"] = {
        "difficulty_level": "hard",
        "evidence_plan": [{"table": TABLE_IS, "queries": queries}],
        "analysis_plan": [
            {
                "agent": "agent_profitability",
                "objective": "Đánh giá khả năng sinh lời.",
                "evidence_queries": [],
            }
        ],
    }

    updates = evidence_node.build_evidence_pack(state)

    assert note_stage_observations == [(0, 2), (0, 2)]
    assert [
        item["scope"] for item in updates["evidence_pack"]["items"]
    ] == ["table", "table", "note_ref", "note_ref"]
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 4


def test_duplicate_chain_is_deduped_and_warm_cache_counts_are_stable(
    monkeypatch,
):
    clear_runtime_evidence_cache()
    query = "lợi nhuận sau thuế"
    calls = 0

    def fake_get_related_info(*, query, table, **_kwargs):
        nonlocal calls
        calls += 1
        return _raw_fact(table, query)

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )
    state = _medium_state("parallel-cache-single-flight", [query])
    state["worker_plan"]["evidence_plan"].append(
        {"table": TABLE_IS, "query": query}
    )

    first = evidence_node.build_evidence_pack(state)
    second = evidence_node.build_evidence_pack(
        {
            **state,
            "evidence_cache": first["evidence_cache"],
        }
    )

    assert calls == 1
    assert first["evidence_pack"]["stats"]["items_n"] == 1
    assert first["evidence_pack"]["stats"]["retrieval_calls_n"] == 1
    assert first["evidence_pack"]["stats"]["cache_hits_n"] == 0
    assert "|table|" in next(iter(first["evidence_cache"]))
    assert second["evidence_pack"]["stats"]["items_n"] == 1
    assert second["evidence_pack"]["stats"]["retrieval_calls_n"] == 0
    assert second["evidence_pack"]["stats"]["cache_hits_n"] == 1


def test_same_search_query_keeps_distinct_requirement_chains(monkeypatch):
    clear_runtime_evidence_cache()
    requirements = ["lợi nhuận sau thuế", "doanh thu thuần"]
    calls = 0

    def fake_get_related_info(*, table, **_kwargs):
        nonlocal calls
        calls += 1
        first = _raw_fact(table, requirements[0])
        second = _raw_fact(table, requirements[1])
        return {
            "context": "\n".join(
                [first["context"], second["context"]]
            ),
            "source": "report.md",
            "documents": [
                *first["documents"],
                *second["documents"],
            ],
            "metadatas": [
                *first["metadatas"],
                *second["metadatas"],
            ],
        }

    monkeypatch.setattr(evidence_node, "get_collection", lambda: object())
    monkeypatch.setattr(
        evidence_node,
        "get_related_info",
        fake_get_related_info,
    )
    state = _medium_state("shared-search-distinct-requirements", requirements)
    state["worker_plan"]["evidence_plan"] = [
        {
            "table": TABLE_IS,
            "queries": requirements,
            "search_queries": {
                requirement: "báo cáo kết quả kinh doanh"
                for requirement in requirements
            },
        }
    ]

    updates = evidence_node.build_evidence_pack(state)

    assert calls == 2
    assert [
        item["query"] for item in updates["evidence_pack"]["items"]
    ] == requirements
    assert [
        item["requirement"]
        for item in updates["evidence_ledger"]["entries"]
    ] == requirements
    assert len(updates["evidence_cache"]) == 2
    assert all(
        "|table_chain:" in cache_key
        for cache_key in updates["evidence_cache"]
    )
