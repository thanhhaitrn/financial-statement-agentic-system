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

    user_q = state.get("user_query", "")
    wq = state.get("w_worker_query", "") if is_worker else ""

    # worker-local context only for workers
    last_resp = state.get("w_last_agent_response", "") if is_worker else state.get("last_agent_response", "")
    tool_obs_list = state.get("w_tool_observations", []) if is_worker else state.get("tool_observations", [])
    tool_obs = "\n".join(tool_obs_list or [])

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],

        # no more `query`
        "user_query": user_q,
        "w_worker_query": wq,

        "plan_json": json.dumps(state.get("plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),

        "last_agent_response": last_resp,
        "tool_observations": tool_obs,

        "tools_list": profile.get("tool_list", ""),
    }

    resp = chain.invoke(payload)
    text = extract_text(resp)

    if is_worker:
        # worker-local writes (safe under parallel fan-out)
        state["w_last_agent_response"] = text
        state["w_last_agent"] = agent_name
        state["w_num_steps"] = state.get("w_num_steps", 0) + 1
    else:
        # global writes (planner/keyworder/synth are sequential)
        state["last_agent_response"] = text
        state["last_agent"] = agent_name
        state["num_steps"] = state.get("num_steps", 0) + 1

    return state