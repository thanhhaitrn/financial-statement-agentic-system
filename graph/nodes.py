import json

from agents.agent_registry import is_analysis_agent
from agents.agent_runner import AGENT_DEFAULT_TABLE, call_analysis_agent, call_worker_agent
from tools.tool_runner import call_tool_for_agent
from agents.planner_runner import run_planner
from agents.synth_runner import run_synth
from agents.keyworder_runner import run_router
from graph.dispatch_nodes import prepare_analysis_dispatch_state
from graph.logger import make_debug_log, make_log
from schemas.agent_outputs import (
    WorkerAction,
    parse_analysis_response,
    parse_analysis_response_payload,
    parse_worker_output,
    parse_worker_response_payload,
)


def agent_planner(state: dict) -> dict:
    return run_planner(state)


def agent_router(state: dict) -> dict:
    return run_router(state)


def agent_bs_node(state: dict) -> dict:
    return call_worker_agent(state, agent_name="agent_bs")


def agent_is_node(state: dict) -> dict:
    return call_worker_agent(state, agent_name="agent_is")


def agent_cf_node(state: dict) -> dict:
    return call_worker_agent(state, agent_name="agent_cf")


def agent_web_node(state: dict) -> dict:
    return call_worker_agent(state, agent_name="agent_web")


def agent_profitability_node(state: dict) -> dict:
    return call_analysis_agent(state, agent_name="agent_profitability")


def agent_liquidity_solvency_node(state: dict) -> dict:
    return call_analysis_agent(state, agent_name="agent_liquidity_solvency")


def agent_cashflow_analysis_node(state: dict) -> dict:
    return call_analysis_agent(state, agent_name="agent_cashflow_analysis")


def agent_efficiency_node(state: dict) -> dict:
    return call_analysis_agent(state, agent_name="agent_efficiency")


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
finalize_profitability_node = _mark_done("agent_profitability")
finalize_liquidity_solvency_node = _mark_done("agent_liquidity_solvency")
finalize_cashflow_analysis_node = _mark_done("agent_cashflow_analysis")
finalize_efficiency_node = _mark_done("agent_efficiency")


def _merge_analysis_output(previous: dict, current: dict) -> dict:
    curr = dict(current or {})
    prev = dict(previous or {})

    answer = str(curr.get("answer", "") or "").strip()
    if not answer:
        answer = str(prev.get("answer", "") or "").strip()

    requirements = []
    seen_requirements = set()
    for item in list(curr.get("requirements", []) or []):
        text = str(item or "").strip()
        if not text or text in seen_requirements:
            continue
        requirements.append(text)
        seen_requirements.add(text)

    return {
        "answer": answer,
        "requirements": requirements,
    }


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


def _normalize_worker_output_table(agent_name: str, data: dict) -> dict:
    normalized = dict(data or {})
    facts = normalized.get("facts", [])
    normalized_facts = facts if isinstance(facts, list) else []
    table = (
        str(normalized.get("table", "") or "").strip()
        or next(
            (
                str((fact or {}).get("table", "") or "").strip()
                for fact in normalized_facts
                if isinstance(fact, dict) and str((fact or {}).get("table", "") or "").strip()
            ),
            "",
        )
        or str(AGENT_DEFAULT_TABLE.get(agent_name, "") or "").strip()
    )

    normalized["table"] = table
    rewritten_facts = []
    for fact in normalized_facts:
        if not isinstance(fact, dict):
            rewritten_facts.append(fact)
            continue
        fact_table = str(fact.get("table", "") or "").strip() or table
        rewritten_facts.append({**fact, "table": fact_table})
    normalized["facts"] = rewritten_facts
    return normalized


def _collect_expected_agents(state: dict, *, phase: str) -> dict:
    expected = set(state.get("expected_workers", []) or [])
    round_n = state.get("followup_rounds", 0)
    dispatch_phase = str(phase or state.get("dispatch_phase", "") or "retrieval").strip() or "retrieval"
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
    web_summary = state.get("web_summary", "")
    existing_worker_results = state.get("worker_results", {}) or {}

    for agent in sorted(expected):
        text = _latest_agent_response_for(state, agent)
        parsed_payload = _latest_parsed_output_for(state, agent)

        try:
            if is_analysis_agent(agent):
                if parsed_payload:
                    parsed_response = parse_analysis_response_payload(parsed_payload)
                    data = parsed_response.model_dump()
                else:
                    parsed = parse_analysis_response(text)
                    data = parsed.model_dump()
                kind = "answer"
                previous = existing_worker_results.get(agent, {})
                merged = _merge_analysis_output(previous, data)
                worker_results[agent] = merged
                summary = {
                    "answer_len": len(str(merged.get("answer", "") or "")),
                    "requirements_n": len(merged.get("requirements", []) or []),
                }
            else:
                if parsed_payload:
                    parsed_response = parse_worker_response_payload(parsed_payload)
                    if isinstance(parsed_response, WorkerAction):
                        raise ValueError("Worker đang trả action, chưa có answer để collect.")
                    data = _normalize_worker_output_table(
                        agent,
                        parsed_response.model_dump(exclude={"kind"}),
                    )
                else:
                    parsed = parse_worker_output(text)
                    data = _normalize_worker_output_table(agent, parsed.model_dump())
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
            previous = existing_worker_results.get(agent, {})
            if is_analysis_agent(agent):
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
            else:
                fallback = {
                    "table": "",
                    "facts": [],
                }

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
        "collected_keys": [collect_key],
        "collect_decision": "analysis" if dispatch_phase == "retrieval" and any(
            is_analysis_agent(str(target.get("agent", "") or "").strip())
            for target in ((state.get("worker_plan", {}) or {}).get("targets", []) or [])
        ) else "synth",
    })

    return updates


def collect_worker_results_node(state: dict) -> dict:
    updates = _collect_expected_agents(state, phase="retrieval")
    if str(updates.get("collect_decision", "") or "").strip() != "analysis":
        return updates

    merged_worker_results = dict(state.get("worker_results", {}) or {})
    merged_worker_results.update(updates.get("worker_results", {}) or {})
    analysis_updates = prepare_analysis_dispatch_state(
        {
            **state,
            **updates,
            "worker_results": merged_worker_results,
        }
    )
    updates["expected_workers"] = analysis_updates.get("expected_workers", [])
    updates["dispatch_phase"] = analysis_updates.get("dispatch_phase", "analysis")
    updates["analysis_dispatch_targets"] = analysis_updates.get("analysis_dispatch_targets", [])
    updates["trace"] = list(updates.get("trace", []) or []) + list(analysis_updates.get("trace", []) or [])
    return updates


def collect_analysis_results_node(state: dict) -> dict:
    return _collect_expected_agents(state, phase="analysis")
