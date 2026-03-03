AGENT_TOOLS_LIST = {
    "agent_planner": [],
    "agent_bs": [
    {"name": "get_related_info", "description": "Retrieve relevant info in the balance sheet.", "args": "query (string)"}
    ],
     "agent_is": [
    {"name": "get_related_info", "description": "Retrieve relevant info in the income statement.", "args": "query (string)"}
    ],
     "agent_cf": [
    {"name": "get_related_info", "description": "Retrieve relevant info in cash flow.", "args": "query (string)"}
    ],
    "agent_web": [
    {"name": "web_search", "description": "Perform a web search.", "args": "query (string)"}
    ],
    "agent_synth": []  # Agent5: tổng hợp
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