import copy
from langgraph.types import Send
from graph.logger import log_step

def dispatch_followups(state: dict):
    reqs = state.get("followup_requests", []) or []
    jobs = []
    expected = set()

    # build plan.targets for followup run (so tool_runner can override query by kws)
    new_targets = []
    for r in reqs:
        agent = r.get("agent")
        table = r.get("table", "")
        kws = r.get("keywords", []) or []
        if not agent:
            continue
        expected.add(agent)
        if table and kws:
            new_targets.append({"table": table, "keywords": kws})

    # reset expected/done for barrier
    state["expected_workers"] = list(expected)
    state["done_workers"] = []
    log_step(state, "followup:dispatch", expected=state["expected_workers"], targets=new_targets[:3])

    for r in reqs:
        agent = r.get("agent")
        if not agent:
            continue
        child = copy.deepcopy(state)
        child["plan"] = {"targets": new_targets}   # overwrite plan for deterministic tool queries
        child["last_agent_response"] = ""
        child["tool_observations"] = []
        child["num_steps"] = 0
        child["seen_tool_calls"] = []
        jobs.append(Send(agent, child))

    return jobs