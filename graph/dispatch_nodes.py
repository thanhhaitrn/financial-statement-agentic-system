import re

from agents.agent_registry import is_analysis_agent, is_retrieval_agent
from graph.logger import make_log


ANALYSIS_RETRIEVAL_ALLOWLIST = {
    "agent_profitability": {"agent_bs", "agent_is"},
    "agent_liquidity_solvency": {"agent_bs", "agent_is", "agent_cf"},
    "agent_cashflow_analysis": {"agent_bs", "agent_is", "agent_cf"},
    "agent_efficiency": {"agent_bs", "agent_is"},
}


def _normalized_targets(worker_plan: dict) -> list[dict]:
    targets = []
    for item in (worker_plan.get("targets", []) or []):
        if not isinstance(item, dict):
            continue
        targets.append(
            {
                "agent": str(item.get("agent", "") or "").strip(),
                "table": str(item.get("table", "") or "").strip(),
                "requirements": [
                    str(req).strip()
                    for req in (item.get("requirements", []) or [])
                    if str(req).strip()
                ],
                "source": str(item.get("source", "") or "").strip(),
            }
        )
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


def _has_explicit_time_hint(value: str) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if re.search(r"\b(19|20)\d{2}\b", text):
        return True
    if re.search(r"\bquý\s*[1-4]\b", text):
        return True
    if re.search(r"\btháng\s*\d{1,2}\b", text):
        return True
    return False


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
            "requirements": _dedupe_keep_order(target.get("requirements", []) or []),
            "source": str(target.get("source", "") or "").strip(),
        }
        if not payload["agent"] or not payload["requirements"]:
            continue

        key = (
            payload["agent"],
            payload["table"],
            tuple(payload["requirements"]),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(payload)

    return deduped


def _retrieval_requirements_by_agent(worker_plan: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for target in _normalized_targets(worker_plan):
        agent = str(target.get("agent", "") or "").strip()
        if not agent or not is_retrieval_agent(agent):
            continue
        grouped.setdefault(agent, [])
        grouped[agent] = _dedupe_keep_order(
            list(grouped.get(agent, []) or []) + list(target.get("requirements", []) or [])
        )
    return grouped


def _analysis_input_results_for_target(state: dict, target: dict) -> dict:
    agent = str(target.get("agent", "") or "").strip()
    requirements = [
        str(item).strip()
        for item in (target.get("requirements", []) or [])
        if str(item).strip()
    ]
    requirement_tokens = set()
    for item in requirements:
        requirement_tokens.update(_text_tokens(item))

    retrieval_requirements = _retrieval_requirements_by_agent(state.get("worker_plan", {}) or {})
    allowlist = ANALYSIS_RETRIEVAL_ALLOWLIST.get(agent, set())
    worker_results = state.get("worker_results", {}) or {}
    prepared: dict[str, dict] = {}

    for retrieval_agent, payload in worker_results.items():
        if not is_retrieval_agent(retrieval_agent):
            continue
        if allowlist and retrieval_agent not in allowlist:
            continue
        if not isinstance(payload, dict):
            continue

        facts = payload.get("facts", [])
        if not isinstance(facts, list):
            continue

        matched_facts = []
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            fact_tokens = _text_tokens(fact.get("item_name", ""))
            if requirement_tokens and fact_tokens and fact_tokens.intersection(requirement_tokens):
                matched_facts.append(fact)

        target_requirement_tokens = set()
        for item in retrieval_requirements.get(retrieval_agent, []):
            target_requirement_tokens.update(_text_tokens(item))

        if matched_facts:
            prepared[retrieval_agent] = {
                "table": str(payload.get("table", "") or "").strip(),
                "facts": matched_facts,
            }
            continue

        if requirement_tokens and target_requirement_tokens and requirement_tokens.intersection(target_requirement_tokens):
            prepared[retrieval_agent] = {
                "table": str(payload.get("table", "") or "").strip(),
                "facts": facts,
            }

    if prepared:
        return prepared

    for retrieval_agent, payload in worker_results.items():
        if not is_retrieval_agent(retrieval_agent):
            continue
        if allowlist and retrieval_agent not in allowlist:
            continue
        if not isinstance(payload, dict):
            continue
        facts = payload.get("facts", [])
        if not isinstance(facts, list):
            continue
        prepared[retrieval_agent] = {
            "table": str(payload.get("table", "") or "").strip(),
            "facts": facts,
        }

    return prepared


def prepare_dispatch_state(state: dict) -> dict:
    worker_plan = dict(state.get("worker_plan", {}) or {})
    targets = _normalized_targets(worker_plan)
    pending_analysis_targets = [
        target
        for target in (state.get("pending_analysis_targets", []) or [])
        if isinstance(target, dict) and is_analysis_agent(str(target.get("agent", "") or "").strip())
    ]
    if pending_analysis_targets:
        targets = _dedupe_targets(targets + pending_analysis_targets)
        worker_plan["targets"] = targets

    expected = sorted(
        {
            target["agent"]
            for target in targets
            if target["agent"] and is_retrieval_agent(target["agent"])
        }
    )
    analysis_agents = sorted(
        {
            target["agent"]
            for target in targets
            if target["agent"] and is_analysis_agent(target["agent"])
        }
    )

    return {
        "expected_workers": expected,
        "dispatch_phase": "retrieval",
        "worker_plan": worker_plan,
        "pending_analysis_targets": [],
        "trace": [
            make_log(
                state,
                "dispatch:prepare",
                expected=expected,
                targets_n=len(targets),
                retrieval_agents=expected,
                analysis_agents=analysis_agents,
            )
        ],
    }


def prepare_followup_dispatch_state(state: dict) -> dict:
    reqs = state.get("followup_requests", []) or []
    existing_targets = _normalized_targets(state.get("worker_plan", {}) or {})
    raw_requirements = []

    for r in reqs:
        if not isinstance(r, dict):
            continue

        requirements = [
            str(item).strip()
            for item in (r.get("requirements", []) or [])
            if str(item).strip()
        ]
        if not requirements:
            continue
        raw_requirements.extend(requirements)

    followup_requirements = _dedupe_keep_order(raw_requirements)
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
        analysis_input_results = _analysis_input_results_for_target(state, target)
        prepared_targets.append(
            {
                "agent": str(target.get("agent", "") or "").strip(),
                "requirements": _dedupe_keep_order(target.get("requirements", []) or []),
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
                        "requirements": target.get("requirements", [])[:2],
                        "input_agents": sorted((target.get("analysis_input_results", {}) or {}).keys()),
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
