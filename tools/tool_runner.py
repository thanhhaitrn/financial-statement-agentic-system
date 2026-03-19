import json
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.planner_hints import infer_table_keywords, infer_table_query_hints
from schemas.keyword_guard import validate_keywords
from tools.registry import TOOLS_MAPPING_2_FUNCTIONS
from agents.agent_tools_list import AGENT_TOOLS_LIST
from graph.logger import make_debug_log, make_log


_COLLECTION = None
MAX_KEYWORDS_PER_TOOL_RUN = 4
SEED_KEYWORD_CUTOFF = 0.88
PLANNER_COMPONENT_CUTOFF = 0.93


def set_collection(collection):
    global _COLLECTION
    _COLLECTION = collection


WORKER_TO_TABLE = {
    "agent_bs": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "agent_is": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "agent_cf": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
}


def _normalize_table_name(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _get_allowed_tools(agent_name: str) -> set:
    return {tool["name"] for tool in AGENT_TOOLS_LIST.get(agent_name, [])}


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


def _get_keywords_for_table(plan: dict, table: str) -> List[str]:
    targets = plan.get("targets", []) or []
    normalized_target = _normalize_table_name(table)

    for target in targets:
        tname = _normalize_table_name(target.get("table", ""))
        if tname == normalized_target:
            kws = target.get("keywords", []) or []
            seen = set()
            cleaned = []
            for kw in kws:
                s = str(kw).strip()
                if s and s not in seen:
                    cleaned.append(s)
                    seen.add(s)
            return cleaned
    return []


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out = []
    for item in items or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _planner_hints_for_table(state: dict, table: str) -> List[str]:
    plan_tables = state.get("plan_tables", {}) or {}
    analysis_axes = plan_tables.get("analysis_axes", []) or []
    return infer_table_keywords(
        table,
        str(state.get("user_query", "") or ""),
        analysis_axes,
    )


def _planner_query_hints_for_table(state: dict, table: str) -> List[str]:
    plan_tables = state.get("plan_tables", {}) or {}
    analysis_axes = plan_tables.get("analysis_axes", []) or []
    return infer_table_query_hints(
        table,
        str(state.get("user_query", "") or ""),
        analysis_axes,
    )


def _guarded_keywords_for_table(
    table: str,
    candidates: List[str],
    *,
    cutoff: float,
) -> List[str]:
    valid, details = validate_keywords(
        table,
        candidates,
        fuzzy=True,
        cutoff=cutoff,
    )
    out = list(valid or [])
    for item in details or []:
        suggestion = item.get("suggested")
        if suggestion and suggestion not in out:
            out.append(suggestion)
    return _dedupe_keep_order(out)


def _refine_keywords_for_table(
    state: dict,
    table: str,
    requested_query: str = "",
) -> Tuple[List[str], List[str], List[str], List[str], str, bool]:
    raw_seed_keywords = _get_keywords_for_table(state.get("plan", {}) or {}, table)
    seed_keywords = _guarded_keywords_for_table(
        table,
        raw_seed_keywords,
        cutoff=SEED_KEYWORD_CUTOFF,
    )
    planner_hints = _planner_hints_for_table(state, table)
    planner_queries = _planner_query_hints_for_table(state, table)

    refined_keywords = _dedupe_keep_order(seed_keywords)
    for keyword in _guarded_keywords_for_table(
        table,
        planner_hints,
        cutoff=PLANNER_COMPONENT_CUTOFF,
    ):
        if keyword not in refined_keywords:
            refined_keywords.append(keyword)

    direct_requested = False
    requested_query = str(requested_query or "").strip()
    if requested_query:
        requested_refined = _guarded_keywords_for_table(
            table,
            [requested_query],
            cutoff=PLANNER_COMPONENT_CUTOFF,
        )
        for keyword in requested_refined:
            if keyword not in refined_keywords:
                refined_keywords.insert(0, keyword)

        if not requested_refined and int(state.get("followup_rounds", 0) or 0) > 0 and not refined_keywords:
            refined_keywords = [requested_query]
            direct_requested = True

    fallback_query = ""
    if not refined_keywords:
        fallback_candidates = _dedupe_keep_order(
            ([requested_query] if requested_query else []) + planner_queries
        )
        if fallback_candidates:
            fallback_query = fallback_candidates[0]

    return (
        seed_keywords,
        planner_hints,
        planner_queries,
        _keywords_to_fetch(refined_keywords),
        fallback_query,
        direct_requested,
    )


def _parse_action_block(action_text: str) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    action_match = re.search(r"(?mi)^\s*ACTION:\s*([^\n]+?)\s*$", action_text)
    if not action_match:
        return None, {}, "No ACTION block found"

    tool_name = action_match.group(1).strip()
    args: Dict[str, Any] = {}

    args_match = re.search(r"(?mis)^\s*ARGUMENTS:\s*(\{.*\})\s*$", action_text)
    if args_match:
        args_text = args_match.group(1).strip()
        try:
            parsed = json.loads(args_text)
            if not isinstance(parsed, dict):
                return tool_name, {}, "ARGUMENTS must decode to a JSON object"
            args = parsed
        except json.JSONDecodeError as e:
            return tool_name, {}, f"Failed to parse ARGUMENTS JSON: {e}"

    return tool_name, args, None


def _normalize_tool_result(raw_result: Any) -> dict:
    if isinstance(raw_result, dict):
        return raw_result
    return {
        "context": str(raw_result or ""),
        "source": "",
    }


def _prepare_get_related_info_args(
    state: dict,
    agent_name: str,
    args: dict,
) -> Tuple[Optional[dict], Optional[str], Optional[dict], List[str]]:
    global _COLLECTION

    if _COLLECTION is None:
        return None, "collection not set. Call set_collection(collection) before running workflow.", None, []

    prepared = dict(args)
    prepared["collection"] = _COLLECTION
    prepared["table"] = WORKER_TO_TABLE.get(agent_name, prepared.get("table", ""))

    table = prepared.get("table", "")
    requested_query = str(args.get("query", "")).strip()
    seed_keywords, planner_hints, planner_queries, refined_keywords, fallback_query, direct_requested = _refine_keywords_for_table(
        state,
        table,
        requested_query=requested_query,
    )

    log_data = {
        "agent": agent_name,
        "table": table,
        "seed_keywords": seed_keywords[:4],
        "planner_hints": planner_hints[:4],
        "planner_queries": planner_queries[:3],
        "refined_keywords": refined_keywords[:4],
    }
    if direct_requested:
        log_data["requested_query"] = requested_query
        log_data["direct_requested"] = True
    if fallback_query:
        log_data["fallback_query"] = fallback_query

    if refined_keywords != _keywords_to_fetch(seed_keywords) or direct_requested or fallback_query:
        log_entry = make_log(state, "tool:keyword_refined", **log_data)
    else:
        log_entry = make_debug_log(state, "tool:keyword_refined", **log_data)

    if refined_keywords:
        prepared["query"] = refined_keywords[0]
        return prepared, None, log_entry, refined_keywords

    if fallback_query:
        prepared["query"] = fallback_query
        return prepared, None, log_entry, [fallback_query]

    return None, f"no usable query available for table={table}", log_entry, []


def _keywords_to_fetch(keywords: List[str], *, limit: int = MAX_KEYWORDS_PER_TOOL_RUN) -> List[str]:
    seen = set()
    out = []

    for keyword in (keywords or []):
        text = str(keyword).strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
        if len(out) >= limit:
            break

    return out


def _already_called(state: dict, agent_name: str, tool_name: str, args: dict) -> bool:
    items = state.get("tool_results", []) or []
    current_round = _current_round(state)
    sig = _safe_json_dumps({"agent": agent_name, "tool": tool_name, "args": args})
    for item in items:
        item_round = item.get("round")
        if item_round is None and current_round > 0:
            continue
        if item_round is not None and int(item_round) != current_round:
            continue
        existing_sig = _safe_json_dumps({
            "agent": item.get("agent", ""),
            "tool": item.get("tool", ""),
            "args": item.get("args", {}),
        })
        if sig == existing_sig:
            return True
    return False


def call_tool_for_agent(state: dict, agent_name: str) -> dict:
    action_text = _latest_agent_response_for(state, agent_name)
    current_round = _current_round(state)
    count = _tool_call_count_for_round(state, agent_name)

    if count >= 2:
        return {
            "tool_observations": [
                {
                    "agent": agent_name,
                    "text": "[Tool loop cap reached. Stop calling tools and answer with available evidence.]"
                }
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(
                    state,
                    "tool:loop_cap_reached",
                    agent=agent_name,
                    count=count,
                )
            ],
        }

    if not action_text.strip():
        return {
            "tool_observations": [
                {"agent": agent_name, "text": f"[No worker response found for {agent_name}]"}
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(state, "tool:skip_empty_response", agent=agent_name)
            ],
        }

    tool_name, args, parse_error = _parse_action_block(action_text)
    if parse_error:
        return {
            "tool_observations": [
                {"agent": agent_name, "text": f"[No valid tool action by {agent_name}: {parse_error}]"}
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(
                    state,
                    "tool:skip_no_action",
                    agent=agent_name,
                    preview=action_text[:120],
                    reason=parse_error,
                )
            ],
        }

    allowed_tools = _get_allowed_tools(agent_name)
    if tool_name not in allowed_tools:
        return {
            "tool_observations": [
                {"agent": agent_name, "text": f"[Tool '{tool_name}' NOT allowed for {agent_name}]"}
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(
                    state,
                    "tool:blocked_not_allowed",
                    agent=agent_name,
                    tool=tool_name,
                )
            ],
        }

    tool_func = TOOLS_MAPPING_2_FUNCTIONS.get(tool_name)
    if not tool_func:
        return {
            "tool_observations": [
                {"agent": agent_name, "text": f"[Unknown tool: {tool_name}]"}
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(
                    state,
                    "tool:blocked_unknown_tool",
                    agent=agent_name,
                    tool=tool_name,
                )
            ],
        }

    prepared_args = dict(args)
    trace_logs = []
    refined_keywords: List[str] = []

    if tool_name == "get_related_info":
        prepared_args, prep_error, prep_log, refined_keywords = _prepare_get_related_info_args(
            state,
            agent_name,
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
                    {"agent": agent_name, "text": f"[Tool blocked: {prep_error}]"}
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
                args_preview=_safe_json_dumps(prepared_args)[:200],
            )
        )
        return {
            "tool_observations": [
                {"agent": agent_name, "text": f"[Tool call blocked: repeated identical call: {tool_name}]"}
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": trace_logs,
        }

    trace_logs.append(
        make_log(
            state,
            "tool:start",
            agent=agent_name,
            tool=tool_name,
            table=prepared_args.get("table", ""),
            query=prepared_args.get("query", ""),
        )
    )

    try:
        raw_results = tool_func(**prepared_args)
        results = _normalize_tool_result(raw_results)
    except Exception as e:
        trace_logs.append(
            make_log(
                state,
                "tool:error_runtime",
                agent=agent_name,
                tool=tool_name,
                error_type=type(e).__name__,
                error=str(e)[:250],
            )
        )
        return {
            "tool_observations": [
                {"agent": agent_name, "text": f"[Tool error: {tool_name} failed: {type(e).__name__}: {str(e)[:200]}]"}
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": trace_logs,
        }

    ctx = (results.get("context") or "").strip()
    src = results.get("source", "")
    obs = (
        f"[{tool_name} source={src} table={prepared_args.get('table','')} "
        f"query={prepared_args.get('query','')}]\n"
        f"{ctx[:1200] if ctx else '<EMPTY_CONTEXT>'}"
    )

    trace_logs.append(
        make_log(
            state,
            "tool:done",
            agent=agent_name,
            tool=tool_name,
            table=prepared_args.get("table", ""),
            query=prepared_args.get("query", ""),
            context_len=len(ctx),
            empty=(len(ctx) == 0),
        )
    )

    updates = {
        "tool_observations": [
            {"agent": agent_name, "text": obs}
        ],
        "tool_results": [
            {
                "agent": agent_name,
                "kind": "primary",
                "round": current_round,
                "tool": tool_name,
                "args": prepared_args,
                "results": results,
            }
        ],
        "tool_call_counts": _round_count_update(state, agent_name, count + 1),
        "trace": trace_logs,
    }

    if tool_name == "get_related_info":
        followup_keywords = [
            kw
            for kw in refined_keywords
            if kw != str(prepared_args.get("query", "")).strip()
        ]

        for follow_index, keyword in enumerate(followup_keywords, start=1):
            follow_args = dict(prepared_args)
            follow_args["query"] = keyword

            if _already_called(state, agent_name, tool_name, follow_args):
                continue

            try:
                raw_follow = tool_func(**follow_args)
                follow_results = _normalize_tool_result(raw_follow)
                follow_ctx = (follow_results.get("context") or "").strip()
                follow_src = follow_results.get("source", "")

                updates["tool_observations"].append(
                    {
                        "agent": agent_name,
                        "text": (
                            f"[AUTO_FOLLOWUP source={follow_src} table={follow_args.get('table','')} "
                            f"query={follow_args.get('query','')}]\n"
                            f"{follow_ctx[:1200] if follow_ctx else '<EMPTY_CONTEXT>'}"
                        ),
                    }
                )
                updates["tool_results"].append(
                    {
                        "agent": agent_name,
                        "kind": "followup",
                        "round": current_round,
                        "tool": tool_name,
                        "args": follow_args,
                        "results": follow_results,
                    }
                )
                updates["trace"].append(
                    make_log(
                        state,
                        "tool:followup_done",
                        agent=agent_name,
                        tool=tool_name,
                        table=follow_args.get("table", ""),
                        query=follow_args.get("query", ""),
                        followup_index=follow_index,
                        context_len=len(follow_ctx),
                        empty=(len(follow_ctx) == 0),
                    )
                )
            except Exception as e:
                updates["tool_observations"].append(
                    {
                        "agent": agent_name,
                        "text": f"[Tool followup error: {tool_name} failed: {type(e).__name__}: {str(e)[:200]}]",
                    }
                )
                updates["trace"].append(
                    make_log(
                        state,
                        "tool:error_runtime",
                        agent=agent_name,
                        tool=tool_name,
                        error_type=type(e).__name__,
                        error=str(e)[:250],
                    )
                )
                break

    return updates
