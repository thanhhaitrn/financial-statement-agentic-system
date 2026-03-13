import json
import re
from pydantic import ValidationError
from schemas.agent_outputs import SynthDecision
from graph.logger import log_step
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE

synth_chain = PROMPT_TEMPLATE | llm.with_structured_output(SynthDecision)

DEFAULT = {
    "status": "error",
    "answer": "Chưa đủ dữ liệu để trả lời.",
    "missing": [],
    "followups": [],
}

def _fallback_from_text(text: str) -> dict:
    m = re.search(r"^\s*ANSWER:\s*(.*)$", text, flags=re.MULTILINE | re.DOTALL)
    ans = m.group(1).strip() if m else text.strip()
    return {
        "status": "answer",
        "answer": ans,
        "missing": [],
        "followups": [],
    }

def run_synth(state: dict) -> dict:
    state["last_agent"] = "agent_synth"
    log_step(state, "synth:start", followup_rounds=state.get("followup_rounds", 0))

    profile = AGENT_PROFILES["agent_synth"]

    worker_bundle = {
        "worker_results": state.get("worker_results", {}),
        "worker_messages": state.get("worker_messages", []),
    }

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "w_worker_query": "",
        "plan_json": json.dumps(state.get("plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(worker_bundle, ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": "",
    }

    try:
        dec = synth_chain.invoke(payload)

        if hasattr(dec, "model_dump"):
            d = dec.model_dump()
        elif isinstance(dec, dict):
            d = dec
        else:
            text = getattr(dec, "content", str(dec))
            d = _fallback_from_text(text)

    except Exception as e:
        log_step(
            state,
            "synth:error",
            error_type=type(e).__name__,
            error=str(e)[:250],
        )
        d = DEFAULT.copy()

    state["synth_decision"] = d
    state["followup_requests"] = d.get("followups") or []
    state["missing_components"] = d.get("missing") or []
    state["final_answer"] = d.get("answer") or DEFAULT["answer"]
    state["last_agent_response"] = state["final_answer"]

    log_step(
        state,
        "synth:done",
        status=d.get("status"),
        followups_n=len(state["followup_requests"]),
        answer_preview=(state["final_answer"] or "")[:160],
    )
    return state