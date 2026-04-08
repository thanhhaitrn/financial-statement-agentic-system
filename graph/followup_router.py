from langgraph.types import Send

from graph.router import build_worker_query


def dispatch_followups(state: dict):
    reqs = state.get("followup_requests", []) or []
    jobs = []
    seen = set()
    planner_plan = state.get("planner_plan", {}) or {}
    company = planner_plan.get("company", "") or ""
    time_hint = planner_plan.get("time_hint", "") or ""

    for r in reqs:
        if not isinstance(r, dict):
            continue

        agent = str(r.get("agent", "")).strip()
        table = str(r.get("table", "") or "").strip()
        requirements = [
            str(item).strip()
            for item in (r.get("requirements", []) or [])
            if str(item).strip()
        ]
        keywords = [str(k).strip() for k in (r.get("keywords", []) or []) if str(k).strip()]
        if not agent or agent in seen:
            continue

        worker_query = str(r.get("query", "") or "").strip()
        if not worker_query and table and requirements:
            worker_query = build_worker_query(table, requirements, company, time_hint)
        if not worker_query and table and keywords:
            worker_query = build_worker_query(table, keywords, company, time_hint)
        if not worker_query:
            worker_query = state.get("user_query", "")

        jobs.append(
            Send(
                agent,
                {
                    "worker_query": worker_query,
                    "followup_rounds": state.get("followup_rounds", 0),
                },
            )
        )
        seen.add(agent)

    return jobs
