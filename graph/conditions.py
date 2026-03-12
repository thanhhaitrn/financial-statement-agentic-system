import re
from graph.logger import log_step

def should_continue(state: dict) -> str:
    raw = state.get("w_last_agent_response", "")
    response = raw.content if hasattr(raw, "content") else (
        "\n".join(map(str, raw)) if isinstance(raw, list) else str(raw)
    )

    action_match = bool(re.search(r"^\s*ACTION:\s*", response, flags=re.MULTILINE))
    answer_match = bool(re.search(r"^\s*ANSWER:\s*", response, flags=re.MULTILINE))

    # worker-local tool state
    tool_obs_len = len(state.get("w_tool_observations", []) or [])
    last_ctx = ((state.get("w_last_tool_results") or {}).get("context") or "").strip()
    has_tool_ctx = bool(last_ctx)

    # worker-local step counter
    if state.get("w_num_steps", 0) >= 8:
        return "collect"

    # if we already got tool output and the model STILL asks for tools, stop looping
    if action_match and (tool_obs_len > 0 or has_tool_ctx):
        return "collect"

    if action_match:
        return "tools"

    if answer_match:
        return "collect"

    return "collect"