import json

from agents.agent_runner import call_worker_agent
from tools.tool_runner import call_tool_for_agent
from agents.planner_runner import run_planner
from agents.synth_runner import prepare_synth_context, run_synth
from agents.keyworder_runner import run_keyworder
from graph.logger import make_debug_log, make_log
from schemas.agent_outputs import WorkerAction, parse_worker_output, parse_worker_response_payload


def agent_planner(state: dict) -> dict:
    return run_planner(state)


def agent_keyworder(state: dict) -> dict:
    return run_keyworder(state)


def agent_bs_node(state: dict) -> dict:
    return call_worker_agent(state, agent_name="agent_bs")


def agent_is_node(state: dict) -> dict:
    return call_worker_agent(state, agent_name="agent_is")


def agent_cf_node(state: dict) -> dict:
    return call_worker_agent(state, agent_name="agent_cf")


def agent_web_node(state: dict) -> dict:
    return call_worker_agent(state, agent_name="agent_web")


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


def _latest_parsed_output_for(state: dict, agent_name: str) -> dict:
    items = state.get("worker_messages", []) or []
    current_round = state.get("followup_rounds", 0)

    for item in reversed(items):
        if (
            str(item.get("agent", "")).strip() == agent_name
            and str(item.get("kind", "")) == "agent_response"
            and item.get("round", 0) == current_round
        ):
            parsed = item.get("parsed_output")
            if isinstance(parsed, dict):
                return parsed
    return {}


def _dedupe_worker_facts(facts: list[dict]) -> list[dict]:
    deduped = []
    seen = set()

    for fact in (facts or []):
        if not isinstance(fact, dict):
            continue
        key = (
            str(fact.get("item_name", "")).strip(),
            str(fact.get("time_hint", "")).strip(),
            str(fact.get("value", "")).strip(),
            str(fact.get("source", "")).strip(),
            str(fact.get("table", "")).strip(),
        )
        if key in seen:
            continue
        deduped.append(fact)
        seen.add(key)

    return deduped


def _merge_worker_output(previous: dict, current: dict) -> dict:
    prev = dict(previous or {})
    curr = dict(current or {})

    table = str(curr.get("table", "")).strip() or str(prev.get("table", "")).strip()
    facts = _dedupe_worker_facts(
        list(prev.get("facts", []) or []) + list(curr.get("facts", []) or [])
    )

    return {
        "table": table,
        "facts": facts,
    }


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
    existing_worker_results = state.get("worker_results", {}) or {}

    for agent in sorted(expected):
        text = _latest_agent_response_for(state, agent)
        parsed_payload = _latest_parsed_output_for(state, agent)

        try:
            if parsed_payload:
                parsed_response = parse_worker_response_payload(parsed_payload)
                if isinstance(parsed_response, WorkerAction):
                    raise ValueError("Worker đang trả action, chưa có answer để collect.")
                data = parsed_response.model_dump(exclude={"kind"})
            else:
                parsed = parse_worker_output(text)
                data = parsed.model_dump()
            kind = "answer"
            summary = {
                "table": data.get("table", ""),
                "facts_n": len(data.get("facts", []) or []),
            }

            if agent == "agent_web":
                web_summary = json.dumps(data, ensure_ascii=False)
            else:
                previous = existing_worker_results.get(agent, {})
                merged = _merge_worker_output(previous, data)
                worker_results[agent] = merged
                summary = {
                    "table": merged.get("table", ""),
                    "facts_n": len(merged.get("facts", []) or []),
                }

        except Exception as e:
            fallback = {
                "table": "",
                "facts": [],
            }
            previous = existing_worker_results.get(agent, {})

            if agent == "agent_web":
                kind = "fallback"
                summary = {
                    "error": "fallback",
                    "preview": text[:140],
                }
                web_summary = json.dumps(
                    {
                        "error": "worker did not return valid ANSWER",
                        "raw": text[:300],
                    },
                    ensure_ascii=False,
                )
            else:
                if previous:
                    kind = "fallback_keep_previous"
                    worker_results[agent] = previous
                    summary = {
                        "table": previous.get("table", ""),
                        "facts_n": len(previous.get("facts", []) or []),
                        "error": "fallback_keep_previous",
                    }
                else:
                    kind = "fallback"
                    worker_results[agent] = fallback
                    summary = {
                        "error": "fallback",
                        "preview": text[:140],
                    }

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
