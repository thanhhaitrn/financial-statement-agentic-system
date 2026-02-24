AGENT_TOOLS_LIST = {
    "agent_main": [
        {
            "name": "get_related_info",
            "description": "Retrieve relevant information in knowledge base.",
            "args": "query (string)"
        },
        {
            "name": "web_search",
            "description": "Perform a web search.",
            "args": "query (string)"
        }
    ],
    "agent_loan": [
        {
            "name": "calculate_dti",
            "description": "Calculate the DTI",
            "args": "None"
        },
    ]
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