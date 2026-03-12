import json
from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from llm.client import llm

WORKER_AGENTS = {"agent_bs", "agent_is", "agent_cf", "agent_web"}

def extract_text(resp):
    if isinstance(resp, str):
        return resp
    if hasattr(resp, "content"):
        return resp.content
    return str(resp)

def call_agent(state: dict, agent_name: str) -> dict:
    profile = AGENT_PROFILES[agent_name]
    chain = PROMPT_TEMPLATE | llm

    is_worker = agent_name in WORKER_AGENTS

    query_text = state.get("w_worker_query", "") if is_worker else state.get("query", "")
    user_q = state.get("user_query", state.get("query", ""))

    # use worker-local context only for workers
    last_resp = state.get("w_last_agent_response", "") if is_worker else ""
    tool_obs = "\n".join(state.get("w_tool_observations", []) or []) if is_worker else ""

    response = chain.invoke({
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "query": query_text,
        "user_query": user_q,

        # planner/keyworder/synth read plan + results; workers can too, harmless
        "plan_json": json.dumps(state.get("plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),

        # avoid legacy keys
        "last_agent_response": last_resp,
        "tool_observations": tool_obs,

        "tools_list": profile.get("tool_list", "")
    })

    text = extract_text(response)

    # worker-local writes (safe for concurrency)
    state["w_last_agent_response"] = text
    state["w_last_agent"] = agent_name
    state["w_num_steps"] = state.get("w_num_steps", 0) + 1
    return state