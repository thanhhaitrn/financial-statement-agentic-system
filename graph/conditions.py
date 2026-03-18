import re


def _current_round(state: dict) -> int:
    return int((state or {}).get("followup_rounds", 0) or 0)


def _latest_agent_response_for(state: dict, agent_name: str) -> str:
    items = state.get("worker_messages", []) or []
    current_round = _current_round(state)

    for item in reversed(items):
        if (
            str(item.get("agent", "")).strip() == agent_name
            and str(item.get("kind", "")) == "agent_response"
            and item.get("round", 0) == current_round
        ):
            return str(item.get("response", "") or "")
    return ""


def _is_forced_collect(state: dict, agent_name: str) -> bool:
    current_round = _current_round(state)
    items = state.get("force_collect_agents", {}) or {}
    value = items.get(agent_name)
    try:
        return int(value) == current_round
    except (TypeError, ValueError):
        return False


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


def make_should_continue(agent_name: str):
    def route(state: dict) -> str:
        if _is_forced_collect(state, agent_name):
            return "collect"

        count = _tool_call_count_for_round(state, agent_name)
        if count >= 2:
            return "collect"

        text = _latest_agent_response_for(state, agent_name)
        return "tools" if re.search(r"\bACTION\s*:", text) else "collect"
    return route


def should_synthesize_after_collect(state: dict) -> str:
    return state.get("collect_decision", "stop")


def synth_route(state: dict) -> str:
    d = state.get("synth_decision", {}) or {}
    rounds = state.get("followup_rounds", 0)

    if d.get("status") == "need_more" and rounds < 2:
        return "followup"

    return "end"
