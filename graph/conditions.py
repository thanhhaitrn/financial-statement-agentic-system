from common import dedupe_keep_order as _dedupe_keep_order
"""LangGraph branch predicates that decide the next workflow node."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

DEFAULT_MAX_TOOL_CALLS_PER_ROUND = 2


def _current_round(state: dict) -> int:
    return int((state or {}).get("followup_rounds", 0) or 0)

def _latest_parsed_output_for(state: dict, agent_name: str) -> dict:
    items = state.get("worker_messages", []) or []
    current_round = _current_round(state)

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


def _assigned_requirements_for_agent(state: dict, agent_name: str) -> list[str]:
    def evidence_queries_to_requirements(target: dict) -> list[str]:
        return [
            str(item.get("query", "") or "").strip()
            for item in (target.get("evidence_queries", []) or [])
            if isinstance(item, dict) and str(item.get("query", "") or "").strip()
        ]

    dispatch_target = state.get("dispatch_target")
    if isinstance(dispatch_target, dict) and str(dispatch_target.get("agent", "")).strip() == agent_name:
        return _dedupe_keep_order(
            list(dispatch_target.get("requirements", []) or [])
            + evidence_queries_to_requirements(dispatch_target)
        )

    worker_plan = state.get("worker_plan", {}) or {}
    requirements = []
    for target in (worker_plan.get("targets", []) or []):
        if str(target.get("agent", "")).strip() != agent_name:
            continue
        requirements.extend(target.get("requirements", []) or [])
        requirements.extend(evidence_queries_to_requirements(target))
    for target in (worker_plan.get("analysis_plan", []) or []):
        if str(target.get("agent", "")).strip() != agent_name:
            continue
        requirements.extend(target.get("requirements", []) or [])
        requirements.extend(evidence_queries_to_requirements(target))
    return _dedupe_keep_order(requirements)


def _max_tool_calls_for_agent(state: dict, agent_name: str) -> int:
    requirements_n = len(_assigned_requirements_for_agent(state, agent_name))
    if requirements_n > 0:
        return min(requirements_n, DEFAULT_MAX_TOOL_CALLS_PER_ROUND)
    return DEFAULT_MAX_TOOL_CALLS_PER_ROUND


def make_should_continue(agent_name: str):
    def route(state: dict) -> str:
        if _is_forced_collect(state, agent_name):
            return "collect"

        count = _tool_call_count_for_round(state, agent_name)
        if count >= _max_tool_calls_for_agent(state, agent_name):
            return "collect"

        parsed = _latest_parsed_output_for(state, agent_name)
        kind = str(parsed.get("kind", "")).strip().lower()
        if kind == "tool_calls":
            return "tools"
        if kind == "answer":
            return "collect"

        return "collect"
    return route


def should_synthesize_after_collect(state: dict) -> str:
    return state.get("collect_decision", "stop")


def synth_route(state: dict) -> str:
    d = state.get("synth_decision", {}) or {}
    rounds = state.get("followup_rounds", 0)
    followups = state.get("followup_requests", []) or []

    if d.get("status") == "need_more" and 0 < int(rounds or 0) <= 5 and followups:
        return "followup"

    return "end"
