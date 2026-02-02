from agents.prompts import PROMPT_TEMPLATE
from agents.profiles import AGENT_PROFILES
from llm.client import llm  # wherever you initialized it

def extract_text(response):
    if isinstance(response, str):
        return response
    return str(response)


def call_agent(state: dict, agent_name: str) -> dict:
    profile = AGENT_PROFILES[agent_name]

    chain = PROMPT_TEMPLATE | llm

    response = chain.invoke({
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "query": state.get("query", ""),
        "last_agent_response": state.get("last_agent_response", ""),
        "tool_observations": "\n".join(state.get("tool_observations", [])),
        "tools_list": profile["tool_list"]
    })

    text = extract_text(response)

    state["last_agent_response"] = text
    state["last_agent"] = agent_name
    state["num_steps"] += 1

    return state