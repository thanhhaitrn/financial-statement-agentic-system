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
    state["last_agent"] = "agent_planner"
    log_step(state, "planner:start", query=state.get("query",""), user_query=state.get("user_query",""), worker_query=state.get("worker_query",""))

    profile = AGENT_PROFILES["agent_planner"]

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "query": state.get("query", ""),
        "user_query": state.get("user_query", state.get("query", "")),
        "plan_json": "{}",  
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": state.get("last_agent_response", ""),
        "tool_observations": "\n".join(state.get("tool_observations", [])),
        "tools_list": profile.get("tool_list", ""),
    }

    try:
        plan_obj: PlannerTablesOnly = planner_chain.invoke(payload)
        state["plan_tables"] = plan_obj.model_dump()
        state["last_agent_response"] = json.dumps(state["plan_tables"], ensure_ascii=False)

        log_step(state, "planner:done", plan_tables=state["plan_tables"])

    except (ValidationError, Exception) as e:
        state["plan_tables"] = DEFAULT_PLAN_TABLES  # ✅
        state.setdefault("tool_observations", []).append(
            f"[Planner structured_output failed: {type(e).__name__}]"
        )
        log_step(state, "planner:error", error_type=type(e).__name__, error=str(e)[:200])

    state["num_steps"] = state.get("num_steps", 0) + 1
    return state