import json
from pydantic import ValidationError
from schemas.agent_outputs import KeywordPlan
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import log_step
from schemas.keyword_guard import validate_keywords

DEFAULT_KEYWORD_PLAN = {"targets": []}

keyworder_chain = PROMPT_TEMPLATE | llm.with_structured_output(KeywordPlan)

def run_keyworder(state: dict) -> dict:
    state["last_agent"] = "agent_keyworder"
    log_step(state, "keyworder:start", plan_tables=state.get("plan_tables", {}))

    profile = AGENT_PROFILES["agent_keyworder"]
    plan_tables = state.get("plan_tables", {}) or {}

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "query": state.get("query", ""),
        "user_query": state.get("user_query", state.get("query", "")),
        "plan_json": json.dumps(plan_tables, ensure_ascii=False),
        "worker_results_json": json.dumps(state.get("worker_results", {}), ensure_ascii=False),
        "web_summary": state.get("web_summary", ""),
        "last_agent_response": state.get("last_agent_response", ""),
        "tool_observations": "\n".join(state.get("tool_observations", [])),
        "tools_list": profile.get("tool_list", ""),
    }

    try:
        kp: KeywordPlan = keyworder_chain.invoke(payload)
        plan = kp.model_dump()

        # ---- ENFORCE ALLOWED KEYWORDS (hard gate) ----
        cleaned_targets = []
        invalid_all = []

        for t in plan.get("targets", []) or []:
            table = (t.get("table") or "").strip()
            kws = t.get("keywords", []) or []

            valid_kws, invalid = validate_keywords(table, kws, fuzzy=True, cutoff=0.88)
            invalid_all.extend([{"table": table, **x} for x in invalid])

            # hard rule: if no valid keywords, drop this target
            if not valid_kws:
                continue

            cleaned_targets.append({
                "table": table,
                "keywords": valid_kws
            })

        plan["targets"] = cleaned_targets

        # If planner selected tables but keyworder produced nothing usable -> treat as failure
        selected_tables = plan_tables.get("tables", []) or []
        if selected_tables and not plan["targets"]:
            raise ValueError("Keyworder produced no valid targets after whitelist validation")

        state["plan"] = plan
        state["last_agent_response"] = json.dumps(state["plan"], ensure_ascii=False)

        if invalid_all:
            log_step(state, "keyworder:invalid_keywords", invalid_count=len(invalid_all), samples=invalid_all[:5])

    except (ValidationError, Exception) as e:
        state["plan"] = DEFAULT_KEYWORD_PLAN
        state.setdefault("tool_observations", []).append(
            f"[Keyworder structured_output failed: {type(e).__name__}]"
        )
        log_step(state, "keyworder:error", error_type=type(e).__name__, error=str(e)[:250])

    state["num_steps"] = state.get("num_steps", 0) + 1
    log_step(state, "keyworder:done", plan=state.get("plan", {}))
    return state