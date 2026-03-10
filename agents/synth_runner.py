import json
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import log_step

synth_chain = PROMPT_TEMPLATE | llm

def run_synth(state: dict) -> dict:
    state["last_agent"] = "agent_synth"
    metrics = (state.get("plan", {}) or {}).get("metrics", []) or []
    log_step(state, "synth:start",
             followup_rounds=state.get("followup_rounds", 0),
             expected=state.get("expected_workers", []),
             done=state.get("done_workers", []))

    profile = AGENT_PROFILES["agent_synth"]

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "query": state.get("query", ""),
        "user_query": state.get("user_query", state.get("query", "")),
        "plan_json": json.dumps(state.get("plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": state.get("last_agent_response", ""),
        "tool_observations": "\n".join(state.get("tool_observations", [])),
        "tools_list": "",
    }

    try:
        resp = synth_chain.invoke(payload)
        text = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as e:
        text = "ANSWER: Chưa đủ dữ liệu để trả lời."
        log_step(state, "synth:error", error_type=type(e).__name__, error=str(e)[:200])

    state["final_answer"] = text
    state["last_agent_response"] = text
    state["synth_decision"] = {"status": "answer", "answer": text, "followups": [], "missing": []}

    state["followup_requests"] = []
    state["missing_components"] = []

    state["num_steps"] = state.get("num_steps", 0) + 1
    log_step(state, "synth:done", answer_preview=text[:160])

    return state