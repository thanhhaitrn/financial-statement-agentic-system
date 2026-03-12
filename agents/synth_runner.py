import json
from pydantic import ValidationError
from schemas.agent_outputs import SynthDecision
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import log_step

DEFAULT = {"status": "answer", "answer": "Chưa đủ dữ liệu để trả lời.", "missing": [], "followups": []}

synth_chain = PROMPT_TEMPLATE | llm.with_structured_output(SynthDecision)

def run_synth(state: dict) -> dict:
    state["last_agent"] = "agent_synth"
    log_step(state, "synth:start")

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
        dec: SynthDecision = synth_chain.invoke(payload)
        d = dec.model_dump()
    except (ValidationError, Exception) as e:
        d = DEFAULT
        log_step(state, "synth:error", error_type=type(e).__name__, error=str(e)[:250])

    state["synth_decision"] = d
    state["followup_requests"] = d.get("followups", [])
    state["missing_components"] = d.get("missing", [])
    state["num_steps"] = state.get("num_steps", 0) + 1

    log_step(state, "synth:done", status=d.get("status"), followups_n=len(state["followup_requests"]))
    return state