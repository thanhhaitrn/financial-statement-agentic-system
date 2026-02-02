import json
from tools.registry import TOOLS_MAPPING_2_FUNCTIONS
from agents.agent_tools_list import AGENT_TOOLS_LIST

def call_tool(state: dict) -> dict:
    action_text = state.get("last_agent_response", "")
    agent_name = state.get("last_agent")

    if "ACTION:" not in action_text:
        state.setdefault("tool_observations", []).append(
            f"[No action found by {agent_name}: {action_text}]"
        )
        return state

    tool_name = action_text.split("ACTION:")[1].split("\n")[0].strip()

    allowed_tools = {
        tool["name"] for tool in AGENT_TOOLS_LIST.get(agent_name, [])
    }
    if tool_name not in allowed_tools:
        state.setdefault("tool_observations", []).append(
            f"[Tool '{tool_name}' NOT allowed for {agent_name}]"
        )
        return state

    args = {}
    if "ARGUMENTS:" in action_text:
        args_text = action_text.split("ARGUMENTS:")[1].strip()
        try:
            args = json.loads(args_text)
        except json.JSONDecodeError:
            state.setdefault("tool_observations", []).append(
                f"[Failed to parse arguments: {args_text}]"
            )
            return state

    tool_func = TOOLS_MAPPING_2_FUNCTIONS.get(tool_name)
    if not tool_func:
        state.setdefault("tool_observations", []).append(
            f"[Unknown tool: {tool_name}]"
        )
        return state

    results = tool_func(**args)

    state.setdefault("tool_observations", []).append(
        f"[{tool_name} results: {results.get('context')}]"
    )
    state["last_tool_results"] = results

    return state