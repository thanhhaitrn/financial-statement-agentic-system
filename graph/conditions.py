import re
from graph.logger import log_step

def should_continue(state: dict) -> str:
    raw = state.get("last_agent_response", "")
    response = raw.content if hasattr(raw, "content") else (
        "\n".join(map(str, raw)) if isinstance(raw, list) else str(raw)
    )

    action_match = bool(re.search(r"^\s*ACTION:\s*", response, flags=re.MULTILINE))
    answer_match = bool(re.search(r"^\s*ANSWER:\s*", response, flags=re.MULTILINE))

    tool_obs_len = len(state.get("tool_observations", []))
    last_ctx = ((state.get("last_tool_results") or {}).get("context") or "").strip()
    has_tool_ctx = bool(last_ctx)

    # hard stop safety
    if state.get("num_steps", 0) >= 8:
        # print("-> collect (cap)")
        return "collect"

    # if we already got tool output and the model STILL asks for tools, stop looping
    if action_match and (tool_obs_len > 0 or has_tool_ctx):
        # print("-> collect (stop after tool)")
        return "collect"

    if action_match:
        # print("-> tools")
        return "tools"

    if answer_match:
        # print("-> collect")
        return "collect"

    # print("-> collect (fallback)")
    return "collect"

def which_agents(state: dict) -> str:
    response = state.get("last_agent", "")
    return response

def ready_to_synthesize(state: dict) -> str:
    expected = set(state.get("expected_workers", []))
    done = set(state.get("done_workers", []))

    decision = "synth" if expected and expected.issubset(done) else "wait"

    log_step(
        state,
        "barrier",
        decision=decision,
        expected_n=len(expected),
        done_n=len(done),
        expected=sorted(expected),
        done=sorted(done),
    )

    return decision

def synth_route(state: dict) -> str:
    d = state.get("synth_decision", {}) or {}
    rounds = state.get("followup_rounds", 0)

    if d.get("status") == "need_more" and rounds < 2:
        state["followup_rounds"] = rounds + 1
        return "followup"

    return "end"
