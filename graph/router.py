"""Graph routing helpers for evidence-pack and analysis dispatch."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

from __future__ import annotations

from collections import defaultdict

from langgraph.types import Send

from agents.agent_registry import is_analysis_agent
from common import dedupe_keep_order as _dedupe_keep_order

ROUTE_METADATA_FIELDS = (
    "time_hint",
    "period",
    "unit",
    "value_type",
    "evidence_query",
    "source",
)


def _route_metadata(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        key: payload.get(key)
        for key in ROUTE_METADATA_FIELDS
        if payload.get(key) not in ("", None, [], {})
    }


def build_worker_query(
    table: str = "",
    requirements: list[str] | None = None,
    company: str = "",
    time_hint: str = "",
) -> str:
    parts = []
    if str(table or "").strip():
        parts.append(str(table).strip())
    parts.extend(
        [
            str(item).strip()
            for item in (requirements or [])
            if str(item).strip()
        ]
    )
    if company:
        parts.append(company)
    if time_hint and str(time_hint).strip() not in parts:
        parts.append(str(time_hint).strip())
    return " | ".join(parts)


def _analysis_targets(worker_plan: dict) -> list[dict]:
    if worker_plan.get("analysis_plan"):
        targets = []
        for item in worker_plan.get("analysis_plan", []) or []:
            if not isinstance(item, dict):
                continue
            agent = str(item.get("agent", "") or "").strip()
            if not is_analysis_agent(agent):
                continue
            targets.append(
                {
                    "agent": agent,
                    "objective": str(item.get("objective", "") or "").strip(),
                    "evidence_queries": list(item.get("evidence_queries", []) or []),
                    **_route_metadata(item),
                }
            )
        return targets

    targets = worker_plan.get("targets", []) or []
    grouped = defaultdict(list)
    grouped_evidence_queries = defaultdict(list)
    grouped_payloads = {}

    for target in targets:
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if not is_analysis_agent(agent):
            continue

        grouped[agent].extend(
            [
                str(item).strip()
                for item in (target.get("requirements", []) or [])
                if str(item).strip()
            ]
        )
        grouped_evidence_queries[agent].extend(
            item
            for item in (target.get("evidence_queries", []) or [])
            if isinstance(item, dict)
        )
        grouped_payloads.setdefault(agent, {})
        grouped_payloads[agent].update(
            {
                key: value
                for key, value in dict(target).items()
                if key not in {"requirements", "evidence_queries"}
                and value not in ("", None, [], {})
            }
        )

    normalized_targets = []
    for agent, requirements in grouped.items():
        target = dict(grouped_payloads.get(agent, {}) or {})
        target["requirements"] = _dedupe_keep_order(requirements)
        if grouped_evidence_queries.get(agent):
            seen_queries = set()
            target["evidence_queries"] = []
            for query in grouped_evidence_queries[agent]:
                key = (
                    str(query.get("table", "") or "").strip(),
                    str(query.get("query", "") or "").strip(),
                    tuple(
                        (field, str(query.get(field, "") or ""))
                        for field in ROUTE_METADATA_FIELDS
                    ),
                )
                if key in seen_queries:
                    continue
                seen_queries.add(key)
                target["evidence_queries"].append(dict(query))
        normalized_targets.append(target)

    return normalized_targets


def dispatch_analysis_workers(state: dict):
    worker_plan = state.get("worker_plan", {}) or {}
    planner_plan = state.get("planner_plan", {}) or {}

    company = planner_plan.get("company", "") or ""
    time_hint = planner_plan.get("time_hint", "") or ""
    targets = state.get("analysis_dispatch_targets", []) or _analysis_targets(worker_plan)

    jobs = []
    for target in targets:
        agent = str(target.get("agent", "") or "").strip()
        worker_query_items = [
            str(item).strip()
            for item in (target.get("requirements", []) or [])
            if str(item).strip()
        ]
        if not worker_query_items:
            worker_query_items = _dedupe_keep_order(
                [
                    str(item.get("query", "") or "").strip()
                    for item in (target.get("evidence_queries", []) or [])
                    if isinstance(item, dict) and str(item.get("query", "") or "").strip()
                ]
            )
        if not agent:
            continue

        dispatch_target = dict(target)
        for key, value in _route_metadata(planner_plan).items():
            dispatch_target.setdefault(key, value)
        target_time_hint = str(dispatch_target.get("time_hint", "") or time_hint).strip()

        jobs.append(
            Send(
                agent,
                {
                    "user_query": state.get("user_query", ""),
                    "dataset_id": state.get("dataset_id", ""),
                    "debug_trace": bool(state.get("debug_trace", False)),
                    "worker_plan": worker_plan,
                    "evidence_pack": state.get("evidence_pack", {}) or {},
                    "evidence_cache": state.get("evidence_cache", {}) or {},
                    "worker_results": state.get("worker_results", {}) or {},
                    "worker_query": build_worker_query(
                        "",
                        worker_query_items,
                        company,
                        target_time_hint,
                    ),
                    "dispatch_target": dispatch_target,
                    "analysis_input_results": dispatch_target.get("analysis_input_results", {}) or {},
                    "evidence_queries": list(dispatch_target.get("evidence_queries", []) or []),
                    "followup_rounds": state.get("followup_rounds", 0),
                },
            )
        )

    return jobs


def route_after_evidence(state: dict):
    decision = str(state.get("collect_decision", "") or state.get("dispatch_phase", "") or "").strip()
    if decision == "analysis":
        jobs = dispatch_analysis_workers(state)
        if jobs:
            return jobs
        return "agent_synth"
    if decision == "synth":
        return "agent_synth"
    return "end"
