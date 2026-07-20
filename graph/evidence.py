"""Build a shared evidence pack from router evidence_plan before analysis runs."""
# Code note: Evidence executor replaces LLM retrieval workers with deterministic scoped retrieval and shared cache.

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from agents.agent_registry import is_analysis_agent
from graph.dispatch_nodes import prepare_analysis_dispatch_state
from graph.logger import make_log
from schemas.table_names import TABLE_NOTE, TABLE_REPORT_SECTION
from tools.evidence import (
    cache_item_from_result,
    dedupe_facts,
    evidence_cache_key,
    get_runtime_cache_item,
    merge_worker_fact_payload,
    normalize_evidence_table,
    normalize_evidence_query,
    filter_facts_for_query,
    result_to_facts,
    set_runtime_cache_item,
)
from tools.tool_runner import get_collection
from tools.tools import get_related_info, needs_full_schedule, web_search


# Defaults validated on batch apec q181-210 v3 (2026-07-19): raising 5->10/12
# lifted context_recall 0.545->0.609 AND context_precision 0.466->0.528 — the
# gold rows previously cut at rank 6-15 (entity/per-class note rows) carry both.
EVIDENCE_FACTS_LIMIT = int(os.getenv("EVIDENCE_FACTS_LIMIT", "10"))
NOTE_EVIDENCE_FACTS_LIMIT = int(os.getenv("NOTE_FACTS_LIMIT", "12"))
EASY_NOTE_OR_REPORT_SECTION_FACTS_LIMIT = 10
NOTE_LLM_FACTS_LIMIT = NOTE_EVIDENCE_FACTS_LIMIT
# List / superlative / per-entity questions need EVERY row of a note schedule
# (per-project receivables, per-borrower loans, ~20 rows) in the evidence pack —
# the default 5-fact cap structurally zeroes their context_recall. Matches the
# retrieval-side _SCHEDULE_LIMIT in tools/tools.py.
SCHEDULE_FACTS_LIMIT = int(os.getenv("SCHEDULE_FACTS_LIMIT", "24"))
# Main-statement routes on schedule questions carry a cross-table note slice
# (e.g. the 4-class × 2-value V.9 block), not a whole 24-row schedule — a
# tighter cap keeps precision while the NOTE route holds the full schedule.
SCHEDULE_MAIN_FACTS_LIMIT = int(os.getenv("SCHEDULE_MAIN_FACTS_LIMIT", "16"))
NOTE_REF_FACTS_SCAN_LIMIT = 15
EVIDENCE_VALUE_PREVIEW_LIMIT = 220
EVIDENCE_HINT_PREVIEW_LIMIT = 180
WEB_RESULT_KEY = "WEB"
NOTE_REF_SCOPE = "note_ref"


def _dedupe_keep_order(items: list[Any]) -> list[str]:
    seen = set()
    output = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _needby_values(item: dict) -> list[str]:
    if not isinstance(item, dict):
        return []
    raw = item.get("needby")
    if raw is None:
        raw = item.get("needed_by")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    return _dedupe_keep_order(
        [
            str(agent).strip()
            for agent in values
            if is_analysis_agent(str(agent).strip())
        ]
    )


def _evidence_item_queries(item: dict) -> list[str]:
    if not isinstance(item, dict):
        return []

    queries = []
    query = str(item.get("query", "") or "").strip()
    if query:
        queries.append(query)

    value = item.get("queries")
    if isinstance(value, (list, tuple, set)):
        queries.extend(str(query).strip() for query in value if str(query).strip())
    elif str(value or "").strip():
        queries.append(str(value).strip())

    return _dedupe_keep_order(queries)


def _evidence_query_metadata(item: dict, map_key: str, scalar_key: str, query: str) -> str:
    values = item.get(map_key)
    if isinstance(values, dict):
        direct = str(values.get(query, "") or "").strip()
        if direct:
            return direct
    return str(item.get(scalar_key, "") or "").strip()


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _compact_fact_for_prompt(fact: dict) -> dict:
    if not isinstance(fact, dict):
        return {}

    compact = {}
    for key in (
        "content_type",
        "table",
        "item_name",
        "time_hint",
        "value",
        "source",
        "status",
        "message",
        "note_ref",
        "note_number",
        "note_title",
        "subheading",
    ):
        value = fact.get(key)
        if value in ("", None, [], {}):
            continue
        limit = EVIDENCE_VALUE_PREVIEW_LIMIT if key == "value" else EVIDENCE_HINT_PREVIEW_LIMIT
        if key in {"item_name", "source", "table", "time_hint", "status", "content_type"}:
            limit = 120
        compact[key] = _compact_text(value, limit=limit)
    return compact


def _annotate_facts_for_route(
    facts: list[dict],
    *,
    needby: list[str] | None = None,
    evidence_query: str = "",
    source_table: str = "",
    source_item: str = "",
) -> list[dict]:
    routed_facts = []
    needed_by = _dedupe_keep_order(needby or [])
    query_text = str(evidence_query or "").strip()
    source_table_text = str(source_table or "").strip()
    source_item_text = str(source_item or "").strip()

    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        payload = dict(fact)
        if needed_by:
            payload["needby"] = _dedupe_keep_order(
                _needby_values(payload) + needed_by
            )
        if query_text:
            payload["evidence_query"] = query_text
        if source_table_text:
            payload["source_table"] = source_table_text
        if source_item_text:
            payload["source_item"] = source_item_text
        routed_facts.append(payload)

    return routed_facts


def _compact_facts_for_prompt(facts: Any) -> list[dict]:
    if not isinstance(facts, list):
        return []
    return [
        compact
        for compact in (_compact_fact_for_prompt(fact) for fact in facts)
        if compact
    ]


def _compact_worker_result_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    if not isinstance(payload.get("facts"), list):
        return payload
    compact = {
        "table": _compact_text(payload.get("table", ""), limit=120),
        "facts": _compact_facts_for_prompt(payload.get("facts", [])),
    }
    return {key: value for key, value in compact.items() if value not in ("", None, [], {})}


def _compact_worker_results(results: dict) -> dict:
    output = {}
    for key, payload in (results or {}).items():
        output[key] = _compact_worker_result_payload(payload)
    return output


def _compact_evidence_targets(targets: list[dict]) -> list[dict]:
    compact_targets = []
    for target in targets or []:
        if not isinstance(target, dict):
            continue
        payload = {
            "mode": str(target.get("mode", "") or "").strip(),
            "table": str(target.get("table", "") or "").strip(),
            "requirements": _dedupe_keep_order(target.get("requirements", []) or [])[:8],
        }
        compact_targets.append(
            {
                key: value
                for key, value in payload.items()
                if value not in ("", None, [], {})
            }
        )
    return compact_targets


def _compact_evidence_item(item: dict) -> dict:
    payload = {
        "scope": str(item.get("scope", "") or "").strip(),
        "table": str(item.get("table", "") or "").strip(),
        "query": _compact_text(item.get("query", ""), limit=120),
        "note_ref": _compact_text(item.get("note_ref", ""), limit=40),
        "source_table": _compact_text(item.get("source_table", ""), limit=120),
        "source_item": _compact_text(item.get("source_item", ""), limit=120),
        "needby": _needby_values(item),
        "facts_n": int(item.get("facts_n", 0) or 0),
        "source": _compact_text(item.get("source", ""), limit=120),
        "cache_hit": bool(item.get("cache_hit", False)),
        "facts_preview": _compact_facts_for_prompt(item.get("facts_preview", []) or []),
    }
    return {
        key: value
        for key, value in payload.items()
        if value not in ("", None, [], {})
    }


def _compact_evidence_items(items: list[dict]) -> list[dict]:
    return [
        compact
        for compact in (_compact_evidence_item(item) for item in items or [])
        if compact
    ]


def _normalized_retrieval_targets(worker_plan: dict) -> list[dict]:
    evidence_plan = worker_plan.get("evidence_plan", []) or []
    allow_web = bool(worker_plan.get("need_web", False))
    if evidence_plan:
        grouped: dict[tuple[str, str], dict] = {}
        order: list[tuple[str, str]] = []
        for item in evidence_plan:
            if not isinstance(item, dict):
                continue
            table = normalize_evidence_table(item.get("table", ""))
            mode = "table" if table else "web"
            if mode == "web" and not allow_web:
                continue
            key = (mode, table)
            if key not in grouped:
                grouped[key] = {
                    "mode": mode,
                    "table": table,
                    "requirements": [],
                    "needby_by_query": {},
                    "search_queries": {},
                    "canonical_queries": {},
                    "source": str(item.get("source", "") or "").strip(),
                    "evidence_items": [],
                }
                order.append(key)
            for raw_query in _evidence_item_queries(item):
                canonical_query = normalize_evidence_query(
                    _evidence_query_metadata(item, "canonical_queries", "canonical_query", raw_query)
                    or raw_query,
                    table=table,
                )
                query = raw_query or canonical_query
                if not query and mode != "web":
                    continue
                if query:
                    grouped[key]["requirements"].append(query)
                    grouped[key]["needby_by_query"][query] = _dedupe_keep_order(
                        list(grouped[key]["needby_by_query"].get(query, []) or [])
                        + _needby_values(item)
                    )
                    if canonical_query:
                        grouped[key]["canonical_queries"][query] = canonical_query
                    search_query = (
                        _evidence_query_metadata(item, "search_queries", "search_query", raw_query)
                        or str(
                            item.get("original_query")
                            or item.get("raw_query")
                            or raw_query
                            or ""
                        ).strip()
                    )
                    if search_query:
                        grouped[key]["search_queries"][query] = search_query
            grouped[key]["evidence_items"].append(dict(item))

        output = []
        for key in order:
            target = grouped[key]
            target["requirements"] = _dedupe_keep_order(target.get("requirements", []) or [])
            if target["requirements"] or target.get("mode") == "web":
                output.append(target)
        return output

    targets = []
    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue
        table = normalize_evidence_table(target.get("table", ""))
        mode = "table" if table else "web"
        if mode == "web" and not allow_web:
            continue
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])
        if not requirements and mode != "web":
            continue
        targets.append(
            {
                "mode": mode,
                "table": table,
                "requirements": requirements,
                "source": str(target.get("source", "") or "").strip(),
            }
        )
    return targets


def _planned_analysis_agents(worker_plan: dict) -> list[str]:
    agents = []
    for target in (worker_plan.get("analysis_plan", []) or []):
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if agent and is_analysis_agent(agent) and agent not in agents:
            agents.append(agent)

    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if agent and is_analysis_agent(agent) and agent not in agents:
            agents.append(agent)
    return agents


def _difficulty_level_from_state(state: dict, worker_plan: dict) -> str:
    for source in (state.get("planner_plan", {}), worker_plan):
        if not isinstance(source, dict):
            continue
        difficulty = str(source.get("difficulty_level", "") or "").strip().lower()
        if difficulty in {"easy", "medium", "hard"}:
            return difficulty
    return ""


def _easy_note_or_report_section_only(state: dict, worker_plan: dict) -> bool:
    if _difficulty_level_from_state(state, worker_plan) != "easy":
        return False

    targets = _normalized_retrieval_targets(worker_plan)
    if not targets:
        return False

    allowed_tables = {TABLE_NOTE, TABLE_REPORT_SECTION}
    for target in targets:
        table = normalize_evidence_table(target.get("table", ""))
        mode = str(target.get("mode", "") or "").strip().lower() or ("table" if table else "web")
        if mode != "table" or table not in allowed_tables:
            return False
    return True


def _facts_limit_for_table(state: dict, worker_plan: dict, table: str) -> int:
    table_name = normalize_evidence_table(table)
    # Any-table, not just NOTE: get_related_info already widens its rerank cut to
    # _SCHEDULE_LIMIT for schedule questions on main-table routes, and a per-class
    # note schedule (V.9) answering a BS-routed question would otherwise be sliced
    # back to EVIDENCE_FACTS_LIMIT by result_to_facts.
    if needs_full_schedule(str((state or {}).get("user_query", "") or "")):
        if table_name == TABLE_NOTE:
            return SCHEDULE_FACTS_LIMIT
        return SCHEDULE_MAIN_FACTS_LIMIT
    if (
        table_name in {TABLE_NOTE, TABLE_REPORT_SECTION}
        and _easy_note_or_report_section_only(state, worker_plan)
    ):
        return EASY_NOTE_OR_REPORT_SECTION_FACTS_LIMIT
    if table_name == TABLE_NOTE:
        return NOTE_EVIDENCE_FACTS_LIMIT
    return EVIDENCE_FACTS_LIMIT


def _effective_analysis_agents(state: dict, worker_plan: dict, agents: list[str]) -> list[str]:
    difficulty = _difficulty_level_from_state(state, worker_plan)
    if difficulty in {"easy", "medium"}:
        return []
    return agents


def _should_fetch_note_ref_context(state: dict, worker_plan: dict, planned_agents: list[str]) -> bool:
    difficulty = _difficulty_level_from_state(state, worker_plan)
    if difficulty in {"easy", "medium"}:
        return False
    if difficulty == "hard":
        return True
    return bool(planned_agents)


def _web_result_to_payload(result: dict, query: str) -> dict:
    context = str((result or {}).get("context", "") or "").strip()
    return {
        "table": "",
        "facts": [
            {
                "content_type": "web_fact",
                "item_name": query,
                "time_hint": "",
                "value": context,
                "source": str((result or {}).get("source", "") or "").strip(),
                "table": "",
                "status": "found" if context else "not_found_after_search",
                "evidence_text": context,
            }
        ] if context else [],
    }


def _compact_log_text(value: Any, *, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _limit_evidence_facts(facts: Any, *, limit: int = EVIDENCE_FACTS_LIMIT) -> list[dict]:
    if not isinstance(facts, list):
        return []
    return [
        fact
        for fact in facts
        if isinstance(fact, dict)
    ][:limit]


def _limit_evidence_facts_for_table(
    table: str,
    facts: Any,
    *,
    state: dict | None = None,
    worker_plan: dict | None = None,
) -> list[dict]:
    limit = _facts_limit_for_table(state or {}, worker_plan or {}, table)
    return _limit_evidence_facts(facts, limit=limit)


def _facts_log_preview(facts: Any, *, limit: int = EVIDENCE_FACTS_LIMIT) -> list[dict]:
    if not isinstance(facts, list):
        return []

    preview = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        preview.append(
            {
                "table": _compact_log_text(fact.get("table", ""), limit=80),
                "item_name": _compact_log_text(fact.get("item_name", ""), limit=120),
                "note_ref": _compact_log_text(fact.get("note_ref", ""), limit=40),
                "time_hint": _compact_log_text(fact.get("time_hint", ""), limit=80),
                "value": _compact_log_text(fact.get("value", ""), limit=160),
                "status": _compact_log_text(fact.get("status", ""), limit=60),
                "source": _compact_log_text(fact.get("source", ""), limit=120),
            }
        )
        if len(preview) >= limit:
            break
    return preview


def _merge_worker_results(existing: dict, current: dict) -> dict:
    merged = dict(existing or {})
    for result_key, payload in (current or {}).items():
        merged[result_key] = merge_worker_fact_payload(merged.get(result_key, {}), payload)
    return merged


def _llm_facts_limit_for_table(state: dict, worker_plan: dict, table: str) -> int:
    table_name = normalize_evidence_table(table)
    if table_name == TABLE_NOTE and needs_full_schedule(
        str((state or {}).get("user_query", "") or "")
    ):
        # ragas_facts_by_table (RAGAS retrieved_contexts) flows through this cap
        # too, so schedule questions must keep the whole per-entity schedule.
        return SCHEDULE_FACTS_LIMIT
    if (
        table_name in {TABLE_NOTE, TABLE_REPORT_SECTION}
        and _easy_note_or_report_section_only(state, worker_plan)
    ):
        return EASY_NOTE_OR_REPORT_SECTION_FACTS_LIMIT
    if table_name == TABLE_NOTE:
        return NOTE_LLM_FACTS_LIMIT
    return EVIDENCE_FACTS_LIMIT


def _limit_note_facts_for_llm(results: dict, *, state: dict, worker_plan: dict) -> dict:
    output = {}
    for result_key, payload in (results or {}).items():
        if not isinstance(payload, dict):
            output[result_key] = payload
            continue

        item = dict(payload)
        table = _result_key_for_table(item.get("table", "") or result_key)
        facts = item.get("facts", [])
        if table in {TABLE_NOTE, TABLE_REPORT_SECTION} and isinstance(facts, list):
            limit = _llm_facts_limit_for_table(state, worker_plan, table)
            item["facts"] = [
                fact
                for fact in facts
                if isinstance(fact, dict)
            ][:limit]
        output[result_key] = item
    return output


def _limit_note_item_previews_for_llm(items: list[dict], *, state: dict, worker_plan: dict) -> list[dict]:
    output = []
    facts_seen_by_table: dict[str, int] = {}

    for item in items or []:
        if not isinstance(item, dict):
            continue

        payload = dict(item)
        table = normalize_evidence_table(payload.get("table", ""))
        previews = payload.get("facts_preview", [])
        if table in {TABLE_NOTE, TABLE_REPORT_SECTION} and isinstance(previews, list):
            limit = _llm_facts_limit_for_table(state, worker_plan, table)
            seen = int(facts_seen_by_table.get(table, 0) or 0)
            remaining = max(limit - seen, 0)
            limited_previews = [
                fact
                for fact in previews
                if isinstance(fact, dict)
            ][:remaining]
            facts_seen_by_table[table] = seen + len(limited_previews)
            payload["facts_preview"] = limited_previews
        output.append(payload)

    return output


def _result_key_for_table(table: str = "", *, mode: str = "table") -> str:
    table_name = normalize_evidence_table(table)
    if str(mode or "").strip().lower() == "web" or not table_name:
        return WEB_RESULT_KEY
    return table_name


def _row_label_from_fact(fact: dict) -> str:
    item_name = str((fact or {}).get("item_name", "") or "").strip()
    return item_name.split("|", 1)[0].strip()


def _is_valid_note_ref(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text.strip(" -–—.;,") != ""


def _note_ref_queries_from_results(worker_results: dict) -> list[dict]:
    queries = []
    seen = set()

    for result_key, payload in (worker_results or {}).items():
        if not isinstance(payload, dict):
            continue
        table = _result_key_for_table(payload.get("table", "") or result_key)
        if not table or table == TABLE_NOTE or table == WEB_RESULT_KEY:
            continue

        for fact in payload.get("facts", []) or []:
            if not isinstance(fact, dict):
                continue
            note_ref = str(fact.get("note_ref", "") or "").strip()
            if not _is_valid_note_ref(note_ref):
                continue
            row_label = _row_label_from_fact(fact)
            if not row_label:
                continue

            key = (note_ref, row_label.lower())
            if key in seen:
                continue
            seen.add(key)
            queries.append(
                {
                    "query": f"thuyết minh {note_ref} {row_label}",
                    "note_ref": note_ref,
                    "source_table": table,
                    "source_item": row_label,
                    "source_query": str(fact.get("evidence_query", "") or "").strip(),
                    "needby": _needby_values(fact),
                }
            )

    return queries


def _fact_matches_note_ref(fact: dict, note_ref: str) -> bool:
    ref = str(note_ref or "").strip()
    if not ref or not isinstance(fact, dict):
        return False
    text = " ".join(
        str(fact.get(key, "") or "")
        for key in ("item_name", "subheading")
    )
    return bool(
        re.search(
            rf"\bthuyết\s+minh\s+{re.escape(ref)}(?=\D|$)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _filter_note_ref_facts(facts: list[dict], note_ref: str) -> list[dict]:
    exact_facts = [
        fact
        for fact in facts or []
        if _fact_matches_note_ref(fact, note_ref)
    ]
    return exact_facts or list(facts or [])


def _coerce_existing_worker_results(existing: dict) -> dict:
    output = {}
    for key, payload in (existing or {}).items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        if is_analysis_agent(key_text):
            output[key_text] = payload
            continue
        output[key_text] = payload
    return output


def build_evidence_pack(state: dict) -> dict:
    started_at = time.perf_counter()
    worker_plan = state.get("worker_plan", {}) or {}
    collection = get_collection()
    trace = []

    retrieval_targets = _normalized_retrieval_targets(worker_plan)
    planned_analysis_agents = _effective_analysis_agents(
        state,
        worker_plan,
        _planned_analysis_agents(worker_plan),
    )

    if collection is None and any(target.get("mode") != "web" for target in retrieval_targets):
        return {
            "evidence_pack": {
                "items": [],
                "facts_by_table": {},
                "targets": retrieval_targets,
                "error": "collection_not_set",
            },
            "worker_results": {},
            "expected_workers": [],
            "dispatch_phase": "synth",
            "collect_decision": "synth",
            "trace": [
                make_log(
                    state,
                    "evidence:error",
                    error="collection not set. Call set_collection(collection) before running workflow.",
                )
            ],
        }

    existing_cache = state.get("evidence_cache", {}) or {}
    evidence_cache_updates = {}
    evidence_items = []
    current_worker_results: dict[str, dict] = {}
    web_summary_payload = {}
    processed_keys = set()
    retrieval_calls = 0
    cache_hits = 0

    for target in retrieval_targets:
        table = normalize_evidence_table(target.get("table", ""))
        mode = str(target.get("mode", "") or "").strip().lower() or ("table" if table else "web")
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])

        if mode == "web":
            query = " ".join(requirements) or str(state.get("user_query", "") or "").strip()
            needby = _dedupe_keep_order(
                agent
                for requirement in requirements
                for agent in (target.get("needby_by_query", {}) or {}).get(requirement, [])
            )
            cache_key = evidence_cache_key(
                dataset_id=str(state.get("dataset_id", "") or ""),
                table="",
                query=query,
                mode="web",
            )
            if cache_key in processed_keys:
                continue
            processed_keys.add(cache_key)

            cached = (
                existing_cache.get(cache_key)
                or evidence_cache_updates.get(cache_key)
                or get_runtime_cache_item(cache_key)
            )
            if isinstance(cached, dict) and cached:
                cache_hits += 1
                result = cached
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:cache_hit",
                        tool="web_search",
                        scope="web",
                        table="",
                        query=query,
                        cache_key=cache_key,
                        facts_n=len(_limit_evidence_facts(result.get("facts", []))),
                        facts_preview=_facts_log_preview(_limit_evidence_facts(result.get("facts", []))),
                    )
                )
            else:
                tool_started_at = time.perf_counter()
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:start",
                        tool="web_search",
                        scope="web",
                        table="",
                        query=query,
                        cache_key=cache_key,
                    )
                )
                raw_result = web_search(query)
                web_payload = _web_result_to_payload(raw_result, query)
                facts = _limit_evidence_facts(web_payload.get("facts", []))
                result = {
                    "tool": "web_search",
                    "table": "",
                    "query": query,
                    "canonical_query": query,
                    "context": str(raw_result.get("context", "") or ""),
                    "source": str(raw_result.get("source", "") or ""),
                    "documents": [],
                    "metadatas": [],
                    "facts": facts,
                }
                evidence_cache_updates[cache_key] = result
                set_runtime_cache_item(cache_key, result)
                retrieval_calls += 1
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:done",
                        tool="web_search",
                        scope="web",
                        table="",
                        query=query,
                        cache_key=cache_key,
                        source=result.get("source", ""),
                        context_len=len(str(result.get("context", "") or "")),
                        facts_n=len(result.get("facts", []) or []),
                        facts_preview=_facts_log_preview(result.get("facts", [])),
                        cache_stored=True,
                        duration_ms=int((time.perf_counter() - tool_started_at) * 1000),
                    )
                )

            payload = _web_result_to_payload(result, query)
            payload["facts"] = _limit_evidence_facts(payload.get("facts", []))
            payload["facts"] = _annotate_facts_for_route(
                payload.get("facts", []),
                needby=needby,
                evidence_query=query,
            )
            result_key = _result_key_for_table("", mode="web")
            current_worker_results[result_key] = merge_worker_fact_payload(
                current_worker_results.get(result_key, {}),
                payload,
            )
            web_summary_payload[cache_key] = {
                "query": query,
                "source": result.get("source", ""),
                "facts": _compact_facts_for_prompt(payload.get("facts", [])),
                "context_preview": _compact_text(result.get("context", ""), limit=360),
            }
            evidence_items.append(
                {
                    "key": cache_key,
                    "scope": "web",
                    "table": "",
                    "query": query,
                    "needby": needby,
                    "cache_hit": isinstance(cached, dict) and bool(cached),
                    "facts_n": len(payload.get("facts", []) or []),
                    "facts_preview": _facts_log_preview(payload.get("facts", [])),
                    "source": result.get("source", ""),
                }
            )
            continue

        for requirement in requirements:
            query = str(requirement or "").strip()
            needby = _dedupe_keep_order(
                (target.get("needby_by_query", {}) or {}).get(query, []) or []
            )
            canonical_query = normalize_evidence_query(
                (target.get("canonical_queries") or {}).get(query) or query,
                table=table,
            )
            search_query = str((target.get("search_queries") or {}).get(query, "") or query).strip()
            cache_key = evidence_cache_key(
                dataset_id=str(state.get("dataset_id", "") or ""),
                table=table,
                query=search_query,
                mode="table",
                intent=str(state.get("user_query", "") or ""),
            )
            if cache_key in processed_keys:
                continue
            processed_keys.add(cache_key)

            cached = (
                existing_cache.get(cache_key)
                or evidence_cache_updates.get(cache_key)
                or get_runtime_cache_item(cache_key)
            )
            if isinstance(cached, dict) and cached:
                result = cached
                cache_facts = filter_facts_for_query(
                    dedupe_facts(result.get("facts", []) or []),
                    table=table,
                    query=canonical_query or query,
                    source=str(result.get("source", "") or ""),
                )
                facts = _limit_evidence_facts_for_table(
                    table,
                    cache_facts,
                    state=state,
                    worker_plan=worker_plan,
                )
                if table == TABLE_NOTE:
                    result = dict(result)
                    result["facts"] = cache_facts
                cache_hits += 1
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:cache_hit",
                        tool="get_related_info",
                        scope="table",
                        table=table,
                        query=query,
                        canonical_query=canonical_query if canonical_query != query else "",
                        search_query=search_query if search_query != query else "",
                        cache_key=cache_key,
                        facts_n=len(facts),
                        facts_preview=_facts_log_preview(facts),
                    )
                )
            else:
                facts_limit = _facts_limit_for_table(state, worker_plan, table)
                retrieval_limit = (
                    max(NOTE_REF_FACTS_SCAN_LIMIT, facts_limit)
                    if table == TABLE_NOTE
                    else facts_limit
                )
                tool_started_at = time.perf_counter()
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:start",
                        tool="get_related_info",
                        scope="table",
                        table=table,
                        query=query,
                        canonical_query=canonical_query if canonical_query != query else "",
                        search_query=search_query if search_query != query else "",
                        cache_key=cache_key,
                        strict_table=(table in {TABLE_NOTE, TABLE_REPORT_SECTION}),
                    )
                )
                raw_result = get_related_info(
                    query=search_query,
                    table=table,
                    collection=collection,
                    strict_table=(table in {TABLE_NOTE, TABLE_REPORT_SECTION}),
                    limit=retrieval_limit,
                    intent=str(state.get("user_query", "") or "").strip(),
                )
                cache_facts = result_to_facts(
                    raw_result,
                    table=table,
                    query=canonical_query,
                    limit=retrieval_limit,
                )
                facts = _limit_evidence_facts_for_table(
                    table,
                    cache_facts,
                    state=state,
                    worker_plan=worker_plan,
                )
                result = cache_item_from_result(
                    raw_result,
                    table=table,
                    query=query,
                    tool="get_related_info",
                    facts=cache_facts,
                )
                result["canonical_query"] = canonical_query
                result["search_query"] = search_query
                evidence_cache_updates[cache_key] = result
                set_runtime_cache_item(cache_key, result)
                retrieval_calls += 1
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:done",
                        tool="get_related_info",
                        scope="table",
                        table=table,
                        query=query,
                        canonical_query=canonical_query if canonical_query != query else "",
                        search_query=search_query if search_query != query else "",
                        cache_key=cache_key,
                        source=result.get("source", ""),
                        context_len=len(str(result.get("context", "") or "")),
                        facts_n=len(facts),
                        facts_preview=_facts_log_preview(facts),
                        cache_stored=True,
                        duration_ms=int((time.perf_counter() - tool_started_at) * 1000),
                    )
                )

            payload = {
                "table": table,
                "facts": _annotate_facts_for_route(
                    facts,
                    needby=needby,
                    evidence_query=query,
                ),
            }
            result_key = _result_key_for_table(table, mode="table")
            current_worker_results[result_key] = merge_worker_fact_payload(
                current_worker_results.get(result_key, {}),
                payload,
            )
            evidence_items.append(
                {
                    "key": cache_key,
                    "scope": "table",
                    "table": table,
                    "query": query,
                    "needby": needby,
                    "cache_hit": isinstance(cached, dict) and bool(cached),
                    "facts_n": len(facts),
                    "facts_preview": _facts_log_preview(facts),
                    "source": result.get("source", ""),
                }
            )

    if _should_fetch_note_ref_context(state, worker_plan, planned_analysis_agents):
        for note_request in _note_ref_queries_from_results(current_worker_results):
            query = str(note_request.get("query", "") or "").strip()
            if not query:
                continue
            cache_key = evidence_cache_key(
                dataset_id=str(state.get("dataset_id", "") or ""),
                table=TABLE_NOTE,
                query=query,
                mode="table",
                intent=str(state.get("user_query", "") or ""),
            )
            if cache_key in processed_keys:
                continue
            processed_keys.add(cache_key)

            cached = (
                existing_cache.get(cache_key)
                or evidence_cache_updates.get(cache_key)
                or get_runtime_cache_item(cache_key)
            )
            if isinstance(cached, dict) and cached:
                result = cached
                cache_facts = _filter_note_ref_facts(
                    filter_facts_for_query(
                        dedupe_facts(result.get("facts", []) or []),
                        table=TABLE_NOTE,
                        query=query,
                        source=str(result.get("source", "") or ""),
                    ),
                    str(note_request.get("note_ref", "") or ""),
                )
                facts = _limit_evidence_facts_for_table(
                    TABLE_NOTE,
                    cache_facts,
                    state=state,
                    worker_plan=worker_plan,
                )
                result = dict(result)
                result["facts"] = cache_facts
                cache_hits += 1
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:cache_hit",
                        tool="get_related_info",
                        scope=NOTE_REF_SCOPE,
                        table=TABLE_NOTE,
                        query=query,
                        note_ref=note_request.get("note_ref", ""),
                        source_table=note_request.get("source_table", ""),
                        source_item=note_request.get("source_item", ""),
                        cache_key=cache_key,
                        facts_n=len(facts),
                        facts_preview=_facts_log_preview(facts),
                    )
                )
            else:
                tool_started_at = time.perf_counter()
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:start",
                        tool="get_related_info",
                        scope=NOTE_REF_SCOPE,
                        table=TABLE_NOTE,
                        query=query,
                        note_ref=note_request.get("note_ref", ""),
                        source_table=note_request.get("source_table", ""),
                        source_item=note_request.get("source_item", ""),
                        cache_key=cache_key,
                        strict_table=True,
                    )
                )
                raw_result = get_related_info(
                    query=query,
                    table=TABLE_NOTE,
                    collection=collection,
                    strict_table=True,
                    limit=NOTE_REF_FACTS_SCAN_LIMIT,
                    intent=str(state.get("user_query", "") or "").strip(),
                )
                cache_facts = _filter_note_ref_facts(
                    result_to_facts(
                        raw_result,
                        table=TABLE_NOTE,
                        query=query,
                        limit=NOTE_REF_FACTS_SCAN_LIMIT,
                    ),
                    str(note_request.get("note_ref", "") or ""),
                )
                facts = _limit_evidence_facts_for_table(
                    TABLE_NOTE,
                    cache_facts,
                    state=state,
                    worker_plan=worker_plan,
                )
                result = cache_item_from_result(
                    raw_result,
                    table=TABLE_NOTE,
                    query=query,
                    tool="get_related_info",
                    facts=cache_facts,
                )
                evidence_cache_updates[cache_key] = result
                set_runtime_cache_item(cache_key, result)
                retrieval_calls += 1
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:done",
                        tool="get_related_info",
                        scope=NOTE_REF_SCOPE,
                        table=TABLE_NOTE,
                        query=query,
                        note_ref=note_request.get("note_ref", ""),
                        source_table=note_request.get("source_table", ""),
                        source_item=note_request.get("source_item", ""),
                        cache_key=cache_key,
                        source=result.get("source", ""),
                        context_len=len(str(result.get("context", "") or "")),
                        facts_n=len(facts),
                        facts_preview=_facts_log_preview(facts),
                        cache_stored=True,
                        duration_ms=int((time.perf_counter() - tool_started_at) * 1000),
                    )
                )

            payload = {
                "table": TABLE_NOTE,
                "facts": _annotate_facts_for_route(
                    facts,
                    needby=_needby_values(note_request),
                    evidence_query=str(note_request.get("source_query", "") or "").strip() or query,
                    source_table=str(note_request.get("source_table", "") or "").strip(),
                    source_item=str(note_request.get("source_item", "") or "").strip(),
                ),
            }
            current_worker_results[TABLE_NOTE] = merge_worker_fact_payload(
                current_worker_results.get(TABLE_NOTE, {}),
                payload,
            )
            evidence_items.append(
                {
                    "key": cache_key,
                    "scope": NOTE_REF_SCOPE,
                    "table": TABLE_NOTE,
                    "query": query,
                    "note_ref": note_request.get("note_ref", ""),
                    "source_table": note_request.get("source_table", ""),
                    "source_item": note_request.get("source_item", ""),
                    "needby": _needby_values(note_request),
                    "cache_hit": isinstance(cached, dict) and bool(cached),
                    "facts_n": len(facts),
                    "facts_preview": _facts_log_preview(facts),
                    "source": result.get("source", ""),
                }
            )

    merged_worker_results = _limit_note_facts_for_llm(
        _merge_worker_results(
            _coerce_existing_worker_results(state.get("worker_results", {}) or {}),
            current_worker_results,
        ),
        state=state,
        worker_plan=worker_plan,
    )
    compact_worker_results = _compact_worker_results(merged_worker_results)
    evidence_pack = {
        "targets": _compact_evidence_targets(retrieval_targets),
        "items": _compact_evidence_items(
            _limit_note_item_previews_for_llm(
                evidence_items,
                state=state,
                worker_plan=worker_plan,
            )
        ),
        "facts_by_table": {
            result_key: payload
            for result_key, payload in compact_worker_results.items()
            if not is_analysis_agent(str(result_key or "").strip())
        },
        "stats": {
            "targets_n": len(retrieval_targets),
            "items_n": len(evidence_items),
            "retrieval_calls_n": retrieval_calls,
            "cache_hits_n": cache_hits,
            "facts_n": sum(
                len((payload or {}).get("facts", []) or [])
                for payload in merged_worker_results.values()
                if isinstance(payload, dict)
            ),
        },
    }

    updates = {
        "evidence_pack": evidence_pack,
        "ragas_facts_by_table": {
            result_key: payload
            for result_key, payload in merged_worker_results.items()
            if not is_analysis_agent(str(result_key or "").strip())
        },
        "evidence_cache": evidence_cache_updates,
        "worker_results": compact_worker_results,
        "web_summary": json.dumps(web_summary_payload, ensure_ascii=False) if web_summary_payload else state.get("web_summary", ""),
        "last_agent": "evidence_executor",
        "dispatch_phase": "analysis" if planned_analysis_agents else "synth",
        "collect_decision": "analysis" if planned_analysis_agents else "synth",
        "trace": list(trace) + [
            make_log(
                state,
                "evidence:built",
                targets_n=len(retrieval_targets),
                analysis_agents=planned_analysis_agents,
                items_n=len(evidence_items),
                retrieval_calls_n=retrieval_calls,
                cache_hits_n=cache_hits,
                facts_n=evidence_pack["stats"]["facts_n"],
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        ],
    }

    if planned_analysis_agents:
        analysis_updates = prepare_analysis_dispatch_state(
            {
                **state,
                **updates,
                "worker_results": merged_worker_results,
            }
        )
        updates["expected_workers"] = analysis_updates.get("expected_workers", [])
        updates["analysis_dispatch_targets"] = analysis_updates.get("analysis_dispatch_targets", [])
        updates["dispatch_phase"] = analysis_updates.get("dispatch_phase", "analysis")
        updates["trace"] = list(updates.get("trace", []) or []) + list(analysis_updates.get("trace", []) or [])
    else:
        updates["expected_workers"] = []
        updates["analysis_dispatch_targets"] = []

    return updates
