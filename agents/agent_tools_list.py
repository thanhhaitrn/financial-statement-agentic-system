"""Declare which tool calls each agent is allowed to request."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

from tools.langchain_tools import (
    get_langchain_tools_for_agent,
    get_tool_names_for_agent as _get_tool_names_for_agent,
    get_tool_prompt_specs_for_agent,
)

AGENT_NAMES = (
    "agent_planner",
    "agent_router",
    "agent_bs",
    "agent_is",
    "agent_cf",
    "agent_note",
    "agent_web",
    "agent_profitability",
    "agent_liquidity_solvency",
    "agent_cashflow_analysis",
    "agent_efficiency",
    "agent_synth",
)

AGENT_TOOLS_LIST = {
    agent_name: get_tool_prompt_specs_for_agent(agent_name)
    for agent_name in AGENT_NAMES
}


def build_tools_list(agent_name: str) -> str:
    tools = AGENT_TOOLS_LIST.get(agent_name, [])

    tool_lines = ["Available tools:\n"]

    for i, tool in enumerate(tools, start=1):
        tool_lines.append(
            f"""{i}. {tool['name']}
Description: {tool['description']}
Arguments: {tool['args']}
"""
        )

    return "\n".join(tool_lines)


AGENT_TOOL_PROMPTS = {
    agent_name: (build_tools_list(agent_name) if tools else "")
    for agent_name, tools in AGENT_TOOLS_LIST.items()
}


def get_tools_list(agent_name: str) -> str:
    return AGENT_TOOL_PROMPTS.get(agent_name, "")


def get_tools_for_bind(agent_name: str):
    """Return LangChain BaseTool objects for native bind_tools experiments."""
    return get_langchain_tools_for_agent(agent_name)


def get_tool_names_for_agent(agent_name: str) -> set[str]:
    """Return allowed public tool names for an agent."""
    return _get_tool_names_for_agent(agent_name)
