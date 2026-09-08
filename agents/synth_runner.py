"""Synthesize worker outputs into the final user-facing answer."""
# Code note: Agent modules coordinate LLM prompts, tool calls, and structured outputs; comments here call out control-flow constraints.

from __future__ import annotations

import json
import re
import time
import unicodedata
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from pydantic import ValidationError

from agents.agent_registry import ANALYSIS_ASPECT_LABELS, is_analysis_agent
from config.domain_catalog import CURRENT_PERIOD_MARKERS, PREVIOUS_PERIOD_MARKERS
from config.runtime_policy import DEFAULT_POLICY, active_policy
from agents.profiles import AGENT_PROFILES
from agents.prompts import PROMPT_TEMPLATE
from graph.dispatch_nodes import prepare_followup_dispatch_state
from graph.logger import debug_enabled, make_debug_log, make_log
from llm.invoke import extract_usage_metadata, invoke_prompt, merge_usage_metadata
from schemas.agent_outputs import (
    AnalysisOutput,
    SynthFollowupRequest,
    SynthDecision,
    parse_analysis_response,
    parse_analysis_response_payload,
)
from schemas.requirements import (
    REQUIREMENT_EXHAUSTIVE_ABSENT,
    REQUIREMENT_MATCHED,
    normalize_fact_status,
    normalize_requirement_text,
    normalize_requirements_keep_order,
    requirement_evidence_state,
)
from schemas.numbers import parse_financial_decimal
from schemas.evidence_origin import fact_provenance, is_report_fact
from schemas.financial_validation import (
    canonical_profitability_metrics,
    financial_answer_violations,
)
from ingestion.period_normalize import query_section_total_key
from evaluation.narrative_semantics import (
    narrative_concept_atoms as _semantic_narrative_concept_atoms,
    narrative_evidence_surface,
)
from tools.query_routing import (
    fact_reporting_basis,
    fact_metric_slot,
    fact_period_labels,
    fact_matches_required_slots,
    fact_slot_score,
    metric_slot_compatibility,
    parse_query_slots,
    query_reporting_basis,
    reporting_basis_compatible,
)
from common import dedupe_keep_order as _dedupe_keep_order

DEFAULT_DECISION = {
    "status": "error",
    "answer": "Không thể tạo SynthDecision hợp lệ.",
    "followups": [],
}

MAX_SYNTH_FACTS_PER_AGENT = DEFAULT_POLICY.synth.max_facts_per_agent
MAX_FOLLOWUP_REQUIREMENTS = DEFAULT_POLICY.execution.max_followup_requirements
# Two follow-up rounds: the missing data for stub answers exists in the source
# (front-matter, note schedules, policies) and just needs one more routed fetch.
# The extra round only runs when round 1 returns need_more with pending followups.
MAX_FOLLOWUP_ROUNDS = DEFAULT_POLICY.execution.max_followup_rounds
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
_HARD_ANALYSIS_ASPECT_LABELS = dict(ANALYSIS_ASPECT_LABELS)
_INTERNAL_AGENT_LABEL_RE = re.compile(r"\bagent_[a-z0-9_]+\b", re.IGNORECASE)
_LEADING_EVIDENCE_BOILERPLATE_RE = re.compile(
    r"^(?:dua\s+(?:tren|vao)\s+(?:cac\s+)?so\s+lieu\s+hien\s+co|"
    r"theo\s+(?:cac\s+)?so\s+lieu\s+hien\s+co)\b",
    re.IGNORECASE,
)
_SUMMARY_NUMBER_RE = re.compile(
    r"(?<![a-z0-9_])[+-]?\d+(?:[.,]\d+)*(?![a-z0-9_])",
    re.IGNORECASE,
)
_SUMMARY_FORMULA_RE = re.compile(
    r"=|\d(?:[\d.,\s]*)(?:/|÷|×|\*)\s*[+-]?\d",
    re.IGNORECASE,
)
_CURRENT_PERIOD_MARKERS = CURRENT_PERIOD_MARKERS
_PREVIOUS_PERIOD_MARKERS = PREVIOUS_PERIOD_MARKERS
_PERIOD_WORD_RE = re.compile(
    r"\b(nam nay|nam hien tai|nam truoc|ky nay|ky truoc|hien tai|truoc do|"
    r"cuoi ky|cuoi nam|dau ky|dau nam|20\d{2})\b"
)


class NormalizedFact(TypedDict):
    fact_id: str
    company: str
    fiscal_year: str
    note_ref: str
    section_path: str
    block_id: str
    source_page: str
    item_name: str
    row_label: str
    column_label: str
    subheading: str
    time_hint: str
    period: str
    period_label: str
    period_role: str
    reporting_basis: str
    source_table: str
    source_item: str
    evidence_role: str
    linked_parent_fact_id: str
    linked_parent_item: str
    linked_parent_value: str
    linked_parent_period_label: str
    linked_parent_aggregation_level: str
    value_type: str
    aggregation_level: str
    unit: str
    value_kind: str
    parsed_value: str
    metric_label: str
    entity_label: str
    scope_label: str
    counterparty: str
    transaction_type: str
    movement_type: str
    geography: str
    policy_topic: str
    section_key: str
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


def _canonical_profitability_metric_ledger(
    worker_results_payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Expose exact calculations to Synth without authoring its prose.

    Financial arithmetic should be deterministic, but the final narrative
    should still synthesize the specialist analyses. Keep only the operands,
    formulas, bases and comparison directions that the model needs; source
    paths and other verbose provenance remain available in ``retrieval_facts``.
    """

    calculation = canonical_profitability_metrics(worker_results_payload)
    if calculation.get("status") != "complete":
        return {}

    compact_inputs: Dict[str, Dict[str, Any]] = {}
    for key, raw_item in (calculation.get("inputs", {}) or {}).items():
        if not isinstance(raw_item, dict):
            continue
        item = {
            field: raw_item.get(field)
            for field in (
                "fact_id",
                "label",
                "value",
                "unit",
                "fiscal_year",
                "period",
                "period_role",
                "reporting_basis",
            )
            if raw_item.get(field) not in (None, "")
        }
        if item:
            compact_inputs[str(key)] = item

    return {
        "status": "complete",
        "scope": calculation.get("scope", ""),
        "period": calculation.get("period", ""),
        "amount_unit": calculation.get("amount_unit", ""),
        "inputs": compact_inputs,
        "derived_denominators": calculation.get("derived_denominators", {}) or {},
        "metrics": calculation.get("metrics", {}) or {},
        "comparatives": calculation.get("comparatives", {}) or {},
    }

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
    fact_id = str(raw_fact.get("fact_id", "") or "").strip()
    company = str(raw_fact.get("company", "") or "").strip()
    fiscal_year = str(raw_fact.get("fiscal_year", "") or "").strip()
    note_ref = str(raw_fact.get("note_ref", "") or "").strip()
    section_path = str(raw_fact.get("section_path", "") or "").strip()
    block_id = str(raw_fact.get("block_id", "") or "").strip()
    source_page = str(raw_fact.get("source_page", "") or "").strip()
    row_label = str(raw_fact.get("row_label", "") or "").strip()
    column_label = str(raw_fact.get("column_label", "") or "").strip()
    period = str(raw_fact.get("period", "") or "").strip()
    period_label = str(raw_fact.get("period_label", "") or "").strip()
    period_role = str(raw_fact.get("period_role", "") or "").strip()
    reporting_basis = str(raw_fact.get("reporting_basis", "") or "").strip()
    source_table = str(raw_fact.get("source_table", "") or "").strip()
    source_item = str(raw_fact.get("source_item", "") or "").strip()
    evidence_role = str(raw_fact.get("evidence_role", "") or "").strip()
    linked_parent_fact_id = str(
        raw_fact.get("linked_parent_fact_id", "") or ""
    ).strip()
    linked_parent_item = str(
        raw_fact.get("linked_parent_item", "") or ""
    ).strip()
    linked_parent_value = str(
        raw_fact.get("linked_parent_value", "") or ""
    ).strip()
    linked_parent_period_label = str(
        raw_fact.get("linked_parent_period_label", "") or ""
    ).strip()
    linked_parent_aggregation_level = str(
        raw_fact.get("linked_parent_aggregation_level", "") or ""
    ).strip()
    value_type = str(raw_fact.get("value_type", "") or "").strip()
    aggregation_level = str(raw_fact.get("aggregation_level", "") or "").strip()
    unit = str(raw_fact.get("unit", "") or "").strip()
    value_kind = str(raw_fact.get("value_kind", "") or "").strip()
    parsed_value = str(raw_fact.get("parsed_value", "") or "").strip()
    metric_label = str(raw_fact.get("metric_label", "") or "").strip()
    entity_label = str(raw_fact.get("entity_label", "") or "").strip()
    scope_label = str(raw_fact.get("scope_label", "") or "").strip()
    counterparty = str(raw_fact.get("counterparty", "") or "").strip()
    transaction_type = str(raw_fact.get("transaction_type", "") or "").strip()
    movement_type = str(raw_fact.get("movement_type", "") or "").strip()
    geography = str(raw_fact.get("geography", "") or "").strip()
    policy_topic = str(raw_fact.get("policy_topic", "") or "").strip()
    section_key = str(raw_fact.get("section_key", "") or "").strip()

    if not item_name and value in ("", None):
        return None

    return {
        **fact_provenance(raw_fact),
        "fact_id": fact_id,
        "company": company,
        "fiscal_year": fiscal_year,
        "note_ref": note_ref,
        "section_path": section_path,
        "block_id": block_id,
        "source_page": source_page,
        "item_name": item_name,
        "row_label": row_label,
        "column_label": column_label,
        # Parent line / matrix section (e.g. "Tài sản cố định hữu hình — Nguyên
        # giá"); lets the LLM disambiguate flattened labels like "Số cuối kỳ | Cộng".
        "subheading": subheading,
        "time_hint": time_hint,
        "period": period,
        "period_label": period_label,
        "period_role": period_role,
        "reporting_basis": reporting_basis,
        "source_table": source_table,
        "source_item": source_item,
        "evidence_role": evidence_role,
        "linked_parent_fact_id": linked_parent_fact_id,
        "linked_parent_item": linked_parent_item,
        "linked_parent_value": linked_parent_value,
        "linked_parent_period_label": linked_parent_period_label,
        "linked_parent_aggregation_level": linked_parent_aggregation_level,
        # Slot disambiguators so the LLM picks the exact period / value-type asked
        # (nguyên giá vs giá trị còn lại vs hao mòn) and states the right unit.
        "value_type": value_type,
        "aggregation_level": aggregation_level,
        "unit": unit,
        "value_kind": value_kind,
        "parsed_value": parsed_value,
        "metric_label": metric_label,
        "entity_label": entity_label,
        "scope_label": scope_label,
        "counterparty": counterparty,
        "transaction_type": transaction_type,
        "movement_type": movement_type,
        "geography": geography,
        "policy_topic": policy_topic,
        "section_key": section_key,
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
            str(fact.get("fact_id", "")).strip(),
            str(fact.get("company", "")).strip(),
            str(fact.get("fiscal_year", "")).strip(),
            str(fact.get("table", "")).strip(),
            str(fact.get("item_name", "")).strip(),
            str(fact.get("row_label", "")).strip(),
            str(fact.get("column_label", "")).strip(),
            str(fact.get("subheading", "")).strip(),
            str(fact.get("note_ref", "")).strip(),
            str(fact.get("section_path", "")).strip(),
            str(fact.get("block_id", "")).strip(),
            str(fact.get("time_hint", "")).strip(),
            str(fact.get("period", "")).strip(),
            str(fact.get("period_role", "")).strip(),
            str(fact.get("value_type", "")).strip(),
            str(fact.get("aggregation_level", "")).strip(),
            str(fact.get("counterparty", "")).strip(),
            str(fact.get("transaction_type", "")).strip(),
            str(fact.get("movement_type", "")).strip(),
            str(fact.get("geography", "")).strip(),
            str(fact.get("policy_topic", "")).strip(),
            str(fact.get("section_key", "")).strip(),
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

    ``fact_id`` and ``source`` stay attached so deterministic derived values can
    cite their exact operands. Empty fields are omitted.
    """
    out = {
        **fact_provenance(fact),
        "fact_id": fact.get("fact_id", ""),
        "company": fact.get("company", ""),
        "fiscal_year": fact.get("fiscal_year", ""),
        "note_ref": fact.get("note_ref", ""),
        "section_path": fact.get("section_path", ""),
        "block_id": fact.get("block_id", ""),
        "source_page": fact.get("source_page", ""),
        "item_name": fact.get("item_name", ""),
        "row_label": fact.get("row_label", ""),
        "column_label": fact.get("column_label", ""),
        "subheading": fact.get("subheading", ""),
        "time_hint": fact.get("time_hint", ""),
        "period": fact.get("period", ""),
        "period_label": fact.get("period_label", ""),
        "period_role": fact.get("period_role", ""),
        "reporting_basis": fact.get("reporting_basis", ""),
        "source_table": fact.get("source_table", ""),
        "source_item": fact.get("source_item", ""),
        "evidence_role": fact.get("evidence_role", ""),
        "linked_parent_fact_id": fact.get("linked_parent_fact_id", ""),
        "linked_parent_item": fact.get("linked_parent_item", ""),
        "linked_parent_value": fact.get("linked_parent_value", ""),
        "linked_parent_period_label": fact.get(
            "linked_parent_period_label",
            "",
        ),
        "linked_parent_aggregation_level": fact.get(
            "linked_parent_aggregation_level",
            "",
        ),
        "value_type": fact.get("value_type", ""),
        "aggregation_level": fact.get("aggregation_level", ""),
        "unit": fact.get("unit", ""),
        "value_kind": fact.get("value_kind", ""),
        "parsed_value": fact.get("parsed_value", ""),
        "metric_label": fact.get("metric_label", ""),
        "entity_label": fact.get("entity_label", ""),
        "scope_label": fact.get("scope_label", ""),
        "counterparty": fact.get("counterparty", ""),
        "transaction_type": fact.get("transaction_type", ""),
        "movement_type": fact.get("movement_type", ""),
        "geography": fact.get("geography", ""),
        "policy_topic": fact.get("policy_topic", ""),
        "section_key": fact.get("section_key", ""),
        "value": fact.get("value", ""),
        "table": fact.get("table", ""),
        "source": fact.get("source", ""),
        "message": str(fact.get("message", "") or "").strip(),
        "status": fact.get("status", "found"),
    }
    return {key: val for key, val in out.items() if str(val).strip()}


def _retrieval_results_payload(worker_results: Dict[str, Any]) -> Dict[str, Any]:
    """Return the structured retrieval branch from either synth context mode."""

    nested = (worker_results or {}).get("retrieval_facts")
    if isinstance(nested, dict):
        return nested
    return {
        result_key: payload
        for result_key, payload in (worker_results or {}).items()
        if isinstance(payload, dict) and isinstance(payload.get("facts"), list)
    }


def _iter_retrieval_facts(worker_results: Dict[str, Any]):
    for key, payload in _retrieval_results_payload(worker_results).items():
        if not isinstance(payload, dict):
            continue
        for fact in payload.get("facts", []) or []:
            if isinstance(fact, dict):
                # Retain the WEB container boundary even for legacy artifacts
                # whose individual fact metadata has already been stripped.
                yield {**fact, "source_kind": "web"} if str(key).upper() == "WEB" else fact


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


def _analysis_context(
    analysis_results: Dict[str, CompactAnalysisResult],
    retrieval_results: Dict[str, CompactWorkerResult],
) -> Dict[str, Any]:
    """Keep analysis prose and its source-of-truth fact ledger together.

    Analysis agents are useful interpreters, but their prose must not replace the
    structured facts used to verify units, signs, periods, and stale ``missing``
    claims.  Both representations are compacted before reaching this function.
    """

    return {
        "analysis_outputs": analysis_results,
        "retrieval_facts": retrieval_results,
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
        compact_retrieval_results, retrieval_stats = _build_compact_worker_results(
            normalized_retrieval_results
        )
        compact_worker_results = _analysis_context(
            compact_analysis_results,
            compact_retrieval_results,
        )
        facts_n_kept = _count_facts_in_results(compact_worker_results)
        if retrieval_stats.get("facts_n_raw", 0) > 0 and facts_n_kept == 0:
            raise RuntimeError(
                "synth fact contract violated: retrieval facts exist but the "
                "analysis payload contains zero facts"
            )
        stats = {
            "agents_n": analysis_stats.get("agents_n", 0),
            "answers_n": analysis_stats.get("answers_n", 0),
            "requirements_n": analysis_stats.get("requirements_n", 0),
            "retrieval_sources_n": retrieval_stats.get("agents_n", 0),
            "facts_n_raw": retrieval_stats.get("facts_n_raw", 0),
            "facts_n_kept": facts_n_kept,
            "facts_n_omitted_from_synth": max(
                retrieval_stats.get("facts_n_raw", 0) - facts_n_kept,
                0,
            ),
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


def _response_mode_from_state(state: dict) -> str:
    for source_key in ("planner_plan", "worker_plan"):
        source = state.get(source_key, {})
        if not isinstance(source, dict):
            continue
        response_mode = str(source.get("response_mode", "") or "").strip().lower()
        if response_mode in {"extractive", "grounded_interpretation"}:
            return response_mode
    return "extractive"


def _synth_plan_payload(state: dict) -> Any:
    worker_plan = state.get("worker_plan", {})
    if not isinstance(worker_plan, dict):
        return worker_plan

    plan_payload = dict(worker_plan)
    difficulty = _difficulty_level_from_state(state)
    if difficulty:
        plan_payload["difficulty_level"] = difficulty
    plan_payload["response_mode"] = _response_mode_from_state(state)
    planner_plan = state.get("planner_plan", {})
    if isinstance(planner_plan, dict):
        premise_requirements = [
            str(item).strip()
            for item in (planner_plan.get("premise_requirements", []) or [])
            if str(item).strip()
        ]
        if premise_requirements:
            plan_payload["premise_requirements"] = premise_requirements
    return plan_payload


def _synth_difficulty_instruction(state: dict) -> str:
    if _response_mode_from_state(state) == "grounded_interpretation":
        return """

            CONTRACT RIÊNG CHO GROUNDED INTERPRETATION
            - Đây không phải phân tích theo bốn trục tài chính và cũng không phải
              trích xuất nguyên văn một kết luận có sẵn.
            - Dùng các sự kiện/premise trong worker_results_json làm căn cứ. Khi
              premise liên quan đã có, không được từ chối toàn bộ chỉ vì báo cáo
              không viết sẵn phần "ý nghĩa", "tác động" hoặc "hàm ý".
            - Chỉ trả lời ngắn theo hai phần: `Dữ liệu trích xuất` và
              `Suy luận đánh giá`; không tạo thêm phần thứ ba.
            - Phần suy luận phải bắt đầu bằng câu như "Từ các dữ kiện này có
              thể suy ra..." để thể hiện rõ nó dựa trên các fact vừa nêu; không
              trình bày suy luận như fact được báo cáo.
            - Chỉ khi một giới hạn thật sự làm thay đổi cách hiểu kết luận, nêu
              caveat ngắn ngay trong phần suy luận; không thêm cảnh báo khuôn mẫu.
            - Các claim về yêu cầu pháp lý cụ thể, hiệu quả thực tế, incentive,
              cơ cấu sở hữu hoặc huy động vốn chỉ được khẳng định khi có nguồn
              tương ứng. Nếu không, chỉ nêu như khả năng chung có điều kiện hoặc
              ghi rõ báo cáo chưa chứng minh.
            - Không dùng format bốn khía cạnh tài chính và không thêm mục
              `Kết luận tổng thể`.
            """

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
            - Nếu payload có typed_decimal_calculation, dùng nó làm context kiểm tra toán hạng, công thức và kết quả Decimal; không tự thay biến đầu vào bằng fact khác.
            - Không dùng format phân tích theo khía cạnh, không thêm mục "*Nhận xét*:" hoặc "**Kết luận tổng thể**".
            - Chỉ tạo followups khi thiếu biến đầu vào bắt buộc cho phép tính/câu hỏi chính.
            """

    return ""


def _ascii_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFD", str(value or "").lower())
    return " ".join(
        "".join(char for char in normalized if unicodedata.category(char) != "Mn")
        .replace("đ", "d")
        .split()
    )


def _period_role(fact: dict) -> str:
    explicit_role = _ascii_text(fact.get("period_role", ""))
    if explicit_role in {"current", "previous"}:
        return explicit_role

    explicit_period = _ascii_text(fact.get("period", ""))
    if explicit_period in {"cuoi", "cuoi ky", "cuoi nam", "ky nay", "nam nay"}:
        return "current"
    if explicit_period in {"dau", "dau ky", "dau nam", "ky truoc", "nam truoc"}:
        return "previous"

    text = _ascii_text(
        " ".join(
            str(fact.get(key, "") or "")
            for key in (
                "time_hint",
                "period",
                "period_label",
                "item_name",
                "row_label",
                "column_label",
                "subheading",
            )
        )
    )
    if any(marker in text for marker in _CURRENT_PERIOD_MARKERS):
        return "current"
    if any(marker in text for marker in _PREVIOUS_PERIOD_MARKERS):
        return "previous"
    return ""


def _slot_text(value: Any) -> str:
    text = _ascii_text(value)
    text = re.sub(
        r"\b\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}(?!\d)",
        " ",
        text,
    )
    text = re.sub(
        r"\b\d{1,2}\s*thang\s*\d{1,2}\s*nam\s*(?:19|20)\d{2}\b",
        " ",
        text,
    )
    text = re.sub(
        r"\b(?:vnd|dong|nghin dong|trieu dong|ty dong)\b",
        " ",
        text,
    )
    text = _PERIOD_WORD_RE.sub(" ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _source_report_key(fact: dict) -> str:
    source = str(fact.get("source", "") or "").strip()
    return _ascii_text(source.split("#", 1)[0].split("?", 1)[0])


def _metric_key(fact: dict) -> tuple[str, ...]:
    section_key = _ascii_text(fact.get("section_key", ""))
    row_label = str(fact.get("row_label", "") or "").strip()
    raw_item = row_label or str(fact.get("item_name", "") or "").split("|", 1)[0]
    item = section_key or _slot_text(raw_item)
    item = " ".join(re.sub(r"[^a-z0-9]+", " ", item).split())
    # Canonical statement sections are the row identity.  Repeated/noisy
    # balance-sheet headers must not split code 270/300/400 siblings.
    subheading = "" if section_key else _slot_text(fact.get("subheading", ""))
    column_label = "" if section_key else _slot_text(fact.get("column_label", ""))
    return (
        _ascii_text(fact.get("table", "")),
        _ascii_text(fact.get("note_ref", "")),
        item,
        subheading,
        _ascii_text(fact.get("value_type", "")),
        _ascii_text(fact.get("aggregation_level", "")),
        section_key,
        column_label,
        _ascii_text(fact.get("unit", "")),
        _ascii_text(fact.get("company", "")),
        _ascii_text(fact.get("fiscal_year", "")),
        _source_report_key(fact),
    )


def _label_overlap_score(query: str, facts: list[dict]) -> int:
    query_tokens = set(re.findall(r"[a-z0-9]+", _ascii_text(query)))
    label_tokens = set()
    for fact in facts:
        label_tokens.update(
            re.findall(
                r"[a-z0-9]+",
                _ascii_text(
                    f"{fact.get('subheading', '')} {fact.get('item_name', '')}"
                    f" {fact.get('row_label', '')} {fact.get('column_label', '')}"
                    f" {fact.get('value_type', '')} {fact.get('aggregation_level', '')}"
                ),
            )
        )
    return len({token for token in query_tokens & label_tokens if len(token) > 2})


_CALCULATION_STOP_TOKENS = {
    "bao",
    "bang",
    "cong",
    "cua",
    "duoc",
    "gia",
    "la",
    "nam",
    "ngay",
    "nhieu",
    "phan",
    "tai",
    "thay",
    "tinh",
    "tri",
    "trong",
    "voi",
}


def _semantic_tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _ascii_text(value))
        if len(token) > 2
        and token not in _CALCULATION_STOP_TOKENS
        and not re.fullmatch(r"20\d{2}", token)
    }


def _fact_search_text(fact: dict) -> str:
    parts = [
        str(fact.get(key, "") or "")
        for key in (
            "item_name",
            "row_label",
            "column_label",
            "subheading",
            "value_type",
            "aggregation_level",
            "note_ref",
            "metric_label",
            "entity_label",
            "scope_label",
            "counterparty",
            "transaction_type",
            "movement_type",
            "geography",
            "policy_topic",
            "section_key",
        )
    ]
    explicit_period = _ascii_text(fact.get("period", ""))
    if explicit_period in {"cuoi", "cuoi ky", "cuoi nam"}:
        parts.append("cuối kỳ")
    elif explicit_period in {"dau", "dau ky", "dau nam"}:
        parts.append("đầu kỳ")
    return " ".join(parts)


_SEMANTIC_AXIS_MARKERS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "credit_direction": (
        ("lending", ("cho vay", "phai thu ve cho vay")),
        ("borrowing", ("di vay", "vay ngan han", "vay dai han", "no vay", "vay")),
    ),
    "profit_level": (
        ("profit_after_tax", ("loi nhuan sau thue",)),
        (
            "profit_before_tax",
            ("loi nhuan truoc thue", "loi nhuan ke toan truoc thue"),
        ),
        ("operating_profit", ("loi nhuan thuan tu hoat dong kinh doanh",)),
        ("gross_profit", ("loi nhuan gop",)),
    ),
    "recognition": (
        (
            "commitment",
            (
                "cam ket",
                "nghia vu tiem tang",
                "khoan thanh toan toi thieu trong tuong lai",
            ),
        ),
        (
            "recognized_liability",
            (
                "no thue",
                "no phai tra",
                "khoan phai tra",
                "nghia vu da ghi nhan",
            ),
        ),
    ),
    "value_basis": (
        ("gross", ("nguyen gia", "gia goc")),
        (
            "net",
            (
                "gia tri con lai",
                "gia tri ghi so",
                "gia tri thuan",
                "sau du phong",
            ),
        ),
    ),
    "tax_basis": (
        ("tax_expense", ("chi phi thue thu nhap",)),
        (
            "deferred_tax_balance",
            (
                "tai san thue thu nhap hoan lai",
                "thue thu nhap hoan lai phai tra",
            ),
        ),
    ),
    "transaction_direction": (
        ("purchase", ("mua hang hoa", "mua dich vu", "mua hang hoa va dich vu")),
        ("sale", ("ban hang hoa", "ban dich vu", "doanh thu voi")),
    ),
    "movement": (
        ("reclassification", ("phan loai lai", "tai phan loai")),
        (
            "total_change",
            (
                "thay doi tong",
                "chenh lech tong",
                "tang tong",
                "giam tong",
            ),
        ),
    ),
    "stock_flow": (
        (
            "flow",
            (
                "dong tien",
                "luu chuyen tien",
                "tien thu",
                "tien chi",
                "phat sinh trong ky",
                "phat sinh trong nam",
                "chi phi",
                "doanh thu",
                "loi nhuan",
                "ghi tang",
                "ghi giam",
            ),
        ),
        (
            "balance",
            (
                "so du",
                "cuoi ky",
                "dau ky",
                "cuoi nam",
                "dau nam",
                "gia tri ghi so",
            ),
        ),
    ),
}


def _semantic_axis_values(value: Any) -> dict[str, str]:
    """Return only explicit semantic classes found in a query/fact label.

    These are contrastive accounting meanings which share many lexical tokens.
    Treating them as ordinary fuzzy matches is unsafe: for example, ``vay`` is
    contained in ``cho vay`` and a lease commitment is not a recognized lease
    liability.
    """

    text = _ascii_text(value)
    output: dict[str, str] = {}
    for axis, classes in _SEMANTIC_AXIS_MARKERS.items():
        for semantic_class, markers in classes:
            if any(marker in text for marker in markers):
                output[axis] = semantic_class
                break
    return output


def _semantic_slot_compatible(segment: str, fact: dict) -> bool:
    requested = _semantic_axis_values(segment)
    if not requested:
        return True
    candidate = _semantic_axis_values(_fact_search_text(fact))
    return all(
        candidate.get(axis) == semantic_class
        for axis, semantic_class in requested.items()
    )


def _fact_match_score(segment: str, fact: dict) -> int:
    if not _semantic_slot_compatible(segment, fact):
        return 0
    segment_tokens = _semantic_tokens(segment)
    fact_tokens = _semantic_tokens(_fact_search_text(fact))
    if not segment_tokens or not fact_tokens:
        return 0
    overlap = len(segment_tokens & fact_tokens)
    score = overlap * 10
    value_type = _ascii_text(fact.get("value_type", ""))
    aggregation = _ascii_text(fact.get("aggregation_level", ""))
    segment_norm = _ascii_text(segment)
    if value_type and value_type in segment_norm:
        score += 30
    if aggregation == "total" and any(marker in segment_norm for marker in ("tong", "cong")):
        score += 20
    return score


def _calculation_facts(worker_results_payload: Dict[str, Any]) -> list[tuple[dict, Decimal]]:
    output: list[tuple[dict, Decimal]] = []
    seen = set()
    for fact in _iter_retrieval_facts(worker_results_payload):
        if not is_report_fact(fact):
            continue
        if normalize_fact_status(fact.get("status", "found")) != "found":
            continue
        # Derived values are valid only when every operand can be traced
        # back to one stable source fact. Legacy/placeholder facts may
        # still be shown as evidence, but they are not calculation inputs.
        if not str(fact.get("fact_id", "") or "").strip():
            continue
        if not str(fact.get("source", "") or "").strip():
            continue
        value = parse_financial_decimal(
            fact.get("parsed_value", "") or fact.get("value")
        )
        if value is None:
            continue
        key = (
            str(fact.get("fact_id", "") or ""),
            str(fact.get("fiscal_year", "") or ""),
            str(fact.get("table", "") or ""),
            str(fact.get("note_ref", "") or ""),
            str(fact.get("item_name", "") or ""),
            str(fact.get("subheading", "") or ""),
            str(fact.get("period", "") or fact.get("time_hint", "") or ""),
            str(fact.get("value_type", "") or ""),
            str(fact.get("value", "") or ""),
            str(fact.get("source", "") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append((fact, value))
    return output


def _typed_label_compatible(left: Any, right: Any) -> bool:
    left_value = _ascii_text(left)
    right_value = _ascii_text(right)
    if not left_value or not right_value:
        return False
    if left_value == right_value:
        return True
    return (
        f" {left_value} " in f" {right_value} "
        or f" {right_value} " in f" {left_value} "
    )


def _same_fact_scope(
    left: dict,
    right: dict,
    *,
    require_same_period: bool = True,
    require_same_table: bool = True,
    require_same_typed_scope: bool = True,
) -> bool:
    """Require compatible report/entity scope without requiring one note_ref.

    Cross-metric ratios can legitimately use different note references or
    statement tables (for example profit/assets).  Callers choose whether the
    table itself is part of the required scope; company and reporting period
    remain compatible in every mode.
    """

    fields = (
        ("company", "fiscal_year", "table")
        if require_same_table
        else ("company", "fiscal_year")
    )
    for field in fields:
        left_value = _ascii_text(left.get(field, ""))
        right_value = _ascii_text(right.get(field, ""))
        if left_value and right_value and left_value != right_value:
            return False

    left_report = _source_report_key(left)
    right_report = _source_report_key(right)
    if not left_report or not right_report or left_report != right_report:
        return False

    if require_same_period:
        left_role = _period_role(left)
        right_role = _period_role(right)
        if left_role and right_role and left_role != right_role:
            return False

    if require_same_typed_scope:
        label_fields = ("entity_label", "scope_label", "counterparty", "geography")
        for field in label_fields:
            left_value = _ascii_text(left.get(field, ""))
            right_value = _ascii_text(right.get(field, ""))
            if (
                left_value
                and right_value
                and not _typed_label_compatible(left_value, right_value)
            ):
                return False

        for field in ("transaction_type", "movement_type", "policy_topic"):
            left_value = _ascii_text(left.get(field, ""))
            right_value = _ascii_text(right.get(field, ""))
            if not left_value or not right_value or left_value == right_value:
                continue
            if (
                field == "movement_type"
                and not require_same_period
                and {left_value, right_value}
                == {"opening_balance", "closing_balance"}
            ):
                continue
            return False
    return True


_DIMENSIONLESS_VALUE_KINDS = {"count", "percent", "ratio", "multiple"}


def _facts_have_compatible_units(facts: list[dict]) -> bool:
    """Require a complete, identical unit contract for amount calculations."""

    if not facts:
        return False
    kinds = [_ascii_text(fact.get("value_kind", "")) for fact in facts]
    units = [_ascii_text(fact.get("unit", "")) for fact in facts]
    if all(kind in _DIMENSIONLESS_VALUE_KINDS for kind in kinds):
        nonempty_units = {unit for unit in units if unit}
        return len(nonempty_units) <= 1
    return all(units) and len(set(units)) == 1


def _calculation_operation(query_norm: str) -> str:
    if any(marker in query_norm for marker in ("bao nhieu lan", "gap bao nhieu", "gap may lan")):
        return "multiple"
    if any(
        marker in query_norm
        for marker in ("ty le thay doi", "tang bao nhieu phan tram", "giam bao nhieu phan tram")
    ):
        return "percent_change"
    if any(marker in query_norm for marker in ("ty trong", "he so", "ty le", "ti le")):
        return "ratio"
    if any(
        marker in query_norm
        for marker in (
            "chenh lech",
            "su thay doi",
            "thay doi la bao nhieu",
            "tang bao nhieu",
            "giam bao nhieu",
        )
    ):
        return "delta"
    return ""


def _requested_period_role(query_norm: str) -> str:
    if any(marker in query_norm for marker in ("31/12", "cuoi ky", "cuoi nam", "nam nay", "ky nay")):
        return "current"
    if any(marker in query_norm for marker in ("1/1", "01/01", "dau ky", "dau nam", "nam truoc", "ky truoc")):
        return "previous"
    return ""


def _fact_operand(role: str, fact: dict, value: Decimal) -> dict[str, Any]:
    return {
        "role": role,
        "fact_id": str(fact.get("fact_id", "") or "").strip(),
        "item_name": str(fact.get("item_name", "") or "").strip(),
        "subheading": str(fact.get("subheading", "") or "").strip(),
        "metric_label": str(fact.get("metric_label", "") or "").strip(),
        "entity_label": str(fact.get("entity_label", "") or "").strip(),
        "scope_label": str(fact.get("scope_label", "") or "").strip(),
        "period": str(
            fact.get("period_label", "")
            or fact.get("time_hint", "")
            or fact.get("period", "")
            or ""
        ).strip(),
        "value_type": str(fact.get("value_type", "") or "").strip(),
        "aggregation_level": str(fact.get("aggregation_level", "") or "").strip(),
        "value": str(value),
        "unit": str(fact.get("unit", "") or "").strip(),
        "table": str(fact.get("table", "") or "").strip(),
        "fiscal_year": str(fact.get("fiscal_year", "") or "").strip(),
        "note_ref": str(fact.get("note_ref", "") or "").strip(),
        "section_path": str(fact.get("section_path", "") or "").strip(),
        "block_id": str(fact.get("block_id", "") or "").strip(),
        "counterparty": str(fact.get("counterparty", "") or "").strip(),
        "transaction_type": str(fact.get("transaction_type", "") or "").strip(),
        "movement_type": str(fact.get("movement_type", "") or "").strip(),
        "geography": str(fact.get("geography", "") or "").strip(),
        "policy_topic": str(fact.get("policy_topic", "") or "").strip(),
        "section_key": str(fact.get("section_key", "") or "").strip(),
        "source": str(fact.get("source", "") or "").strip(),
        "source_page": str(fact.get("source_page", "") or "").strip(),
    }


def _calculation_contract_from_state(state: dict) -> dict[str, Any]:
    worker_plan = state.get("worker_plan", {}) or {}
    if not isinstance(worker_plan, dict):
        return {}

    operation = ""
    explicit_operands: list[dict] = []
    role_operands: list[dict] = []
    seen_explicit = set()
    seen_roles = set()

    def consume(payload: dict, *, query: str = "", table: str = "") -> bool:
        nonlocal operation
        item_operation = str(payload.get("operation", "") or "").strip()
        if item_operation:
            if operation and operation != item_operation:
                return False
            operation = item_operation

        for operand in payload.get("operands", []) or []:
            if not isinstance(operand, dict):
                continue
            spec = dict(operand)
            key = json.dumps(spec, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen_explicit:
                seen_explicit.add(key)
                explicit_operands.append(spec)

        operand_role = str(payload.get("operand_role", "") or "").strip()
        if operand_role:
            spec = {
                "role": operand_role,
                "query": str(query or payload.get("query", "") or "").strip(),
                "table": str(table or payload.get("table", "") or "").strip(),
            }
            for field in (
                "period",
                "period_role",
                "period_label",
                "value_type",
                "aggregation_level",
                "metric",
                "entity",
                "scope_label",
                "counterparty",
                "transaction_type",
                "movement_type",
                "geography",
                "policy_topic",
                "section_key",
            ):
                value = payload.get(field)
                if value not in ("", None, [], {}):
                    spec[field] = value
            key = json.dumps(spec, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen_roles:
                seen_roles.add(key)
                role_operands.append(spec)
        return True

    for item in worker_plan.get("evidence_plan", []) or []:
        if not isinstance(item, dict):
            continue
        if not consume(item):
            return {}
        query_metadata = item.get("query_metadata", {})
        if isinstance(query_metadata, dict):
            for query, metadata in query_metadata.items():
                if not isinstance(metadata, dict):
                    continue
                if not consume(
                    metadata,
                    query=str(query or ""),
                    table=str(item.get("table", "") or ""),
                ):
                    return {}

    operands = explicit_operands or role_operands
    if not operation or not operands:
        return {}
    return {"operation": operation, "operands": operands}


def _operand_period_role(value: Any) -> str:
    text = _ascii_text(value)
    if text in {"current", "cuoi", "cuoi ky", "cuoi nam", "ky nay", "nam nay"}:
        return "current"
    if text in {"previous", "dau", "dau ky", "dau nam", "ky truoc", "nam truoc"}:
        return "previous"
    return ""


_OPERAND_DIMENSION_FIELDS = (
    ("entity", "entity_label", True),
    ("scope_label", "scope_label", True),
    ("counterparty", "counterparty", True),
    ("transaction_type", "transaction_type", False),
    ("movement_type", "movement_type", False),
    ("geography", "geography", True),
    ("policy_topic", "policy_topic", False),
)


def _operand_declared_dimensions_match(spec: dict, fact: dict) -> tuple[bool, int]:
    """Require every declared typed dimension to exist and match on the fact."""

    matched = 0
    for spec_field, fact_field, allow_containment in _OPERAND_DIMENSION_FIELDS:
        required = _ascii_text(spec.get(spec_field, ""))
        if not required:
            continue
        actual = _ascii_text(fact.get(fact_field, ""))
        if not actual:
            return False, matched
        compatible = required == actual
        if allow_containment:
            compatible = compatible or _typed_label_compatible(required, actual)
        if not compatible:
            return False, matched
        matched += 1
    return True, matched


def _rank_typed_operand_candidates(
    spec: dict,
    facts_with_values: list[tuple[dict, Decimal]],
) -> list[tuple[int, tuple[str, ...], dict, Decimal]]:
    table = _ascii_text(spec.get("table", ""))
    value_type = _ascii_text(spec.get("value_type", ""))
    aggregation = _ascii_text(
        spec.get("aggregation_level", "") or spec.get("aggregation", "")
    )
    metric = _ascii_text(spec.get("metric", ""))
    required_section_key = (
        str(spec.get("section_key", "") or "").strip()
        or query_section_total_key(str(spec.get("query", "") or ""))
        or query_section_total_key(str(spec.get("metric", "") or ""))
    )
    period_role = _operand_period_role(
        spec.get("period_role", "")
        or spec.get("period", "")
        or spec.get("role", "")
    )
    requested_reporting_basis = str(
        spec.get("reporting_basis", "")
        or query_reporting_basis(spec.get("query", ""))
        or ""
    ).strip()
    required_period_label = str(spec.get("period_label", "") or "").strip()
    if not required_period_label:
        raw_period = str(spec.get("period", "") or "").strip()
        if re.fullmatch(r"(?:19|20)\d{2}", raw_period) or re.fullmatch(
            r"\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}",
            raw_period,
        ):
            required_period_label = raw_period
    search_text = " ".join(
        str(spec.get(key, "") or "")
        for key in (
            "query",
            "metric",
            "entity",
            "scope_label",
            "counterparty",
            "transaction_type",
            "movement_type",
            "geography",
            "policy_topic",
        )
    ).strip()
    search_tokens = _semantic_tokens(search_text)

    ranked: list[tuple[int, tuple[str, ...], dict, Decimal]] = []
    for fact, value in facts_with_values:
        dimensions_match, matched_dimensions = _operand_declared_dimensions_match(
            spec,
            fact,
        )
        if not dimensions_match:
            continue
        if table and _ascii_text(fact.get("table", "")) != table:
            continue
        if value_type and value_type not in _ascii_text(
            f"{fact.get('value_type', '')} {_fact_search_text(fact)}"
        ):
            continue
        if aggregation and aggregation != _ascii_text(
            fact.get("aggregation_level", "")
        ):
            continue
        fact_section_key = str(fact.get("section_key", "") or "").strip()
        section_key_match = bool(
            required_section_key and fact_section_key == required_section_key
        )
        if (
            required_section_key
            and fact_section_key
            and fact_section_key != required_section_key
        ):
            continue
        metric_compatible = True
        metric_exact = False
        metric_coverage = 0.0
        if metric and not section_key_match:
            metric_text = fact_metric_slot(fact, _fact_search_text(fact))
            (
                metric_compatible,
                metric_exact,
                metric_coverage,
            ) = metric_slot_compatibility(
                spec.get("metric", ""),
                metric_text,
            )
            # Parser-owned metric metadata is a binding constraint. Rendered
            # document prose may contain coincidental words from company names
            # or display labels and must not manufacture an arithmetic operand.
            if metric_text and not metric_compatible:
                continue
        fact_period_role = _period_role(fact)
        # A requested period is a required slot, not a ranking hint.  A fact
        # without an explicit/inferable role must not silently bind to either
        # leg of a two-period calculation.
        if period_role and fact_period_role != period_role:
            continue
        actual_reporting_basis = (
            str(fact.get("reporting_basis", "") or "").strip()
            or fact_reporting_basis(fact)
        )
        if (
            requested_reporting_basis
            and actual_reporting_basis
            and not reporting_basis_compatible(
                requested_reporting_basis,
                actual_reporting_basis,
            )
        ):
            continue
        if (
            required_period_label
            and required_period_label not in fact_period_labels(fact)
        ):
            continue

        score = _fact_match_score(search_text, fact) if search_text else 1
        if search_tokens and score <= 0:
            continue
        fact_search_norm = _ascii_text(_fact_search_text(fact))
        if metric_exact:
            score += 180
        elif metric_compatible:
            score += int(metric_coverage * 100)
        elif metric and metric in fact_search_norm:
            # Legacy rows without a parser-owned metric keep a bounded lexical
            # fallback; typed rows have already passed the metric contract.
            score += 40
        if value_type:
            score += 30
        if aggregation:
            score += 20
        if required_section_key and fact_section_key == required_section_key:
            score += 200
        if period_role and fact_period_role == period_role:
            score += 20
        if requested_reporting_basis:
            score += 35
        if required_period_label:
            score += 40
        score += matched_dimensions * 40
        ranked.append((score, _metric_key(fact), fact, value))

    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return ranked


def _bind_typed_operand(
    spec: dict,
    facts_with_values: list[tuple[dict, Decimal]],
) -> tuple[dict, Decimal] | None:
    ranked = _rank_typed_operand_candidates(spec, facts_with_values)
    if not ranked:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][2], ranked[0][3]


def _bind_share_operand_pair(
    specs: list[dict],
    facts_with_values: list[tuple[dict, Decimal]],
) -> tuple[tuple[dict, Decimal], tuple[dict, Decimal]] | None:
    """Jointly bind a component/total share within one evidence schedule."""

    by_role = {
        str(spec.get("role", "") or "").strip(): spec
        for spec in specs
        if isinstance(spec, dict)
    }
    numerator_spec = by_role.get("numerator")
    denominator_spec = by_role.get("denominator")
    if not numerator_spec or not denominator_spec:
        return None

    numerator_ranked = _rank_typed_operand_candidates(
        numerator_spec,
        facts_with_values,
    )
    denominator_ranked = _rank_typed_operand_candidates(
        denominator_spec,
        facts_with_values,
    )
    pairs = []
    for numerator_item in numerator_ranked:
        numerator_score, _numerator_key, numerator_fact, numerator = numerator_item
        for denominator_item in denominator_ranked:
            (
                denominator_score,
                _denominator_key,
                denominator_fact,
                denominator,
            ) = denominator_item
            if numerator_fact.get("fact_id") == denominator_fact.get("fact_id"):
                continue
            if denominator == 0:
                continue
            if not _facts_have_compatible_units(
                [numerator_fact, denominator_fact]
            ):
                continue
            if not _same_fact_scope(
                numerator_fact,
                denominator_fact,
                require_same_table=True,
                require_same_typed_scope=False,
            ):
                continue

            numerator_block = str(
                numerator_fact.get("block_id", "") or ""
            ).strip()
            denominator_block = str(
                denominator_fact.get("block_id", "") or ""
            ).strip()
            if (
                numerator_block
                or denominator_block
            ) and numerator_block != denominator_block:
                continue
            numerator_note = str(
                numerator_fact.get("note_ref", "") or ""
            ).strip()
            denominator_note = str(
                denominator_fact.get("note_ref", "") or ""
            ).strip()
            if (
                numerator_note
                and denominator_note
                and numerator_note != denominator_note
            ):
                continue

            pair_score = numerator_score + denominator_score
            if numerator_block and numerator_block == denominator_block:
                pair_score += 300
            if numerator_note and numerator_note == denominator_note:
                pair_score += 60
            pair_key = (
                str(numerator_fact.get("fact_id", "") or ""),
                str(denominator_fact.get("fact_id", "") or ""),
            )
            pairs.append(
                (
                    pair_score,
                    pair_key,
                    (numerator_fact, numerator),
                    (denominator_fact, denominator),
                )
            )

    pairs.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if not pairs:
        return None
    if len(pairs) > 1 and pairs[0][0] == pairs[1][0]:
        return None
    return pairs[0][2], pairs[0][3]


_OPERAND_ROLE_LABELS = {
    "current": "kỳ hiện tại",
    "previous": "kỳ trước",
    "numerator": "tử số",
    "denominator": "mẫu số",
    "component": "thành phần",
    "opening": "số đầu kỳ",
    "additions": "phát sinh tăng",
    "reductions": "phát sinh giảm",
    "closing": "số cuối kỳ",
    "total": "tổng",
}


def _typed_operand_state(
    state: dict,
    worker_results_payload: Dict[str, Any],
) -> dict[str, Any]:
    """Expose bound and unresolved typed operands without authoring an answer."""

    if _difficulty_level_from_state(state) != "medium":
        return {}
    contract = _calculation_contract_from_state(state)
    if not contract:
        operation = _calculation_operation(
            _ascii_text(state.get("user_query", ""))
        )
        required_roles = {
            "delta": ("current", "previous"),
            "percent_change": ("current", "previous"),
            "multiple": ("current", "previous"),
            "ratio": ("numerator", "denominator"),
        }.get(operation, ())
        if not required_roles:
            return {}
        return {
            "status": "missing_contract",
            "operation": operation,
            "matched_operands": [],
            "unresolved_operands": [
                {
                    "role": role,
                    "role_label": _OPERAND_ROLE_LABELS[role],
                    "reason_code": "missing_typed_operand_contract",
                }
                for role in required_roles
            ],
        }

    facts_with_values = _calculation_facts(worker_results_payload)
    matched: list[tuple[str, str, dict, Decimal]] = []
    unresolved: list[dict[str, str]] = []
    used_fact_keys = set()
    for spec in contract.get("operands", []) or []:
        role = str(spec.get("role", "") or "").strip()
        role_label = _OPERAND_ROLE_LABELS.get(role, role or "toán hạng")
        ranked = _rank_typed_operand_candidates(spec, facts_with_values)
        if not ranked:
            unresolved.append(
                {
                    "role": role,
                    "role_label": role_label,
                    "reason_code": "no_matching_typed_fact",
                }
            )
            continue
        if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
            unresolved.append(
                {
                    "role": role,
                    "role_label": role_label,
                    "reason_code": "ambiguous_typed_fact",
                }
            )
            continue
        fact, value = ranked[0][2], ranked[0][3]
        fact_key = (
            str(fact.get("fact_id", "") or ""),
            _metric_key(fact),
            str(fact.get("value", "") or ""),
        )
        if fact_key in used_fact_keys:
            unresolved.append(
                {
                    "role": role,
                    "role_label": role_label,
                    "reason_code": "duplicate_operand_fact",
                }
            )
            continue
        used_fact_keys.add(fact_key)
        matched.append((role, role_label, fact, value))

    if not unresolved:
        return {}

    return {
        "status": "incomplete",
        "operation": str(contract.get("operation", "") or ""),
        "matched_operands": [
            {
                **_fact_operand(role, fact, value),
                "role_label": role_label,
            }
            for role, role_label, fact, value in matched
        ],
        "unresolved_operands": unresolved,
    }


def _explicit_calculation(
    state: dict,
    query: str,
    facts_with_values: list[tuple[dict, Decimal]],
) -> dict[str, Any] | None:
    contract = _calculation_contract_from_state(state)
    if not contract:
        return None

    operation = str(contract.get("operation", "") or "")
    bound: dict[str, list[tuple[dict, Decimal]]] = {}
    used_fact_keys = set()
    operand_specs = [
        spec
        for spec in (contract.get("operands", []) or [])
        if isinstance(spec, dict)
    ]
    if operation == "share":
        pair = _bind_share_operand_pair(operand_specs, facts_with_values)
        if pair is None:
            return None
        bound = {
            "numerator": [pair[0]],
            "denominator": [pair[1]],
        }
        used_fact_keys = {
            (
                str(fact.get("fact_id", "") or ""),
                _metric_key(fact),
                str(fact.get("value", "") or ""),
            )
            for fact, _value in pair
        }

    for spec in ([] if operation == "share" else operand_specs):
        role = str(spec.get("role", "") or "").strip()
        if not role:
            return None
        match = _bind_typed_operand(spec, facts_with_values)
        if match is None:
            return None
        fact, value = match
        fact_key = (
            str(fact.get("fact_id", "") or ""),
            _metric_key(fact),
            str(fact.get("value", "") or ""),
        )
        if fact_key in used_fact_keys:
            return None
        used_fact_keys.add(fact_key)
        bound.setdefault(role, []).append((fact, value))

    all_bound = [item for values in bound.values() for item in values]
    if not _facts_have_compatible_units([fact for fact, _value in all_bound]):
        return None

    unit = str(all_bound[0][0].get("unit", "") or "").strip() if all_bound else ""
    if operation in {"delta", "percent_change", "multiple"}:
        if len(bound.get("current", [])) != 1 or len(bound.get("previous", [])) != 1:
            return None
        current_fact, current = bound["current"][0]
        previous_fact, previous = bound["previous"][0]
        if (
            not _same_fact_scope(
                current_fact,
                previous_fact,
                require_same_period=False,
            )
            or _metric_key(current_fact) != _metric_key(previous_fact)
        ):
            return None
        if operation in {"percent_change", "multiple"} and previous == 0:
            return None
        difference = current - previous
        result = {
            "operation": operation,
            "metric": str(current_fact.get("row_label", "") or current_fact.get("item_name", "") or ""),
            "current_period": str(current_fact.get("period_label", "") or current_fact.get("time_hint", "") or "kỳ hiện tại"),
            "current_value": str(current),
            "previous_period": str(previous_fact.get("period_label", "") or previous_fact.get("time_hint", "") or "kỳ trước"),
            "previous_value": str(previous),
            "difference": str(difference),
            "direction": "tăng" if difference > 0 else "giảm" if difference < 0 else "không đổi",
            "unit": unit,
            "table": str(current_fact.get("table", "") or ""),
            "operands": [
                _fact_operand("current", current_fact, current),
                _fact_operand("previous", previous_fact, previous),
            ],
        }
        if operation == "percent_change":
            result.update(
                result=str((difference / abs(previous)) * Decimal(100)),
                result_unit="%",
            )
        elif operation == "multiple":
            result.update(result=str(current / previous), result_unit="lần")
        return result

    if operation in {"ratio", "share"}:
        if len(bound.get("numerator", [])) != 1 or len(bound.get("denominator", [])) != 1:
            return None
        numerator_fact, numerator = bound["numerator"][0]
        denominator_fact, denominator = bound["denominator"][0]
        if denominator == 0 or not _same_fact_scope(
            numerator_fact,
            denominator_fact,
            require_same_table=False,
            require_same_typed_scope=False,
        ):
            return None
        percent_output = operation == "share" or any(
            marker in _ascii_text(query) for marker in ("ty le", "ti le", "ty trong", "%")
        )
        return {
            "operation": "ratio",
            "metric": str(query or "tỷ lệ").strip(),
            "numerator_value": str(numerator),
            "denominator_value": str(denominator),
            "result": str((numerator / denominator) * (Decimal(100) if percent_output else Decimal(1))),
            "result_unit": "%" if percent_output else "lần",
            "unit": unit,
            "table": str(numerator_fact.get("table", "") or ""),
            "operands": [
                _fact_operand("numerator", numerator_fact, numerator),
                _fact_operand("denominator", denominator_fact, denominator),
            ],
        }

    if operation == "sum":
        components = bound.get("component", [])
        if not components or len(components) != len(contract.get("operands", [])):
            return None
        if any(
            not _same_fact_scope(components[0][0], fact)
            for fact, _value in components[1:]
        ):
            return None
        total = sum((value for _fact, value in components), Decimal(0))
        return {
            "operation": "sum",
            "metric": str(query or "tổng").strip(),
            "result": str(total),
            "result_unit": unit,
            "unit": unit,
            "table": str(components[0][0].get("table", "") or ""),
            "operands": [
                _fact_operand("component", fact, value)
                for fact, value in components
            ],
        }

    return None


def _two_period_calculation(
    query: str,
    facts_with_values: list[tuple[dict, Decimal]],
    *,
    operation: str,
) -> dict[str, Any] | None:
    groups: dict[tuple[str, ...], dict[str, list[tuple[dict, Decimal]]]] = {}
    for fact, value in facts_with_values:
        role = _period_role(fact)
        key = _metric_key(fact)
        if not role or not key[2]:
            continue
        groups.setdefault(key, {"current": [], "previous": []})[role].append((fact, value))

    candidates = []
    for key, roles in groups.items():
        if len(roles["current"]) != 1 or len(roles["previous"]) != 1:
            continue
        current_fact, current = roles["current"][0]
        previous_fact, previous = roles["previous"][0]
        if not _facts_have_compatible_units([current_fact, previous_fact]):
            continue
        score = _label_overlap_score(query, [current_fact, previous_fact])
        if _ascii_text(current_fact.get("aggregation_level", "")) == "total" and "tong" in _ascii_text(query):
            score += 5
        candidates.append((score, key, current_fact, current, previous_fact, previous))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None

    _, _, current_fact, current, previous_fact, previous = candidates[0]
    if operation in {"multiple", "percent_change"} and previous == 0:
        return None

    difference = current - previous
    result: dict[str, Any] = {
        "operation": operation,
        "metric": str(
            current_fact.get("row_label", "")
            or current_fact.get("item_name", "")
            or ""
        ).strip(),
        "current_period": str(
            current_fact.get("period_label", "")
            or current_fact.get("time_hint", "")
            or current_fact.get("period", "")
            or "năm hiện tại"
        ).strip(),
        "current_value": str(current),
        "previous_period": str(
            previous_fact.get("period_label", "")
            or previous_fact.get("time_hint", "")
            or previous_fact.get("period", "")
            or "năm trước"
        ).strip(),
        "previous_value": str(previous),
        "difference": str(difference),
        "direction": "tăng" if difference > 0 else "giảm" if difference < 0 else "không đổi",
        "unit": str(current_fact.get("unit", "") or previous_fact.get("unit", "") or "").strip(),
        "table": str(current_fact.get("table", "") or "").strip(),
        "operands": [
            _fact_operand("current", current_fact, current),
            _fact_operand("previous", previous_fact, previous),
        ],
    }
    if operation == "percent_change":
        result["result"] = str((difference / abs(previous)) * Decimal(100))
        result["result_unit"] = "%"
    elif operation == "multiple":
        result["result"] = str(current / previous)
        result["result_unit"] = "lần"
    return result


def _ratio_segments(query_norm: str) -> tuple[str, str]:
    cleaned = re.sub(r"^(?:tinh\s+)?(?:ty le|ti le|ty trong|he so)\s+", "", query_norm)
    match = re.search(r"(.+?)\s+(?:tren|so voi|chia cho|trong)\s+(.+)", cleaned)
    if not match:
        return "", ""
    numerator = match.group(1).strip()
    denominator = re.split(
        r"\s+(?:tai|vao|nam|la bao nhieu|bang bao nhieu)\b",
        match.group(2).strip(),
        maxsplit=1,
    )[0].strip()
    return numerator, denominator


def _ratio_calculation(
    query: str,
    facts_with_values: list[tuple[dict, Decimal]],
) -> dict[str, Any] | None:
    query_norm = _ascii_text(query)
    numerator_segment, denominator_segment = _ratio_segments(query_norm)
    if not numerator_segment or not denominator_segment:
        return None

    requested_period = _requested_period_role(query_norm)
    ranked_pairs = []
    for numerator_fact, numerator in facts_with_values:
        numerator_score = _fact_match_score(numerator_segment, numerator_fact)
        if numerator_score <= 0:
            continue
        numerator_role = _period_role(numerator_fact)
        if requested_period and numerator_role != requested_period:
            continue
        for denominator_fact, denominator in facts_with_values:
            if numerator_fact is denominator_fact or denominator == 0:
                continue
            denominator_score = _fact_match_score(denominator_segment, denominator_fact)
            if denominator_score <= 0:
                continue
            denominator_role = _period_role(denominator_fact)
            if requested_period and denominator_role != requested_period:
                continue
            if numerator_role and denominator_role and numerator_role != denominator_role:
                continue
            if not _same_fact_scope(
                numerator_fact,
                denominator_fact,
                require_same_typed_scope=False,
            ):
                continue
            if not _facts_have_compatible_units(
                [numerator_fact, denominator_fact]
            ):
                continue

            score = numerator_score + denominator_score
            if (
                str(numerator_fact.get("note_ref", "") or "").strip()
                and str(numerator_fact.get("note_ref", "") or "").strip()
                == str(denominator_fact.get("note_ref", "") or "").strip()
            ):
                score += 10
            if (
                _ascii_text(numerator_fact.get("subheading", ""))
                and _ascii_text(numerator_fact.get("subheading", ""))
                == _ascii_text(denominator_fact.get("subheading", ""))
            ):
                score += 5
            ranked_pairs.append(
                (
                    score,
                    _metric_key(numerator_fact),
                    _metric_key(denominator_fact),
                    numerator_fact,
                    numerator,
                    denominator_fact,
                    denominator,
                )
            )

    if not ranked_pairs:
        return None
    ranked_pairs.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    if len(ranked_pairs) > 1 and ranked_pairs[0][0] == ranked_pairs[1][0]:
        return None

    _, _, _, numerator_fact, numerator, denominator_fact, denominator = ranked_pairs[0]
    ratio = numerator / denominator
    percent_output = any(marker in query_norm for marker in ("ty le", "ti le", "ty trong", "%"))
    return {
        "operation": "ratio",
        "metric": f"{numerator_segment} / {denominator_segment}",
        "numerator_value": str(numerator),
        "denominator_value": str(denominator),
        "result": str(ratio * Decimal(100) if percent_output else ratio),
        "result_unit": "%" if percent_output else "lần",
        "unit": str(numerator_fact.get("unit", "") or denominator_fact.get("unit", "") or "").strip(),
        "table": str(numerator_fact.get("table", "") or "").strip(),
        "operands": [
            _fact_operand("numerator", numerator_fact, numerator),
            _fact_operand("denominator", denominator_fact, denominator),
        ],
    }


def _typed_decimal_calculation(
    state: dict,
    worker_results_payload: Dict[str, Any],
) -> dict[str, Any] | None:
    """Bind typed operands and return an exact, provenance-carrying calculation."""

    if _difficulty_level_from_state(state) != "medium":
        return None
    query = str(state.get("user_query", "") or "").strip()
    facts_with_values = _calculation_facts(worker_results_payload)
    # A derived number is permitted only when the evidence plan explicitly
    # declares the operation and every operand role.  Query-text heuristics can
    # identify that a calculation was requested, but they are not an operand
    # contract and must never choose the nearest two numbers.  When the contract
    # is absent or cannot bind uniquely, ``_typed_operand_state`` exposes the
    # missing slots to Synth without writing a response on the model's behalf.
    if not _calculation_contract_from_state(state):
        return None
    return _explicit_calculation(state, query, facts_with_values)


def _calculation_evidence_ledger(calculation: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(calculation, dict) or not calculation:
        return {"schema_version": 1, "entries": []}

    operation = str(calculation.get("operation", "") or "").strip()
    result = calculation.get("result")
    if result in ("", None) and operation == "delta":
        result = calculation.get("difference")
    result_unit = str(
        calculation.get("result_unit", "")
        or calculation.get("unit", "")
        or ""
    ).strip()
    operands = []
    for operand in calculation.get("operands", []) or []:
        if not isinstance(operand, dict):
            continue
        operands.append(
            {
                key: operand.get(key)
                for key in (
                    "role",
                    "fact_id",
                    "value",
                    "unit",
                    "period",
                    "value_type",
                    "aggregation_level",
                    "table",
                    "fiscal_year",
                    "item_name",
                    "metric_label",
                    "entity_label",
                    "scope_label",
                    "counterparty",
                    "transaction_type",
                    "movement_type",
                    "geography",
                    "policy_topic",
                    "section_key",
                    "note_ref",
                    "section_path",
                    "block_id",
                    "source",
                    "source_page",
                )
                if operand.get(key) not in ("", None)
            }
        )
    return {
        "schema_version": 1,
        "entries": [
            {
                "kind": "derived_calculation",
                "operation": operation,
                "metric": str(calculation.get("metric", "") or "").strip(),
                "result": str(result) if result not in ("", None) else "",
                "result_unit": result_unit,
                "operands": operands,
            }
        ],
    }


def _canonical_lookup_candidate(
    state: dict,
    worker_results_payload: Dict[str, Any],
) -> dict[str, Any] | None:
    """Return the unique fact matching an easy lookup as model context."""

    if _difficulty_level_from_state(state) != "easy":
        return None
    query = str(state.get("user_query", "") or "").strip()
    slots = parse_query_slots(query)
    if slots.operation != "lookup" or not (slots.metric or slots.entity):
        return None

    candidates: list[tuple[float, dict]] = []
    for fact in _iter_retrieval_facts(worker_results_payload):
        if not is_report_fact(fact):
            continue
        if normalize_fact_status(fact.get("status", "found")) != "found":
            continue
        value = str(fact.get("value", "") or "").strip()
        if not value or len(value) > 500:
            continue
        if not fact_matches_required_slots(slots, fact, value):
            continue
        candidates.append((fact_slot_score(slots, fact, value), fact))

    if not candidates:
        return None

    # Duplicate parser/index representations of the same cell are harmless;
    # conflicting values for the requested slot are an ambiguity and must not
    # be resolved by rank alone.
    distinct_values = {
        (
            str(fact.get("value", "") or "").strip(),
            _ascii_text(fact.get("unit", "")),
        )
        for _score, fact in candidates
    }
    if len(distinct_values) != 1:
        return None

    candidates.sort(
        key=lambda item: (
            item[0],
            bool(str(item[1].get("fact_id", "") or "").strip()),
            bool(str(item[1].get("source", "") or "").strip()),
        ),
        reverse=True,
    )
    _score, fact = candidates[0]
    return {
        key: fact.get(key)
        for key in (
            "fact_id",
            "item_name",
            "row_label",
            "metric_label",
            "value",
            "parsed_value",
            "unit",
            "fiscal_year",
            "period",
            "period_label",
            "period_role",
            "time_hint",
            "value_type",
            "aggregation_level",
            "table",
            "note_ref",
            "source",
            "source_page",
        )
        if fact.get(key) not in (None, "")
    }


def _synth_system_instruction(state: dict, profile: Dict[str, Any]) -> str:
    return (
        f"{profile['system_instruction']}{_synth_difficulty_instruction(state)}"
        """

            CONTRACT FACT LEDGER — ƯU TIÊN CAO NHẤT
            - Contract này thay thế mọi chỉ dẫn trước đó nói rằng payload có
              analysis_outputs thì không có raw facts/retrieval_facts. Trong
              analysis mode, worker_results_json có đồng thời
              `analysis_outputs` và `retrieval_facts`.
            - `analysis_outputs` là diễn giải của worker. `retrieval_facts` là
              fact ledger có cấu trúc và là nguồn sự thật để kiểm tra giá trị,
              dấu, đơn vị, kỳ và provenance. Nếu hai nhánh mâu thuẫn về các
              trường này, bắt buộc theo fact ledger và sửa diễn giải của worker.
            - Trước mọi câu nói "thiếu", "không có dữ liệu", "không tìm thấy"
              hoặc tương đương, phải kiểm tra toàn bộ `retrieval_facts`. Nếu có
              fact status=`found` khớp metric/chủ thể/kỳ/value_type/scope thì
              phải dùng fact đó và không được tuyên bố metric ấy còn thiếu.
            - Giữ nguyên scale/unit của fact. Với unit=`VND`, trình bày số VND
              nguyên gốc; không tự chia cho 10^6/10^9/10^12 và không đổi thành
              triệu/tỷ/nghìn tỷ nếu user_query không yêu cầu quy đổi.
            - `parsed_value` là giá trị chuẩn để xác định dấu. Với các dòng tổng
              CFO/CFI/CFF (lưu chuyển tiền thuần từ hoạt động kinh doanh/đầu
              tư/tài chính), parsed_value > 0 là dòng tiền vào ròng và
              parsed_value < 0 là dòng tiền ra ròng; tuyệt đối không diễn giải
              ngược dấu. Khi nêu số vẫn giữ unit và provenance của fact.
            - Các payload `canonical_lookup_candidate`,
              `typed_decimal_calculation`, `typed_operand_state` và
              `canonical_financial_metrics` là context hỗ trợ đã bind từ facts.
              Hãy dùng chúng để kiểm tra lựa chọn fact, toán hạng, công thức,
              basis và provenance; chính bạn vẫn sở hữu toàn bộ nội dung và
              cách diễn đạt câu trả lời cuối cùng.
            """
    )


def _build_payload(
    state: dict,
    profile: Dict[str, Any],
    worker_results_payload: Dict[str, Any],
) -> SynthPayload:
    prompt_worker_results = dict(worker_results_payload or {})
    typed_calculation = _typed_decimal_calculation(
        state,
        prompt_worker_results,
    )
    if typed_calculation:
        prompt_worker_results["typed_decimal_calculation"] = typed_calculation
    typed_operand_state = _typed_operand_state(state, prompt_worker_results)
    if typed_operand_state:
        prompt_worker_results["typed_operand_state"] = typed_operand_state
    lookup_candidate = _canonical_lookup_candidate(state, prompt_worker_results)
    if lookup_candidate:
        prompt_worker_results["canonical_lookup_candidate"] = lookup_candidate
    canonical_financial_metrics = _canonical_profitability_metric_ledger(
        prompt_worker_results
    )
    if canonical_financial_metrics:
        prompt_worker_results["canonical_financial_metrics"] = (
            canonical_financial_metrics
        )
    return {
        "role": profile["role"],
        "tools_list": "",
        "system_instruction": _synth_system_instruction(state, profile),
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": _safe_json_dumps(_synth_plan_payload(state)),
        "worker_results_json": _safe_json_dumps(prompt_worker_results),
        "allowed_keywords_json": "{}",
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


_GROUNDED_FACT_HEADING = "Dữ liệu trích xuất"
_GROUNDED_INFERENCE_HEADING = "Suy luận đánh giá"
_GROUNDED_FACT_HEADER = "du lieu trich xuat"
_GROUNDED_INFERENCE_HEADER = "suy luan danh gia"
_GROUNDED_INFERENCE_MARKERS = (
    "tu du kien",
    "tu cac du kien",
    "tu nhung du kien",
    "tu cac fact",
    "tu nhung fact",
    "tu cac su kien",
    "tu nhung su kien",
    "dua tren du kien",
    "dua tren cac du kien",
    "dua tren nhung du kien",
    "dua tren fact",
    "dua tren cac fact",
    "dua tren nhung fact",
    "dua tren cac su kien",
    "dua tren nhung su kien",
)
_GROUNDED_LIMIT_HEADER = "gioi han bang chung"
_GROUNDED_LEGACY_HEADERS = (
    "fact duoc bao cao xac nhan",
    "suy luan dua tren fact",
    _GROUNDED_LIMIT_HEADER,
)
_GROUNDED_REFUSAL_PREFIXES = (
    "khong co thong tin",
    "khong co du lieu",
    "du lieu hien co khong",
    "bao cao khong co thong tin",
    "bao cao khong neu",
    "khong the xac dinh",
    "khong the danh gia",
)
_GROUNDED_INFERENCE_REFUSALS = (
    *_GROUNDED_REFUSAL_PREFIXES,
    "khong the suy ra",
    "khong du co so de suy ra",
    "khong the tra loi",
)
_GROUNDED_RISKY_CLAIMS = (
    "huy dong von",
    "kenh von",
    "gan loi ich",
    "da dang hoa co cau so huu",
    "tang minh bach",
    "ky luat tai chinh",
    "nghia vu cong bo thong tin",
)
_GROUNDED_CONDITIONAL_MARKERS = (
    "co the",
    "ve nguyen tac",
    "thong thuong",
    "co kha nang",
    "neu",
)


def _grounded_sections(answer: Any) -> tuple[str, str] | None:
    answer_text = str(answer or "")
    normalized = _ascii_text(answer_text)
    if not normalized:
        return None
    # Only the current canonical two-heading format satisfies the runtime
    # contract.  In particular, do not silently accept either of the former
    # headings or the legacy third "Giới hạn bằng chứng" section: accepting
    # them would let stale model output bypass the repair pass.
    if any(header in normalized for header in _GROUNDED_LEGACY_HEADERS):
        return None
    if (
        _GROUNDED_FACT_HEADING not in answer_text
        or _GROUNDED_INFERENCE_HEADING not in answer_text
    ):
        return None
    fact_index = normalized.find(_GROUNDED_FACT_HEADER)
    inference_index = normalized.find(_GROUNDED_INFERENCE_HEADER)
    if (
        fact_index < 0
        or not (fact_index < inference_index)
    ):
        return None
    fact_section = normalized[
        fact_index + len(_GROUNDED_FACT_HEADER) : inference_index
    ].strip(" :*-")
    inference_section = normalized[
        inference_index + len(_GROUNDED_INFERENCE_HEADER) :
    ].strip(" :*-")
    if min(
        len(_semantic_tokens(section))
        for section in (fact_section, inference_section)
    ) < 2:
        return None
    return fact_section, inference_section


def _grounded_premise_requirements(state: dict) -> list[str]:
    for source_key in ("planner_plan", "worker_plan"):
        source = state.get(source_key, {})
        if not isinstance(source, dict):
            continue
        premises = _dedupe_keep_order(
            str(item).strip()
            for item in (source.get("premise_requirements", []) or [])
            if str(item).strip()
        )
        if premises:
            return premises
    return []


def _grounded_concept_atoms(
    value: Any,
    *,
    requirement: bool = False,
) -> set[str]:
    return _semantic_narrative_concept_atoms(
        value,
        requirement=requirement,
    )


def _grounded_fact_text(fact: dict) -> str:
    # Routing queries and requirements explain why a fact was fetched; they
    # are not evidence that the requested premise is true.  Project only the
    # actual fact payload so an exact route hint cannot turn an unrelated
    # policy/valuation row into a corporate-history event.
    return narrative_evidence_surface(fact)


def _grounded_valid_facts(
    worker_results_payload: Dict[str, Any],
) -> list[dict]:
    candidates = []
    for fact in _iter_retrieval_facts(worker_results_payload):
        if not is_report_fact(fact):
            continue
        if normalize_fact_status(fact.get("status", "found")) != "found":
            continue
        value = str(
            fact.get("value", "")
            or fact.get("parsed_value", "")
            or ""
        ).strip()
        if not value:
            continue
        if not (
            str(fact.get("fact_id", "") or "").strip()
            or str(fact.get("source", "") or "").strip()
            or str(fact.get("source_page", "") or "").strip()
        ):
            continue
        candidates.append(fact)
    return candidates


def _grounded_premise_bindings(
    state: dict,
    worker_results_payload: Dict[str, Any],
) -> tuple[dict[str, list[dict]], list[str]]:
    premises = _grounded_premise_requirements(state)
    facts = _grounded_valid_facts(worker_results_payload)
    if not premises:
        query_tokens = _semantic_tokens(state.get("user_query", ""))
        matched = (
            list(facts)
            if not query_tokens
            else [
                fact
                for fact in facts
                if len(
                    query_tokens.intersection(
                        _semantic_tokens(_grounded_fact_text(fact))
                    )
                )
                >= (2 if len(query_tokens) >= 4 else 1)
            ]
        )
        return ({"__query__": matched} if matched else {}), (
            [] if matched else ["__query__"]
        )

    bindings: dict[str, list[dict]] = {}
    missing = []
    for premise in premises:
        required_atoms = _grounded_concept_atoms(
            premise,
            requirement=True,
        )
        premise_tokens = _semantic_tokens(premise)
        matched = []
        covered_atoms = set()
        for fact in facts:
            fact_text = _grounded_fact_text(fact)
            fact_atoms = _grounded_concept_atoms(fact_text)
            if required_atoms:
                if not required_atoms.intersection(fact_atoms):
                    continue
                covered_atoms.update(required_atoms.intersection(fact_atoms))
                matched.append(fact)
                continue

            overlap = premise_tokens.intersection(_semantic_tokens(fact_text))
            minimum_overlap = 2 if len(premise_tokens) >= 4 else 1
            if len(overlap) >= minimum_overlap:
                matched.append(fact)

        complete = bool(matched) and (
            not required_atoms or required_atoms.issubset(covered_atoms)
        )
        if complete:
            bindings[premise] = matched
        else:
            missing.append(premise)
    return bindings, missing


def _grounded_premise_facts(
    state: dict,
    worker_results_payload: Dict[str, Any],
) -> list[dict]:
    bindings, missing = _grounded_premise_bindings(
        state,
        worker_results_payload,
    )
    if missing:
        return []
    output = []
    seen = set()
    for facts in bindings.values():
        for fact in facts:
            key = (
                str(fact.get("fact_id", "") or ""),
                str(fact.get("source", "") or ""),
                str(fact.get("item_name", "") or ""),
                str(fact.get("value", "") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            output.append(fact)
    return output


def _grounded_claims_supported(
    sections: tuple[str, str],
    premise_facts: list[dict],
) -> bool:
    fact_section, inference_section = sections
    evidence_text = _ascii_text(
        " ".join(
            str(fact.get(key, "") or "")
            for fact in premise_facts
            for key in (
                "item_name",
                "row_label",
                "metric_label",
                "section_path",
                "value",
            )
        )
    )
    for claim in _GROUNDED_RISKY_CLAIMS:
        # Reported-fact claims must actually occur in evidence.
        if claim in fact_section and claim not in evidence_text:
            return False
        # External/general implications are allowed only as explicitly
        # conditional inference when the report itself does not establish them.
        if (
            claim in inference_section
            and claim not in evidence_text
            and not any(
                marker in inference_section
                for marker in _GROUNDED_CONDITIONAL_MARKERS
            )
        ):
            return False
    return True


def _grounded_fact_section_covers_premises(
    fact_section: str,
    state: dict,
) -> bool:
    premises = _grounded_premise_requirements(state)
    if not premises:
        return True
    section_atoms = _grounded_concept_atoms(fact_section)
    section_tokens = _semantic_tokens(fact_section)
    for premise in premises:
        required_atoms = _grounded_concept_atoms(
            premise,
            requirement=True,
        )
        if required_atoms:
            if not required_atoms.issubset(section_atoms):
                return False
            continue
        premise_tokens = _semantic_tokens(premise)
        minimum_overlap = 2 if len(premise_tokens) >= 4 else 1
        if len(premise_tokens.intersection(section_tokens)) < minimum_overlap:
            return False
    return True


def _grounded_interpretation_contract_passes(
    decision: dict,
    *,
    state: dict | None = None,
    worker_results_payload: Dict[str, Any] | None = None,
) -> bool:
    if str(decision.get("status", "") or "").strip().lower() != "answer":
        return False
    sections = _grounded_sections(decision.get("answer", ""))
    if sections is None:
        return False
    fact_section, inference_section = sections
    if any(marker in inference_section for marker in _GROUNDED_INFERENCE_REFUSALS):
        return False
    if not any(marker in inference_section for marker in _GROUNDED_INFERENCE_MARKERS):
        return False
    if state is None or worker_results_payload is None:
        return True
    bindings, missing = _grounded_premise_bindings(
        state,
        worker_results_payload,
    )
    premise_facts = _grounded_premise_facts(state, worker_results_payload)
    return (
        bool(bindings)
        and not missing
        and bool(premise_facts)
        and _grounded_fact_section_covers_premises(fact_section, state)
        and _grounded_claims_supported(
            sections,
            premise_facts,
        )
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


def _analysis_outputs_payload(worker_results: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(worker_results.get("analysis_outputs"), dict):
        return worker_results.get("analysis_outputs", {})
    return {
        agent_name: payload
        for agent_name, payload in (worker_results or {}).items()
        if is_analysis_agent(agent_name) and isinstance(payload, dict)
    }


def _hard_analysis_agents(worker_results: Dict[str, Any]) -> List[str]:
    """Return analysis agents with substantive output in canonical display order."""

    outputs = _analysis_outputs_payload(worker_results)
    return [
        agent_name
        for agent_name in _HARD_ANALYSIS_ASPECT_LABELS
        if isinstance(outputs.get(agent_name), dict)
        and str((outputs.get(agent_name) or {}).get("answer", "") or "").strip()
    ]


def _hard_analysis_heading_part(raw_line: Any) -> str:
    """Return the exact hard-layout part named by a Markdown heading line."""

    heading = re.sub(r"^#{1,6}\s*", "", str(raw_line or "").strip())
    heading = heading.strip(" *_`")
    heading = re.sub(r"^\d+\s*[.)-]\s*", "", heading)
    normalized_heading = _ascii_text(heading.strip(" *_`:")).rstrip(":")
    if normalized_heading == _ascii_text("Tóm tắt đánh giá"):
        return "summary"
    for agent_name, label in _HARD_ANALYSIS_ASPECT_LABELS.items():
        if normalized_heading == _ascii_text(label):
            return agent_name
    if normalized_heading == _ascii_text("Kết luận tổng thể"):
        return "legacy_conclusion"
    if normalized_heading == _ascii_text("Giới hạn bằng chứng"):
        return "legacy_evidence_limit"
    return ""


def _hard_analysis_contract_violations(
    state: dict,
    decision: Dict[str, Any],
    worker_results: Dict[str, Any],
) -> List[str]:
    """Validate the user-facing BLUF + multi-aspect contract for hard analysis.

    The section list is derived from analysis outputs, not reclassified by the
    synthesizer. This keeps the plan/dispatch/result chain as the single source
    of truth and makes every completed specialist perspective visible.
    """

    if _response_mode_from_state(state) == "grounded_interpretation":
        return []
    # A legitimate request for a new analysis perspective must complete before
    # the final multi-aspect layout is enforced. Otherwise a formatting repair
    # would accidentally turn `need_more` into a terminal answer.
    if str(decision.get("status", "") or "").strip().lower() != "answer":
        return []

    agents = _hard_analysis_agents(worker_results)
    if _difficulty_level_from_state(state) != "hard" and not agents:
        return []

    answer = str(decision.get("answer", "") or "").strip()
    violations: List[str] = []
    heading_positions: Dict[str, int] = {}
    cursor = 0
    for raw_line in answer.splitlines():
        part = _hard_analysis_heading_part(raw_line)
        if part and part not in heading_positions:
            heading_positions[part] = cursor
        cursor += len(raw_line) + 1
    base_summary, repaired_sections, _unassigned = _hard_analysis_base_parts(answer)

    if "summary" not in heading_positions:
        violations.append("missing_bluf_summary")
    elif not base_summary:
        violations.append("empty_bluf_summary")

    aspect_positions = []
    for agent_name in agents:
        position = heading_positions.get(agent_name, -1)
        if position < 0 or agent_name not in repaired_sections:
            violations.append(f"missing_aspect:{agent_name}")
        else:
            aspect_positions.append(position)

    summary_position = heading_positions.get("summary", -1)
    if summary_position >= 0 and aspect_positions and summary_position > min(aspect_positions):
        violations.append("bluf_not_first")
    if "legacy_conclusion" in heading_positions:
        violations.append("fixed_bottom_conclusion")
    if "legacy_evidence_limit" in heading_positions:
        violations.append("legacy_evidence_limit_section")
    if _INTERNAL_AGENT_LABEL_RE.search(answer):
        violations.append("internal_agent_label_exposed")

    if base_summary:
        violations.extend(_summary_presentation_violations(base_summary))

    return list(dict.fromkeys(violations))


def _hard_analysis_base_parts(
    answer: Any,
) -> Tuple[str, Dict[str, str], str]:
    """Split an answer into BLUF and named aspect sections for validation."""

    summary_lines: List[str] = []
    unassigned_lines: List[str] = []
    section_lines: Dict[str, List[str]] = {}
    active_part = "unassigned"

    for raw_line in str(answer or "").splitlines():
        heading_part = _hard_analysis_heading_part(raw_line)

        if heading_part == "summary":
            active_part = "summary"
            continue

        if heading_part in _HARD_ANALYSIS_ASPECT_LABELS:
            active_part = heading_part
            section_lines.setdefault(heading_part, [])
            continue

        # These legacy bottom sections are deliberately not carried into the
        # summary-first answer. Their useful conclusions belong in the BLUF.
        if heading_part in {"legacy_conclusion", "legacy_evidence_limit"}:
            active_part = "discard"
            continue

        if active_part == "summary":
            summary_lines.append(raw_line)
        elif active_part in _HARD_ANALYSIS_ASPECT_LABELS:
            section_lines.setdefault(active_part, []).append(raw_line)
        elif active_part == "unassigned":
            unassigned_lines.append(raw_line)

    sections = {
        agent_name: "\n".join(lines).strip()
        for agent_name, lines in section_lines.items()
        if "\n".join(lines).strip()
    }
    return (
        "\n".join(summary_lines).strip(),
        sections,
        "\n".join(unassigned_lines).strip(),
    )


def _has_leading_evidence_boilerplate(value: Any) -> bool:
    normalized = _ascii_text(value)
    normalized = re.sub(r"^[\s*_#`>\-]+", "", normalized)
    return bool(_LEADING_EVIDENCE_BOILERPLATE_RE.match(normalized))


def _summary_number_is_period(text: str, match: re.Match[str]) -> bool:
    """Allow report years and quarter labels while rejecting financial values."""

    token = match.group(0).lstrip("+-")
    if re.fullmatch(r"(?:19|20)\d{2}", token):
        return True

    if token in {"1", "2", "3", "4"}:
        prefix = text[max(0, match.start() - 12) : match.start()]
        if re.search(r"(?:\bquy|\bq)\s*$", prefix):
            return True

    return False


def _summary_presentation_violations(summary: Any) -> List[str]:
    """Detect quantitative detail that belongs in aspect sections, not BLUF."""

    text = _ascii_text(summary)
    violations: List[str] = []
    if _SUMMARY_FORMULA_RE.search(text):
        violations.append("summary_contains_formula")

    has_financial_number = "%" in text or bool(re.search(r"\b(?:vnd|vnđ)\b", text))
    if not has_financial_number:
        has_financial_number = any(
            not _summary_number_is_period(text, match)
            for match in _SUMMARY_NUMBER_RE.finditer(text)
        )
    if has_financial_number:
        violations.append("summary_contains_financial_number")

    return violations


def _contains_insufficient_data_language(value: Any) -> bool:
    text = _ascii_text(value)
    return any(
        _ascii_text(marker) in text
        for marker in _INSUFFICIENT_ANSWER_MARKERS
    )


def _missing_claim_segments(answer: Any) -> List[str]:
    return [
        segment.strip()
        for segment in re.split(r"(?:\n+|(?<=[.!?;:])\s+)", str(answer or ""))
        if segment.strip() and _contains_insufficient_data_language(segment)
    ]


def _missing_segment_mentions_requirement(segment: str, requirement: str) -> bool:
    segment_text = _ascii_text(segment)
    requirement_text = _ascii_text(normalize_requirement_text(requirement))
    if not segment_text or not requirement_text:
        return False
    if requirement_text in segment_text:
        return True

    requirement_tokens = _semantic_tokens(requirement_text)
    segment_tokens = _semantic_tokens(segment_text)
    if not requirement_tokens:
        return False
    minimum_overlap = 1 if len(requirement_tokens) == 1 else 2
    return len(requirement_tokens.intersection(segment_tokens)) >= minimum_overlap


def _facts_matching_requirement(
    requirement: str,
    worker_results_payload: Dict[str, Any],
) -> List[dict]:
    matches = []
    for fact in _iter_retrieval_facts(worker_results_payload):
        if not is_report_fact(fact):
            continue
        if normalize_fact_status(fact.get("status", "found")) != "found":
            continue
        if requirement_evidence_state(requirement, [fact]) == REQUIREMENT_MATCHED:
            matches.append(fact)
    return matches


def _fact_ledger_contract_violations(
    decision: Dict[str, Any],
    worker_results_payload: Dict[str, Any],
) -> Dict[str, List[dict]]:
    """Find metrics called missing even though a matching found fact exists."""

    analysis_requirements = []
    for payload in _analysis_outputs_payload(worker_results_payload).values():
        if not isinstance(payload, dict):
            continue
        analysis_requirements.extend(
            _normalize_requirements_list(payload.get("requirements"), limit=0)
        )

    followup_requirements = _followup_requirement_items(
        list(decision.get("followups", []) or [])
    )
    missing_segments = _missing_claim_segments(decision.get("answer", ""))
    violations: Dict[str, List[dict]] = {}

    for requirement in _dedupe_keep_order(
        [*analysis_requirements, *followup_requirements]
    ):
        matching_facts = _facts_matching_requirement(
            requirement,
            worker_results_payload,
        )
        if not matching_facts:
            continue
        claimed_missing_in_answer = any(
            _missing_segment_mentions_requirement(segment, requirement)
            for segment in missing_segments
        )
        claimed_missing_in_followup = requirement in followup_requirements
        if claimed_missing_in_answer or claimed_missing_in_followup:
            violations[requirement] = matching_facts

    return violations


_PRESENTATION_VIOLATION_KINDS = frozenset(
    {
        "missing_aspect",
        "missing_bluf_summary",
        "empty_bluf_summary",
        "bluf_not_first",
        "summary_contains_financial_number",
        "summary_contains_formula",
        "boilerplate_evidence_lead_in",
        "internal_agent_label_exposed",
        "fixed_bottom_conclusion",
        "legacy_evidence_limit_section",
        "grounded_interpretation_contract",
    }
)


def _violation_kind(item: Any) -> str:
    return str(item).split(":", 1)[0]


def _missing_aspect_count(violations: List[str]) -> int:
    return sum(1 for item in violations if _violation_kind(item) == "missing_aspect")


def _score_synth_candidate(violations: List[str]) -> Tuple[int, int, int]:
    """Rank key for one candidate: lower is better, correctness first.

    A wrong or false-missing claim outranks any layout problem, so an answer that
    loses a section is still preferred over one that misstates the numbers.
    """

    critical = sum(
        1
        for item in violations
        if _violation_kind(item) not in _PRESENTATION_VIOLATION_KINDS
    )
    missing_aspect = _missing_aspect_count(violations)
    presentation = sum(
        1
        for item in violations
        if _violation_kind(item) in _PRESENTATION_VIOLATION_KINDS
        and _violation_kind(item) != "missing_aspect"
    )
    return (critical, missing_aspect, presentation)


def _model_decision_is_usable(decision: Dict[str, Any]) -> bool:
    """Return whether a model response is a usable public SynthDecision."""

    status = str(decision.get("status", "") or "").strip().lower()
    answer = str(decision.get("answer", "") or "").strip()
    followups = list(decision.get("followups", []) or [])
    if status == "answer":
        return bool(answer)
    if status == "need_more":
        return bool(answer or followups)
    return False


def _collect_synth_quality_violations(
    state: dict,
    payload: SynthPayload,
    worker_results_payload: Dict[str, Any],
    decision: Dict[str, Any],
) -> List[str]:
    """Collect semantic diagnostics without changing the model's answer."""

    violations: List[str] = []
    if not _model_decision_is_usable(decision):
        violations.append("invalid_synth_decision")

    if _has_leading_evidence_boilerplate(decision.get("answer", "")):
        violations.append("boilerplate_evidence_lead_in")

    if (
        _response_mode_from_state(state) == "grounded_interpretation"
        and not _grounded_interpretation_contract_passes(
            decision,
            state=state,
            worker_results_payload=worker_results_payload,
        )
    ):
        violations.append("grounded_interpretation_contract")

    violations.extend(
        _hard_analysis_contract_violations(
            state,
            decision,
            worker_results_payload,
        )
    )
    violations.extend(
        f"fact_ledger:false_missing:{requirement}"
        for requirement in _fact_ledger_contract_violations(
            decision,
            worker_results_payload,
        )
    )
    violations.extend(
        financial_answer_violations(
            decision.get("answer", ""),
            worker_results_payload,
            user_query=payload.get("user_query", ""),
            plan_context=payload.get("plan_json"),
        )
    )
    return list(dict.fromkeys(str(item) for item in violations if str(item)))


def _quality_repair_guidance(violations: List[str]) -> str:
    """Translate diagnostic codes into concrete model-editing instructions."""

    guidance: List[str] = []
    if "unexpected_compact_vnd_unit" in violations:
        guidance.append(
            "giữ số VND đầy đủ theo fact, không tự đổi sang triệu/tỷ"
        )
    if any(item.startswith("profitability_ratio_basis:") for item in violations):
        guidance.append(
            "ROA/ROE năm dùng mẫu số bình quân đầu-cuối kỳ và phải nói rõ basis; "
            "nếu dùng số cuối kỳ thì ghi rõ là cách tính đơn giản hóa"
        )
    if "cashflow_quality:cfo_pat_negative_profit_not_conversion_measure" in violations:
        guidance.append(
            "khi PAT âm, bỏ tỷ lệ CFO/PAT và mọi nhãn chất lượng lợi nhuận "
            "tốt/kém dựa trên tỷ lệ đó; chỉ mô tả CFO dương/âm so với PAT và "
            "chênh lệch tuyệt đối"
        )
    if any(
        item.startswith("cashflow_comparison:cfi_amount_expected_")
        or item.startswith("cashflow_comparison:cff_amount_expected_")
        for item in violations
    ):
        guidance.append(
            "so sánh CFI/CFF theo giá trị có dấu: một số âm ít âm hơn là subtotal "
            "tăng; chỉ nói 'mức âm giảm' khi đang mô tả độ lớn dòng tiền ra"
        )
    if "note_ref_coverage:linked_detail_required" in violations:
        guidance.append(
            "chọn ít nhất một note_detail liên quan và viết 'Theo Thuyết minh "
            "[note_ref], trong [khoản mục cha] có ...'; dùng để giải thích cơ cấu "
            "hoặc rủi ro, tuyệt đối không cộng vào số dòng cha"
        )
    if "profitability_level:explicit_benchmark_required" in violations:
        guidance.append(
            "bỏ nhãn cao/thấp, mạnh/yếu, tốt/kém nếu ngay câu đó không có "
            "benchmark kỳ trước, mục tiêu hoặc ngành"
        )
    if any(
        item in violations
        for item in (
            "summary_contains_financial_number",
            "summary_contains_formula",
        )
    ):
        guidance.append(
            "viết Tóm tắt đánh giá chỉ bằng nhận định định tính; chuyển mọi KPI, "
            "số tiền, tỷ lệ, công thức và nguồn xuống section khía cạnh tương ứng; "
            "được giữ năm/quý để nêu bối cảnh"
        )
    if "boilerplate_evidence_lead_in" in violations:
        guidance.append(
            "bỏ câu dẫn 'Dựa trên số liệu hiện có' và bắt đầu trực tiếp bằng nội dung"
        )
    if not guidance:
        return ""
    return " Hướng sửa cụ thể: " + "; ".join(guidance) + "."


def _retry_synth_quality_once(
    state: dict,
    payload: SynthPayload,
    worker_results_payload: Dict[str, Any],
    initial_decision: Dict[str, Any],
) -> Tuple[
    Dict[str, Any],
    Optional[SynthUsage],
    str,
    List[str],
    List[str],
    bool,
    str,
    Dict[str, Any],
]:
    """Run up to two model repairs, then return the best model-authored answer."""

    initial_violations = _collect_synth_quality_violations(
        state,
        payload,
        worker_results_payload,
        initial_decision,
    )
    if not initial_violations:
        return (
            initial_decision,
            None,
            "",
            [],
            [],
            False,
            "initial",
            {
                "repair_rounds": 0,
                "candidate_scores": {},
                "selection_reason": "no_violations",
                "degraded_dimensions": [],
            },
        )

    required_aspects = [
        _HARD_ANALYSIS_ASPECT_LABELS[agent_name]
        for agent_name in _hard_analysis_agents(worker_results_payload)
    ]
    retry_payload = dict(payload)
    retry_payload["last_agent_response"] = str(
        initial_decision.get("answer", "") or ""
    )
    retry_payload["system_instruction"] = (
        str(payload.get("system_instruction", "") or "")
        + "\n\nQUALITY REVIEW — ONE MODEL-OWNED REPAIR. "
        "Bản trước có các violation sau: "
        + "; ".join(initial_violations)
        + _quality_repair_guidance(initial_violations)
        + ". Hãy tự viết lại toàn bộ SynthDecision dựa trên facts và các "
        "lookup/calculation context trong worker_results_json. Giữ nguyên "
        "mọi fact, kỳ, dấu, đơn vị, công thức, basis và provenance hợp lệ; "
        "không gọi dữ liệu đã có là thiếu. Với dòng tiền, kiểm tra riêng dấu "
        "CFO/CFI/CFF và tổng đại số trước khi nói tiền tăng/giảm. Xóa mọi "
        "nhãn nội bộ agent_*, canonical_*, field/schema/validator khỏi answer. "
        "Các heading phải đúng nguyên văn, không có hậu tố trong ngoặc. Khi "
        "viết Tóm tắt đánh giá, chỉ nêu nhận định định tính, không đưa KPI, "
        "số tiền, tỷ lệ, công thức hoặc nguồn vào phần này; được giữ năm/quý "
        "để nêu bối cảnh và chuyển toàn bộ chi tiết định lượng xuống section "
        "khía cạnh tương ứng. Bắt đầu trực tiếp bằng nội dung, không dùng câu "
        "dẫn như 'Dựa trên số liệu hiện có'. Khi "
        "có note_detail liên kết khoản mục đang dùng, diễn giải chọn lọc theo "
        "quan hệ 'Theo Thuyết minh [note_ref], trong [khoản mục cha] có ...'; "
        "không cộng cấu phần note vào khoản mục cha. Giữ các heading/aspect cần thiết"
        + (": " + ", ".join(required_aspects) if required_aspects else "")
        + ". Không thêm diagnostics hay tên violation vào answer. Chỉ xuất "
        "JSON SynthDecision; nội dung cuối cùng hoàn toàn do bạn tạo."
    )
    repaired, usage, mode = _invoke_synth(retry_payload)

    # ---- Candidate pipeline: initial -> general_repair -> targeted_repair ----
    # Every candidate is scored BEFORE any acceptance decision.  Selection is
    # correctness-first: a missing section is preferable to a false-missing or
    # wrong-number claim.  All candidates are model-authored; code never composes
    # prose, it only chooses between whole model answers.
    candidates: List[Tuple[str, Dict[str, Any], List[str]]] = []
    if _model_decision_is_usable(initial_decision):
        candidates.append(("initial", initial_decision, initial_violations))

    general_violations: List[str] = []
    if _model_decision_is_usable(repaired):
        general_violations = _collect_synth_quality_violations(
            state,
            payload,
            worker_results_payload,
            repaired,
        )
        candidates.append(("general_repair", repaired, general_violations))

    repair_rounds = 1
    # One targeted repair only when the general rewrite fixed content but lost
    # required sections: ask the model to keep its corrected figures AND restore
    # every section, instead of forcing a choice between correctness and coverage.
    aspect_regression = bool(
        general_violations
        and _model_decision_is_usable(initial_decision)
        and _missing_aspect_count(general_violations)
        > _missing_aspect_count(initial_violations)
    )
    if aspect_regression:
        dropped = sorted(
            {
                str(item).split(":", 1)[1]
                for item in general_violations
                if str(item).startswith("missing_aspect:")
            }
        )
        dropped_labels = [
            _HARD_ANALYSIS_ASPECT_LABELS.get(agent_name, agent_name)
            for agent_name in dropped
        ]
        targeted_payload = dict(payload)
        targeted_payload["last_agent_response"] = str(repaired.get("answer", "") or "")
        targeted_payload["system_instruction"] = (
            str(payload.get("system_instruction", "") or "")
            + "\n\nTARGETED REPAIR — GIỮ SỐ ĐÃ SỬA, KHÔI PHỤC ĐỦ SECTION. "
            "Bản sửa gần nhất (trong last_agent_response) đã chỉnh đúng số liệu "
            "nhưng làm RƠI các phần bắt buộc: "
            + ", ".join(dropped_labels)
            + ". Hãy giữ NGUYÊN mọi số liệu, công thức, kỳ, dấu, đơn vị và basis "
            "đã được sửa trong bản đó, đồng thời VIẾT LẠI ĐỦ các phần bị thiếu "
            "dựa trên facts và analysis_outputs trong worker_results_json. "
            "Bản trước khi sửa (để tham chiếu nội dung các phần bị rơi):\n"
            + str(initial_decision.get("answer", "") or "")
            + "\nTUYỆT ĐỐI không tái tạo các lỗi cũ: không nói dữ liệu đã có là "
            "thiếu, không đổi số/dấu/kỳ, không lộ nhãn nội bộ agent_*. Giữ "
            "'Tóm tắt đánh giá' ở đầu và đủ các heading. Trong phần tóm tắt, "
            "chỉ giữ nhận định định tính, không KPI, số tiền, tỷ lệ, công thức "
            "hoặc nguồn; được giữ năm/quý làm bối cảnh. Chuyển chi tiết định "
            "lượng xuống section tương ứng và không dùng câu dẫn 'Dựa trên số "
            "liệu hiện có'"
            + (": " + ", ".join(required_aspects) if required_aspects else "")
            + ". Chỉ xuất JSON SynthDecision."
        )
        targeted, targeted_usage, targeted_mode = _invoke_synth(targeted_payload)
        repair_rounds = 2
        usage = merge_usage_metadata(usage, targeted_usage) or usage
        mode = targeted_mode or mode
        if _model_decision_is_usable(targeted):
            candidates.append(
                (
                    "targeted_repair",
                    targeted,
                    _collect_synth_quality_violations(
                        state,
                        payload,
                        worker_results_payload,
                        targeted,
                    ),
                )
            )

    candidate_scores = {
        name: list(_score_synth_candidate(violations))
        for name, _decision, violations in candidates
    }

    if candidates:
        # Lexicographic score (critical factual, missing aspect, presentation);
        # ties go to the newest candidate, which already absorbed earlier fixes.
        best_index, (accepted_candidate, accepted, remaining_violations) = min(
            enumerate(candidates),
            key=lambda item: (_score_synth_candidate(item[1][2]), -item[0]),
        )
        selection_reason = "correctness_first_lexicographic"
        if accepted_candidate == "initial" and not _model_decision_is_usable(repaired):
            # Preserve the diagnostic that a repair ran but produced nothing usable.
            accepted_candidate = "initial_after_invalid_repair"
        degraded_dimensions = [
            dimension
            for dimension, count in zip(
                ("critical_factual", "missing_aspect", "presentation"),
                _score_synth_candidate(remaining_violations),
            )
            if count
        ]
    else:
        accepted = repaired
        accepted_candidate = "initial_after_invalid_repair"
        remaining_violations = _collect_synth_quality_violations(
            state,
            payload,
            worker_results_payload,
            accepted,
        )
        selection_reason = "no_usable_candidate"
        degraded_dimensions = ["invalid_model_output"]

    diagnostics = {
        "repair_rounds": repair_rounds,
        "candidate_scores": candidate_scores,
        "selection_reason": selection_reason,
        "degraded_dimensions": degraded_dimensions,
    }
    return (
        accepted,
        usage,
        mode,
        initial_violations,
        remaining_violations,
        True,
        accepted_candidate,
        diagnostics,
    )


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
    return sum(1 for _fact in _iter_retrieval_facts(worker_results))


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

    (
        payload_worker_results,
        normalize_logs,
        context_mode,
        facts_n,
        requirements_n,
    ) = _prepare_synth_inputs(state)
    payload = _build_payload(state, profile, payload_worker_results)

    typed_calculation = _typed_decimal_calculation(
        state,
        payload_worker_results,
    )
    evidence_ledger = dict(state.get("evidence_ledger", {}) or {})
    if typed_calculation:
        calculation_ledger = _calculation_evidence_ledger(typed_calculation)
        evidence_ledger = {
            "schema_version": 1,
            "entries": [
                *list(evidence_ledger.get("entries", []) or []),
                *(calculation_ledger.get("entries", []) or []),
            ],
        }

    initial_decision, usage, invoke_mode = _invoke_synth(payload)
    (
        decision,
        retry_usage,
        retry_mode,
        initial_violations,
        remaining_violations,
        retry_attempted,
        accepted_candidate,
        quality_diagnostics,
    ) = _retry_synth_quality_once(
        state,
        payload,
        payload_worker_results,
        initial_decision,
    )
    usage = merge_usage_metadata(usage, retry_usage) or None

    trace.append(
        make_log(
            state,
            "synth:quality_check",
            initial_violations=initial_violations,
            retry_attempted=retry_attempted,
            accepted_candidate=accepted_candidate,
            remaining_violations=remaining_violations,
            repair_rounds=quality_diagnostics.get("repair_rounds", 0),
            candidate_scores=quality_diagnostics.get("candidate_scores", {}),
            selection_reason=quality_diagnostics.get("selection_reason", ""),
            degraded_dimensions=quality_diagnostics.get("degraded_dimensions", []),
        )
    )

    if invoke_mode != "structured" or (
        retry_attempted and retry_mode and retry_mode != "structured"
    ):
        fallback_log = make_debug_log(
            state,
            "synth:structured_output_fallback",
            initial_mode=invoke_mode,
            retry_mode=retry_mode if retry_attempted else "",
        )
        if fallback_log:
            trace.append(fallback_log)

    decision, followup_sanitize_log = _sanitize_followups(state, decision)
    if followup_sanitize_log:
        trace.append(followup_sanitize_log)

    if context_mode == "analysis":
        decision, analysis_followup_filter_log = (
            _keep_only_new_analysis_agent_followups(
                state,
                decision,
                payload_worker_results,
            )
        )
        if analysis_followup_filter_log:
            trace.append(analysis_followup_filter_log)

    followup_updates: Dict[str, Any] = {}
    dispatchable_followups = list(decision.get("followups", []) or [])
    current_round = int(state.get("followup_rounds", 0) or 0)
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
    elif (
        str(decision.get("status", "") or "").strip().lower() == "need_more"
        and dispatchable_followups
    ):
        trace.append(
            make_log(
                state,
                "synth:followup_limit_reached",
                current_round=current_round,
                max_rounds=MAX_FOLLOWUP_ROUNDS,
                followups_n=len(dispatchable_followups),
            )
        )

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
        "followup_rounds": followup_updates.get(
            "followup_rounds",
            state.get("followup_rounds", 0),
        ),
        "planner_plan": followup_updates.get(
            "planner_plan",
            state.get("planner_plan", {}),
        ),
        "pending_analysis_targets": followup_updates.get(
            "pending_analysis_targets",
            state.get("pending_analysis_targets", []),
        ),
        "analysis_dispatch_targets": followup_updates.get(
            "analysis_dispatch_targets",
            state.get("analysis_dispatch_targets", []),
        ),
        "dispatch_phase": followup_updates.get(
            "dispatch_phase",
            state.get("dispatch_phase", ""),
        ),
        "evidence_ledger": evidence_ledger,
        "trace": [*trace, *normalize_logs, done_log],
    }
