import json
from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from graph.logger import make_log

WORKER_AGENTS = {"agent_bs", "agent_is", "agent_cf", "agent_web"}


def extract_text(resp):
    if isinstance(resp, str):
        return resp
    if hasattr(resp, "content"):
        return str(resp.content or "")
    return str(resp or "")


def _tool_obs_for_agent(state: dict, agent_name: str) -> str:
    items = state.get("tool_observations", []) or []
    lines = [
        str(x.get("text", ""))
        for x in items
        if str(x.get("agent", "")).strip() == agent_name
    ]
    return "\n".join(lines)


def call_agent(state: dict, agent_name: str) -> dict:
    profile = AGENT_PROFILES[agent_name]
    chain = PROMPT_TEMPLATE | llm

    is_worker = agent_name in WORKER_AGENTS

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": state.get("worker_query", "") if is_worker else "",
        "plan_json": json.dumps(state.get("plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": state.get("last_agent_response", "") if not is_worker else "",
        "tool_observations": _tool_obs_for_agent(state, agent_name) if is_worker else "",
        "tools_list": profile.get("tool_list", ""),
    }

    resp = chain.invoke(payload)
    text = extract_text(resp)
    log_entry = make_log(
        state,
        "agent:done",
        agent_name=agent_name,
        is_worker=is_worker,
        response_preview=text[:160],
    )


    if is_worker:
        return {
            "last_agent": agent_name,
            "worker_messages": [
                {
                    "agent": agent_name,
                    "kind": "agent_response",
                    "round": state.get("followup_rounds", 0),
                    "response": text
                }
            ],
            "trace": [log_entry]
        }

    return {
        "last_agent": agent_name,
        "last_agent_response": text,
        "trace": [log_entry]
    }