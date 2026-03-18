import json
from config.allowed_keywords import ALLOWED_KEYWORDS
from schemas.agent_outputs import KeywordPlan
from agents.profiles import AGENT_PROFILES
from llm.client import llm
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import make_debug_log, make_log
from schemas.keyword_guard import repair_keywords, validate_keywords

keyworder_chain = PROMPT_TEMPLATE | llm.with_structured_output(KeywordPlan)


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _selected_tables(plan_tables: dict) -> list[str]:
    selected = []

    for table in (plan_tables.get("tables", []) or []):
        text = str(table).strip()
        if text:
            selected.append(text)

    for axis in (plan_tables.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        for table in (axis.get("tables", []) or []):
            text = str(table).strip()
            if text:
                selected.append(text)

    return _dedupe_keep_order(selected)


def _plan_components_by_table(plan_tables: dict, selected_tables: list[str]) -> dict[str, list[str]]:
    by_table = {table: [] for table in selected_tables}
    global_components = _dedupe_keep_order(plan_tables.get("required_components", []) or [])

    for axis in (plan_tables.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        tables = _dedupe_keep_order(axis.get("tables", []) or [])
        components = _dedupe_keep_order(axis.get("components", []) or [])
        for table in tables:
            if table not in by_table:
                by_table[table] = []
            by_table[table].extend(components)

    for table in list(by_table.keys()):
        specific_components = _dedupe_keep_order(by_table[table])
        if specific_components:
            by_table[table] = specific_components
        else:
            by_table[table] = global_components

    return by_table


def _allowed_keywords_payload(selected_tables: list[str]) -> str:
    allowed = {
        table: sorted(ALLOWED_KEYWORDS.get(table, set()))
        for table in selected_tables
    }
    return json.dumps(allowed, ensure_ascii=False)


def _fallback_plan_from_components(plan_tables: dict, selected_tables: list[str]) -> dict:
    component_map = _plan_components_by_table(plan_tables, selected_tables)
    targets = []

    for table in selected_tables:
        repairs, details = validate_keywords(
            table,
            component_map.get(table, []),
            fuzzy=True,
            cutoff=0.93,
        )
        for item in details:
            suggestion = item.get("suggested")
            if suggestion and suggestion not in repairs:
                repairs.append(suggestion)
        targets.append(
            {
                "table": table,
                "keywords": _dedupe_keep_order(repairs),
            }
        )

    return {"targets": targets}


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

    selected_tables = _selected_tables(plan_tables)
    plan_components_by_table = _plan_components_by_table(plan_tables, selected_tables)
    fallback_plan = _fallback_plan_from_components(plan_tables, selected_tables)

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": json.dumps(plan_tables, ensure_ascii=False),
        "worker_results_json": "{}",
        "allowed_keywords_json": _allowed_keywords_payload(selected_tables),
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
        planner_component_repairs = []

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
                            "source": "keyword",
                        }
                    )

            if not valid_kws and plan_components_by_table.get(table):
                component_valid, component_details = validate_keywords(
                    table,
                    plan_components_by_table.get(table, []),
                    fuzzy=True,
                    cutoff=0.93,
                )
                for repaired_kw in component_valid:
                    if repaired_kw not in valid_kws:
                        valid_kws.append(repaired_kw)
                for x in component_details:
                    suggestion = x.get("suggested")
                    if not suggestion:
                        continue
                    repaired_all.append(
                        {
                            "table": table,
                            "from": x.get("raw", ""),
                            "to": suggestion,
                            "source": "planner_component",
                        }
                    )
                    planner_component_repairs.append(
                        {
                            "table": table,
                            "from": x.get("raw", ""),
                            "to": suggestion,
                        }
                    )

            valid_kws = list(dict.fromkeys(valid_kws))
            cleaned_targets.append({"table": table, "keywords": valid_kws})

        plan["targets"] = cleaned_targets

        empty_tables = {
            target["table"]
            for target in cleaned_targets
            if not target["keywords"]
        }
        if empty_tables:
            fallback_by_table = {
                target["table"]: target["keywords"]
                for target in fallback_plan.get("targets", [])
            }
            for target in cleaned_targets:
                if not target["keywords"]:
                    target["keywords"] = fallback_by_table.get(target["table"], [])

        if selected_tables and any(not t["keywords"] for t in cleaned_targets):
            missing_tables = [t["table"] for t in cleaned_targets if not t["keywords"]]
            raise ValueError(
                f"Keyworder produced no valid keywords for tables: {', '.join(missing_tables)}"
            )

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

        if planner_component_repairs:
            debug_log = make_debug_log(
                state,
                "keyworder:planner_component_repairs",
                repaired_count=len(planner_component_repairs),
                samples=planner_component_repairs[:5],
            )
            if debug_log:
                updates["trace"].append(debug_log)

        updates["trace"].append(
            make_log(
                state,
                "keyworder:done",
                plan=plan,
            )
        )
        return updates

    except Exception as e:
        updates["plan"] = fallback_plan
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
