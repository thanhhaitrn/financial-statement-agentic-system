from __future__ import annotations

from collections import defaultdict

from langgraph.types import Send

from agents.agent_registry import is_analysis_agent, is_retrieval_agent


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


def _retrieval_targets(worker_plan: dict) -> list[dict]:
    targets = worker_plan.get("targets", []) or []
    grouped = defaultdict(list)
    grouped_tables = {}

    for target in targets:
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if not is_retrieval_agent(agent):
            continue

        key = (
            agent,
            str(target.get("table", "") or "").strip(),
        )
        grouped[key].extend(
            [
                str(item).strip()
                for item in (target.get("requirements", []) or [])
                if str(item).strip()
            ]
        )
        grouped_tables[key] = dict(target)

    normalized_targets = []
    for key, requirements in grouped.items():
        target = dict(grouped_tables.get(key, {}) or {})
        target["requirements"] = _dedupe_keep_order(requirements)
        normalized_targets.append(target)

    return normalized_targets


def _analysis_targets(worker_plan: dict) -> list[dict]:
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


def dispatch_workers(state: dict):
    worker_plan = state.get("worker_plan", {}) or {}
    planner_plan = state.get("planner_plan", {}) or {}

    company = planner_plan.get("company", "") or ""
    time_hint = planner_plan.get("time_hint", "") or ""
    targets = _retrieval_targets(worker_plan)

    jobs = []
    for target in targets:
        agent = str(target.get("agent", "") or "").strip()
        table = str(target.get("table", "") or "").strip()
        requirements = [
            str(item).strip()
            for item in (target.get("requirements", []) or [])
            if str(item).strip()
        ]
        if not agent:
            continue

        jobs.append(
            Send(
                agent,
                {
                    "worker_query": build_worker_query(
                        table,
                        requirements,
                        company,
                        time_hint,
                    ),
                    "dispatch_target": target,
                    "followup_rounds": state.get("followup_rounds", 0),
                },
            )
        )

    return jobs


def dispatch_analysis_workers(state: dict):
    worker_plan = state.get("worker_plan", {}) or {}
    planner_plan = state.get("planner_plan", {}) or {}

    company = planner_plan.get("company", "") or ""
    time_hint = planner_plan.get("time_hint", "") or ""
    targets = state.get("analysis_dispatch_targets", []) or _analysis_targets(worker_plan)

    jobs = []
    for target in targets:
        agent = str(target.get("agent", "") or "").strip()
        requirements = [
            str(item).strip()
            for item in (target.get("requirements", []) or [])
            if str(item).strip()
        ]
        if not agent:
            continue

        jobs.append(
            Send(
                agent,
                {
                    "worker_query": build_worker_query(
                        "",
                        requirements,
                        company,
                        time_hint,
                    ),
                    "dispatch_target": target,
                    "analysis_input_results": target.get("analysis_input_results", {}) or {},
                    "followup_rounds": state.get("followup_rounds", 0),
                },
            )
        )

    return jobs


def route_after_router(state: dict):
    jobs = dispatch_workers(state)
    if jobs:
        return jobs
    return "end"


def route_after_worker_collect(state: dict):
    decision = str(state.get("collect_decision", "") or "").strip()
    if decision == "analysis":
        jobs = dispatch_analysis_workers(state)
        if jobs:
            return jobs
        return "agent_synth"
    if decision == "synth":
        return "agent_synth"
    return "end"
