import json
import re

from schemas.agent_outputs import SynthDecision
from graph.logger import make_log
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
    start_log = make_log(
        state,
        "synth:start",
        followup_rounds=state.get("followup_rounds", 0),
    )

    profile = AGENT_PROFILES["agent_synth"]
    worker_results = state.get("worker_results", {})

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": json.dumps(state.get("plan", {}), ensure_ascii=False),
        "worker_results_json": json.dumps(worker_results, ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": "",
    }

    updates = {
        "last_agent": "agent_synth",
        "trace": [start_log],
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
        updates["trace"].append(
            make_log(
                state,
                "synth:error",
                error_type=type(e).__name__,
                error=str(e)[:250],
            )
        )
        d = DEFAULT.copy()

    updates.update({
        "synth_decision": d,
        "followup_requests": d.get("followups") or [],
        "missing_components": d.get("missing") or [],
        "final_answer": d.get("answer") or DEFAULT["answer"],
        "last_agent_response": d.get("answer") or DEFAULT["answer"],
    })

    updates["trace"].append(
        make_log(
            state,
            "synth:done",
            status=d.get("status"),
            followups_n=len(updates["followup_requests"]),
            answer_preview=(updates["final_answer"] or "")[:160],
        )
    )

    return updates