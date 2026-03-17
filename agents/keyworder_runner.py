import json
from schemas.agent_outputs import KeywordPlan
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import make_debug_log, make_log
from schemas.keyword_guard import repair_keywords, validate_keywords

keyworder_chain = PROMPT_TEMPLATE | llm.with_structured_output(KeywordPlan)


def run_keyworder(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_keyworder"]
    plan_tables = state.get("plan_tables", {}) or {}
    trace = []

    start_log = make_debug_log(
        state,
        "keyworder:start",
        plan_tables=state.get("plan_tables", {}),
    )
    if start_log:
        trace.append(start_log)

    selected_tables = []
    seen = set()
    for t in (plan_tables.get("tables", []) or []):
        name = str(t).strip()
        if name and name not in seen:
            selected_tables.append(name)
            seen.add(name)

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": json.dumps(plan_tables, ensure_ascii=False),
        "worker_results_json": "{}",
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": profile.get("tool_list", ""),
    }

    updates = {
        "last_agent": "agent_keyworder",
        "trace": trace,
    }

    try:
        kp: KeywordPlan = keyworder_chain.invoke(payload)
        plan = kp.model_dump()

        targets_in = plan.get("targets", []) or []

        by_table = {}
        for t in targets_in:
            table = str(t.get("table", "")).strip()
            kws = t.get("keywords", []) or []
            if not table:
                continue
            by_table.setdefault(table, [])
            by_table[table].extend(kws)

        cleaned_targets = []
        invalid_all = []
        repaired_all = []

        for table in selected_tables:
            kws = by_table.get(table, [])
            valid_kws, invalid = validate_keywords(table, kws, fuzzy=True, cutoff=0.88)

            invalid = invalid or []
            invalid_all.extend([{"table": table, **x} for x in invalid])

            for x in invalid:
                s = x.get("suggested")
                if s and s not in valid_kws:
                    valid_kws.append(s)
                    repaired_all.append({
                        "table": table,
                        "from": x.get("raw", ""),
                        "to": s,
                    })

            if not valid_kws and kws:
                fallback_repairs, fallback_details = repair_keywords(table, kws)
                for repaired_kw in fallback_repairs:
                    if repaired_kw not in valid_kws:
                        valid_kws.append(repaired_kw)
                for x in fallback_details:
                    repaired_all.append(
                        {
                            "table": table,
                            "from": x.get("raw", ""),
                            "to": x.get("suggested", ""),
                        }
                    )

            valid_kws = list(dict.fromkeys(valid_kws))
            cleaned_targets.append({"table": table, "keywords": valid_kws})

        plan["targets"] = cleaned_targets

        if selected_tables and all(not t["keywords"] for t in cleaned_targets):
            raise ValueError("Keyworder produced no valid keywords for selected tables after repair")

        updates["plan"] = plan

        if invalid_all:
            updates["trace"].append(
                make_log(
                    state,
                    "keyworder:invalid_keywords",
                    invalid_count=len(invalid_all),
                    samples=invalid_all[:5],
                )
            )

        if repaired_all:
            updates["trace"].append(
                make_log(
                    state,
                    "keyworder:repaired_keywords",
                    repaired_count=len(repaired_all),
                    samples=repaired_all[:5],
                )
            )

        updates["trace"].append(
            make_log(
                state,
                "keyworder:done",
                plan=plan,
            )
        )
        return updates

    except Exception as e:
        updates["plan"] = {
            "targets": [{"table": t, "keywords": []} for t in selected_tables]
        }
        updates["trace"].append(
            make_log(
                state,
                "keyworder:error",
                error_type=type(e).__name__,
                error=str(e)[:250],
            )
        )
        updates["trace"].append(
            make_log(
                state,
                "keyworder:done",
                plan=updates["plan"],
            )
        )
        return updates
