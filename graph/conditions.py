def should_continue(state: dict) -> str:
    raw = state.get("last_agent_response", "")
    if isinstance(raw, list):
        response = "\n".join(map(str, raw))
    else:
        response = str(raw)

    response = response.upper()

    if state.get("num_steps", 0) >= 3:
        return "end"

    if "HANDOFF" in response:
        return "handoff"

    if "ANSWER" in response:
        return "end"

    if "ACTION" in response:
        return "continue"

    return "end"

def which_agents(state: dict) -> str:
    response = state.get("last_agent", "")
    return response