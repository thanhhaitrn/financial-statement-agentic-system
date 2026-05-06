"""Concrete LangGraph node functions that wrap planner, router, agents, and tools."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

from agents.agent_runner import call_analysis_agent
from tools.tool_runner import call_tool_for_agent
from agents.planner_runner import run_planner
from agents.synth_runner import run_synth
from agents.keyworder_runner import run_router
from graph.evidence import build_evidence_pack
from graph.logger import make_debug_log, make_log
from schemas.agent_outputs import (
    parse_analysis_response,
    parse_analysis_response_payload,
)


def agent_planner(state: dict) -> dict:
    return run_planner(state)


def agent_router(state: dict) -> dict:
    return run_router(state)


def evidence_pack_node(state: dict) -> dict:
    return build_evidence_pack(state)


def agent_profitability_node(state: dict) -> dict:
    return call_analysis_agent(state, agent_name="agent_profitability")


def agent_liquidity_solvency_node(state: dict) -> dict:
    return call_analysis_agent(state, agent_name="agent_liquidity_solvency")


def agent_cashflow_analysis_node(state: dict) -> dict:
    return call_analysis_agent(state, agent_name="agent_cashflow_analysis")


def agent_efficiency_node(state: dict) -> dict:
    return call_analysis_agent(state, agent_name="agent_efficiency")


def tools_profitability_node(state: dict) -> dict:
    return call_tool_for_agent(state, "agent_profitability")


def tools_liquidity_solvency_node(state: dict) -> dict:
    return call_tool_for_agent(state, "agent_liquidity_solvency")


def tools_cashflow_analysis_node(state: dict) -> dict:
    return call_tool_for_agent(state, "agent_cashflow_analysis")


def tools_efficiency_node(state: dict) -> dict:
    return call_tool_for_agent(state, "agent_efficiency")


def agent_synth_node(state: dict) -> dict:
    return run_synth(state)


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


finalize_profitability_node = _mark_done("agent_profitability")
finalize_liquidity_solvency_node = _mark_done("agent_liquidity_solvency")
finalize_cashflow_analysis_node = _mark_done("agent_cashflow_analysis")
finalize_efficiency_node = _mark_done("agent_efficiency")


def _merge_analysis_output(previous: dict, current: dict, *, round_n: int | None = None) -> dict:
    curr = dict(current or {})
    prev = dict(previous or {})

    answer = str(curr.get("answer", "") or "").strip()
    if not answer:
        answer = str(prev.get("answer", "") or "").strip()
        output_round = prev.get("round")
    else:
        output_round = round_n

    requirements = []
    seen_requirements = set()
    for item in list(curr.get("requirements", []) or []):
        text = str(item or "").strip()
        if not text or text in seen_requirements:
            continue
        requirements.append(text)
        seen_requirements.add(text)

    merged = {
        "answer": answer,
        "requirements": requirements,
    }
    if output_round is not None:
        merged["round"] = output_round
    return merged


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


def _collect_expected_analysis_agents(state: dict) -> dict:
    expected = set(state.get("expected_workers", []) or [])
    round_n = state.get("followup_rounds", 0)
    dispatch_phase = "analysis"
    collected_keys = set(str(item) for item in (state.get("collected_keys", []) or []))
    collect_key = f"{round_n}:{dispatch_phase}"
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

    if collect_key in collected_keys:
        updates["trace"].append(
            make_log(
                state,
                "collect:skip_already_collected",
                round=round_n,
                phase=dispatch_phase,
            )
        )
        updates["collect_decision"] = "stop"
        return updates

    worker_results = {}
    existing_worker_results = state.get("worker_results", {}) or {}

    for agent in sorted(expected):
        text = _latest_agent_response_for(state, agent)
        parsed_payload = _latest_parsed_output_for(state, agent)

        try:
            if parsed_payload:
                parsed_response = parse_analysis_response_payload(parsed_payload)
                data = parsed_response.model_dump()
            else:
                parsed = parse_analysis_response(text)
                data = parsed.model_dump()
            kind = "answer"
            previous = existing_worker_results.get(agent, {})
            merged = _merge_analysis_output(previous, data, round_n=round_n)
            worker_results[agent] = merged
            summary = {
                "answer_len": len(str(merged.get("answer", "") or "")),
                "requirements_n": len(merged.get("requirements", []) or []),
                "analysis_round": merged.get("round"),
            }

        except Exception:
            previous = existing_worker_results.get(agent, {})
            fallback = {
                "answer": "",
                "requirements": [],
            }
            if previous:
                kind = "fallback_keep_previous"
                worker_results[agent] = previous
                summary = {
                    "answer_len": len(str(previous.get("answer", "") or "")),
                    "requirements_n": len(previous.get("requirements", []) or []),
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
        "last_agent": "collector",
        "collected_rounds": [round_n],
        "collected_keys": [collect_key],
        "collect_decision": "synth",
    })

    return updates


def collect_analysis_results_node(state: dict) -> dict:
    return _collect_expected_analysis_agents(state)
