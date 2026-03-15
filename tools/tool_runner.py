import json
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.registry import TOOLS_MAPPING_2_FUNCTIONS
from agents.agent_tools_list import AGENT_TOOLS_LIST
from graph.logger import make_log


_COLLECTION = None


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

    make_log(state, "tool:using_keywords", agent=agent_name, table=table, kws=keywords[:4])

    if not keywords:
        return None, f"no keywords in plan for table={table}"

    # Deterministic: always override query from plan keyword #1
    prepared["query"] = keywords[0]
    return prepared, None


def _already_called(state: dict, agent_name: str, tool_name: str, args: dict) -> bool:
    items = state.get("tool_results", []) or []
    sig = _safe_json_dumps({"agent": agent_name, "tool": tool_name, "args": args})
    for item in items:
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

    if not action_text.strip():
        make_log(state, "tool:skip_empty_response", agent=agent_name)
        return {
            "tool_observations": [{"agent": agent_name, "text": f"[No worker response found for {agent_name}]"}]
        }

    tool_name, args, parse_error = _parse_action_block(action_text)
    if parse_error:
        make_log(state, "tool:skip_no_action", agent=agent_name, preview=action_text[:120], reason=parse_error)
        return {
            "tool_observations": [{"agent": agent_name, "text": f"[No valid tool action by {agent_name}: {parse_error}]"}]
        }

    allowed_tools = _get_allowed_tools(agent_name)
    if tool_name not in allowed_tools:
        make_log(state, "tool:blocked_not_allowed", agent=agent_name, tool=tool_name)
        return {
            "tool_observations": [{"agent": agent_name, "text": f"[Tool '{tool_name}' NOT allowed for {agent_name}]"}]
        }

    tool_func = TOOLS_MAPPING_2_FUNCTIONS.get(tool_name)
    if not tool_func:
        make_log(state, "tool:blocked_unknown_tool", agent=agent_name, tool=tool_name)
        return {
            "tool_observations": [{"agent": agent_name, "text": f"[Unknown tool: {tool_name}]"}]
        }

    prepared_args = dict(args)

    if tool_name == "get_related_info":
        prepared_args, prep_error = _prepare_get_related_info_args(state, agent_name, prepared_args)
        if prep_error:
            make_log(state, "tool:blocked_prepare", agent=agent_name, tool=tool_name, error=prep_error)
            return {
                "tool_observations": [{"agent": agent_name, "text": f"[Tool blocked: {prep_error}]"}]
            }

    if _already_called(state, agent_name, tool_name, prepared_args):
        make_log(state, "tool:blocked_repeat", agent=agent_name, tool=tool_name, args_preview=_safe_json_dumps(prepared_args)[:200])
        return {
            "tool_observations": [{"agent": agent_name, "text": f"[Tool call blocked: repeated identical call: {tool_name}]"}]
        }

    make_log(
        state,
        "tool:start",
        agent=agent_name,
        tool=tool_name,
        table=prepared_args.get("table", ""),
        query=prepared_args.get("query", ""),
    )

    try:
        raw_results = tool_func(**prepared_args)
        results = _normalize_tool_result(raw_results)
    except Exception as e:
        make_log(state, "tool:error_runtime", agent=agent_name, tool=tool_name, error_type=type(e).__name__, error=str(e)[:250])
        return {
            "tool_observations": [{"agent": agent_name, "text": f"[Tool error: {tool_name} failed: {type(e).__name__}: {str(e)[:200]}]"}]
        }

    ctx = (results.get("context") or "").strip()
    src = results.get("source", "")
    obs = f"[{tool_name} source={src} table={prepared_args.get('table','')} query={prepared_args.get('query','')}]\n{ctx[:1200] if ctx else '<EMPTY_CONTEXT>'}"

    updates = {
        "tool_observations": [{"agent": agent_name, "text": obs}],
        "tool_results": [{
            "agent": agent_name,
            "kind": "primary",
            "tool": tool_name,
            "args": prepared_args,
            "results": results,
        }]
    }

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

    if tool_name == "get_related_info":
        keywords = _get_keywords_for_table(state.get("plan", {}) or {}, prepared_args.get("table", ""))
        if len(keywords) >= 2:
            follow_args = dict(prepared_args)
            follow_args["query"] = keywords[1]

            if not _already_called(state, agent_name, tool_name, follow_args):
                try:
                    raw_follow = tool_func(**follow_args)
                    follow_results = _normalize_tool_result(raw_follow)
                    follow_ctx = (follow_results.get("context") or "").strip()
                    follow_src = follow_results.get("source", "")

                    updates["tool_observations"].append({
                        "agent": agent_name,
                        "text": f"[AUTO_FOLLOWUP source={follow_src} table={follow_args.get('table','')} query={follow_args.get('query','')}]\n{follow_ctx[:1200] if follow_ctx else '<EMPTY_CONTEXT>'}"
                    })
                    updates["tool_results"].append({
                        "agent": agent_name,
                        "kind": "followup",
                        "tool": tool_name,
                        "args": follow_args,
                        "results": follow_results,
                    })
                    make_log(state, "tool:followup_done", agent=agent_name, tool=tool_name, table=follow_args.get("table", ""), query=follow_args.get("query", ""), context_len=len(follow_ctx), empty=(len(follow_ctx) == 0))
                except Exception as e:
                    updates["tool_observations"].append({
                        "agent": agent_name,
                        "text": f"[Tool followup error: {tool_name} failed: {type(e).__name__}: {str(e)[:200]}]"
                    })
                    make_log(state, "tool:error_runtime", agent=agent_name, tool=tool_name, error_type=type(e).__name__, error=str(e)[:250])

    return updates