"""Dispatch worker targets across retrieval and analysis phases."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

import re

from agents.agent_registry import get_default_table, is_analysis_agent, is_retrieval_agent
from graph.logger import make_log
from schemas.requirements import normalize_requirements_keep_order
from schemas.table_names import TABLE_BS, TABLE_CF, TABLE_IS, TABLE_NOTE, normalize_table_heading


ANALYSIS_TABLE_ALLOWLIST = {
    "agent_profitability": {TABLE_BS, TABLE_IS, TABLE_NOTE},
    "agent_liquidity_solvency": {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE},
    "agent_cashflow_analysis": {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE},
    "agent_efficiency": {TABLE_BS, TABLE_IS, TABLE_NOTE},
}


def _evidence_queries_for_analysis(worker_plan: dict, analysis_agent: str) -> list[dict]:
    queries = []
    seen = set()

    for item in (worker_plan.get("evidence_plan", []) or []):
        if not isinstance(item, dict):
            continue
        needby = [
            str(agent).strip()
            for agent in (item.get("needby", []) or item.get("needed_by", []) or [])
            if str(agent).strip()
        ]
        if needby and analysis_agent not in needby:
            continue

        table = normalize_table_heading(str(item.get("table", "") or "").strip())
        query = str(item.get("query", "") or "").strip()
        if not query:
            continue

        key = (
            table,
            query,
        )
        if key in seen:
            continue
        seen.add(key)
        queries.append(
            {
                "table": table,
                "query": query,
            }
        )

    return queries


def _normalized_targets(worker_plan: dict) -> list[dict]:
    targets = []
    for item in (worker_plan.get("analysis_plan", []) or []):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        evidence_queries = list(item.get("evidence_queries", []) or [])
        if not evidence_queries:
            evidence_queries = _evidence_queries_for_analysis(worker_plan, agent)
        payload = {
            "agent": agent,
            "table": "",
            "source": str(item.get("source", "") or "").strip(),
            "objective": str(item.get("objective", "") or "").strip(),
            "evidence_queries": evidence_queries,
        }
        targets.append(payload)

    for item in (worker_plan.get("targets", []) or []):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        if any(
            existing.get("agent") == agent
            and tuple(existing.get("requirements", []) or []) == tuple(
                str(req).strip()
                for req in (item.get("requirements", []) or [])
                if str(req).strip()
            )
            for existing in targets
        ):
            continue
        payload = {
            "agent": agent,
            "table": str(item.get("table", "") or "").strip(),
            "source": str(item.get("source", "") or "").strip(),
            "objective": str(item.get("objective", "") or "").strip(),
            "evidence_queries": list(item.get("evidence_queries", []) or [])
            or (
                _evidence_queries_for_analysis(worker_plan, agent)
                if is_analysis_agent(agent)
                else []
            ),
        }
        requirements = [
            str(req).strip()
            for req in (item.get("requirements", []) or [])
            if str(req).strip()
        ]
        if requirements or not is_analysis_agent(agent):
            payload["requirements"] = requirements
        targets.append(payload)
    return targets


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _text_tokens(value: str) -> set[str]:
    return set(re.findall(r"\w+", str(value or "").lower()))


def _first_analysis_axis(existing_targets: list[dict]) -> str:
    for target in existing_targets:
        agent = str(target.get("agent", "") or "").strip()
        if is_analysis_agent(agent):
            return agent
    return "agent_profitability"


def _dedupe_targets(targets: list[dict]) -> list[dict]:
    deduped = []
    seen = set()

    for target in targets or []:
        if not isinstance(target, dict):
            continue

        payload = {
            "agent": str(target.get("agent", "") or "").strip(),
            "table": str(target.get("table", "") or "").strip(),
            "source": str(target.get("source", "") or "").strip(),
            "objective": str(target.get("objective", "") or "").strip(),
            "evidence_queries": list(target.get("evidence_queries", []) or []),
        }
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])
        if requirements or not is_analysis_agent(payload["agent"]):
            payload["requirements"] = requirements
        if not payload["agent"]:
            continue
        if (
            not is_analysis_agent(payload["agent"])
            and not payload.get("requirements")
        ):
            continue
        if (
            is_analysis_agent(payload["agent"])
            and not payload["objective"]
            and not payload["evidence_queries"]
        ):
            continue

        key = (
            payload["agent"],
            payload["table"],
            tuple(payload.get("requirements", []) or []),
            tuple(
                (
                    str(item.get("table", "") or "").strip(),
                    str(item.get("query", "") or "").strip(),
                )
                for item in payload.get("evidence_queries", []) or []
                if isinstance(item, dict)
            ),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(payload)

    return deduped


def _result_table_from_key_payload(result_key: str, payload: dict) -> str:
    table = normalize_table_heading(str((payload or {}).get("table", "") or "").strip())
    if table:
        return table

    key_text = str(result_key or "").strip()
    if is_retrieval_agent(key_text):
        return normalize_table_heading(get_default_table(key_text))
    if key_text == "WEB":
        return ""
    return normalize_table_heading(key_text)


def _retrieval_requirements_by_table(worker_plan: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item in (worker_plan.get("evidence_plan", []) or []):
        if not isinstance(item, dict):
            continue
        table = normalize_table_heading(str(item.get("table", "") or "").strip())
        query = str(item.get("query", "") or "").strip()
        if not query:
            continue
        grouped.setdefault(table, [])
        grouped[table] = _dedupe_keep_order(list(grouped.get(table, []) or []) + [query])

    for target in _normalized_targets(worker_plan):
        agent = str(target.get("agent", "") or "").strip()
        if not agent or not is_retrieval_agent(agent):
            continue
        table = normalize_table_heading(str(target.get("table", "") or "").strip() or get_default_table(agent))
        grouped.setdefault(table, [])
        grouped[table] = _dedupe_keep_order(
            list(grouped.get(table, []) or []) + list(target.get("requirements", []) or [])
        )
    return grouped


def _analysis_input_results_for_target(state: dict, target: dict) -> dict:
    agent = str(target.get("agent", "") or "").strip()
    requirements = [
        str(item).strip()
        for item in (target.get("requirements", []) or [])
        if str(item).strip()
    ]
    evidence_queries = [
        item
        for item in (target.get("evidence_queries", []) or [])
        if isinstance(item, dict)
    ]
    requirement_tokens = set()
    for item in requirements:
        requirement_tokens.update(_text_tokens(item))
    evidence_query_tokens_by_table: dict[str, set[str]] = {}
    for item in evidence_queries:
        table = normalize_table_heading(str(item.get("table", "") or "").strip())
        query_tokens = _text_tokens(item.get("query", ""))
        query_text = str(item.get("query", "") or "").strip()
        if query_text:
            requirements.append(query_text)
        if not query_tokens:
            continue
        evidence_query_tokens_by_table.setdefault(table, set()).update(query_tokens)

    retrieval_requirements = _retrieval_requirements_by_table(state.get("worker_plan", {}) or {})
    allowlist = ANALYSIS_TABLE_ALLOWLIST.get(agent, set())
    worker_results = state.get("worker_results", {}) or {}
    prepared: dict[str, dict] = {}

    for result_key, payload in worker_results.items():
        if is_analysis_agent(str(result_key or "").strip()):
            continue
        if not isinstance(payload, dict):
            continue
        table = _result_table_from_key_payload(str(result_key or ""), payload)
        if allowlist and table and table not in allowlist:
            continue

        facts = payload.get("facts", [])
        if not isinstance(facts, list):
            continue

        matched_facts = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_tokens = _text_tokens(fact.get("item_name", ""))
            if (
                fact_tokens
                and (
                    (
                        requirement_tokens
                        and fact_tokens.intersection(requirement_tokens)
                    )
                    or (
                        evidence_query_tokens_by_table.get(table)
                        and fact_tokens.intersection(evidence_query_tokens_by_table.get(table, set()))
                    )
                )
            ):
                matched_facts.append(fact)

        target_requirement_tokens = set()
        for item in retrieval_requirements.get(table, []):
            target_requirement_tokens.update(_text_tokens(item))

        if matched_facts:
            prepared[table or str(result_key or "").strip()] = {
                "table": table,
                "facts": matched_facts,
            }
            continue

        if evidence_query_tokens_by_table.get(table):
            prepared[table or str(result_key or "").strip()] = {
                "table": table,
                "facts": facts,
            }
            continue

        if requirement_tokens and target_requirement_tokens and requirement_tokens.intersection(target_requirement_tokens):
            prepared[table or str(result_key or "").strip()] = {
                "table": table,
                "facts": facts,
            }

    if prepared:
        return prepared

    for result_key, payload in worker_results.items():
        if is_analysis_agent(str(result_key or "").strip()):
            continue
        if not isinstance(payload, dict):
            continue
        table = _result_table_from_key_payload(str(result_key or ""), payload)
        if allowlist and table and table not in allowlist:
            continue
        facts = payload.get("facts", [])
        if not isinstance(facts, list):
            continue
        prepared[table or str(result_key or "").strip()] = {
            "table": table,
            "facts": facts,
        }

    return prepared


def prepare_followup_dispatch_state(state: dict) -> dict:
    reqs = state.get("followup_requests", []) or []
    existing_targets = _normalized_targets(state.get("worker_plan", {}) or {})
    raw_requirements = []
    normalized_followup_requests = []

    for r in reqs:
        if not isinstance(r, dict):
            continue

        agent = str(r.get("agent", "") or "").strip()
        table = str(r.get("table", "") or "").strip()
        requirements = normalize_requirements_keep_order(
            r.get("requirements", []) or [],
            table=table,
        )
        if not requirements:
            continue
        raw_requirements.extend(requirements)

        followup_payload = {
            "requirements": requirements,
            "reason": str(r.get("reason", "") or "").strip(),
        }
        if agent and is_retrieval_agent(agent):
            followup_payload["table"] = table or get_default_table(agent)
        elif table:
            followup_payload["table"] = table
        normalized_followup_requests.append(followup_payload)

    followup_requirements = normalize_requirements_keep_order(raw_requirements)
    kept_analysis_targets = _dedupe_targets([
        target
        for target in existing_targets
        if target.get("agent") and is_analysis_agent(target["agent"])
    ])
    planner_plan = state.get("planner_plan", {}) or {}
    followup_axis = _first_analysis_axis(existing_targets)
    followup_plan = {
        "difficulty_level": "medium",
        "analysis_axes": [
            {
                "axis": followup_axis,
                "objective": requirement,
            }
            for requirement in followup_requirements
        ],
        "followup_mode": True,
        "followup_requirements": followup_requirements,
        "followup_requests": normalized_followup_requests,
        "company": planner_plan.get("company", "") or "",
        "time_hint": "",
        "need_web": bool(planner_plan.get("need_web", False)),
    }

    return {
        "dispatch_phase": "retrieval",
        "followup_rounds": state.get("followup_rounds", 0) + 1,
        "planner_plan": followup_plan,
        "pending_analysis_targets": kept_analysis_targets,
        "analysis_dispatch_targets": [],
        "trace": [
            make_log(
                state,
                "followup:prepare",
                requirements_n=len(followup_requirements),
                requirements=followup_requirements[:5],
                analysis_agents_preserved=sorted(
                    {
                        str(target.get("agent", "") or "").strip()
                        for target in kept_analysis_targets
                    }
                ),
                rounds=state.get("followup_rounds", 0) + 1,
            )
        ],
    }


def prepare_analysis_dispatch_state(state: dict) -> dict:
    worker_plan = state.get("worker_plan", {}) or {}
    targets = _normalized_targets(worker_plan)
    analysis_targets = [
        target
        for target in targets
        if target.get("agent") and is_analysis_agent(target["agent"])
    ]
    expected = sorted({target["agent"] for target in analysis_targets})
    prepared_targets = []

    for target in analysis_targets:
        agent = str(target.get("agent", "") or "").strip()
        analysis_input_results = _analysis_input_results_for_target(state, target)
        prepared_targets.append(
            {
                "agent": agent,
                "objective": str(target.get("objective", "") or "").strip(),
                "evidence_queries": list(target.get("evidence_queries", []) or []),
                "analysis_input_results": analysis_input_results,
            }
        )

    return {
        "expected_workers": expected,
        "dispatch_phase": "analysis",
        "analysis_dispatch_targets": prepared_targets,
        "trace": [
            make_log(
                state,
                "analysis_dispatch:prepare",
                expected=expected,
                targets_n=len(prepared_targets),
                targets=[
                    {
                        "agent": target.get("agent", ""),
                        "evidence_queries_n": len(target.get("evidence_queries", []) or []),
                        "input_tables": sorted(
                            {
                                str((payload or {}).get("table", "") or "").strip()
                                for payload in (target.get("analysis_input_results", {}) or {}).values()
                                if isinstance(payload, dict) and str((payload or {}).get("table", "") or "").strip()
                            }
                        ),
                        "input_facts_n": sum(
                            len((payload or {}).get("facts", []) or [])
                            for payload in (target.get("analysis_input_results", {}) or {}).values()
                            if isinstance(payload, dict)
                        ),
                    }
                    for target in prepared_targets[:4]
                ],
            )
        ],
    }
