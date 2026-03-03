import json
from tools.registry import TOOLS_MAPPING_2_FUNCTIONS
from agents.agent_tools_list import AGENT_TOOLS_LIST

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
    raw = state.get("last_agent_response", "")
    action_text = raw.content if hasattr(raw, "content") else str(raw or "")
    agent_name = state.get("last_agent")

    if "ACTION:" not in action_text:
        state.setdefault("tool_observations", []).append(
            f"[No action found by {agent_name}: {action_text}]"
        )
        return state

    tool_name = action_text.split("ACTION:")[1].split("\n")[0].strip()

    allowed_tools = {tool["name"] for tool in AGENT_TOOLS_LIST.get(agent_name, [])}
    if tool_name not in allowed_tools:
        state.setdefault("tool_observations", []).append(
            f"[Tool '{tool_name}' NOT allowed for {agent_name}]"
        )
        return state

    args = {}
    if "ARGUMENTS:" in action_text:
        args_text = action_text.split("ARGUMENTS:")[1].strip()
        try:
            args = json.loads(args_text)
        except json.JSONDecodeError:
            state.setdefault("tool_observations", []).append(
                f"[Failed to parse arguments: {args_text}]"
            )
            return state

    tool_func = TOOLS_MAPPING_2_FUNCTIONS.get(tool_name)
    if not tool_func:
        state.setdefault("tool_observations", []).append(f"[Unknown tool: {tool_name}]")
        return state

    if tool_name == "get_related_info":
        if _COLLECTION is None:
            state.setdefault("tool_observations", []).append(
                "[Tool error: collection not set. Call set_collection(collection) before running workflow.]"
            )
            return state
        args["collection"] = _COLLECTION
        args["table"] = WORKER_TO_TABLE.get(agent_name, args.get("table", ""))

        plan = state.get("plan", {}) or {}
        targets = plan.get("targets", []) or []
        table = args["table"]

        matching = next((t for t in targets if str(t.get("table","")).strip().upper() == str(table).strip().upper()), None)
        if matching:
            kws = matching.get("keywords", []) or []
            if kws:
                args["query"] = kws[0]

    sig = (agent_name, tool_name, args.get("table", ""), args.get("query", ""))
    seen = set(state.get("seen_tool_calls", []))
    if sig in seen:
        state.setdefault("tool_observations", []).append(
            f"[Tool call blocked: repeated identical call: {tool_name} query={args.get('query','')}]"
        )
        return state
    seen.add(sig)
    state["seen_tool_calls"] = list(seen)

    results = tool_func(**args)
    ctx = (results.get("context") or "").strip()
    src = results.get("source", "")
    ctx_short = ctx[:1200]

    state.setdefault("tool_observations", []).append(
        f"[{tool_name} source={src} table={args.get('table','')} query={args.get('query','')}]\n"
        + (ctx_short if ctx_short else "<EMPTY_CONTEXT>")
    )

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

                follow_results = tool_func(**follow_args)
                follow_ctx = (follow_results.get("context") or "").strip()
                follow_ctx_short = follow_ctx[:1200]

                state.setdefault("tool_observations", []).append(
                    f"[AUTO_FOLLOWUP table={table} query={follow_query}]\n"
                    + (follow_ctx_short if follow_ctx_short else "<EMPTY_CONTEXT>")
                )

    state["last_tool_results"] = results
    return state