"""Graph routing helpers for evidence-pack and analysis dispatch."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

from __future__ import annotations

from collections import defaultdict

from langgraph.types import Send

from agents.agent_registry import is_analysis_agent


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
    return " | ".join(parts)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


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
                }
            )
        return targets

    targets = worker_plan.get("targets", []) or []
    grouped = defaultdict(list)
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
        grouped_payloads[agent] = dict(target)

    normalized_targets = []
    for agent, requirements in grouped.items():
        target = dict(grouped_payloads.get(agent, {}) or {})
        target["requirements"] = _dedupe_keep_order(requirements)
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
                        time_hint,
                    ),
                    "dispatch_target": target,
                    "analysis_input_results": target.get("analysis_input_results", {}) or {},
                    "evidence_queries": list(target.get("evidence_queries", []) or []),
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
