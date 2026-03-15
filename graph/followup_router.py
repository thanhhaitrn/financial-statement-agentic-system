from langgraph.types import Send


def dispatch_followups(state: dict):
    reqs = state.get("followup_requests", []) or []
    jobs = []
    seen = set()

    for r in reqs:
        if not isinstance(r, dict):
            continue

        agent = str(r.get("agent", "")).strip()
        if not agent or agent in seen:
            continue

        worker_query = str(r.get("query", "") or "").strip()
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