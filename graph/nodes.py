import json
import re

from agents.agent_runner import call_agent
from tools.tool_runner import call_tool_for_agent, WORKER_TO_TABLE
from agents.planner_runner import run_planner
from agents.synth_runner import run_synth
from agents.keyworder_runner import run_keyworder
from graph.logger import log_step


def agent_planner(state: dict) -> dict:
    return run_planner(state)


def agent_keyworder(state: dict) -> dict:
    return run_keyworder(state)


def agent_bs_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_bs")


def agent_is_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_is")


def agent_cf_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_cf")


def agent_web_node(state: dict) -> dict:
    return call_agent(state, agent_name="agent_web")


def tools_bs_node(state: dict) -> dict:
    return call_tool_for_agent(state, "agent_bs")


def tools_is_node(state: dict) -> dict:
    return call_tool_for_agent(state, "agent_is")


def tools_cf_node(state: dict) -> dict:
    return call_tool_for_agent(state, "agent_cf")


def tools_web_node(state: dict) -> dict:
    return call_tool_for_agent(state, "agent_web")


def _mark_done(agent_name: str):
    def node(state: dict) -> dict:
        log_step(state, "worker:done", agent=agent_name)
        return {"done_workers": [agent_name]}
    return node


finalize_bs_node = _mark_done("agent_bs")
finalize_is_node = _mark_done("agent_is")
finalize_cf_node = _mark_done("agent_cf")
finalize_web_node = _mark_done("agent_web")


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


def collect_all_workers(state: dict) -> dict:
    expected = set(state.get("expected_workers", []) or [])
    done = set(state.get("done_workers", []) or [])
    round_n = state.get("followup_rounds", 0)
    collected_rounds = set(state.get("collected_rounds", []) or [])

    if not expected or not expected.issubset(done):
        log_step(
            state,
            "collect:skip_not_ready",
            round=round_n,
            expected=sorted(expected),
            done=sorted(done),
        )
        return {"collect_decision": "stop"}

    if round_n in collected_rounds:
        log_step(state, "collect:skip_already_collected", round=round_n)
        return {"collect_decision": "stop"}

    worker_results = {}
    web_summary = state.get("web_summary", "")

    for agent in sorted(expected):
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


def agent_synth_node(state: dict) -> dict:
    return run_synth(state)