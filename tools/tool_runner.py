"""Validate, prepare, execute, and record tool calls requested by worker agents."""
# Code note: Tool modules bridge agent requests to retrieval helpers; comments here mark guardrails around external calls.

import json
import time
from typing import Any, Optional, Tuple

from tools.registry import TOOLS_MAPPING_2_FUNCTIONS
from tools.tool_calls import normalize_tool_call
from agents.agent_tools_list import get_tool_names_for_agent
from graph.logger import make_debug_log, make_log
from schemas.requirements import normalize_requirement_text
from tools.evidence import (
    SCOPED_TOOL_TO_TABLE,
    cache_item_from_result,
    evidence_cache_key,
    filter_facts_for_query,
    get_runtime_cache_item,
    observation_text,
    result_to_facts,
    scoped_tool_name_for_table,
    set_runtime_cache_item,
)


_COLLECTION = None
TOOL_CONTEXT_PREVIEW_LIMIT = 1200
TOOL_RESULT_FACTS_LIMIT = 5
DEFAULT_MAX_TOOL_CALLS_PER_ROUND = 2


def set_collection(collection):
    global _COLLECTION
    _COLLECTION = collection


def get_collection():
    return _COLLECTION


SCOPED_TOOL_NAMES = set(SCOPED_TOOL_TO_TABLE.keys())


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _coerce_tool_context(value: Any) -> str:
    return str(value or "").strip()


def _build_tool_context_debug_fields(state: dict, context: str) -> dict:
    preview = context[:TOOL_CONTEXT_PREVIEW_LIMIT] if context else "<EMPTY_CONTEXT>"
    return {
        "context_preview": preview,
        "context_preview_truncated": bool(context) and len(context) > TOOL_CONTEXT_PREVIEW_LIMIT,
    }


def _get_allowed_tools(agent_name: str) -> set:
    return get_tool_names_for_agent(agent_name)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    output = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _latest_agent_response_for(state: dict, agent_name: str) -> str:
    items = state.get("worker_messages", []) or []
    current_round = state.get("followup_rounds", 0)

    for item in reversed(items):
        if (
            str(item.get("agent", "")).strip() == agent_name
            and str(item.get("kind", "")) == "agent_response"
            and item.get("round", 0) == current_round
        ):
            return str(item.get("response", "") or "")
    return ""


def _latest_tool_calls_for(state: dict, agent_name: str) -> list[dict]:
    items = state.get("worker_messages", []) or []
    current_round = state.get("followup_rounds", 0)

    for item in reversed(items):
        if (
            str(item.get("agent", "")).strip() == agent_name
            and str(item.get("kind", "")) == "agent_response"
            and item.get("round", 0) == current_round
        ):
            calls = item.get("tool_calls")
            if calls:
                return [normalize_tool_call(call) for call in calls]

            parsed = item.get("parsed_output")
            if isinstance(parsed, dict) and str(parsed.get("kind", "")).strip() == "tool_calls":
                return [normalize_tool_call(call) for call in (parsed.get("tool_calls", []) or [])]
    return []


def _latest_agent_message_for(state: dict, agent_name: str) -> dict:
    items = state.get("worker_messages", []) or []
    current_round = state.get("followup_rounds", 0)

    for item in reversed(items):
        if (
            str(item.get("agent", "")).strip() == agent_name
            and str(item.get("kind", "")) == "agent_response"
            and item.get("round", 0) == current_round
        ):
            return item if isinstance(item, dict) else {}
    return {}


def _current_round(state: dict) -> int:
    return int((state or {}).get("followup_rounds", 0) or 0)


def _tool_call_count_for_round(state: dict, agent_name: str) -> int:
    current_round = _current_round(state)
    counts = state.get("tool_call_counts", {}) or {}
    value = counts.get(agent_name)

    if isinstance(value, dict):
        try:
            if int(value.get("round", -1)) == current_round:
                return int(value.get("count", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return 0

    if current_round == 0 and isinstance(value, int):
        return value

    return 0


def _round_count_update(state: dict, agent_name: str, count: int) -> dict:
    return {
        agent_name: {
            "round": _current_round(state),
            "count": int(count),
        }
    }


def _force_collect_update(state: dict, agent_name: str) -> dict:
    return {agent_name: _current_round(state)}


def _tool_observation_entry(agent_name: str, text: str, round_n: int) -> dict:
    return {
        "agent": agent_name,
        "text": text,
        "round": round_n,
    }


def _target_matches_agent_table(target: dict, agent_name: str, table: str) -> bool:
    if not isinstance(target, dict):
        return False
    if str(target.get("agent", "") or "").strip() != agent_name:
        return False
    target_table = str(target.get("table", "") or "").strip()
    return not (table and target_table and target_table != table)


def _current_target_for_agent_table(state: dict, agent_name: str, table: str) -> dict:
    latest_message = _latest_agent_message_for(state, agent_name)
    message_target = latest_message.get("dispatch_target")
    if _target_matches_agent_table(message_target, agent_name, table):
        return message_target

    dispatch_target = state.get("dispatch_target")
    if _target_matches_agent_table(dispatch_target, agent_name, table):
        return dispatch_target

    return {}


def _evidence_queries_to_requirements(target: dict, table: str = "") -> list[str]:
    requirements = []
    for item in (target.get("evidence_queries", []) or []):
        if not isinstance(item, dict):
            continue
        query = str(item.get("query", "") or "").strip()
        if not query:
            continue
        query_table = str(item.get("table", "") or "").strip()
        if table and query_table and query_table != table:
            continue
        requirements.append(query)
    return _dedupe_keep_order(requirements)


def _assigned_requirements_for_agent(state: dict, agent_name: str) -> list[str]:
    current_target = _current_target_for_agent_table(state, agent_name, "")
    if current_target:
        return _dedupe_keep_order(
            list(current_target.get("requirements", []) or [])
            + _evidence_queries_to_requirements(current_target)
        )

    worker_plan = state.get("worker_plan", {}) or {}
    requirements = []
    for target in (worker_plan.get("targets", []) or []):
        if str(target.get("agent", "")).strip() != agent_name:
            continue
        requirements.extend(target.get("requirements", []) or [])
        requirements.extend(_evidence_queries_to_requirements(target))
    for target in (worker_plan.get("analysis_plan", []) or []):
        if str(target.get("agent", "")).strip() != agent_name:
            continue
        requirements.extend(target.get("requirements", []) or [])
        requirements.extend(_evidence_queries_to_requirements(target))
    return _dedupe_keep_order(requirements)


def _max_tool_calls_for_agent(state: dict, agent_name: str) -> int:
    requirements_n = len(_assigned_requirements_for_agent(state, agent_name))
    if requirements_n > 0:
        return min(requirements_n, DEFAULT_MAX_TOOL_CALLS_PER_ROUND)
    return DEFAULT_MAX_TOOL_CALLS_PER_ROUND


def _prepare_scoped_info_args(
    state: dict,
    agent_name: str,
    tool_name: str,
    args: dict,
) -> Tuple[Optional[dict], Optional[str], Optional[dict]]:
    global _COLLECTION

    if _COLLECTION is None:
        return None, "collection not set. Call set_collection(collection) before running workflow.", None

    table = SCOPED_TOOL_TO_TABLE.get(tool_name, "")
    prepared = dict(args)
    prepared["collection"] = _COLLECTION
    prepared["table"] = table
    raw_query = str(prepared.get("query", "") or "").strip()
    prepared["query"] = raw_query
    # Thread the full user question into agent-initiated scoped retrieval so the
    # intent lexical fold + slot matching run there too — the agent's own query
    # often drops the discriminating tokens (entity names, "khác", asset class).
    prepared["intent"] = str((state or {}).get("user_query", "") or "").strip()

    if not prepared["query"]:
        return None, f"missing query for scoped retrieval table={table}", None

    log_data = {
        "agent": agent_name,
        "tool": tool_name,
        "table": table,
        "query": prepared["query"],
    }
    if raw_query and raw_query != prepared["query"]:
        log_data["raw_query"] = raw_query
    if bool((state or {}).get("debug_trace", False)):
        log_data["assigned_requirements"] = _assigned_requirements_for_agent(state, agent_name)[:4]

    return prepared, None, make_debug_log(state, "tool:query_prepared", **log_data)


def _parse_tool_call_payload(tool_calls: list[dict]) -> Tuple[Optional[str], dict[str, Any], Optional[str], str]:
    if not tool_calls:
        return None, {}, "No native tool_call found", ""

    call = normalize_tool_call(tool_calls[0])
    tool_name = str(call.get("name", "") or "").strip()
    args = dict(call.get("args", {}) or {})
    tool_call_id = str(call.get("id", "") or "").strip()

    if not tool_name:
        return None, {}, "Native tool_call is missing a tool name", tool_call_id

    return tool_name, args, None, tool_call_id


def _normalize_tool_result(raw_result: Any) -> dict:
    if isinstance(raw_result, dict):
        return raw_result
    return {
        "context": str(raw_result or ""),
        "source": "",
    }


def _evidence_query_candidates(state: dict) -> list[dict]:
    candidates: list[dict] = []
    explicit = state.get("evidence_queries")
    if isinstance(explicit, list):
        candidates.extend(item for item in explicit if isinstance(item, dict))

    dispatch_target = state.get("dispatch_target")
    if isinstance(dispatch_target, dict):
        candidates.extend(
            item
            for item in (dispatch_target.get("evidence_queries", []) or [])
            if isinstance(item, dict)
        )
        target_agent = str(dispatch_target.get("agent", "") or "").strip()
    else:
        target_agent = ""

    worker_plan = state.get("worker_plan", {}) or {}
    for item in (worker_plan.get("analysis_plan", []) or []):
        if not isinstance(item, dict):
            continue
        if target_agent and str(item.get("agent", "") or "").strip() != target_agent:
            continue
        candidates.extend(
            query_item
            for query_item in (item.get("evidence_queries", []) or [])
            if isinstance(query_item, dict)
        )

    return candidates


def _expected_scoped_tool_for_query(state: dict, query: str) -> tuple[str, str]:
    query_text = str(query or "").strip()
    if not query_text:
        return "", ""

    for item in _evidence_query_candidates(state):
        table = str(item.get("table", "") or "").strip()
        evidence_query = str(item.get("query", "") or "").strip()
        if not table or not evidence_query:
            continue

        normalized_query = normalize_requirement_text(query_text, table=table)
        normalized_evidence = normalize_requirement_text(evidence_query, table=table)
        if normalized_query and normalized_evidence and (
            normalized_query == normalized_evidence
            or normalized_query in normalized_evidence
            or normalized_evidence in normalized_query
        ):
            return scoped_tool_name_for_table(table), table

    return "", ""


def _cache_key_for_prepared_args(state: dict, tool_name: str, prepared_args: dict) -> str:
    return evidence_cache_key(
        dataset_id=str((state or {}).get("dataset_id", "") or ""),
        table=str(prepared_args.get("table", "") or ""),
        query=str(prepared_args.get("query", "") or ""),
        mode="table",
        intent=str(prepared_args.get("intent", "") or ""),
    )


def _cache_hit_update(
    state: dict,
    agent_name: str,
    tool_name: str,
    prepared_args: dict,
    cache_key: str,
    cache_item: dict,
    count: int,
) -> dict:
    current_round = _current_round(state)
    cache_payload = dict(cache_item or {})
    table = str(prepared_args.get("table", "") or cache_payload.get("table", "") or "")
    query = str(prepared_args.get("query", "") or cache_payload.get("query", "") or "")
    cache_payload["table"] = table
    cache_payload["query"] = query
    cache_payload["facts"] = filter_facts_for_query(
        cache_payload.get("facts", []) or [],
        table=table,
        query=query,
        source=str(cache_payload.get("source", "") or ""),
    )[:TOOL_RESULT_FACTS_LIMIT]
    obs = observation_text(tool_name, cache_payload)
    return {
        "tool_observations": [
            _tool_observation_entry(agent_name, obs, current_round)
        ],
        "tool_results": [
            {
                "agent": agent_name,
                "kind": "cache_hit",
                "round": current_round,
                "tool": tool_name,
                "tool_call_id": "",
                "args": prepared_args,
                "cache_key": cache_key,
                "results": cache_payload,
            }
        ],
        "tool_call_counts": _round_count_update(state, agent_name, count + 1),
        "trace": [
            make_log(
                state,
                "tool:cache_hit",
                agent=agent_name,
                tool=tool_name,
                table=prepared_args.get("table", ""),
                query=prepared_args.get("query", ""),
                cache_key=cache_key,
            )
        ],
    }


def _tool_call_signature(agent_name: str, tool_name: str, args: dict) -> str:
    return _safe_json_dumps(
        {
            "agent": str(agent_name or "").strip(),
            "tool": str(tool_name or "").strip(),
            "table": str((args or {}).get("table", "") or "").strip(),
            "query": str((args or {}).get("query", "") or "").strip(),
        }
    )


def _already_called(state: dict, agent_name: str, tool_name: str, args: dict) -> bool:
    items = state.get("tool_results", []) or []
    current_round = _current_round(state)
    sig = _tool_call_signature(agent_name, tool_name, args)
    for item in items:
        # Follow-up rounds can reuse the same query text, so only compare calls
        # inside the current round.
        item_round = item.get("round")
        if item_round is None and current_round > 0:
            continue
        if item_round is not None and int(item_round) != current_round:
            continue
        existing_sig = _tool_call_signature(
            str(item.get("agent", "") or ""),
            str(item.get("tool", "") or ""),
            item.get("args", {}) or {},
        )
        if sig == existing_sig:
            return True
    return False


def call_tool_for_agent(state: dict, agent_name: str) -> dict:
    response_text = _latest_agent_response_for(state, agent_name)
    tool_calls = _latest_tool_calls_for(state, agent_name)
    current_round = _current_round(state)
    count = _tool_call_count_for_round(state, agent_name)

    max_calls = _max_tool_calls_for_agent(state, agent_name)
    if count >= max_calls:
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    "[Tool loop cap reached. Stop calling tools and answer with available evidence.]",
                    current_round,
                )
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(
                    state,
                    "tool:loop_cap_reached",
                    agent=agent_name,
                    count=count,
                    max_calls=max_calls,
                )
            ],
        }

    if not tool_calls:
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    f"[No native tool_call found for {agent_name}]",
                    current_round,
                )
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(
                    state,
                    "tool:skip_no_tool_call",
                    agent=agent_name,
                    preview=response_text[:120],
                )
            ],
        }

    tool_name, args, parse_error, tool_call_id = _parse_tool_call_payload(tool_calls)
    if parse_error:
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    f"[No valid native tool_call by {agent_name}: {parse_error}]",
                    current_round,
                )
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(
                    state,
                    "tool:skip_no_tool_call",
                    agent=agent_name,
                    preview=response_text[:120],
                    reason=parse_error,
                )
            ],
        }

    trace_logs = [
        make_log(
            state,
            "tool:call_received",
            agent=agent_name,
            tool=tool_name,
            tool_call_id=tool_call_id,
            round=current_round,
            count=count,
            max_calls=max_calls,
            args_preview=_safe_json_dumps(args)[:200],
        )
    ]

    if tool_name in SCOPED_TOOL_NAMES:
        expected_tool, expected_table = _expected_scoped_tool_for_query(
            state,
            str(args.get("query", "") or ""),
        )
        if expected_tool and expected_tool != tool_name:
            trace_logs.append(
                make_log(
                    state,
                    "tool:scope_corrected",
                    agent=agent_name,
                    original_tool=tool_name,
                    corrected_tool=expected_tool,
                    table=expected_table,
                    query=str(args.get("query", "") or ""),
                )
            )
            tool_name = expected_tool

    allowed_tools = _get_allowed_tools(agent_name)
    if tool_name not in allowed_tools:
        trace_logs.append(
            make_log(
                state,
                "tool:blocked_not_allowed",
                agent=agent_name,
                tool=tool_name,
                allowed_tools=sorted(allowed_tools),
            )
        )
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    f"[Tool '{tool_name}' NOT allowed for {agent_name}]",
                    current_round,
                )
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": trace_logs,
        }

    tool_func = TOOLS_MAPPING_2_FUNCTIONS.get(tool_name)
    if not tool_func:
        trace_logs.append(
            make_log(
                state,
                "tool:blocked_unknown_tool",
                agent=agent_name,
                tool=tool_name,
            )
        )
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    f"[Unknown tool: {tool_name}]",
                    current_round,
                )
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": trace_logs,
        }

    prepared_args = dict(args)

    if tool_name in SCOPED_TOOL_NAMES:
        prepared_args, prep_error, prep_log = _prepare_scoped_info_args(
            state,
            agent_name,
            tool_name,
            prepared_args,
        )
        if prep_log:
            trace_logs.append(prep_log)

        if prep_error:
            trace_logs.append(
                make_log(
                    state,
                    "tool:blocked_prepare",
                    agent=agent_name,
                    tool=tool_name,
                    error=prep_error,
                )
            )
            return {
                "tool_observations": [
                    _tool_observation_entry(
                        agent_name,
                        f"[Tool blocked: {prep_error}]",
                        current_round,
                    )
                ],
                "tool_call_counts": _round_count_update(state, agent_name, count + 1),
                "force_collect_agents": _force_collect_update(state, agent_name),
                "trace": trace_logs,
            }

    if _already_called(state, agent_name, tool_name, prepared_args):
        trace_logs.append(
            make_log(
                state,
                "tool:blocked_repeat",
                agent=agent_name,
                tool=tool_name,
                table=prepared_args.get("table", ""),
                query=prepared_args.get("query", ""),
                args_preview=_safe_json_dumps(prepared_args)[:200],
            )
        )
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    f"[Tool call blocked: repeated identical call: {tool_name}. Stop calling tools and answer with available evidence.]",
                    current_round,
                )
            ],
            "tool_call_counts": _round_count_update(state, agent_name, max(count + 1, max_calls)),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": trace_logs,
        }

    if tool_name in SCOPED_TOOL_NAMES:
        cache_key = _cache_key_for_prepared_args(state, tool_name, prepared_args)
        cache_item = (state.get("evidence_cache", {}) or {}).get(cache_key) or get_runtime_cache_item(cache_key)
        if isinstance(cache_item, dict) and cache_item:
            update = _cache_hit_update(
                state,
                agent_name,
                tool_name,
                prepared_args,
                cache_key,
                cache_item,
                count,
            )
            update["trace"] = trace_logs + list(update.get("trace", []) or [])
            return update
        trace_logs.append(
            make_log(
                state,
                "tool:cache_miss",
                agent=agent_name,
                tool=tool_name,
                table=prepared_args.get("table", ""),
                query=prepared_args.get("query", ""),
                cache_key=cache_key,
            )
        )
    else:
        cache_key = ""

    trace_logs.append(
        make_log(
            state,
            "tool:start",
            agent=agent_name,
            tool=tool_name,
            tool_call_id=tool_call_id,
            table=prepared_args.get("table", ""),
            query=prepared_args.get("query", ""),
        )
    )

    started_at = time.perf_counter()
    try:
        raw_results = tool_func(**prepared_args)
        results = _normalize_tool_result(raw_results)
        duration_ms = int((time.perf_counter() - started_at) * 1000)
    except Exception as e:
        trace_logs.append(
            make_log(
                state,
                "tool:error_runtime",
                agent=agent_name,
                tool=tool_name,
                tool_call_id=tool_call_id,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                error_type=type(e).__name__,
                error=str(e)[:250],
            )
        )
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    f"[Tool error: {tool_name} failed: {type(e).__name__}: {str(e)[:200]}]",
                    current_round,
                )
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": trace_logs,
        }

    ctx = _coerce_tool_context(results.get("context"))
    src = results.get("source", "")
    evidence_cache_update = {}
    if cache_key:
        facts = result_to_facts(
            results,
            table=str(prepared_args.get("table", "") or ""),
            query=str(prepared_args.get("query", "") or ""),
        )
        evidence_cache_update[cache_key] = cache_item_from_result(
            results,
            table=str(prepared_args.get("table", "") or ""),
            query=str(prepared_args.get("query", "") or ""),
            tool=tool_name,
            facts=facts,
        )
        set_runtime_cache_item(cache_key, evidence_cache_update[cache_key])
        trace_logs.append(
            make_log(
                state,
                "tool:cache_store",
                agent=agent_name,
                tool=tool_name,
                table=prepared_args.get("table", ""),
                query=prepared_args.get("query", ""),
                cache_key=cache_key,
                facts_n=len(facts),
            )
        )
    context_debug_fields = _build_tool_context_debug_fields(state, ctx)
    tool_result_payload = evidence_cache_update.get(cache_key) if cache_key else results
    if not isinstance(tool_result_payload, dict) or not tool_result_payload:
        tool_result_payload = results
    obs = observation_text(tool_name, tool_result_payload)

    trace_logs.append(
        make_log(
            state,
            "tool:done",
            agent=agent_name,
            tool=tool_name,
            tool_call_id=tool_call_id,
            table=prepared_args.get("table", ""),
            query=prepared_args.get("query", ""),
            duration_ms=duration_ms,
            context_len=len(ctx),
            empty=(len(ctx) == 0),
            **context_debug_fields,
        )
    )

    return {
        "tool_observations": [
            _tool_observation_entry(agent_name, obs, current_round)
        ],
        "tool_results": [
            {
                "agent": agent_name,
                "kind": "primary",
                "round": current_round,
                "tool": tool_name,
                "tool_call_id": tool_call_id,
                "args": prepared_args,
                "results": tool_result_payload,
            }
        ],
        "tool_call_counts": _round_count_update(state, agent_name, count + 1),
        "trace": trace_logs,
        "evidence_cache": evidence_cache_update,
    }
