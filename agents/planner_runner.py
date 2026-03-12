import json
from pydantic import ValidationError
from schemas.agent_outputs import PlannerTablesOnly
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import log_step

DEFAULT_PLAN_TABLES = {"tables": [], "company": "", "time_hint": "", "need_web": False}
planner_chain = PROMPT_TEMPLATE | llm.with_structured_output(PlannerTablesOnly)

def run_planner(state: dict) -> dict:
    log_step(
        state,
        "planner:start",
        query=state.get("query",""),
        user_query=state.get("user_query",""),
    )

    profile = AGENT_PROFILES["agent_planner"]

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],

        # planner reads global query/user_query
        "query": state.get("query", ""),
        "user_query": state.get("user_query", state.get("query", "")),

        # planner does NOT need these, keep minimal
        "plan_json": "{}",
        "worker_results_json": "{}",
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": profile.get("tool_list", ""),
    }

    try:
        plan_obj: PlannerTablesOnly = planner_chain.invoke(payload)
        state["plan_tables"] = plan_obj.model_dump()
        log_step(state, "planner:done", plan_tables=state["plan_tables"])
    except (ValidationError, Exception) as e:
        state["plan_tables"] = DEFAULT_PLAN_TABLES
        log_step(state, "planner:error", error_type=type(e).__name__, error=str(e)[:200])

    return state