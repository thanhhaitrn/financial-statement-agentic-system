import re
from graph.logger import log_step
import json

def _latest_agent_response_for(state: dict, agent_name: str) -> str:
    items = state.get("worker_messages", []) or []
    current_round = state.get("followup_rounds", 0)

    for item in reversed(items):
        if (
            str(item.get("agent", "")).strip() == agent_name
            and str(item.get("kind", "")) == "agent_response"
            and item.get("round", 0) == current_round
        ):
            return str(item.get("response", "") or "")
    return ""


def make_should_continue(agent_name: str):
    def route(state: dict) -> str:
        text = _latest_agent_response_for(state, agent_name)
        return "tools" if re.search(r"\bACTION\s*:", text) else "collect"
    return route


def collect_all_workers(state: dict) -> dict:
    expected = set(state.get("expected_workers", []) or [])
    done = set(state.get("done_workers", []) or [])
    round_n = state.get("followup_rounds", 0)
    collected_rounds = set(state.get("collected_rounds", []) or [])

    # not ready yet
    if not expected or not expected.issubset(done):
        log_step(
            state,
            "collect:skip_not_ready",
            round=round_n,
            expected=sorted(expected),
            done=sorted(done),
        )
        return {"collect_decision": "stop"}

    # already collected this round
    if round_n in collected_rounds:
        log_step(state, "collect:skip_already_collected", round=round_n)
        return {"collect_decision": "stop"}

    worker_results = {}
    web_summary = state.get("web_summary", "")

    for agent in expected:
        text = _latest_agent_response_for(state, agent)

        m = re.search(r"^\s*ANSWER:\s*(.*)$", text, flags=re.MULTILINE | re.DOTALL)
        if m:
            payload = m.group(1).strip()
            kind = "answer"
            preview = payload[:140]
        else:
            payload = json.dumps(
                {
                    "error": "worker did not return ANSWER",
                    "raw": text[:300],
                },
                ensure_ascii=False,
            )
            kind = "fallback"
            preview = text[:140]

        if agent == "agent_web":
            web_summary = payload
        else:
            worker_results[agent] = payload

        log_step(
            state,
            "collect",
            agent=agent,
            round=round_n,
            kind=kind,
            preview=preview,
        )

    return {
        "worker_results": worker_results,
        "web_summary": web_summary,
        "last_agent": "collector",
        "collected_rounds": [round_n],
        "collect_decision": "synth",
    }


def should_synthesize_after_collect(state: dict) -> str:
    return state.get("collect_decision", "stop")


def synth_route(state: dict) -> str:
    d = state.get("synth_decision", {}) or {}
    rounds = state.get("followup_rounds", 0)

    if d.get("status") == "need_more" and rounds < 2:
        return "followup"

    return "end"