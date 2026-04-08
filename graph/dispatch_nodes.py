from graph.logger import make_log
from graph.router import TABLE_TO_AGENT, _group_targets_by_table

def prepare_dispatch_state(state: dict) -> dict:
    worker_plan = state.get("worker_plan", {}) or {}
    planner_plan = state.get("planner_plan", {}) or {}

    grouped = _group_targets_by_table(worker_plan)
    need_web = bool(planner_plan.get("need_web", False) or worker_plan.get("need_web", False))

    expected = set()
    for table in grouped.keys():
        worker = TABLE_TO_AGENT.get(table)
        if worker:
            expected.add(worker)

    if need_web:
        expected.add("agent_web")

    updates = {
        "expected_workers": sorted(expected),
        "trace": [
            make_log(
                state,
                "dispatch:prepare",
                expected=sorted(expected),
                targets_n=len(worker_plan.get("targets", []) or []),
                tables=list(grouped.keys()),
                need_web=need_web,
            )
        ],
    }

    return updates


def prepare_followup_dispatch_state(state: dict) -> dict:
    reqs = state.get("followup_requests", []) or []

    expected = set()
    new_targets = []

    for r in reqs:
        if not isinstance(r, dict):
            continue

        agent = str(r.get("agent", "")).strip()
        table = str(r.get("table", "")).strip()
        requirements = [
            str(item).strip()
            for item in (r.get("requirements", []) or [])
            if str(item).strip()
        ]
        reason = str(r.get("reason", "") or "").strip()

        if not agent:
            continue

        expected.add(agent)

        if table:
            new_targets.append(
                {
                    "table": table,
                    "keywords": [],
                    "requirements": requirements[:3],
                    "reason": reason,
                    "source": "followup",
                }
            )

    updates = {
        "expected_workers": sorted(expected),
        "followup_rounds": state.get("followup_rounds", 0) + 1,
        "trace": [
            make_log(
                state,
                "followup:prepare",
                expected=sorted(expected),
                targets=[
                    {
                        "table": target.get("table", ""),
                        "requirements": target.get("requirements", []),
                    }
                    for target in new_targets[:3]
                ],
                rounds=state.get("followup_rounds", 0) + 1,
            )
        ],
    }

    if new_targets:
        next_worker_plan = dict(state.get("worker_plan", {}) or {})
        next_worker_plan["targets"] = new_targets
        updates["worker_plan"] = next_worker_plan

    return updates
