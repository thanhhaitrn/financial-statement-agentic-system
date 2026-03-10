import json
from pydantic import ValidationError
from schemas.agent_outputs import SynthDecision
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import log_step

DEFAULT_DECISION = {"status": "answer", "answer": "Chưa đủ dữ liệu để trả lời.", "followups": [], "missing": []}

synth_chain = PROMPT_TEMPLATE | llm

def run_synth(state: dict) -> dict:
    state["last_agent"] = "agent_synth"
    log_step(
        state,
        "synth:start",
        followup_rounds=state.get("followup_rounds", 0),
        expected=state.get("expected_workers", []),
        done=state.get("done_workers", []),
    )

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
        log_step(
            state,
            "synth:parsed",
            status=d.get("status"),
            followups_n=len(d.get("followups", []) or []),
            missing_n=len(d.get("missing", []) or []),
        )
    except (ValidationError, Exception) as e:
        d = DEFAULT_DECISION
        log_step(state, "synth:error", error_type=type(e).__name__, error=str(e)[:200])

    state["synth_decision"] = d
    state["final_answer"] = d.get("answer", "")
    state["last_agent_response"] = d.get("answer", "")
    state["num_steps"] = state.get("num_steps", 0) + 1

    # followup storage for orchestrator
    state["followup_requests"] = d.get("followups", [])
    state["missing_components"] = d.get("missing", [])

    log_step(
        state,
        "synth:done",
        status=d.get("status"),
        answer_preview=(state["final_answer"] or "")[:160],
        followups=state.get("followup_requests", []),
    )

    return state