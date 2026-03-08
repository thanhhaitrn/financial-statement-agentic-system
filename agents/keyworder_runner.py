import json
from pydantic import ValidationError
from schemas.agent_outputs import KeywordPlan
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import log_step

DEFAULT_KEYWORD_PLAN = {"targets": [], "metrics": []}

# cache chain (optional)
keyworder_chain = PROMPT_TEMPLATE | llm.with_structured_output(KeywordPlan)

def run_keyworder(state: dict) -> dict:
    log_step(state, "keyworder:start", plan_tables=state.get("plan_tables", {}))
    profile = AGENT_PROFILES["agent_keyworder"]

    # Pass plan_tables into plan_json so the model can see chosen tables
    plan_tables = state.get("plan_tables", {}) or {}

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "query": state.get("query", ""),
        "user_query": state.get("user_query", state.get("query", "")),
        "plan_json": json.dumps(plan_tables, ensure_ascii=False),  # ✅ tables-only plan
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": state.get("last_agent_response", ""),
        "tool_observations": "\n".join(state.get("tool_observations", [])),
        "tools_list": profile.get("tool_list", ""),
    }

    try:
        kp: KeywordPlan = keyworder_chain.invoke(payload)
        state["plan"] = kp.model_dump()  # ✅ now plan has targets/metrics
        state["last_agent_response"] = json.dumps(state["plan"], ensure_ascii=False)
    except (ValidationError, Exception) as e:
        state["plan"] = DEFAULT_KEYWORD_PLAN
        state.setdefault("tool_observations", []).append(
            f"[Keyworder structured_output failed: {type(e).__name__}]"
        )

    state["last_agent"] = "agent_keyworder"
    state["num_steps"] = state.get("num_steps", 0) + 1
    log_step(state, "keyworder:done", plan=state.get("plan", {}))
    return state