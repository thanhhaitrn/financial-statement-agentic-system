import re

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


def make_should_continue(agent_name: str):
    def route(state: dict) -> str:
        forced = set(state.get("force_collect_agents", []) or [])
        if agent_name in forced:
            return "collect"

        count = int((state.get("tool_call_counts", {}) or {}).get(agent_name, 0))
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