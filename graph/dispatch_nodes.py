"""Dispatch worker targets across retrieval and analysis phases."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

import re

from agents.agent_registry import is_analysis_agent
from graph.logger import make_log
from schemas.requirements import normalize_requirements_keep_order
from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    normalize_table_heading,
)
from tools.evidence import dedupe_facts
from common import dedupe_keep_order as _dedupe_keep_order


ANALYSIS_TABLE_ALLOWLIST = {
    "agent_profitability": {TABLE_BS, TABLE_IS, TABLE_NOTE, TABLE_REPORT_SECTION},
    "agent_liquidity_solvency": {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE, TABLE_REPORT_SECTION},
    "agent_cashflow_analysis": {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE, TABLE_REPORT_SECTION},
    "agent_efficiency": {TABLE_BS, TABLE_IS, TABLE_NOTE, TABLE_REPORT_SECTION},
}
ROUTE_METADATA_FIELDS = (
    "time_hint",
    "period",
    "unit",
    "value_type",
    "evidence_query",
    "source",
)


def _route_metadata(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {}
    return {
        key: payload.get(key)
        for key in ROUTE_METADATA_FIELDS
        if payload.get(key) not in ("", None, [], {})
    }

def _evidence_item_queries(item: dict) -> list[str]:
    if not isinstance(item, dict):
        return []
    queries = []
    query = str(item.get("query", "") or "").strip()
    if query:
        queries.append(query)
    value = item.get("queries")
    if isinstance(value, (list, tuple, set)):
        queries.extend(str(query).strip() for query in value if str(query).strip())
    elif str(value or "").strip():
        queries.append(str(value).strip())
    return _dedupe_keep_order(queries)


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
        for query in _evidence_item_queries(item):
            metadata = _route_metadata(item)
            key = (
                table,
                query,
                tuple((field, str(metadata.get(field, "") or "")) for field in ROUTE_METADATA_FIELDS),
            )
            if key in seen:
                continue
            seen.add(key)
            queries.append(
                {
                    "table": table,
                    "query": query,
                    **metadata,
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
            **_route_metadata(item),
        }
        targets.append(payload)

    for item in (worker_plan.get("targets", []) or []):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        if not is_analysis_agent(agent):
            continue
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
            **_route_metadata(item),
        }
        requirements = [
            str(req).strip()
            for req in (item.get("requirements", []) or [])
            if str(req).strip()
        ]
        if requirements:
            payload["requirements"] = requirements
        targets.append(payload)
    return targets


def _text_tokens(value: str) -> set[str]:
    return set(re.findall(r"\w+", str(value or "").lower()))


def _fact_needby_values(fact: dict) -> list[str]:
    if not isinstance(fact, dict):
        return []
    raw = fact.get("needby")
    if raw is None:
        raw = fact.get("needed_by")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    return [
        str(agent).strip()
        for agent in values
        if is_analysis_agent(str(agent).strip())
    ]


def _fact_visible_to_agent(fact: dict, agent: str) -> bool:
    needby = _fact_needby_values(fact)
    return not needby or agent in needby


def _first_analysis_axis(existing_targets: list[dict]) -> str:
    for target in existing_targets:
        agent = str(target.get("agent", "") or "").strip()
        if is_analysis_agent(agent):
            return agent
    return "agent_profitability"


def _analysis_axes_from_agent_followups(reqs: list[dict]) -> list[dict]:
    axes = []
    seen = set()

    for item in reqs or []:
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        if not is_analysis_agent(agent):
            continue

        requirements = _dedupe_keep_order(item.get("requirements", []) or [])
        objective = "; ".join(requirements)
        key = (agent, objective)
        if key in seen:
            continue
        seen.add(key)
        axes.append(
            {
                "axis": agent,
                "objective": objective or str(item.get("reason", "") or "").strip(),
            }
        )

    return axes


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
            **_route_metadata(target),
        }
        if not is_analysis_agent(payload["agent"]):
            continue
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])
        if requirements:
            payload["requirements"] = requirements
        if not payload["agent"]:
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
            tuple(
                (field, str(payload.get(field, "") or ""))
                for field in ROUTE_METADATA_FIELDS
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
    if key_text == "WEB":
        return ""
    return normalize_table_heading(key_text)


def _limit_facts_for_analysis_prompt(
    table: str,
    facts: list[dict],
    *,
    state: dict | None = None,
) -> list[dict]:
    # Import lazily because graph.evidence imports this module to prepare the
    # analysis dispatch after retrieval. The evidence module is the canonical
    # owner of NOTE=12, schedule NOTE=24, main=10/16 and report-section=10.
    from graph.evidence import _llm_facts_limit_for_table

    runtime_state = state or {}
    limit = _llm_facts_limit_for_table(
        runtime_state,
        runtime_state.get("worker_plan", {}) or {},
        table,
    )
    return [
        fact
        for fact in facts or []
        if isinstance(fact, dict)
    ][:limit]


def _merge_analysis_input_payload(
    existing: dict,
    incoming: dict,
    *,
    state: dict,
    table: str,
) -> dict:
    current = existing if isinstance(existing, dict) else {}
    payload = incoming if isinstance(incoming, dict) else {}
    merged = dict(current)
    for key in ROUTE_METADATA_FIELDS:
        if merged.get(key) in ("", None, [], {}) and payload.get(key) not in ("", None, [], {}):
            merged[key] = payload.get(key)
    merged["table"] = table
    merged["facts"] = _limit_facts_for_analysis_prompt(
        table,
        dedupe_facts(
            list(current.get("facts", []) or [])
            + list(payload.get("facts", []) or [])
        ),
        state=state,
    )
    return merged


def _store_analysis_input_facts(
    prepared: dict[str, dict],
    *,
    result_key: str,
    source_payload: dict,
    table: str,
    facts: list[dict],
    state: dict,
) -> None:
    key = table or str(result_key or "").strip()
    incoming = {
        "table": table,
        "facts": facts,
        **_route_metadata(source_payload),
    }
    prepared[key] = _merge_analysis_input_payload(
        prepared.get(key, {}),
        incoming,
        state=state,
        table=table,
    )


def _retrieval_requirements_by_table(worker_plan: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for item in (worker_plan.get("evidence_plan", []) or []):
        if not isinstance(item, dict):
            continue
        table = normalize_table_heading(str(item.get("table", "") or "").strip())
        grouped.setdefault(table, [])
        grouped[table] = _dedupe_keep_order(
            list(grouped.get(table, []) or []) + _evidence_item_queries(item)
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
    evidence_query_tokens_all: set[str] = set()
    for item in evidence_queries:
        table = normalize_table_heading(str(item.get("table", "") or "").strip())
        query_tokens = _text_tokens(item.get("query", ""))
        query_text = str(item.get("query", "") or "").strip()
        if query_text:
            requirements.append(query_text)
            requirement_tokens.update(query_tokens)
        if not query_tokens:
            continue
        evidence_query_tokens_all.update(query_tokens)
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
        eligible_facts = [
            fact
            for fact in facts
            if isinstance(fact, dict) and _fact_visible_to_agent(fact, agent)
        ]
        if not eligible_facts:
            continue

        matched_facts = []
        for fact in eligible_facts:
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
                    or (
                        table == TABLE_NOTE
                        and evidence_query_tokens_all
                        and fact_tokens.intersection(evidence_query_tokens_all)
                    )
                )
            ):
                matched_facts.append(fact)

        target_requirement_tokens = set()
        for item in retrieval_requirements.get(table, []):
            target_requirement_tokens.update(_text_tokens(item))

        if matched_facts:
            _store_analysis_input_facts(
                prepared,
                result_key=str(result_key or ""),
                source_payload=payload,
                table=table,
                facts=matched_facts,
                state=state,
            )
            continue

        if evidence_query_tokens_by_table.get(table):
            _store_analysis_input_facts(
                prepared,
                result_key=str(result_key or ""),
                source_payload=payload,
                table=table,
                facts=eligible_facts,
                state=state,
            )
            continue

        if (
            requirement_tokens
            and target_requirement_tokens
            and requirement_tokens.intersection(target_requirement_tokens)
        ):
            _store_analysis_input_facts(
                prepared,
                result_key=str(result_key or ""),
                source_payload=payload,
                table=table,
                facts=eligible_facts,
                state=state,
            )

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
        eligible_facts = [
            fact
            for fact in facts
            if isinstance(fact, dict) and _fact_visible_to_agent(fact, agent)
        ]
        if not eligible_facts:
            continue
        _store_analysis_input_facts(
            prepared,
            result_key=str(result_key or ""),
            source_payload=payload,
            table=table,
            facts=eligible_facts,
            state=state,
        )

    return prepared


def prepare_followup_dispatch_state(state: dict) -> dict:
    reqs = state.get("followup_requests", []) or []
    existing_targets = _normalized_targets(state.get("worker_plan", {}) or {})
    raw_requirements = []
    normalized_followup_requests = []
    followup_analysis_axes = _analysis_axes_from_agent_followups(reqs)

    for r in reqs:
        if not isinstance(r, dict):
            continue

        table = str(r.get("table", "") or "").strip()
        agent = str(r.get("agent", "") or "").strip()
        if is_analysis_agent(agent):
            requirements = _dedupe_keep_order(r.get("requirements", []) or [])
        else:
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
            **_route_metadata(r),
        }
        if is_analysis_agent(agent):
            followup_payload["agent"] = agent
        if table:
            followup_payload["table"] = table
        normalized_followup_requests.append(followup_payload)

    followup_requirements = normalize_requirements_keep_order(raw_requirements)
    kept_analysis_targets = _dedupe_targets([
        target
        for target in existing_targets
        if target.get("agent") and is_analysis_agent(target["agent"])
    ])
    planner_plan = state.get("planner_plan", {}) or {}
    followup_route_metadata = _route_metadata(planner_plan)
    explicit_followup_metadata = {}
    for request in normalized_followup_requests:
        for key, value in _route_metadata(request).items():
            explicit_followup_metadata.setdefault(key, value)
    followup_route_metadata.update(explicit_followup_metadata)
    followup_axis = _first_analysis_axis(existing_targets)
    analysis_axes = followup_analysis_axes or [
        {
            "axis": followup_axis,
            "objective": requirement,
        }
        for requirement in followup_requirements
    ]
    followup_plan = {
        "difficulty_level": "hard" if followup_analysis_axes else "medium",
        "analysis_axes": analysis_axes,
        "followup_mode": not bool(followup_analysis_axes),
        "followup_requirements": [] if followup_analysis_axes else followup_requirements,
        "followup_requests": normalized_followup_requests,
        "company": planner_plan.get("company", "") or "",
        "time_hint": str(followup_route_metadata.get("time_hint", "") or "").strip(),
        "need_web": bool(planner_plan.get("need_web", False)),
        **{
            key: value
            for key, value in followup_route_metadata.items()
            if key != "time_hint"
        },
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
    planner_metadata = _route_metadata(state.get("planner_plan", {}) or {})

    for target in analysis_targets:
        agent = str(target.get("agent", "") or "").strip()
        analysis_input_results = _analysis_input_results_for_target(state, target)
        target_metadata = dict(planner_metadata)
        target_metadata.update(_route_metadata(target))
        prepared_targets.append(
            {
                "agent": agent,
                "objective": str(target.get("objective", "") or "").strip(),
                "evidence_queries": list(target.get("evidence_queries", []) or []),
                "analysis_input_results": analysis_input_results,
                **target_metadata,
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
