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
    selected_tables = [str(t).strip() for t in (plan_tables.get("tables", []) or [])]

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

        # ---- ENFORCE TABLE COVERAGE + KEYWORD WHITELIST (REPAIR, NOT DROP) ----
        targets_in = plan.get("targets", []) or []
        by_table = {str(t.get("table", "")).strip(): (t.get("keywords", []) or []) for t in targets_in}

        cleaned_targets = []
        invalid_all = []
        repaired_all = []

        # ensure we have exactly 1 target per selected table
        for table in selected_tables:
            kws = by_table.get(table, []) or []

            valid_kws, invalid = validate_keywords(table, kws, fuzzy=True, cutoff=0.88)
            # collect invalid samples for logging
            invalid_all.extend([{"table": table, **x} for x in (invalid or [])])

            # try repair using suggested canonical keywords (if validate_keywords provides it)
            suggested = [x.get("suggested") for x in (invalid or []) if x.get("suggested")]
            # add suggested if not already present
            for s in suggested:
                if s and s not in valid_kws:
                    valid_kws.append(s)
                    repaired_all.append({"table": table, "from": x.get("raw") if "x" in locals() else "", "to": s})

            # HARD RULE: never drop table; if still empty, keep empty and let tools block/log
            cleaned_targets.append({
                "table": table,
                "keywords": valid_kws
            })

        plan["targets"] = cleaned_targets
        state["plan"] = plan
        state["last_agent_response"] = json.dumps(state["plan"], ensure_ascii=False)

        if invalid_all:
            log_step(state, "keyworder:invalid_keywords", invalid_count=len(invalid_all), samples=invalid_all[:5])
        if repaired_all:
            log_step(state, "keyworder:repaired_keywords", repaired_count=len(repaired_all), samples=repaired_all[:5])

        # if *all* tables have empty keywords -> treat as failure
        if selected_tables and all(not t["keywords"] for t in plan["targets"]):
            raise ValueError("Keyworder produced no valid keywords for all selected tables after whitelist/repair")

    except (ValidationError, Exception) as e:
        state["plan"] = DEFAULT_KEYWORD_PLAN
        state.setdefault("tool_observations", []).append(
            f"[Keyworder structured_output failed: {type(e).__name__}]"
        )
        log_step(state, "keyworder:error", error_type=type(e).__name__, error=str(e)[:250])

    state["num_steps"] = state.get("num_steps", 0) + 1
    log_step(state, "keyworder:done", plan=state.get("plan", {}))
    return state