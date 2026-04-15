import json
import time
from typing import Any, Dict, Optional, Tuple

from schemas.agent_outputs import WorkerAction, parse_worker_response, parse_worker_response_payload
from tools.registry import TOOLS_MAPPING_2_FUNCTIONS
from agents.agent_tools_list import AGENT_TOOLS_LIST
from graph.logger import make_debug_log, make_log


_COLLECTION = None
TOOL_CONTEXT_PREVIEW_LIMIT = 1200
DEFAULT_MAX_TOOL_CALLS_PER_ROUND = 2


def set_collection(collection):
    global _COLLECTION
    _COLLECTION = collection


WORKER_TO_TABLE = {
    "agent_bs": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "agent_is": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "agent_cf": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
}


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
    return {tool["name"] for tool in AGENT_TOOLS_LIST.get(agent_name, [])}


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


def _latest_parsed_output_for(state: dict, agent_name: str) -> dict:
    items = state.get("worker_messages", []) or []
    current_round = state.get("followup_rounds", 0)

    for item in reversed(items):
        if (
            str(item.get("agent", "")).strip() == agent_name
            and str(item.get("kind", "")) == "agent_response"
            and item.get("round", 0) == current_round
        ):
            parsed = item.get("parsed_output")
            if isinstance(parsed, dict):
                return parsed
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


def _get_target_for_agent_table(state: dict, agent_name: str, table: str) -> dict:
    dispatch_target = state.get("dispatch_target")
    if isinstance(dispatch_target, dict) and str(dispatch_target.get("agent", "")).strip() == agent_name:
        return dispatch_target

    worker_plan = state.get("worker_plan", {}) or {}
    targets = worker_plan.get("targets", []) or []

    for target in targets:
        if str(target.get("agent", "")).strip() != agent_name:
            continue
        target_table = str(target.get("table", "") or "").strip()
        if table and target_table and target_table != table:
            continue
        return target
    return {}


def _get_requirements_for_agent_table(state: dict, agent_name: str, table: str) -> list[str]:
    target = _get_target_for_agent_table(state, agent_name, table)
    return _dedupe_keep_order(target.get("requirements", []) or [])


def _assigned_requirements_for_agent(state: dict, agent_name: str) -> list[str]:
    dispatch_target = state.get("dispatch_target")
    if isinstance(dispatch_target, dict) and str(dispatch_target.get("agent", "")).strip() == agent_name:
        return _dedupe_keep_order(dispatch_target.get("requirements", []) or [])

    worker_plan = state.get("worker_plan", {}) or {}
    requirements = []
    for target in (worker_plan.get("targets", []) or []):
        if str(target.get("agent", "")).strip() != agent_name:
            continue
        requirements.extend(target.get("requirements", []) or [])
    return _dedupe_keep_order(requirements)


def _max_tool_calls_for_agent(state: dict, agent_name: str) -> int:
    requirements_n = len(_assigned_requirements_for_agent(state, agent_name))
    if requirements_n > 0:
        return requirements_n
    return DEFAULT_MAX_TOOL_CALLS_PER_ROUND


def _is_followup_dispatch_target(state: dict, agent_name: str) -> bool:
    dispatch_target = state.get("dispatch_target")
    return (
        isinstance(dispatch_target, dict)
        and str(dispatch_target.get("agent", "")).strip() == agent_name
        and str(dispatch_target.get("source", "")).strip() == "followup"
    )


def _requirement_query_for_followup_call(state: dict, agent_name: str, table: str, tool_call_index: int) -> str:
    if not _is_followup_dispatch_target(state, agent_name):
        return ""

    requirements = _get_requirements_for_agent_table(state, agent_name, table)
    if tool_call_index < 0 or tool_call_index >= len(requirements):
        return ""
    return str(requirements[tool_call_index] or "").strip()


def _prepare_get_related_info_args(
    state: dict,
    agent_name: str,
    args: dict,
) -> Tuple[Optional[dict], Optional[str], Optional[dict]]:
    global _COLLECTION

    if _COLLECTION is None:
        return None, "collection not set. Call set_collection(collection) before running workflow.", None

    prepared = dict(args)
    prepared["collection"] = _COLLECTION
    prepared["table"] = WORKER_TO_TABLE.get(agent_name, prepared.get("table", ""))
    prepared["query"] = str(prepared.get("query", "") or "").strip()

    if not prepared["query"]:
        return None, f"missing query for table={prepared.get('table', '')}", None

    log_data = {
        "agent": agent_name,
        "table": prepared.get("table", ""),
        "query": prepared["query"],
    }
    if bool((state or {}).get("debug_trace", False)):
        log_data["assigned_requirements"] = _get_requirements_for_agent_table(
            state,
            agent_name,
            str(prepared.get("table", "") or "").strip(),
        )[:4]

    return prepared, None, make_debug_log(state, "tool:query_prepared", **log_data)


def _parse_action_block(action_text: str) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    try:
        parsed = parse_worker_response(action_text)
    except Exception as exc:
        return None, {}, str(exc)

    if not isinstance(parsed, WorkerAction):
        return None, {}, "Latest worker response is not a tool action"

    return parsed.action, dict(parsed.arguments or {}), None


def _parse_action_payload(payload: Any) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    if not payload:
        return None, {}, "No parsed payload found"

    try:
        parsed = parse_worker_response_payload(payload)
    except Exception as exc:
        return None, {}, str(exc)

    if not isinstance(parsed, WorkerAction):
        return None, {}, "Latest worker response is not a tool action"

    return parsed.action, dict(parsed.arguments or {}), None


def _normalize_tool_result(raw_result: Any) -> dict:
    if isinstance(raw_result, dict):
        return raw_result
    return {
        "context": str(raw_result or ""),
        "source": "",
    }


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
    parsed_payload = _latest_parsed_output_for(state, agent_name)
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

    if not action_text.strip():
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    f"[No worker response found for {agent_name}]",
                    current_round,
                )
            ],
            "tool_call_counts": _round_count_update(state, agent_name, count + 1),
            "force_collect_agents": _force_collect_update(state, agent_name),
            "trace": [
                make_log(state, "tool:skip_empty_response", agent=agent_name)
            ],
        }

    tool_name, args, parse_error = _parse_action_payload(parsed_payload)
    if parse_error:
        tool_name, args, parse_error = _parse_action_block(action_text)
    if parse_error:
        return {
            "tool_observations": [
                _tool_observation_entry(
                    agent_name,
                    f"[No valid tool action by {agent_name}: {parse_error}]",
                    current_round,
                )
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
                _tool_observation_entry(
                    agent_name,
                    f"[Tool '{tool_name}' NOT allowed for {agent_name}]",
                    current_round,
                )
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
                _tool_observation_entry(
                    agent_name,
                    f"[Unknown tool: {tool_name}]",
                    current_round,
                )
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

    if tool_name == "get_related_info":
        prepared_args, prep_error, prep_log = _prepare_get_related_info_args(
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

        enforced_query = _requirement_query_for_followup_call(
            state,
            agent_name,
            str(prepared_args.get("table", "") or "").strip(),
            count,
        )
        if enforced_query:
            original_query = str(prepared_args.get("query", "") or "").strip()
            prepared_args["query"] = enforced_query
            if original_query != enforced_query:
                trace_logs.append(
                    make_log(
                        state,
                        "tool:followup_query_enforced",
                        agent=agent_name,
                        original_query=original_query,
                        enforced_query=enforced_query,
                        requirement_index=count,
                    )
                )

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
                _tool_observation_entry(
                    agent_name,
                    f"[Tool call blocked: repeated identical call: {tool_name}]",
                    current_round,
                )
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
    context_debug_fields = _build_tool_context_debug_fields(state, ctx)
    obs = (
        f"[{tool_name} source={src} table={prepared_args.get('table','')} "
        f"query={prepared_args.get('query','')}]\n"
        f"{ctx[:TOOL_CONTEXT_PREVIEW_LIMIT] if ctx else '<EMPTY_CONTEXT>'}"
    )

    trace_logs.append(
        make_log(
            state,
            "tool:done",
            agent=agent_name,
            tool=tool_name,
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
                "args": prepared_args,
                "results": results,
            }
        ],
        "tool_call_counts": _round_count_update(state, agent_name, count + 1),
        "trace": trace_logs,
    }
