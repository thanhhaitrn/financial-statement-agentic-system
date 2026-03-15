from pydantic import ValidationError
from agents.profiles import AGENT_PROFILES
from schemas.agent_outputs import PlannerTablesOnly
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import log_step

DEFAULT_PLAN_TABLES = {"tables": []}

planner_chain = PROMPT_TEMPLATE | llm.with_structured_output(PlannerTablesOnly)


def run_planner(state: dict) -> dict:
    log_step(
        state,
        "planner:start",
        user_query=state.get("user_query", ""),
    )

    profile = AGENT_PROFILES["agent_planner"]

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "w_worker_query": "",
        "plan_json": "{}",
        "worker_results_json": "{}",
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": profile.get("tool_list", ""),
    }

    updates = {
        "last_agent": "agent_planner",
    }

    try:
        plan_obj: PlannerTablesOnly = planner_chain.invoke(payload)
        updates["plan_tables"] = plan_obj.model_dump()
        log_step(state, "planner:done", plan_tables=updates["plan_tables"])
    except Exception as e:
        updates["plan_tables"] = DEFAULT_PLAN_TABLES
        log_step(state, "planner:error", error_type=type(e).__name__, error=str(e)[:200])

    return updates