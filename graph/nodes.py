import json

from agents.agent_runner import call_agent
from tools.tool_runner import call_tool_for_agent
from agents.planner_runner import run_planner
from agents.synth_runner import prepare_synth_context, run_synth
from agents.keyworder_runner import run_keyworder
from graph.logger import make_debug_log, make_log
from schemas.agent_outputs import parse_worker_output


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


def agent_synth_node(state: dict) -> dict:
    return run_synth(state)


def prepare_synth_context_node(state: dict) -> dict:
    return prepare_synth_context(state)


def _mark_done(agent_name: str):
    def node(state: dict) -> dict:
        trace = []
        log_entry = make_debug_log(state, "worker:done", agent=agent_name)
        if log_entry:
            trace.append(log_entry)
        round_n = int((state or {}).get("followup_rounds", 0) or 0)
        return {
            "done_workers": {agent_name: round_n},
            "trace": trace,
        }
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
    round_n = state.get("followup_rounds", 0)
    collected_rounds = set(state.get("collected_rounds", []) or [])
    done_state = state.get("done_workers", {}) or {}

    if isinstance(done_state, dict):
        done = {
            agent_name
            for agent_name, marked_round in done_state.items()
            if int(marked_round) == round_n
        }
    else:
        done = set(done_state or [])

    updates = {"trace": []}

    if not expected or not expected.issubset(done):
        updates["trace"].append(
            make_log(
                state,
                "collect:skip_not_ready",
                round=round_n,
                expected=sorted(expected),
                done=sorted(done),
            )
        )
        updates["collect_decision"] = "stop"
        return updates

    if round_n in collected_rounds:
        updates["trace"].append(
            make_log(
                state,
                "collect:skip_already_collected",
                round=round_n,
            )
        )
        updates["collect_decision"] = "stop"
        return updates

    worker_results = {}
    web_summary = state.get("web_summary", "")

    for agent in sorted(expected):
        text = _latest_agent_response_for(state, agent)

        try:
            parsed = parse_worker_output(text)
            data = parsed.model_dump()
            data["missing"] = []
            kind = "answer"
            summary = {
                "table": data.get("table", ""),
                "facts_n": len(data.get("facts", []) or []),
            }

            if agent == "agent_web":
                web_summary = json.dumps(data, ensure_ascii=False)
            else:
                worker_results[agent] = data

        except Exception as e:
            fallback = {
                "table": "",
                "facts": [],
                "notes": f"Parse lỗi từ {agent}: {str(e)}"
            }
            kind = "fallback"
            summary = {
                "error": "fallback",
                "preview": text[:140],
            }

            if agent == "agent_web":
                web_summary = json.dumps(
                    {
                        "error": "worker did not return valid ANSWER",
                        "raw": text[:300],
                    },
                    ensure_ascii=False,
                )
            else:
                worker_results[agent] = fallback

        updates["trace"].append(
            make_log(
                state,
                "collect",
                agent=agent,
                round=round_n,
                kind=kind,
                **summary,
            )
        )

    updates.update({
        "worker_results": worker_results,
        "web_summary": web_summary,
        "last_agent": "collector",
        "collected_rounds": [round_n],
        "collect_decision": "synth",
    })

    return updates
