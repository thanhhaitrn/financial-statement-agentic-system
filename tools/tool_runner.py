import json
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import TOOLS_MAPPING_2_FUNCTIONS
from agents.agent_tools_list import AGENT_TOOLS_LIST
from graph.logger import log_step


_COLLECTION = None


def set_collection(collection):
    global _COLLECTION
    _COLLECTION = collection


WORKER_TO_TABLE = {
    "agent_bs": "BẢNG CÂN ĐỐI KẾ TOÁN",
    "agent_is": "BÁO CÁO KẾT QUẢ HOẠT ĐỘNG KINH DOANH",
    "agent_cf": "BÁO CÁO LƯU CHUYỂN TIỀN TỆ",
}


def _to_text(raw: Any) -> str:
    if hasattr(raw, "content"):
        return str(raw.content or "")
    return str(raw or "")


def _normalize_table_name(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _append_observation(state: dict, message: str) -> None:
    state.setdefault("w_tool_observations", []).append(message)


def _append_tool_result(
    state: dict,
    tool_name: str,
    args: dict,
    results: dict,
    kind: str = "primary",
) -> None:
    state.setdefault("w_tool_results", []).append(
        {
            "kind": kind,
            "tool": tool_name,
            "args": dict(args),
            "results": results,
        }
    )


def _get_allowed_tools(agent_name: str) -> set:
    return {tool["name"] for tool in AGENT_TOOLS_LIST.get(agent_name, [])}


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


def _parse_action_block(action_text: str) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """
    Expected formats:
        ACTION: get_related_info
        ARGUMENTS: {"query": "..."}

    or:

        ACTION: get_related_info
        ARGUMENTS:
        {"query": "..."}
    """
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


def _build_tool_signature(agent_name: str, tool_name: str, args: dict) -> str:
    payload = {
        "agent": agent_name,
        "tool": tool_name,
        "args": args,
    }
    return _safe_json_dumps(payload)


def _has_seen_call(state: dict, signature: str) -> bool:
    seen = set(state.get("w_seen_tool_calls", []) or [])
    return signature in seen


def _mark_seen_call(state: dict, signature: str) -> None:
    seen = set(state.get("w_seen_tool_calls", []) or [])
    seen.add(signature)
    state["w_seen_tool_calls"] = list(seen)


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
) -> Tuple[Optional[dict], Optional[str]]:
    global _COLLECTION

    if _COLLECTION is None:
        return None, "collection not set. Call set_collection(collection) before running workflow."

    prepared = dict(args)
    prepared["collection"] = _COLLECTION
    prepared["table"] = WORKER_TO_TABLE.get(agent_name, prepared.get("table", ""))

    table = prepared.get("table", "")
    keywords = _get_keywords_for_table(state.get("plan", {}) or {}, table)

    log_step(state, "tool:using_keywords", agent=agent_name, table=table, kws=keywords[:4])

    if not keywords:
        return None, f"no keywords in plan for table={table}"

    # Deterministic: always override query from plan keyword #1
    prepared["query"] = keywords[0]
    return prepared, None


def _run_tool_once(
    state: dict,
    agent_name: str,
    tool_name: str,
    tool_func,
    args: dict,
    *,
    kind: str = "primary",
) -> bool:
    signature = _build_tool_signature(agent_name, tool_name, args)

    if _has_seen_call(state, signature):
        _append_observation(
            state,
            f"[Tool call blocked: repeated identical call: {tool_name} args={_safe_json_dumps(args)}]",
        )
        log_step(
            state,
            "tool:blocked_repeat",
            agent=agent_name,
            tool=tool_name,
            args_preview=_safe_json_dumps(args)[:200],
        )
        return False

    _mark_seen_call(state, signature)

    log_step(
        state,
        "tool:followup_start" if kind != "primary" else "tool:start",
        agent=agent_name,
        tool=tool_name,
        table=args.get("table", ""),
        query=args.get("query", ""),
    )

    try:
        raw_results = tool_func(**args)
    except Exception as e:
        _append_observation(
            state,
            f"[Tool error: {tool_name} failed: {type(e).__name__}: {str(e)[:200]}]",
        )
        log_step(
            state,
            "tool:error_runtime",
            agent=agent_name,
            tool=tool_name,
            error_type=type(e).__name__,
            error=str(e)[:250],
        )
        return False

    results = _normalize_tool_result(raw_results)
    ctx = (results.get("context") or "").strip()
    src = results.get("source", "")
    ctx_short = ctx[:1200] if ctx else "<EMPTY_CONTEXT>"

    if kind == "primary":
        header = f"[{tool_name} source={src} table={args.get('table','')} query={args.get('query','')}]"
    else:
        header = f"[AUTO_FOLLOWUP source={src} table={args.get('table','')} query={args.get('query','')}]"

    _append_observation(state, f"{header}\n{ctx_short}")
    _append_tool_result(state, tool_name, args, results, kind=kind)

    state["w_last_tool_results"] = results

    log_step(
        state,
        "tool:followup_done" if kind != "primary" else "tool:done",
        agent=agent_name,
        tool=tool_name,
        table=args.get("table", ""),
        query=args.get("query", ""),
        context_len=len(ctx),
        empty=(len(ctx) == 0),
    )
    return True


def call_tool(state: dict) -> dict:
    raw = state.get("w_last_agent_response", "")
    action_text = _to_text(raw)
    agent_name = state.get("w_last_agent", "") or state.get("last_agent", "")

    if not action_text.strip():
        _append_observation(state, f"[No worker response found for {agent_name}]")
        log_step(state, "tool:skip_empty_response", agent=agent_name)
        return state

    tool_name, args, parse_error = _parse_action_block(action_text)
    if parse_error:
        _append_observation(state, f"[No valid tool action by {agent_name}: {parse_error}]")
        log_step(
            state,
            "tool:skip_no_action",
            agent=agent_name,
            preview=action_text[:120],
            reason=parse_error,
        )
        return state

    allowed_tools = _get_allowed_tools(agent_name)
    if tool_name not in allowed_tools:
        _append_observation(state, f"[Tool '{tool_name}' NOT allowed for {agent_name}]")
        log_step(state, "tool:blocked_not_allowed", agent=agent_name, tool=tool_name)
        return state

    tool_func = TOOLS_MAPPING_2_FUNCTIONS.get(tool_name)
    if not tool_func:
        _append_observation(state, f"[Unknown tool: {tool_name}]")
        log_step(state, "tool:blocked_unknown_tool", agent=agent_name, tool=tool_name)
        return state

    prepared_args = dict(args)

    if tool_name == "get_related_info":
        prepared_args, prep_error = _prepare_get_related_info_args(state, agent_name, prepared_args)
        if prep_error:
            _append_observation(state, f"[Tool blocked: {prep_error}]")

            if "collection not set" in prep_error:
                log_step(state, "tool:error_no_collection", agent=agent_name)
            else:
                log_step(
                    state,
                    "tool:blocked_no_keywords",
                    agent=agent_name,
                    table=WORKER_TO_TABLE.get(agent_name, ""),
                )
            return state

    ran_primary = _run_tool_once(
        state,
        agent_name,
        tool_name,
        tool_func,
        prepared_args,
        kind="primary",
    )
    if not ran_primary:
        return state

    # Deterministic follow-up: keyword #2
    if tool_name == "get_related_info":
        table = prepared_args.get("table", "")
        keywords = _get_keywords_for_table(state.get("plan", {}) or {}, table)

        if len(keywords) >= 2:
            follow_args = dict(prepared_args)
            follow_args["query"] = keywords[1]

            _run_tool_once(
                state,
                agent_name,
                tool_name,
                tool_func,
                follow_args,
                kind="followup",
            )

    return state