from graph.logger import make_log
from graph.router import TABLE_TO_AGENT, _group_targets_by_table

def prepare_dispatch_state(state: dict) -> dict:
    plan = state.get("plan", {}) or {}
    plan_tables = state.get("plan_tables", {}) or {}

    grouped = _group_targets_by_table(plan)
    need_web = bool(plan_tables.get("need_web", False) or plan.get("need_web", False))

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
                targets_n=len(plan.get("targets", []) or []),
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
        kws = r.get("keywords", []) or []

        if not agent:
            continue

        expected.add(agent)

        if table and kws:
            new_targets.append(
                {
                    "table": table,
                    "keywords": [str(k).strip() for k in kws if str(k).strip()],
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
                targets=new_targets[:3],
                rounds=state.get("followup_rounds", 0) + 1,
            )
        ],
    }

    if new_targets:
        next_plan = dict(state.get("plan", {}) or {})
        next_plan["targets"] = new_targets
        updates["plan"] = next_plan

    return updates
