"""Build a shared evidence pack from router evidence_plan before analysis runs."""
# Code note: Evidence executor replaces LLM retrieval workers with deterministic scoped retrieval and shared cache.

from __future__ import annotations

import json
import time
from typing import Any

from agents.agent_registry import get_default_table, is_analysis_agent, is_retrieval_agent
from graph.dispatch_nodes import prepare_analysis_dispatch_state
from graph.logger import make_log
from schemas.requirements import normalize_requirements_keep_order
from schemas.table_names import TABLE_NOTE
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
from tools.tools import get_related_info, web_search


EVIDENCE_FACTS_LIMIT = 2
EVIDENCE_VALUE_PREVIEW_LIMIT = 220
EVIDENCE_HINT_PREVIEW_LIMIT = 180
WEB_RESULT_KEY = "WEB"


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
        "interpretation_hint",
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
            query = normalize_evidence_query(item.get("query", ""), table=table)
            if not query and mode != "web":
                continue
            key = (mode, table)
            if key not in grouped:
                grouped[key] = {
                    "mode": mode,
                    "table": table,
                    "requirements": [],
                    "source": str(item.get("source", "") or "").strip(),
                    "evidence_items": [],
                }
                order.append(key)
            if query:
                grouped[key]["requirements"].append(query)
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
        agent = str(target.get("agent", "") or "").strip()
        if not is_retrieval_agent(agent):
            continue
        table = normalize_evidence_table(target.get("table", "") or get_default_table(agent))
        mode = "table" if table else "web"
        if mode == "web" and not allow_web:
            continue
        requirements = normalize_requirements_keep_order(
            target.get("requirements", []) or [],
            table=table,
        )
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
                "interpretation_hint": context[:300],
            }
        ] if context else [],
    }


def _compact_log_text(value: Any, *, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _limit_evidence_facts(facts: Any) -> list[dict]:
    if not isinstance(facts, list):
        return []
    return [
        fact
        for fact in facts
        if isinstance(fact, dict)
    ][:EVIDENCE_FACTS_LIMIT]


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


def _result_key_for_table(table: str = "", *, mode: str = "table") -> str:
    table_name = normalize_evidence_table(table)
    if str(mode or "").strip().lower() == "web" or not table_name:
        return WEB_RESULT_KEY
    return table_name


def _coerce_existing_worker_results(existing: dict) -> dict:
    output = {}
    for key, payload in (existing or {}).items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        if is_analysis_agent(key_text):
            output[key_text] = payload
            continue
        if is_retrieval_agent(key_text):
            table = normalize_evidence_table(get_default_table(key_text))
            result_key = _result_key_for_table(table, mode="web" if not table else "table")
            output[result_key] = merge_worker_fact_payload(output.get(result_key, {}), payload)
            continue
        output[key_text] = payload
    return output


def build_evidence_pack(state: dict) -> dict:
    started_at = time.perf_counter()
    worker_plan = state.get("worker_plan", {}) or {}
    collection = get_collection()
    trace = []

    retrieval_targets = _normalized_retrieval_targets(worker_plan)
    planned_analysis_agents = _planned_analysis_agents(worker_plan)

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
                    "cache_hit": isinstance(cached, dict) and bool(cached),
                    "facts_n": len(payload.get("facts", []) or []),
                    "facts_preview": _facts_log_preview(payload.get("facts", [])),
                    "source": result.get("source", ""),
                }
            )
            continue

        for requirement in requirements:
            query = normalize_evidence_query(requirement, table=table)
            cache_key = evidence_cache_key(
                dataset_id=str(state.get("dataset_id", "") or ""),
                table=table,
                query=query,
                mode="table",
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
                facts = _limit_evidence_facts(
                    filter_facts_for_query(
                        dedupe_facts(result.get("facts", []) or []),
                        table=table,
                        query=query,
                        source=str(result.get("source", "") or ""),
                    )
                )
                cache_hits += 1
                trace.append(
                    make_log(
                        state,
                        "evidence_tool:cache_hit",
                        tool="get_related_info",
                        scope="table",
                        table=table,
                        query=query,
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
                        scope="table",
                        table=table,
                        query=query,
                        cache_key=cache_key,
                        strict_table=(table == TABLE_NOTE),
                    )
                )
                raw_result = get_related_info(
                    query=query,
                    table=table,
                    collection=collection,
                    strict_table=(table == TABLE_NOTE),
                    limit=EVIDENCE_FACTS_LIMIT,
                )
                facts = _limit_evidence_facts(result_to_facts(raw_result, table=table, query=query))
                result = cache_item_from_result(
                    raw_result,
                    table=table,
                    query=query,
                    tool="get_related_info",
                    facts=facts,
                )
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
                "facts": facts,
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
                    "cache_hit": isinstance(cached, dict) and bool(cached),
                    "facts_n": len(facts),
                    "facts_preview": _facts_log_preview(facts),
                    "source": result.get("source", ""),
                }
            )

    merged_worker_results = _compact_worker_results(_merge_worker_results(
        _coerce_existing_worker_results(state.get("worker_results", {}) or {}),
        current_worker_results,
    ))
    evidence_pack = {
        "targets": _compact_evidence_targets(retrieval_targets),
        "items": _compact_evidence_items(evidence_items),
        "facts_by_table": {
            result_key: payload
            for result_key, payload in merged_worker_results.items()
            if not is_analysis_agent(str(result_key or "").strip())
        },
        "stats": {
            "targets_n": len(retrieval_targets),
            "items_n": len(evidence_items),
            "retrieval_calls_n": retrieval_calls,
            "cache_hits_n": cache_hits,
            "facts_n": sum(
                len((payload or {}).get("facts", []) or [])
                for payload in current_worker_results.values()
                if isinstance(payload, dict)
            ),
        },
    }

    updates = {
        "evidence_pack": evidence_pack,
        "evidence_cache": evidence_cache_updates,
        "worker_results": merged_worker_results,
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
