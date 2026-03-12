import json
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

def call_tool(state: dict) -> dict:
    #  worker-local read
    raw = state.get("w_last_agent_response", "")
    action_text = raw.content if hasattr(raw, "content") else str(raw or "")
    agent_name = state.get("w_last_agent", "") or state.get("last_agent", "")

    if "ACTION:" not in action_text:
        state.setdefault("w_tool_observations", []).append(
            f"[No action found by {agent_name}: {action_text}]"
        )
        log_step(state, "tool:skip_no_action", agent=agent_name, preview=action_text[:120])
        return state

    tool_name = action_text.split("ACTION:")[1].split("\n")[0].strip()

    allowed_tools = {tool["name"] for tool in AGENT_TOOLS_LIST.get(agent_name, [])}
    if tool_name not in allowed_tools:
        state.setdefault("w_tool_observations", []).append(
            f"[Tool '{tool_name}' NOT allowed for {agent_name}]"
        )
        log_step(state, "tool:blocked_not_allowed", agent=agent_name, tool=tool_name)
        return state

    args = {}
    if "ARGUMENTS:" in action_text:
        args_text = action_text.split("ARGUMENTS:")[1].strip()
        try:
            args = json.loads(args_text)
        except json.JSONDecodeError:
            state.setdefault("w_tool_observations", []).append(
                f"[Failed to parse arguments: {args_text}]"
            )
            log_step(state, "tool:blocked_bad_args", agent=agent_name, tool=tool_name, args_preview=args_text[:200])
            return state

    tool_func = TOOLS_MAPPING_2_FUNCTIONS.get(tool_name)
    if not tool_func:
        state.setdefault("w_tool_observations", []).append(f"[Unknown tool: {tool_name}]")
        log_step(state, "tool:blocked_unknown_tool", agent=agent_name, tool=tool_name)
        return state

    # Inject collection + table
    if tool_name == "get_related_info":
        if _COLLECTION is None:
            state.setdefault("w_tool_observations", []).append(
                "[Tool error: collection not set. Call set_collection(collection) before running workflow.]"
            )
            log_step(state, "tool:error_no_collection", agent=agent_name)
            return state

        args["collection"] = _COLLECTION
        args["table"] = WORKER_TO_TABLE.get(agent_name, args.get("table", ""))

        # Deterministic: ALWAYS override query from plan keywords
        plan = state.get("plan", {}) or {}
        targets = plan.get("targets", []) or []
        table = args["table"]

        matching = next(
            (t for t in targets
             if str(t.get("table", "")).strip().upper() == str(table).strip().upper()),
            None
        )
        kws = (matching.get("keywords", []) if matching else []) or []

        log_step(state, "tool:using_keywords", agent=agent_name, table=table, kws=kws[:4])

        if not kws:
            state.setdefault("w_tool_observations", []).append(
                f"[Tool blocked: no keywords in plan for table={table}]"
            )
            log_step(state, "tool:blocked_no_keywords", agent=agent_name, table=table)
            return state

        args["query"] = kws[0]

    # seen signatures must be worker-local
    sig = (agent_name, tool_name, args.get("table", ""), args.get("query", ""))
    seen = set(state.get("w_seen_tool_calls", []) or [])
    if sig in seen:
        state.setdefault("w_tool_observations", []).append(
            f"[Tool call blocked: repeated identical call: {tool_name} query={args.get('query','')}]"
        )
        log_step(state, "tool:blocked_repeat", agent=agent_name, tool=tool_name, table=args.get("table",""), query=args.get("query",""))
        return state
    seen.add(sig)
    state["w_seen_tool_calls"] = list(seen)

    log_step(state, "tool:start", agent=agent_name, tool=tool_name, table=args.get("table", ""), query=args.get("query", ""))

    results = tool_func(**args)

    ctx = (results.get("context") or "").strip()
    src = results.get("source", "")
    ctx_short = ctx[:1200]

    state.setdefault("w_tool_observations", []).append(
        f"[{tool_name} source={src} table={args.get('table','')} query={args.get('query','')}]\n"
        + (ctx_short if ctx_short else "<EMPTY_CONTEXT>")
    )

    log_step(state, "tool:done", agent=agent_name, tool=tool_name, table=args.get("table", ""), query=args.get("query", ""),
            context_len=len(ctx), empty=(len(ctx) == 0))

    # Deterministic follow-up: keyword2
    if tool_name == "get_related_info":
        plan = state.get("plan", {}) or {}
        targets = plan.get("targets", []) or []
        table = args.get("table", "")

        matching = next(
            (t for t in targets
             if str(t.get("table", "")).strip().upper() == str(table).strip().upper()),
            None
        )
        kws = (matching.get("keywords", []) if matching else []) or []

        if len(kws) >= 2:
            follow_query = kws[1]
            follow_args = dict(args)
            follow_args["query"] = follow_query

            follow_sig = (agent_name, tool_name, table, follow_query)
            seen = set(state.get("w_seen_tool_calls", []) or [])
            if follow_sig not in seen:
                seen.add(follow_sig)
                state["w_seen_tool_calls"] = list(seen)

                log_step(state, "tool:followup_start", agent=agent_name, table=table, query=follow_query)

                follow_results = tool_func(**follow_args)
                follow_ctx = (follow_results.get("context") or "").strip()
                follow_ctx_short = follow_ctx[:1200]

                state.setdefault("w_tool_observations", []).append(
                    f"[AUTO_FOLLOWUP table={table} query={follow_query}]\n"
                    + (follow_ctx_short if follow_ctx_short else "<EMPTY_CONTEXT>")
                )

                log_step(state, "tool:followup_done", agent=agent_name, table=table, query=follow_query,
                        context_len=len(follow_ctx), empty=(len(follow_ctx) == 0))

    # store worker-local last tool results
    state["w_last_tool_results"] = results
    return state