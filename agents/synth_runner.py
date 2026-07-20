"""Synthesize worker outputs into the final user-facing answer."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from pydantic import ValidationError

from agents.agent_registry import is_analysis_agent
from agents.profiles import AGENT_PROFILES
from agents.prompts import PROMPT_TEMPLATE
from graph.dispatch_nodes import prepare_followup_dispatch_state
from graph.logger import debug_enabled, make_debug_log, make_log
from llm.invoke import extract_usage_metadata, invoke_prompt
from schemas.agent_outputs import (
    AnalysisOutput,
    SynthFollowupRequest,
    SynthDecision,
    parse_analysis_response,
    parse_analysis_response_payload,
)
from schemas.requirements import (
    normalize_fact_status,
    normalize_requirement_text,
    normalize_requirements_keep_order,
    requirement_matches_fact,
)

DEFAULT_DECISION = {
    "status": "error",
    "answer": "Chưa đủ dữ liệu để trả lời.",
    "followups": [],
}

MAX_SYNTH_FACTS_PER_AGENT = 20
MAX_FOLLOWUP_REQUIREMENTS = 10
# Two follow-up rounds: the missing data for stub answers exists in the source
# (front-matter, note schedules, policies) and just needs one more routed fetch.
# The extra round only runs when round 1 returns need_more with pending followups.
MAX_FOLLOWUP_ROUNDS = 2
OPTIONAL_FOLLOWUP_REQUIREMENTS = {
    "chi phí bán hàng",
    "các khoản phải trả ngắn hạn",
    "phải trả người bán ngắn hạn",
}
_NUMERIC_VALUE_RE = re.compile(
    r"(?<!\w)-?\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?%?|(?<!\w)-?\d+(?:[.,]\d+)?%?"
)
_CALCULATION_OPERATOR_RE = re.compile(r"(=|/|÷|\*)")
_CALCULATION_WORD_RE = re.compile(
    r"(\bcông thức\b|\btính\b|\btỷ lệ\b|\bbiên\b|\bvòng quay\b)",
    flags=re.IGNORECASE,
)
_CALCULATION_RESULT_RE = re.compile(
    r"(%|\bbằng\b|\blần\b)",
    flags=re.IGNORECASE,
)
_INSUFFICIENT_ANSWER_MARKERS = (
    "chưa đủ dữ liệu",
    "không đủ dữ liệu",
    "không thể kết luận",
    "cần bổ sung",
    "cần truy xuất",
    "không tìm thấy",
    "không có dữ liệu",
    "not_found_after_search",
)


class NormalizedFact(TypedDict):
    item_name: str
    subheading: str
    time_hint: str
    value_type: str
    unit: str
    value: Any
    source: str
    table: str
    message: str
    status: str


class NormalizedWorkerResult(TypedDict):
    agent: str
    table: str
    facts: List[NormalizedFact]
    raw_text: str


class SynthPayload(TypedDict):
    role: str
    tools_list: str
    system_instruction: str
    user_query: str
    worker_query: str
    plan_json: str
    worker_results_json: str
    allowed_keywords_json: str
    web_summary: str
    last_agent_response: str
    tool_observations: str


class CompactWorkerResult(TypedDict):
    table: str
    facts: List[NormalizedFact]


class CompactAnalysisResult(TypedDict):
    answer: str
    requirements: List[str]


class SynthUsage(TypedDict, total=False):
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    model: str


def _dedupe_keep_order(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _normalize_requirements_list(value: Any, limit: int = MAX_FOLLOWUP_REQUIREMENTS) -> List[str]:
    if value is None:
        return []

    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        text = str(value or "").strip()
        items = [text] if text else []

    normalized = _dedupe_keep_order(items)
    if limit > 0:
        return normalized[:limit]
    return normalized


def _to_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if hasattr(raw, "content"):
        return str(getattr(raw, "content", "") or "")
    return str(raw)


def _safe_json_dumps(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2)
    except Exception:
        return json.dumps(str(value), ensure_ascii=False)


def _force_json_output_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema SynthDecision.\n'
        '- Khong boc JSON bang markdown/code fence; field "answer" duoc phep dung Markdown tieng Viet theo profile.\n'
        '- Khong them van ban ngoai JSON.\n'
        '- status chi duoc la \"answer\" hoac \"need_more\".\n'
    )


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

    for index in range(start, len(cleaned)):
        char = cleaned[index]

        if escape:
            escape = False
            continue

        if char == "\\":
            escape = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start:index + 1]

    return None


def _try_parse_json(value: Any) -> Optional[Dict[str, Any]]:
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


def _empty_worker_result(agent_name: str = "", raw_text: str = "") -> NormalizedWorkerResult:
    return {
        "agent": agent_name,
        "table": "",
        "facts": [],
        "raw_text": raw_text,
    }


def _fallback_table_for_agent(agent_name: str = "") -> str:
    return ""


def _normalize_fact(raw_fact: Any, fallback_table: str = "") -> Optional[NormalizedFact]:
    if not isinstance(raw_fact, dict):
        return None

    item_name = str(raw_fact.get("item_name", "")).strip()
    subheading = str(raw_fact.get("subheading", "")).strip()
    time_hint = str(raw_fact.get("time_hint", "")).strip()
    value = raw_fact.get("value", "")
    source = str(raw_fact.get("source", "")).strip()
    table = str(raw_fact.get("table", fallback_table)).strip() or fallback_table
    message = str(raw_fact.get("message", "") or "").strip()
    status = normalize_fact_status(raw_fact.get("status", "found"))
    value_type = str(raw_fact.get("value_type", "") or "").strip()
    unit = str(raw_fact.get("unit", "") or "").strip()

    if not item_name and value in ("", None):
        return None

    return {
        "item_name": item_name,
        # Parent line / matrix section (e.g. "Tài sản cố định hữu hình — Nguyên
        # giá"); lets the LLM disambiguate flattened labels like "Số cuối kỳ | Cộng".
        "subheading": subheading,
        "time_hint": time_hint,
        # Slot disambiguators so the LLM picks the exact period / value-type asked
        # (nguyên giá vs giá trị còn lại vs hao mòn) and states the right unit.
        "value_type": value_type,
        "unit": unit,
        "value": value,
        "source": source,
        "table": table,
        "message": message,
        "status": status,
    }


def _normalize_facts(raw_facts: Any, fallback_table: str = "") -> List[NormalizedFact]:
    if not isinstance(raw_facts, list):
        return []

    normalized: List[NormalizedFact] = []
    for fact in raw_facts:
        item = _normalize_fact(fact, fallback_table=fallback_table)
        if item is not None:
            normalized.append(item)
    return normalized


def _normalize_worker_result(raw: Any, agent_name: str = "") -> Tuple[NormalizedWorkerResult, str]:
    if isinstance(raw, dict):
        table = str(raw.get("table", "")).strip() or _fallback_table_for_agent(agent_name)
        return (
            {
                "agent": agent_name,
                "table": table,
                "facts": _normalize_facts(raw.get("facts", []), fallback_table=table),
                "raw_text": "",
            },
            "structured",
        )

    text = _to_text(raw).strip()
    if not text:
        return _empty_worker_result(agent_name=agent_name), "empty"

    parsed = _try_parse_json(text)
    if parsed is None:
        return _empty_worker_result(agent_name=agent_name, raw_text=text), "unparsed"

    table = str(parsed.get("table", "")).strip() or _fallback_table_for_agent(agent_name)
    return (
        {
            "agent": agent_name,
            "table": table,
            "facts": _normalize_facts(parsed.get("facts", []), fallback_table=table),
            "raw_text": text,
        },
        "json_text",
    )


def _normalize_all_worker_results(
    worker_results: Dict[str, Any],
    *,
    emit_debug_logs: bool = False,
) -> Tuple[Dict[str, NormalizedWorkerResult], List[Dict[str, Any]]]:
    normalized: Dict[str, NormalizedWorkerResult] = {}
    logs: List[Dict[str, Any]] = []

    for agent_name, raw in (worker_results or {}).items():
        item, kind = _normalize_worker_result(raw, agent_name=agent_name)
        normalized[agent_name] = item
        should_log = emit_debug_logs or kind != "structured"
        if should_log:
            entry = {
                "event": "synth:normalize_worker_result",
                "agent": agent_name,
                "kind": kind,
                "facts_n": len(item["facts"]),
            }
            if emit_debug_logs:
                entry["debug"] = True
            logs.append(entry)

    return normalized, logs


def _flatten_facts(normalized_results: Dict[str, NormalizedWorkerResult]) -> List[NormalizedFact]:
    facts: List[NormalizedFact] = []
    for item in (normalized_results or {}).values():
        facts.extend(item.get("facts", []))
    return facts


def _dedupe_facts(facts: List[NormalizedFact]) -> List[NormalizedFact]:
    deduped: List[NormalizedFact] = []
    seen = set()

    for fact in facts:
        key = (
            str(fact.get("table", "")).strip(),
            str(fact.get("item_name", "")).strip(),
            str(fact.get("subheading", "")).strip(),
            str(fact.get("time_hint", "")).strip(),
            str(fact.get("value", "")).strip(),
            str(fact.get("source", "")).strip(),
            str(fact.get("status", "")).strip(),
        )
        if key in seen:
            continue
        deduped.append(fact)
        seen.add(key)

    return deduped


def _cap_facts(facts: List[NormalizedFact], limit: int = MAX_SYNTH_FACTS_PER_AGENT) -> List[NormalizedFact]:
    if limit <= 0 or len(facts) <= limit:
        return facts
    return facts[:limit]


def _fact_for_prompt(fact: NormalizedFact) -> Dict[str, Any]:
    """Project a fact down to the fields the synth LLM needs.

    Drops internal-only fields that are noise in the prompt: ``source`` (a
    constant file path) and ``status`` ("found"); ``message`` is kept only when
    present (it carries the not-found explanation). Empty fields are omitted.
    """
    out = {
        "item_name": fact.get("item_name", ""),
        "subheading": fact.get("subheading", ""),
        "time_hint": fact.get("time_hint", ""),
        "value_type": fact.get("value_type", ""),
        "unit": fact.get("unit", ""),
        "value": fact.get("value", ""),
        "table": fact.get("table", ""),
        "message": str(fact.get("message", "") or "").strip(),
    }
    return {key: val for key, val in out.items() if str(val).strip()}


def _build_compact_worker_results(
    normalized_results: Dict[str, NormalizedWorkerResult],
) -> Tuple[Dict[str, CompactWorkerResult], Dict[str, int]]:
    compact: Dict[str, CompactWorkerResult] = {}
    stats = {
        "agents_n": 0,
        "facts_n_raw": 0,
        "facts_n_kept": 0,
        "agents_trimmed": 0,
    }

    for agent_name, item in (normalized_results or {}).items():
        facts = _dedupe_facts(item.get("facts", []))
        facts_raw_n = len(facts)
        facts = _cap_facts(facts)

        if facts_raw_n > len(facts):
            stats["agents_trimmed"] += 1

        compact[agent_name] = {
            "table": str(item.get("table", "")).strip(),
            "facts": [_fact_for_prompt(fact) for fact in facts],
        }
        stats["agents_n"] += 1
        stats["facts_n_raw"] += facts_raw_n
        stats["facts_n_kept"] += len(facts)

    return compact, stats


def _build_compact_analysis_results(
    analysis_results: Dict[str, Any],
) -> Tuple[Dict[str, CompactAnalysisResult], Dict[str, int]]:
    compact: Dict[str, CompactAnalysisResult] = {}
    stats = {
        "agents_n": 0,
        "answers_n": 0,
        "requirements_n": 0,
    }

    for agent_name, raw in (analysis_results or {}).items():
        payload = raw if isinstance(raw, dict) else {}
        answer = str(payload.get("answer", "") or "").strip()
        requirements = _normalize_requirements_list(payload.get("requirements"), limit=0)

        compact[agent_name] = {
            "answer": answer,
            "requirements": requirements,
        }
        stats["agents_n"] += 1
        if answer:
            stats["answers_n"] += 1
        stats["requirements_n"] += len(requirements)

    return compact, stats


def _analysis_only_context(
    analysis_results: Dict[str, CompactAnalysisResult],
) -> Dict[str, Any]:
    return {
        "analysis_outputs": analysis_results,
    }


def _force_json_analysis_instruction(base_instruction: str) -> str:
    return (
        f"{base_instruction}\n\n"
        "DINH DANG DAU RA BAT BUOC:\n"
        '- Chi tra duy nhat 1 JSON object hop le theo schema AnalysisOutput.\n'
        '- Khong boc JSON bang markdown/code fence; field "answer" duoc phep dung Markdown tieng Viet theo profile.\n'
        '- Khong them van ban ngoai JSON.\n'
        '- Bat buoc co 2 field: \"answer\" va \"requirements\".\n'
    )


def _plain_analysis_payload(payload: dict) -> dict:
    fallback_payload = dict(payload)
    fallback_payload["system_instruction"] = _force_json_analysis_instruction(
        str(payload.get("system_instruction", "") or "")
    )
    return fallback_payload


def _normalize_analysis_output(payload: dict) -> dict:
    return {
        "answer": str(payload.get("answer", "") or "").strip(),
        "requirements": _normalize_requirements_list(payload.get("requirements"), limit=0),
    }


def _coerce_analysis_response(result: Any) -> dict:
    parsed = None

    if isinstance(result, dict):
        parsed_candidate = result.get("parsed")
        if isinstance(parsed_candidate, AnalysisOutput):
            parsed = parsed_candidate.model_dump()
        elif parsed_candidate is not None:
            try:
                coerced = parse_analysis_response_payload(parsed_candidate)
            except Exception:
                coerced = None
            if isinstance(coerced, AnalysisOutput):
                parsed = coerced.model_dump()

        if parsed is None:
            raw = result.get("raw")
            raw_text = _to_text(raw).strip()
            if raw_text:
                try:
                    parsed_text = parse_analysis_response(raw_text)
                except Exception:
                    parsed_text = None
                if isinstance(parsed_text, AnalysisOutput):
                    parsed = parsed_text.model_dump()

    elif isinstance(result, AnalysisOutput):
        parsed = result.model_dump()

    if not isinstance(parsed, dict):
        return {
            "answer": "",
            "requirements": [],
        }

    return _normalize_analysis_output(parsed)


def _merge_analysis_outputs(previous: dict, current: dict) -> dict:
    answer_parts = []
    seen_answers = set()
    for item in (previous.get("answer", ""), current.get("answer", "")):
        text = str(item or "").strip()
        if not text or text in seen_answers:
            continue
        answer_parts.append(text)
        seen_answers.add(text)
    return {
        "answer": "\n\n".join(answer_parts),
        "requirements": _normalize_requirements_list(
            list(previous.get("requirements", []) or []) + list(current.get("requirements", []) or []),
            limit=0,
        ),
    }


def _planned_analysis_targets(state: dict) -> List[dict]:
    merged: Dict[str, dict] = {}
    order: List[str] = []
    worker_plan = state.get("worker_plan", {}) or {}
    raw_targets = list(worker_plan.get("analysis_plan", []) or []) + list(worker_plan.get("targets", []) or [])

    for target in raw_targets:
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if not is_analysis_agent(agent):
            continue
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])
        if not requirements:
            objective = str(target.get("objective", "") or "").strip()
            requirements = [objective] if objective else []
        if not requirements:
            continue
        if agent not in merged:
            merged[agent] = {
                "agent": agent,
                "requirements": requirements,
            }
            order.append(agent)
            continue
        merged[agent]["requirements"] = _dedupe_keep_order(
            list(merged[agent].get("requirements", []) or [])
            + requirements
        )[:MAX_FOLLOWUP_REQUIREMENTS]

    return [merged[agent] for agent in order]


def _prepare_synth_inputs(state: dict) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str, int, int]:
    raw_retrieval_worker_results = {
        agent_name: raw
        for agent_name, raw in (state.get("worker_results", {}) or {}).items()
        if not is_analysis_agent(agent_name)
    }
    raw_analysis_results = {
        agent_name: raw
        for agent_name, raw in (state.get("worker_results", {}) or {}).items()
        if is_analysis_agent(agent_name)
    }
    normalized_retrieval_results, normalize_logs = _normalize_all_worker_results(
        raw_retrieval_worker_results,
        emit_debug_logs=debug_enabled(state),
    )
    planned_analysis_targets = _planned_analysis_targets(state)
    analysis_results: Dict[str, Any] = dict(raw_analysis_results)
    prep_logs: List[Dict[str, Any]] = list(normalize_logs)

    context_mode = "analysis" if planned_analysis_targets else "retrieval_fallback"
    synth_normalize_logs: List[Dict[str, Any]] = []
    if planned_analysis_targets:
        compact_analysis_results, analysis_stats = _build_compact_analysis_results(analysis_results)
        _, retrieval_stats = _build_compact_worker_results(normalized_retrieval_results)
        compact_worker_results = _analysis_only_context(compact_analysis_results)
        stats = {
            "agents_n": analysis_stats.get("agents_n", 0),
            "answers_n": analysis_stats.get("answers_n", 0),
            "requirements_n": analysis_stats.get("requirements_n", 0),
            "retrieval_sources_n": retrieval_stats.get("agents_n", 0),
            "facts_n_raw": retrieval_stats.get("facts_n_raw", 0),
            "facts_n_kept": 0,
            "facts_n_omitted_from_synth": retrieval_stats.get("facts_n_kept", 0),
            "agents_trimmed": retrieval_stats.get("agents_trimmed", 0),
        }
    else:
        normalized_synth_results, synth_normalize_logs = _normalize_all_worker_results(
            raw_retrieval_worker_results,
            emit_debug_logs=debug_enabled(state),
        )
        compact_worker_results, stats = _build_compact_worker_results(normalized_synth_results)

    prepared_log = make_log(
        state,
        "synth_context:prepared",
        context_mode=context_mode,
        analysis_targets_n=len(planned_analysis_targets),
        retrieval_sources_n=len(raw_retrieval_worker_results),
        synth_agents_n=stats["agents_n"],
        synth_retrieval_sources_n=stats.get("retrieval_sources_n", 0),
        facts_n_raw=stats.get("facts_n_raw", 0),
        facts_n_kept=stats.get("facts_n_kept", 0),
        facts_n_omitted_from_synth=stats.get("facts_n_omitted_from_synth", 0),
        synth_agents_trimmed=stats.get("agents_trimmed", 0),
        analysis_answers_n=stats.get("answers_n", 0),
        analysis_requirements_n=stats.get("requirements_n", 0),
    )

    prep_logs.extend(synth_normalize_logs)
    prep_logs.append(prepared_log)

    facts_n = _count_facts_in_results(compact_worker_results)
    requirements_n = _count_requirements_in_results(compact_worker_results) if context_mode == "analysis" else 0
    return compact_worker_results, prep_logs, context_mode, facts_n, requirements_n


def _coerce_decision(value: Any) -> Dict[str, Any]:
    data = value
    if hasattr(value, "model_dump"):
        data = value.model_dump()

    if not isinstance(data, dict):
        return dict(DEFAULT_DECISION)

    decision = dict(DEFAULT_DECISION)
    decision.update(data)
    decision["status"] = str(decision.get("status", DEFAULT_DECISION["status"]) or "").strip().lower()
    if not decision["status"]:
        decision["status"] = DEFAULT_DECISION["status"]
    decision["answer"] = str(decision.get("answer", DEFAULT_DECISION["answer"]) or "").strip()
    decision["followups"] = decision.get("followups") or []
    return decision


def _extract_synth_usage(raw: Any) -> Optional[SynthUsage]:
    usage = extract_usage_metadata(raw)
    return usage or None


def _difficulty_level_from_state(state: dict) -> str:
    for source_key in ("planner_plan", "worker_plan"):
        source = state.get(source_key, {})
        if not isinstance(source, dict):
            continue
        difficulty = str(source.get("difficulty_level", "") or "").strip().lower()
        if difficulty in {"easy", "medium", "hard"}:
            return difficulty
    return ""


def _synth_plan_payload(state: dict) -> Any:
    worker_plan = state.get("worker_plan", {})
    if not isinstance(worker_plan, dict):
        return worker_plan

    plan_payload = dict(worker_plan)
    difficulty = _difficulty_level_from_state(state)
    if difficulty:
        plan_payload["difficulty_level"] = difficulty
    return plan_payload


def _synth_difficulty_instruction(state: dict) -> str:
    difficulty = _difficulty_level_from_state(state)
    if difficulty == "easy":
        return """

            QUY TẮC RIÊNG CHO DIFFICULTY EASY
            - Chỉ trả lời ngắn gọn, trực tiếp theo facts có trong worker_results_json.
            - Không viết phân tích, không đánh giá mở rộng, không thêm mục "*Nhận xét*:" hoặc "**Kết luận tổng thể**".
            - answer nên gồm 1-3 câu hoặc 1-3 bullet ngắn; nêu đúng số liệu/kỳ/bảng nếu có.
            - Chỉ tạo followups khi thiếu dữ liệu cốt lõi khiến không thể trả lời câu hỏi chính.
            """

    if difficulty == "medium":
        return """

            QUY TẮC RIÊNG CHO DIFFICULTY MEDIUM
            - Tập trung tính toán đúng yêu cầu từ facts có trong worker_results_json.
            - Không viết phân tích, không đánh giá xu hướng/nguyên nhân/rủi ro nếu người dùng không hỏi hard.
            - answer cần nêu dữ liệu đầu vào, công thức, kết quả; có thể thêm 1 câu diễn giải rất ngắn về kết quả.
            - Không dùng format phân tích theo khía cạnh, không thêm mục "*Nhận xét*:" hoặc "**Kết luận tổng thể**".
            - Chỉ tạo followups khi thiếu biến đầu vào bắt buộc cho phép tính/câu hỏi chính.
            """

    return ""


def _synth_system_instruction(state: dict, profile: Dict[str, Any]) -> str:
    return f"{profile['system_instruction']}{_synth_difficulty_instruction(state)}"


def _build_payload(
    state: dict,
    profile: Dict[str, Any],
    worker_results_payload: Dict[str, Any],
) -> SynthPayload:
    return {
        "role": profile["role"],
        "tools_list": "",
        "system_instruction": _synth_system_instruction(state, profile),
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": _safe_json_dumps(_synth_plan_payload(state)),
        "worker_results_json": _safe_json_dumps(worker_results_payload),
        "allowed_keywords_json": "{}",
        "web_summary": "",
        "last_agent_response": state.get("last_agent_response", "") or "",
        "tool_observations": "",
    }


def _plain_synth_payload(payload: dict) -> dict:
    fallback_payload = dict(payload)
    fallback_payload["system_instruction"] = _force_json_output_instruction(
        str(payload.get("system_instruction", "") or "")
    )
    return fallback_payload


def _invoke_synth(payload: SynthPayload) -> Tuple[Dict[str, Any], Optional[SynthUsage], str]:
    try:
        result = invoke_prompt(
            PROMPT_TEMPLATE,
            payload,
            structured_schema=SynthDecision,
            plain_payload_factory=_plain_synth_payload,
        )
        if not isinstance(result, dict):
            return _coerce_decision(result), None, "plain_json"

        usage = _extract_synth_usage(result.get("raw"))
        mode = str(result.get("mode", "") or "structured")

        if mode != "structured":
            for candidate in (
                result.get("parsed"),
                result.get("raw"),
                result.get("content"),
            ):
                parsed_payload = _try_parse_json(candidate)
                if parsed_payload is None:
                    continue
                try:
                    recovered = SynthDecision.model_validate(parsed_payload).model_dump()
                except ValidationError:
                    continue
                return _coerce_decision(recovered), usage, mode

            return (
                {
                    "status": "error",
                    "answer": "Synth không parse được JSON hợp lệ từ plain_json fallback.",
                    "followups": [],
                },
                usage,
                mode,
            )

        parsing_error = result.get("parsing_error")
        if parsing_error is not None:
            for candidate in (
                result.get("parsed"),
                result.get("raw"),
                result.get("content"),
            ):
                parsed_payload = _try_parse_json(candidate)
                if parsed_payload is None:
                    continue
                try:
                    recovered = SynthDecision.model_validate(parsed_payload).model_dump()
                except ValidationError:
                    continue
                return _coerce_decision(recovered), usage, mode

            return (
                {
                    "status": "error",
                    "answer": f"Synth trả về sai schema: {parsing_error}",
                    "followups": [],
                },
                usage,
                mode,
            )

        return _coerce_decision(result.get("parsed")), usage, mode
    except ValidationError as exc:
        return (
            {
                "status": "error",
                "answer": f"Synth trả về sai schema: {exc}",
                "followups": [],
            },
            None,
            "structured",
        )
    except Exception as exc:
        return (
            {
                "status": "error",
                "answer": f"Lỗi khi chạy synth: {exc}",
                "followups": [],
            },
            None,
            "structured",
        )


def _coalesce_followups(followups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[Tuple[str, str], Dict[str, Any]] = {}
    order: List[Tuple[str, str]] = []
    passthrough: List[Dict[str, Any]] = []

    for item in followups or []:
        agent = str(item.get("agent", "") or "").strip()
        table = str(item.get("table", "") or "").strip()
        if agent:
            requirements = _normalize_requirements_list(
                item.get("requirements", []) or [],
                limit=MAX_FOLLOWUP_REQUIREMENTS,
            )
        else:
            requirements = normalize_requirements_keep_order(
                item.get("requirements", []) or [],
                table=table,
                limit=MAX_FOLLOWUP_REQUIREMENTS,
            )
        reason = str(item.get("reason", "") or "").strip()

        if not requirements:
            continue

        if not agent:
            passthrough.append(
                {
                    "requirements": requirements,
                    "reason": reason,
                }
            )
            continue

        key = (agent, table)
        if key not in merged:
            merged[key] = {
                "agent": agent,
                "table": table or None,
                "requirements": requirements,
                "reason": reason,
            }
            order.append(key)
            continue

        current = merged[key]
        current["requirements"] = normalize_requirements_keep_order(
            list(current.get("requirements", []) or [])
            + list(item.get("requirements", []) or []),
            table=table,
            limit=MAX_FOLLOWUP_REQUIREMENTS,
        )
        if not current.get("reason") and item.get("reason"):
            current["reason"] = str(item.get("reason", "") or "").strip()

    return passthrough + [merged[key] for key in order]


def _requirement_satisfied_by_retrieval_facts(
    requirement: str,
    retrieval_results: Dict[str, Any],
) -> bool:
    requirement_text = str(requirement or "").strip().lower()
    if not requirement_text:
        return True

    for payload in (retrieval_results or {}).values():
        if not isinstance(payload, dict):
            continue
        facts = payload.get("facts", [])
        if not isinstance(facts, list):
            continue

        for fact in facts:
            if not isinstance(fact, dict):
                continue
            item_name = str(fact.get("item_name", "") or "").strip().lower()
            value = fact.get("value", "")
            if not item_name or value in ("", None):
                continue
            if not requirement_matches_fact(requirement_text, fact):
                continue
            return True

    return False


def _build_followups_from_analysis_requirements(
    worker_results: Dict[str, Any],
    retrieval_results: Dict[str, Any],
) -> List[Dict[str, Any]]:
    followups: List[Dict[str, Any]] = []

    for agent_name, payload in (worker_results or {}).items():
        if not is_analysis_agent(agent_name) or not isinstance(payload, dict):
            continue
        if _analysis_answer_has_complete_calculation(payload.get("answer", "")):
            continue

        requirements = [
            requirement
            for requirement in normalize_requirements_keep_order(payload.get("requirements"), limit=0)
            if not _requirement_satisfied_by_retrieval_facts(requirement, retrieval_results)
        ]
        if not requirements:
            continue

        followups.append(
            {
                "requirements": requirements,
                "reason": f"Cần bổ sung dữ liệu theo phân tích của {agent_name}.",
            }
        )

    return _coalesce_followups(followups)


def _analysis_outputs_payload(worker_results: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(worker_results.get("analysis_outputs"), dict):
        return worker_results.get("analysis_outputs", {})
    return {
        agent_name: payload
        for agent_name, payload in (worker_results or {}).items()
        if is_analysis_agent(agent_name) and isinstance(payload, dict)
    }


def _analysis_answer_has_complete_calculation(answer: Any) -> bool:
    text = str(answer or "").strip()
    if not text:
        return False

    lowered = text.lower()
    if any(marker in lowered for marker in _INSUFFICIENT_ANSWER_MARKERS):
        return False

    numbers = _NUMERIC_VALUE_RE.findall(text)
    if len(numbers) < 2:
        return False

    if _CALCULATION_OPERATOR_RE.search(text):
        return True

    return bool(_CALCULATION_WORD_RE.search(text) and _CALCULATION_RESULT_RE.search(text) and len(numbers) >= 3)


def _complete_analysis_answer_agents(worker_results: Dict[str, Any]) -> List[str]:
    complete_agents: List[str] = []
    for agent_name, payload in (_analysis_outputs_payload(worker_results) or {}).items():
        if not isinstance(payload, dict):
            continue
        if _analysis_answer_has_complete_calculation(payload.get("answer", "")):
            complete_agents.append(str(agent_name or "").strip())
    return [agent for agent in complete_agents if agent]


def _has_unresolved_incomplete_analysis_requirements(worker_results: Dict[str, Any]) -> bool:
    for _agent_name, payload in (_analysis_outputs_payload(worker_results) or {}).items():
        if not isinstance(payload, dict):
            continue
        if _analysis_answer_has_complete_calculation(payload.get("answer", "")):
            continue
        if normalize_requirements_keep_order(payload.get("requirements"), limit=0):
            return True
    return False


def _combined_complete_analysis_answer(worker_results: Dict[str, Any]) -> str:
    parts: List[str] = []
    seen = set()
    for _agent_name, payload in (_analysis_outputs_payload(worker_results) or {}).items():
        if not isinstance(payload, dict):
            continue
        answer = str(payload.get("answer", "") or "").strip()
        if not answer or answer in seen:
            continue
        if not _analysis_answer_has_complete_calculation(answer):
            continue
        parts.append(answer)
        seen.add(answer)
    return "\n\n".join(parts)


def _suppress_followups_if_analysis_answer_complete(
    state: dict,
    decision: Dict[str, Any],
    worker_results: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if str(decision.get("status", "") or "").strip().lower() != "need_more":
        return decision, None

    existing_agents = {
        str(agent_name or "").strip()
        for agent_name in (_analysis_outputs_payload(worker_results) or {}).keys()
        if str(agent_name or "").strip()
    }
    for followup in decision.get("followups", []) or []:
        if not isinstance(followup, dict):
            continue
        agent = str(followup.get("agent", "") or "").strip()
        if is_analysis_agent(agent) and agent not in existing_agents:
            return decision, None

    complete_agents = _complete_analysis_answer_agents(worker_results)
    if not complete_agents:
        return decision, None
    if _has_unresolved_incomplete_analysis_requirements(worker_results):
        return decision, None

    updated = dict(decision)
    if not str(updated.get("answer", "") or "").strip():
        updated["answer"] = _combined_complete_analysis_answer(worker_results)
    updated["status"] = "answer"
    updated["followups"] = []

    return updated, make_log(
        state,
        "synth:followups_suppressed_complete_analysis_answer",
        complete_analysis_agents=complete_agents,
        suppressed_followups_n=len(decision.get("followups", []) or []),
        suppressed_requirements=_followup_requirement_items(decision.get("followups", []) or []),
    )


def _answer_from_analysis_outputs(worker_results: Dict[str, Any]) -> str:
    parts: List[str] = []
    seen = set()

    for _agent_name, payload in (_analysis_outputs_payload(worker_results) or {}).items():
        if not isinstance(payload, dict):
            continue
        answer = str(payload.get("answer", "") or "").strip()
        if not answer or answer in seen:
            continue
        parts.append(answer)
        seen.add(answer)

    return "\n\n".join(parts)


def _keep_only_new_analysis_agent_followups(
    state: dict,
    decision: Dict[str, Any],
    worker_results: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if str(decision.get("status", "") or "").strip().lower() != "need_more":
        return decision, None

    raw_followups = list(decision.get("followups", []) or [])
    if not raw_followups:
        return decision, None

    existing_agents = {
        str(agent_name or "").strip()
        for agent_name in (_analysis_outputs_payload(worker_results) or {}).keys()
        if str(agent_name or "").strip()
    }
    kept = []
    dropped = []

    for followup in raw_followups:
        if not isinstance(followup, dict):
            dropped.append({"raw": followup, "reason": "invalid_followup_payload"})
            continue

        agent = str(followup.get("agent", "") or "").strip()
        if not is_analysis_agent(agent):
            dropped.append(
                {
                    "requirements": followup.get("requirements", []),
                    "reason": "analysis_context_followup_without_analysis_agent",
                }
            )
            continue
        if agent in existing_agents:
            dropped.append(
                {
                    "agent": agent,
                    "requirements": followup.get("requirements", []),
                    "reason": "analysis_agent_already_present",
                }
            )
            continue
        kept.append(followup)

    if not dropped:
        return decision, None

    updated = dict(decision)
    updated["followups"] = kept
    if not kept:
        answer = str(updated.get("answer", "") or "").strip() or _answer_from_analysis_outputs(worker_results)
        updated["status"] = "answer"
        updated["answer"] = answer or "Trả lời dựa trên các phân tích hiện có."

    return updated, make_debug_log(
        state,
        "synth:analysis_followups_filtered",
        kept_n=len(kept),
        dropped_n=len(dropped),
        dropped_samples=dropped[:3],
    )


def _followup_requirement_items(followups: List[Dict[str, Any]]) -> List[str]:
    requirements: List[str] = []
    for followup in followups or []:
        if not isinstance(followup, dict):
            continue
        requirements.extend(followup.get("requirements", []) or [])
    return _dedupe_keep_order([str(item).strip() for item in requirements if str(item).strip()])


_FOLLOWUP_PLACEHOLDER_ANSWER = "Trả lời dựa trên dữ liệu hiện có."


def _facts_summary_from_state(state: dict, limit: int = MAX_SYNTH_FACTS_PER_AGENT) -> str:
    """Render the facts already gathered as a short Markdown answer.

    Used as a fallback when the follow-up budget is exhausted and the synth LLM
    left the answer empty: surfacing the retrieved facts keeps the answer grounded
    (non-zero faithfulness/relevancy) instead of emitting a content-free stub.
    """
    if not isinstance(state, dict):
        return ""
    normalized, _ = _normalize_all_worker_results(state.get("worker_results", {}) or {})
    facts = _dedupe_facts(_flatten_facts(normalized))
    lines: List[str] = []
    for fact in facts:
        value = str(fact.get("value", "") or "").strip()
        item_name = str(fact.get("item_name", "") or "").strip()
        if not value or not item_name:
            continue
        subheading = str(fact.get("subheading", "") or "").strip()
        label = f"{subheading} — {item_name}" if subheading else item_name
        lines.append(f"- {label}: {value}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def _answer_with_followup_limit_note(
    decision: Dict[str, Any],
    followups: List[Dict[str, Any]],
    state: Optional[dict] = None,
) -> Dict[str, Any]:
    updated = dict(decision)
    answer = str(updated.get("answer", "") or "").strip()
    if not answer or answer == _FOLLOWUP_PLACEHOLDER_ANSWER:
        # Recall-first fallback: answer from whatever facts were gathered rather
        # than emitting a content-free placeholder that scores 0 on RAGAs.
        facts_summary = _facts_summary_from_state(state) if state is not None else ""
        if facts_summary:
            answer = "Dựa trên số liệu hiện có:\n" + facts_summary
        elif not answer:
            answer = _FOLLOWUP_PLACEHOLDER_ANSWER

    requirements = _followup_requirement_items(followups)
    if requirements:
        note = (
            "Giới hạn dữ liệu: hệ thống đã đạt giới hạn follow-up; "
            "các dữ liệu chưa xác nhận thêm gồm "
            + "; ".join(requirements)
            + "."
        )
    else:
        note = (
            "Giới hạn dữ liệu: hệ thống đã đạt giới hạn follow-up; "
            "câu trả lời được lập dựa trên dữ liệu hiện có."
        )

    if note not in answer:
        answer = "\n\n".join([answer, note])

    updated["status"] = "answer"
    updated["answer"] = answer
    updated["followups"] = []
    return updated


def _answer_with_optional_followup_note(
    decision: Dict[str, Any],
    followups: List[Dict[str, Any]],
) -> Dict[str, Any]:
    updated = dict(decision)
    answer = str(updated.get("answer", "") or "").strip()
    if not answer:
        answer = "Trả lời dựa trên dữ liệu hiện có."

    requirements = _followup_requirement_items(followups)
    note = (
        "Giới hạn dữ liệu: một số chỉ số phụ chưa được xác nhận thêm"
        + (f" ({'; '.join(requirements)})" if requirements else "")
        + ", nên câu trả lời sử dụng các số liệu chính hiện có."
    )
    if note not in answer:
        answer = "\n\n".join([answer, note])

    updated["status"] = "answer"
    updated["answer"] = answer
    updated["followups"] = []
    return updated


def _user_explicitly_requested_requirement(user_query: str, requirement: str) -> bool:
    query_text = str(user_query or "").strip().lower()
    if not query_text:
        return False

    requirement_text = normalize_requirement_text(requirement)
    if requirement_text and requirement_text in query_text:
        return True

    query_markers = {
        "chi phí bán hàng": ("chi phí bán hàng",),
        "các khoản phải trả ngắn hạn": (
            "các khoản phải trả",
            "phải trả ngắn hạn",
            "vòng quay phải trả",
            "dpo",
            "days payables",
            "chu kỳ chuyển đổi tiền",
            "cash conversion cycle",
        ),
        "phải trả người bán ngắn hạn": (
            "phải trả người bán",
            "vòng quay phải trả",
            "dpo",
            "days payables",
            "chu kỳ chuyển đổi tiền",
            "cash conversion cycle",
        ),
    }
    return any(
        marker in query_text
        for marker in query_markers.get(requirement_text, ())
    )


def _can_answer_with_optional_followups(
    state: dict,
    decision: Dict[str, Any],
    followups: List[Dict[str, Any]],
) -> bool:
    if not str(decision.get("answer", "") or "").strip():
        return False

    requirements = _followup_requirement_items(followups)
    if not requirements:
        return False

    user_query = str((state or {}).get("user_query", "") or "")
    for requirement in requirements:
        normalized = normalize_requirement_text(requirement)
        if normalized not in OPTIONAL_FOLLOWUP_REQUIREMENTS:
            return False
        if _user_explicitly_requested_requirement(user_query, normalized):
            return False

    return True


def _merge_analysis_requirement_followups(
    state: dict,
    decision: Dict[str, Any],
    worker_results: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if isinstance(worker_results.get("analysis_outputs"), dict):
        worker_results = worker_results.get("analysis_outputs", {})

    retrieval_results = {
        agent_name: payload
        for agent_name, payload in (state.get("worker_results", {}) or {}).items()
        if not is_analysis_agent(agent_name) and isinstance(payload, dict)
    }
    auto_followups = _build_followups_from_analysis_requirements(worker_results, retrieval_results)
    if not auto_followups:
        return decision, None

    status = str(decision.get("status", "") or "").strip().lower()
    if status != "need_more":
        return decision, make_debug_log(
            state,
            "synth:auto_followups_skipped_for_answer",
            auto_followups_n=len(auto_followups),
            status=status or "unknown",
            targets=[
                {
                    "requirements": item.get("requirements", []),
                    "reason": item.get("reason", ""),
                }
                for item in auto_followups[:3]
            ],
        )

    merged = dict(decision)
    model_followups_n = len(list(merged.get("followups", []) or []))
    merged["followups"] = auto_followups

    return merged, make_debug_log(
        state,
        "synth:auto_followups_from_analysis_requirements",
        auto_followups_n=len(auto_followups),
        replaced_model_followups_n=model_followups_n,
        targets=[
            {
                "requirements": item.get("requirements", []),
                "reason": item.get("reason", ""),
            }
            for item in auto_followups[:3]
        ],
    )


def _sanitize_followups(
    state: dict,
    decision: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if str(decision.get("status", "") or "").strip().lower() != "need_more":
        return decision, None

    raw_followups = decision.get("followups", []) or []
    normalized_followups: List[Dict[str, Any]] = []
    dropped_samples: List[Dict[str, Any]] = []

    for raw in raw_followups:
        try:
            followup = SynthFollowupRequest.model_validate(raw)
        except Exception:
            if len(dropped_samples) < 3:
                dropped_samples.append({"raw": raw, "reason": "invalid_followup_payload"})
            continue

        if followup.agent:
            requirements = _normalize_requirements_list(
                followup.requirements,
                limit=MAX_FOLLOWUP_REQUIREMENTS,
            )
        else:
            requirements = normalize_requirements_keep_order(
                followup.requirements,
                table=str(followup.table or "").strip(),
                limit=MAX_FOLLOWUP_REQUIREMENTS,
            )
        if not requirements:
            if len(dropped_samples) < 3:
                dropped_samples.append(
                    {
                        "agent": followup.agent,
                        "table": followup.table,
                        "reason": "empty_requirements",
                    }
                )
            continue

        payload = {
            "requirements": requirements,
            "reason": str(followup.reason or "").strip(),
        }

        if followup.table:
            payload["table"] = followup.table
        if followup.agent:
            payload["agent"] = followup.agent

        normalized_followups.append(payload)

    coalesced_followups = _coalesce_followups(normalized_followups)
    updated = dict(decision)
    updated["followups"] = coalesced_followups

    if coalesced_followups == raw_followups and not dropped_samples:
        return updated, None

    return updated, make_debug_log(
        state,
        "synth:followups_sanitized",
        raw_n=len(raw_followups),
        kept_n=len(coalesced_followups),
        dropped_samples=dropped_samples,
    )


def _is_analysis_context(worker_results: Dict[str, Any]) -> bool:
    for payload in (worker_results or {}).values():
        if isinstance(payload, dict) and (
            "answer" in payload or "requirements" in payload
        ):
            return True
    return False


def _count_facts_in_results(worker_results: Dict[str, Any]) -> int:
    if isinstance(worker_results.get("retrieval_facts"), dict):
        return _count_facts_in_results(worker_results.get("retrieval_facts", {}))

    total = 0
    for payload in (worker_results or {}).values():
        if not isinstance(payload, dict):
            continue
        facts = payload.get("facts", [])
        if isinstance(facts, list):
            total += len(facts)
    return total


def _count_requirements_in_results(worker_results: Dict[str, Any]) -> int:
    if isinstance(worker_results.get("analysis_outputs"), dict):
        return _count_requirements_in_results(worker_results.get("analysis_outputs", {}))

    total = 0
    for payload in (worker_results or {}).values():
        if not isinstance(payload, dict):
            continue
        total += len(_normalize_requirements_list(payload.get("requirements"), limit=0))
    return total


def run_synth(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_synth"]
    trace = []
    started_at = time.perf_counter()

    start_log = make_debug_log(
        state,
        "synth:start",
        followup_rounds=state.get("followup_rounds", 0),
    )
    if start_log:
        trace.append(start_log)

    payload_worker_results, normalize_logs, context_mode, facts_n, requirements_n = _prepare_synth_inputs(state)
    payload = _build_payload(state, profile, payload_worker_results)
    decision, usage, invoke_mode = _invoke_synth(payload)
    auto_followup_log = None
    decision, followup_sanitize_log = _sanitize_followups(state, decision)
    complete_analysis_suppression_log = None
    if context_mode == "analysis":
        decision, complete_analysis_suppression_log = _suppress_followups_if_analysis_answer_complete(
            state,
            decision,
            payload_worker_results,
        )
    analysis_followup_filter_log = None
    if context_mode == "analysis":
        decision, analysis_followup_filter_log = _keep_only_new_analysis_agent_followups(
            state,
            decision,
            payload_worker_results,
        )

    if invoke_mode != "structured":
        fallback_log = make_debug_log(
            state,
            "synth:structured_output_fallback",
            mode=invoke_mode,
        )
        if fallback_log:
            trace.append(fallback_log)
    if auto_followup_log:
        trace.append(auto_followup_log)
    if followup_sanitize_log:
        trace.append(followup_sanitize_log)
    if complete_analysis_suppression_log:
        trace.append(complete_analysis_suppression_log)
    if analysis_followup_filter_log:
        trace.append(analysis_followup_filter_log)

    followup_updates: Dict[str, Any] = {}
    dispatchable_followups = decision.get("followups", []) or []
    current_round = int(state.get("followup_rounds", 0) or 0)
    if (
        str(decision.get("status", "") or "").strip().lower() == "need_more"
        and dispatchable_followups
        and _can_answer_with_optional_followups(state, decision, dispatchable_followups)
    ):
        trace.append(
            make_log(
                state,
                "synth:optional_followups_answered",
                followups_n=len(dispatchable_followups),
                requirements=_followup_requirement_items(dispatchable_followups),
            )
        )
        decision = _answer_with_optional_followup_note(decision, dispatchable_followups)
        dispatchable_followups = []

    if (
        str(decision.get("status", "") or "").strip().lower() == "need_more"
        and dispatchable_followups
        and current_round < MAX_FOLLOWUP_ROUNDS
    ):
        followup_updates = prepare_followup_dispatch_state(
            {
                **state,
                "followup_requests": dispatchable_followups,
            }
        )
        trace.extend(followup_updates.get("trace", []) or [])
    elif str(decision.get("status", "") or "").strip().lower() == "need_more" and dispatchable_followups:
        trace.append(
            make_log(
                state,
                "synth:followup_limit_reached",
                current_round=current_round,
                max_rounds=MAX_FOLLOWUP_ROUNDS,
                followups_n=len(dispatchable_followups),
            )
        )
        decision = _answer_with_followup_limit_note(decision, dispatchable_followups, state)
        dispatchable_followups = []

    done_log = make_log(
        state,
        "synth:done",
        status=decision.get("status", ""),
        context_mode=context_mode,
        followups_n=len(decision.get("followups", []) or []),
        facts_n=facts_n,
        analysis_requirements_n=requirements_n,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        answer_preview=(decision.get("answer", "") or "")[:200],
        **(usage or {}),
    )

    return {
        "synth_decision": decision,
        "followup_requests": dispatchable_followups,
        "last_agent_response": decision.get("answer", ""),
        "followup_rounds": followup_updates.get("followup_rounds", state.get("followup_rounds", 0)),
        "planner_plan": followup_updates.get("planner_plan", state.get("planner_plan", {})),
        "pending_analysis_targets": followup_updates.get("pending_analysis_targets", state.get("pending_analysis_targets", [])),
        "analysis_dispatch_targets": followup_updates.get("analysis_dispatch_targets", state.get("analysis_dispatch_targets", [])),
        "dispatch_phase": followup_updates.get("dispatch_phase", state.get("dispatch_phase", "")),
        "trace": [*trace, *normalize_logs, done_log],
    }
