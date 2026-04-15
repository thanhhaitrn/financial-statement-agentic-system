import json
import re
import time
from typing import Any, Optional

from pydantic import ValidationError

from agents.agent_tools_list import get_tools_list
from agents.profiles import AGENT_PROFILES
from datasets.registry import get_dataset
from schemas.agent_outputs import PlannerEvidencePlan
from llm.invoke import extract_usage_metadata, invoke_prompt
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import make_debug_log, make_log

DEFAULT_PLANNER_PLAN = {
    "difficulty_level": "easy",
    "analysis_axes": [],
    "company": "",
    "time_hint": "",
    "need_web": False,
}
EVALUATIVE_INTENT_PATTERNS = [
    r"\bđánh giá\b",
    r"\bnhận xét\b",
    r"\bgiải thích\b",
    r"\bxu hướng\b",
    r"\bchất lượng\b",
    r"\bbền vững\b",
    r"\brủi ro\b",
    r"\btốt không\b",
    r"\bmạnh không\b",
    r"\byếu không\b",
    r"\bassess\b",
    r"\bevaluate\b",
    r"\bexplain\b",
    r"\btrend\b",
    r"\bquality\b",
    r"\bsustainable\b",
    r"\brisk\b",
    r"\bgood profit\b",
    r"\bgenerate(?:s|d)? good profit\b",
]


def _force_json_output_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema PlannerEvidencePlan.\n'
        '- Khong markdown, khong ```json, khong van ban ngoai JSON.\n'
    )
def _plain_planner_payload(payload: dict) -> dict:
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
        return value

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


def _extract_company_from_query(user_query: str) -> str:
    text = " ".join(str(user_query or "").strip().split())
    if not text:
        return ""

    patterns = [
        r"\bcủa\s+(.+?)(?=\s+(?:tại|ngày|năm|quý|q[1-4]|là|bao nhiêu|bao nhiêu\?|$))",
        r"\b(?:công ty|doanh nghiệp|tập đoàn)\s+(.+?)(?=\s+(?:tại|ngày|năm|quý|q[1-4]|là|bao nhiêu|bao nhiêu\?|$))",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        company = match.group(1).strip(" ,.?")
        if company:
            return company

    return ""


def _coerce_planner_plan(result: Any) -> tuple[PlannerEvidencePlan, Optional[str], Optional[str]]:
    if isinstance(result, PlannerEvidencePlan):
        return result, None, None

    parsing_error = None

    if isinstance(result, dict):
        parsed = result.get("parsed")
        if isinstance(parsed, PlannerEvidencePlan):
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
            return PlannerEvidencePlan.model_validate(payload), parsing_error, source
        except ValidationError:
            continue

    if parsing_error:
        raise ValueError(parsing_error)

    raise ValueError("Planner did not return a valid PlannerEvidencePlan payload.")


def _normalize_company(value: str) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    output = []
    for item in items or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _planner_trace_summary(planner_plan: dict) -> dict:
    analysis_axes = planner_plan.get("analysis_axes", []) or []
    analysis_axes_trace = []
    for axis in analysis_axes:
        if not isinstance(axis, dict):
            continue
        analysis_axes_trace.append(dict(axis))

    return {
        "difficulty_level": str(planner_plan.get("difficulty_level", "") or "").strip(),
        "analysis_axes_n": len(analysis_axes),
        "analysis_axes": analysis_axes_trace,
        "company": str(planner_plan.get("company", "") or "").strip(),
        "time_hint": str(planner_plan.get("time_hint", "") or "").strip(),
        "need_web": bool(planner_plan.get("need_web", False)),
    }


def _enrich_plan_fields(state: dict, planner_plan: dict) -> dict:
    enriched = dict(planner_plan or {})
    dataset = None
    user_query = str((state or {}).get("user_query", "") or "")
    dataset_id = str((state or {}).get("dataset_id", "") or "").strip()
    if dataset_id:
        dataset = get_dataset(dataset_id)

    query_company = _extract_company_from_query(user_query)
    if not str(enriched.get("company", "") or "").strip():
        if query_company:
            enriched["company"] = query_company
        elif dataset is not None:
            enriched["company"] = dataset.company

    enriched["time_hint"] = ""

    return enriched


def _normalize_intent_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _contains_evaluative_intent(text: str) -> bool:
    normalized = _normalize_intent_text(text)
    if not normalized:
        return False
    return any(re.search(pattern, normalized, flags=re.IGNORECASE) for pattern in EVALUATIVE_INTENT_PATTERNS)


def _collect_planner_objectives(planner_plan: dict) -> list[str]:
    objectives = []
    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        objective = str(axis.get("objective", "") or "").strip()
        if objective:
            objectives.append(objective)
    return _dedupe_keep_order(objectives)


def _apply_planner_difficulty_heuristics(state: dict, planner_plan: dict) -> tuple[dict, Optional[dict]]:
    enriched = dict(planner_plan or {})
    current = _normalize_intent_text(enriched.get("difficulty_level", ""))
    if current == "hard":
        return enriched, None

    text_candidates = [
        str((state or {}).get("user_query", "") or "").strip(),
        *(_collect_planner_objectives(enriched)),
    ]
    if not any(_contains_evaluative_intent(text) for text in text_candidates):
        return enriched, None

    enriched["difficulty_level"] = "hard"
    return enriched, make_debug_log(
        state,
        "planner:difficulty_upgraded_by_heuristic",
        previous_difficulty=current or "",
        upgraded_difficulty="hard",
        matched_texts=[text for text in text_candidates if _contains_evaluative_intent(text)][:3],
    )


def run_planner(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_planner"]
    trace = []
    started_at = time.perf_counter()
    llm_usage = {}

    start_log = make_debug_log(
        state,
        "planner:start",
        user_query=state.get("user_query", ""),
    )
    if start_log:
        trace.append(start_log)

    payload = {
        "role": profile["role"],
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": "{}",
        "worker_results_json": "{}",
        "allowed_keywords_json": "{}",
        "web_summary": "",
        "last_agent_response": "",
        "tool_observations": "",
        "tools_list": get_tools_list("agent_planner"),
    }

    updates = {
        "last_agent": "agent_planner",
        "trace": trace,
    }

    try:
        raw_result = invoke_prompt(
            PROMPT_TEMPLATE,
            payload,
            structured_schema=PlannerEvidencePlan,
            plain_payload_factory=_plain_planner_payload,
        )
        llm_usage = extract_usage_metadata(raw_result.get("raw"))
        plan_obj, parse_warning, recovered_from = _coerce_planner_plan(raw_result)
        updates["planner_plan"] = _enrich_plan_fields(state, plan_obj.model_dump())
        updates["planner_plan"], heuristic_log = _apply_planner_difficulty_heuristics(
            state,
            updates["planner_plan"],
        )
        if raw_result.get("mode") != "structured":
            fallback_log = make_debug_log(
                state,
                "planner:structured_output_fallback",
                mode=raw_result.get("mode", "plain_json"),
            )
            if fallback_log:
                updates["trace"].append(fallback_log)
        if heuristic_log:
            updates["trace"].append(heuristic_log)
        if parse_warning and recovered_from:
            debug_log = make_debug_log(
                state,
                "planner:recovered_from_raw",
                source=recovered_from,
                parsing_error=parse_warning,
            )
            if debug_log:
                updates["trace"].append(debug_log)
        updates["trace"].append(
            make_log(
                state,
                "planner:done",
                **_planner_trace_summary(updates["planner_plan"]),
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                **llm_usage,
            )
        )
    except Exception as e:
        updates["planner_plan"] = _enrich_plan_fields(state, DEFAULT_PLANNER_PLAN)
        updates["trace"].append(
            make_log(
                state,
                "planner:error",
                error_type=type(e).__name__,
                error=str(e)[:250],
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        )

    dataset_id = str((state or {}).get("dataset_id", "") or "").strip()
    dataset = get_dataset(dataset_id) if dataset_id else None
    query_company = str((updates.get("planner_plan", {}) or {}).get("company", "") or "").strip()
    dataset_company = str(getattr(dataset, "company", "") or "").strip()
    if query_company and dataset_company and _normalize_company(query_company) not in _normalize_company(dataset_company):
        debug_log = make_debug_log(
            state,
            "planner:dataset_company_mismatch",
            query_company=query_company,
            dataset_company=dataset_company,
        )
        if debug_log:
            updates["trace"].append(debug_log)

    return updates
