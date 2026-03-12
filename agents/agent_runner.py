from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from llm.client import llm
import json

def extract_text(response):
    if isinstance(response, str):
        return response
    if hasattr(response, "content"):
        return response.content
    return str(response)

def call_agent(state: dict, agent_name: str) -> dict:
    profile = AGENT_PROFILES[agent_name]
    chain = PROMPT_TEMPLATE | llm

    response = chain.invoke({
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "query": state.get("worker_query", ""),
        "user_query": state.get("user_query", state.get("root_query", state.get("query", ""))),
        "plan_json": json.dumps(state.get("plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": state.get("last_agent_response", ""),
        "tool_observations": "\n".join(state.get("tool_observations", [])),
        "tools_list": profile.get("tool_list", "")
    })

    text = extract_text(response)
    state["w_last_agent_response"] = text
    state["w_last_agent"] = agent_name
    state["w_num_steps"] = state.get("w_num_steps", 0) + 1
    return state