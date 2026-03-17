from agents.profiles import AGENT_PROFILES
from schemas.agent_outputs import PlannerTablesOnly
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import make_debug_log, make_log

DEFAULT_PLAN_TABLES = {"tables": []}

planner_chain = PROMPT_TEMPLATE | llm.with_structured_output(PlannerTablesOnly)


def run_planner(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_planner"]
    trace = []

    start_log = make_debug_log(
        state,
        "planner:start",
        user_query=state.get("user_query", ""),
    )
    if start_log:
        trace.append(start_log)

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": "{}",
        "worker_results_json": "{}",
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": profile.get("tool_list", ""),
    }

    updates = {
        "last_agent": "agent_planner",
        "trace": trace,
    }

    try:
        plan_obj: PlannerTablesOnly = planner_chain.invoke(payload)
        updates["plan_tables"] = plan_obj.model_dump()
        updates["trace"].append(
            make_log(
                state,
                "planner:done",
                plan_tables=updates["plan_tables"],
            )
        )
    except Exception as e:
        updates["plan_tables"] = DEFAULT_PLAN_TABLES
        updates["trace"].append(
            make_log(
                state,
                "planner:error",
                error_type=type(e).__name__,
                error=str(e)[:250],
            )
        )

    return updates
