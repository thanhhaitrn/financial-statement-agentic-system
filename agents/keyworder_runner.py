import json
import re
import time
from typing import Any, Optional

from pydantic import ValidationError

from agents.agent_tools_list import get_tools_list
from agents.agent_registry import is_analysis_agent, is_retrieval_agent
from agents.profiles import AGENT_PROFILES
from config.allowed_keywords import (
    ALIASES,
    ALLOWED_KEYWORDS,
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    build_allowed_keywords_payload,
)
from graph.dispatch_nodes import prepare_dispatch_state
from graph.logger import make_debug_log, make_log
from llm.invoke import extract_usage_metadata, invoke_prompt
from schemas.agent_outputs import DispatchPlan, Target
from agents.prompts import PROMPT_TEMPLATE

MAX_TARGET_REQUIREMENTS = 8
FOLLOWUP_ROUTE_STOPWORDS = {
    "va",
    "và",
    "ve",
    "về",
    "cua",
    "của",
    "cho",
    "tu",
    "từ",
    "den",
    "đến",
    "cuoi",
    "cuối",
    "ky",
    "kỳ",
    "neu",
    "nếu",
    "muon",
    "muốn",
    "so",
    "sanh",
    "xu",
    "huong",
    "hướng",
    "nam",
    "năm",
    "quy",
    "quý",
    "thang",
    "tháng",
}
TABLE_TO_AGENT = {
    TABLE_BS: "agent_bs",
    TABLE_IS: "agent_is",
    TABLE_CF: "agent_cf",
}
KEYWORD_TO_TABLE = {
    keyword: table
    for table, keywords in ALLOWED_KEYWORDS.items()
    for keyword in keywords
}


def _followup_requirements_from_plan(planner_plan: dict) -> list[str]:
    return _dedupe_keep_order(
        [
            str(item).strip()
            for item in (planner_plan.get("followup_requirements", []) or [])
            if str(item).strip()
        ]
    )


def _is_followup_mode(planner_plan: dict) -> bool:
    if planner_plan.get("followup_mode"):
        return True
    return bool(_followup_requirements_from_plan(planner_plan))


def _text_tokens(text: str) -> set[str]:
    tokens = set()
    for item in re.findall(r"\w+", str(text or "").lower()):
        if not item or item in FOLLOWUP_ROUTE_STOPWORDS:
            continue
        if re.fullmatch(r"(19|20)\d{2}", item):
            continue
        tokens.add(item)
    return tokens


def _candidate_route_specs() -> list[dict]:
    candidates = []
    for keyword, table in KEYWORD_TO_TABLE.items():
        candidates.append(
            {
                "agent": TABLE_TO_AGENT.get(table, ""),
                "table": table,
                "match_text": str(keyword or "").strip().lower(),
                "tokens": _text_tokens(keyword),
            }
        )

    for alias, canonical in ALIASES.items():
        table = KEYWORD_TO_TABLE.get(canonical)
        if not table:
            continue
        candidates.append(
            {
                "agent": TABLE_TO_AGENT.get(table, ""),
                "table": table,
                "match_text": str(alias or "").strip().lower(),
                "tokens": _text_tokens(alias),
            }
        )
    return candidates


FOLLOWUP_ROUTE_CANDIDATES = _candidate_route_specs()


def _heuristic_followup_route(requirement: str) -> tuple[str, str]:
    text = str(requirement or "").strip().lower()

    if any(
        marker in text
        for marker in (
            "dòng tiền",
            "lưu chuyển tiền",
            "tiền thu",
            "tiền chi",
            "trả nợ",
            "vay",
            "cổ tức",
        )
    ):
        return "agent_cf", TABLE_CF

    if any(
        marker in text
        for marker in (
            "vốn chủ sở hữu",
            "tổng tài sản",
            "tổng cộng tài sản",
            "nguồn vốn",
            "nợ",
            "hàng tồn kho",
            "phải thu",
            "phải trả",
        )
    ):
        return "agent_bs", TABLE_BS

    return "agent_is", TABLE_IS


def _route_followup_requirement(requirement: str) -> tuple[str, str]:
    normalized_requirement = str(requirement or "").strip().lower()
    req_tokens = _text_tokens(normalized_requirement)
    best_candidate = None
    best_score = 0.0

    for candidate in FOLLOWUP_ROUTE_CANDIDATES:
        score = 0.0
        match_text = str(candidate.get("match_text", "") or "").strip()
        candidate_tokens = set(candidate.get("tokens", set()) or set())

        if match_text and match_text in normalized_requirement:
            score += 5.0

        overlap = len(req_tokens.intersection(candidate_tokens))
        if overlap:
            score += overlap / max(len(candidate_tokens), 1)
            score += overlap / max(len(req_tokens), 1)

        if score > best_score:
            best_candidate = candidate
            best_score = score

    if best_candidate and best_score >= 1.0:
        return (
            str(best_candidate.get("agent", "") or "").strip(),
            str(best_candidate.get("table", "") or "").strip(),
        )

    return _heuristic_followup_route(requirement)


def _normalize_followup_router_targets(worker_plan: dict, planner_plan: dict) -> dict:
    followup_requirements = _followup_requirements_from_plan(planner_plan)
    if not followup_requirements:
        return worker_plan

    followup_set = set(followup_requirements)
    grouped_requirements: dict[tuple[str, str], list[str]] = {}
    assigned = set()
    normalized_targets = []

    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue

        agent = str(target.get("agent", "") or "").strip()
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])

        if is_analysis_agent(agent):
            normalized_targets.append(
                {
                    "agent": agent,
                    "requirements": requirements[:MAX_TARGET_REQUIREMENTS],
                }
            )
            continue

        if not is_retrieval_agent(agent):
            continue

        for requirement in requirements:
            if requirement not in followup_set:
                continue
            routed_agent, routed_table = _route_followup_requirement(requirement)
            key = (routed_agent, routed_table)
            grouped_requirements.setdefault(key, [])
            grouped_requirements[key].append(requirement)
            assigned.add(requirement)

    for requirement in followup_requirements:
        if requirement in assigned:
            continue
        routed_agent, routed_table = _route_followup_requirement(requirement)
        key = (routed_agent, routed_table)
        grouped_requirements.setdefault(key, [])
        grouped_requirements[key].append(requirement)

    retrieval_targets = []
    for (agent, table), requirements in grouped_requirements.items():
        retrieval_targets.append(
            {
                "agent": agent,
                "table": table,
                "requirements": _dedupe_keep_order(requirements)[:MAX_TARGET_REQUIREMENTS],
                "source": "followup",
            }
        )

    return {
        "targets": retrieval_targets + normalized_targets,
    }


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


def _split_retrieval_requirement_item(value: str) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []

    parts = [item.strip() for item in re.split(r"\s*[;,]\s*", text) if item.strip()]
    if len(parts) <= 1:
        return [text]
    return parts


def _normalize_retrieval_requirements(requirements: list[str]) -> list[str]:
    expanded = []
    for item in requirements or []:
        expanded.extend(_split_retrieval_requirement_item(item))
    return _dedupe_keep_order(expanded)


def _router_trace_targets(worker_plan: dict) -> list[dict]:
    targets = []
    for item in (worker_plan.get("targets", []) or []):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "") or "").strip()
        payload = {
            "agent": agent,
            "requirements": [
                str(req).strip()
                for req in (item.get("requirements", []) or [])
                if str(req).strip()
            ][:2],
        }
        if not is_analysis_agent(agent):
            payload["table"] = str(item.get("table", "") or "").strip()
        targets.append(payload)
    return targets


def _force_json_output_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema DispatchPlan.\n'
        '- Khong markdown, khong ```json, khong van ban ngoai JSON.\n'
        '- Output phai co dang: {"targets":[...]}.\n'
    )


def _plain_router_payload(payload: dict) -> dict:
    fallback_payload = dict(payload)
    fallback_payload["system_instruction"] = _force_json_output_instruction(
        str(payload.get("system_instruction", "") or "")
    )
    return fallback_payload


def _to_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    content = getattr(raw, "content", None)
    if isinstance(content, str):
        return content
    if content is not None:
        try:
            return json.dumps(content, ensure_ascii=False)
        except Exception:
            return str(content)
    return str(raw)


def _extract_first_json_object(text: str) -> Optional[str]:
    if not text:
        return None

    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    start = cleaned.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False

    for idx in range(start, len(cleaned)):
        ch = cleaned[idx]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start:idx + 1]

    return None


def _try_parse_json_object(value: Any) -> Optional[dict]:
    if isinstance(value, dict):
        if isinstance(value.get("targets"), list):
            return value

        content = value.get("content")
        if content is not None:
            return _try_parse_json_object(content)
        return None

    text = _to_text(value).strip()
    if not text:
        return None

    for candidate in (text, _extract_first_json_object(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed

    return None


def _coerce_dispatch_plan(result: Any) -> tuple[DispatchPlan, Optional[str], Optional[str]]:
    if isinstance(result, DispatchPlan):
        return result, None, None

    parsing_error = None

    if isinstance(result, dict):
        parsed = result.get("parsed")
        if isinstance(parsed, DispatchPlan):
            return parsed, None, None

        if result.get("parsing_error") is not None:
            parsing_error = str(result.get("parsing_error"))[:250]

        candidates = [
            ("parsed_dict", parsed if isinstance(parsed, dict) else None),
            ("parsed_text", parsed),
            ("raw", result.get("raw")),
            ("content", result.get("content")),
        ]
    else:
        candidates = [("result", result)]

    for source, candidate in candidates:
        payload = _try_parse_json_object(candidate)
        if payload is None:
            continue
        try:
            return DispatchPlan.model_validate(payload), parsing_error, source
        except ValidationError:
            continue

    if parsing_error:
        raise ValueError(parsing_error)

    raise ValueError("Router did not return a valid DispatchPlan payload.")


def _normalize_target_payload(item: Any) -> Optional[dict]:
    if not isinstance(item, dict):
        return None

    try:
        target = Target.model_validate(item)
    except ValidationError:
        return None

    payload = target.model_dump(exclude_none=True)
    requirements = payload.get("requirements", []) or []
    if is_retrieval_agent(str(payload.get("agent", "") or "").strip()):
        requirements = _normalize_retrieval_requirements(requirements)
    else:
        requirements = _dedupe_keep_order(requirements)

    payload["requirements"] = requirements[:MAX_TARGET_REQUIREMENTS]
    if not payload["requirements"]:
        return None
    return payload


def _sanitize_router_plan_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return {"targets": []}

    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        return {"targets": []}

    normalized_targets = []
    seen = set()

    for item in raw_targets:
        target = _normalize_target_payload(item)
        if target is None:
            continue

        key = (
            str(target.get("agent", "")).strip(),
            str(target.get("table", "") or "").strip(),
            tuple(target.get("requirements", []) or []),
        )
        if key in seen:
            continue
        seen.add(key)
        normalized_targets.append(target)

    return {"targets": normalized_targets}


def _planner_analysis_targets(planner_plan: dict) -> list[dict]:
    merged: dict[str, dict] = {}
    order: list[str] = []

    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue

        agent = str(axis.get("axis", "") or "").strip()
        if not is_analysis_agent(agent):
            continue

        objective = str(axis.get("objective", "") or "").strip()
        if agent not in merged:
            merged[agent] = {
                "agent": agent,
                "requirements": [],
            }
            order.append(agent)

        if objective:
            merged[agent]["requirements"] = _dedupe_keep_order(
                list(merged[agent].get("requirements", []) or []) + [objective]
            )[:MAX_TARGET_REQUIREMENTS]

    return [merged[agent] for agent in order]


def _finalize_router_targets(worker_plan: dict, planner_plan: dict) -> dict:
    normalized_targets = list((worker_plan or {}).get("targets", []) or [])
    retrieval_targets = [
        target
        for target in normalized_targets
        if is_retrieval_agent(str(target.get("agent", "") or "").strip())
    ]

    difficulty_level = str(planner_plan.get("difficulty_level", "") or "").strip().lower()
    if difficulty_level != "hard":
        return {"targets": retrieval_targets}

    planned_analysis_targets = _planner_analysis_targets(planner_plan)
    planned_by_agent = {
        str(target.get("agent", "") or "").strip(): dict(target)
        for target in planned_analysis_targets
        if str(target.get("agent", "") or "").strip()
    }

    analysis_targets = []
    for agent in [str(target.get("agent", "") or "").strip() for target in planned_analysis_targets]:
        planned_target = planned_by_agent.get(agent, {})
        requirements = _dedupe_keep_order(list(planned_target.get("requirements", []) or []))[:MAX_TARGET_REQUIREMENTS]
        analysis_targets.append(
            {
                "agent": agent,
                "requirements": requirements,
            }
        )

    return {"targets": retrieval_targets + analysis_targets}


def run_router(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_router"]
    planner_plan = state.get("planner_plan", {}) or {}
    trace = []
    started_at = time.perf_counter()
    llm_usage = {}

    start_log = make_debug_log(
        state,
        "router:start",
        planner_plan=planner_plan,
    )
    if start_log:
        trace.append(start_log)

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": json.dumps(planner_plan, ensure_ascii=False),
        "worker_results_json": "{}",
        "allowed_keywords_json": build_allowed_keywords_payload(),
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": get_tools_list("agent_router"),
    }

    updates = {
        "last_agent": "agent_router",
        "trace": trace,
    }

    try:
        raw_result = invoke_prompt(
            PROMPT_TEMPLATE,
            payload,
            structured_schema=DispatchPlan,
            plain_payload_factory=_plain_router_payload,
        )
        llm_usage = extract_usage_metadata(raw_result.get("raw"))
        plan_obj, parse_warning, recovered_from = _coerce_dispatch_plan(raw_result)
        worker_plan = _finalize_router_targets(
            _sanitize_router_plan_payload(plan_obj.model_dump()),
            planner_plan,
        )
        if _is_followup_mode(planner_plan):
            worker_plan = _normalize_followup_router_targets(worker_plan, planner_plan)
        dispatch_updates = prepare_dispatch_state(
            {
                **state,
                "worker_plan": worker_plan,
            }
        )

        if any(
            str(item.get("agent", "") or "").strip() == "agent_web"
            for item in (worker_plan.get("targets", []) or [])
        ):
            worker_plan["need_web"] = True

        updates["worker_plan"] = dispatch_updates.get("worker_plan", worker_plan)
        updates["expected_workers"] = dispatch_updates.get("expected_workers", [])
        updates["dispatch_phase"] = dispatch_updates.get("dispatch_phase", "retrieval")
        updates["pending_analysis_targets"] = dispatch_updates.get("pending_analysis_targets", [])

        if raw_result.get("mode") != "structured":
            fallback_log = make_debug_log(
                state,
                "router:structured_output_fallback",
                mode=raw_result.get("mode", "plain_json"),
            )
            if fallback_log:
                updates["trace"].append(fallback_log)

        if parse_warning and recovered_from:
            debug_log = make_debug_log(
                state,
                "router:recovered_from_raw",
                source=recovered_from,
                parsing_error=parse_warning,
            )
            if debug_log:
                updates["trace"].append(debug_log)

        updates["trace"].append(
            make_log(
                state,
                "router:done",
                targets_n=len((updates.get("worker_plan", {}) or {}).get("targets", []) or []),
                targets=_router_trace_targets(updates.get("worker_plan", {}) or {}),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                **llm_usage,
            )
        )
        updates["trace"].extend(dispatch_updates.get("trace", []) or [])
        return updates

    except Exception as e:
        updates["worker_plan"] = {"targets": []}
        updates["trace"].append(
            make_log(
                state,
                "router:error",
                error_type=type(e).__name__,
                error=str(e)[:250],
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )
        updates["trace"].append(
            make_log(
                state,
                "router:done",
                targets_n=0,
                targets=[],
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )
        return updates


def run_keyworder(state: dict) -> dict:
    return run_router(state)
