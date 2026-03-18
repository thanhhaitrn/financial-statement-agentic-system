from langgraph.types import Send

from graph.router import build_worker_query


def dispatch_followups(state: dict):
    reqs = state.get("followup_requests", []) or []
    jobs = []
    seen = set()
    plan_tables = state.get("plan_tables", {}) or {}
    company = plan_tables.get("company", "") or ""
    time_hint = plan_tables.get("time_hint", "") or ""

    for r in reqs:
        if not isinstance(r, dict):
            continue

        agent = str(r.get("agent", "")).strip()
        table = str(r.get("table", "") or "").strip()
        keywords = [str(k).strip() for k in (r.get("keywords", []) or []) if str(k).strip()]
        if not agent or agent in seen:
            continue

        worker_query = str(r.get("query", "") or "").strip()
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
