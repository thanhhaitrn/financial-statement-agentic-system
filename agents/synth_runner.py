from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from pydantic import ValidationError

from agents.planner_hints import infer_table_keywords, infer_table_query_hints
from agents.profiles import AGENT_PROFILES
from agents.prompts import PROMPT_TEMPLATE
from config.allowed_keywords import build_allowed_keywords_payload
from graph.logger import debug_enabled, make_debug_log, make_log
from llm.invoke import extract_usage_metadata, invoke_prompt
from schemas.keyword_guard import repair_keywords, validate_keywords
from schemas.agent_outputs import SynthDecision
from schemas.table_names import TABLE_BS, TABLE_CF, TABLE_IS

DEFAULT_DECISION = {
    "status": "error",
    "answer": "Chưa đủ dữ liệu để trả lời.",
    "missing": [],
    "followups": [],
}

MAX_SYNTH_FACTS_PER_TABLE = 6
MAX_SYNTH_WEB_CHARS = 1200
MAX_FOLLOWUP_KEYWORDS = 3

TABLE_TO_AGENT = {
    TABLE_BS: "agent_bs",
    TABLE_IS: "agent_is",
    TABLE_CF: "agent_cf",
}
AGENT_TO_TABLE = {agent: table for table, agent in TABLE_TO_AGENT.items()}


class NormalizedFact(TypedDict):
    item_name: str
    time_hint: str
    value: Any
    source: str
    table: str


class NormalizedWorkerResult(TypedDict):
    agent: str
    table: str
    facts: List[NormalizedFact]
    raw_text: str
    action_pending: bool


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


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


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
        '- Khong markdown, khong ```json, khong van ban ngoai JSON.\n'
        '- status chi duoc la \"answer\" hoac \"need_more\".\n'
    )


def _normalize_table_name(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _planned_target_keywords_by_table(state: dict) -> Dict[str, List[str]]:
    planned: Dict[str, List[str]] = {}
    worker_plan = state.get("worker_plan", {}) or {}
    planner_plan = state.get("planner_plan", {}) or {}

    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue
        table = str(target.get("table", "") or "").strip()
        if not table:
            continue
        planned.setdefault(table, [])
        planned[table].extend(
            [
                str(keyword).strip()
                for keyword in (target.get("keywords", []) or [])
                if str(keyword).strip()
            ]
        )

    for axis in (planner_plan.get("analysis_axes", []) or []):
        if not isinstance(axis, dict):
            continue
        for table in (axis.get("tables", []) or []):
            text = str(table or "").strip()
            if text and text not in planned:
                planned[text] = []

    return {
        table: _dedupe_keep_order(keywords)
        for table, keywords in planned.items()
    }


def _followup_requirements_for_table(
    state: dict,
    table: str,
    *,
    preferred: Optional[List[str]] = None,
) -> List[str]:
    table_name = str(table or "").strip()
    planner_plan = state.get("planner_plan", {}) or {}
    analysis_axes = planner_plan.get("analysis_axes", []) or []
    user_query = str(state.get("user_query", "") or "").strip()

    requirements = _dedupe_keep_order(preferred or [])
    if requirements:
        return requirements[:MAX_FOLLOWUP_KEYWORDS]

    query_hints = infer_table_query_hints(table_name, user_query, analysis_axes)
    requirements = _dedupe_keep_order(query_hints)
    if requirements:
        return requirements[:MAX_FOLLOWUP_KEYWORDS]

    planned_keywords = _planned_target_keywords_by_table(state).get(table_name, [])
    if planned_keywords:
        return planned_keywords[:MAX_FOLLOWUP_KEYWORDS]

    if user_query:
        return [user_query]

    return []


def _facts_count_by_table(
    normalized_worker_results: Dict[str, NormalizedWorkerResult],
) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for item in (normalized_worker_results or {}).values():
        table = str(item.get("table", "") or "").strip()
        if not table:
            continue
        counts[table] = counts.get(table, 0) + len(item.get("facts", []) or [])
    return counts


def _attempted_queries_by_agent_table(state: dict) -> Dict[Tuple[str, str], set[str]]:
    attempted: Dict[Tuple[str, str], set[str]] = {}

    for item in (state.get("tool_results", []) or []):
        if str(item.get("tool", "") or "").strip() != "get_related_info":
            continue

        agent = str(item.get("agent", "") or "").strip()
        args = item.get("args", {}) or {}
        table = str(args.get("table", "") or "").strip() or AGENT_TO_TABLE.get(agent, "")
        query = str(args.get("query", "") or "").strip()

        if not agent or not table or not query:
            continue

        key = (agent, table)
        attempted.setdefault(key, set()).add(_normalize_text(query))

    return attempted


def _suggest_followup_keywords(
    table: str,
    state: dict,
    *,
    preferred: Optional[List[str]] = None,
) -> List[str]:
    table_name = str(table or "").strip()
    if table_name not in TABLE_TO_AGENT:
        return _dedupe_keep_order(preferred or [])[:MAX_FOLLOWUP_KEYWORDS]

    planner_plan = state.get("planner_plan", {}) or {}
    analysis_axes = planner_plan.get("analysis_axes", []) or []
    user_query = str(state.get("user_query", "") or "").strip()
    planned_keywords = _planned_target_keywords_by_table(state).get(table_name, [])
    planner_hints = infer_table_keywords(table_name, user_query, analysis_axes)
    repaired_user_query, _ = repair_keywords(table_name, [user_query])

    candidates = _dedupe_keep_order(
        list(preferred or [])
        + planned_keywords
        + planner_hints
        + repaired_user_query
    )

    valid_keywords, details = validate_keywords(
        table_name,
        candidates,
        fuzzy=True,
        cutoff=0.88,
    )
    for item in details or []:
        suggestion = item.get("suggested")
        if suggestion and suggestion not in valid_keywords:
            valid_keywords.append(suggestion)

    return _dedupe_keep_order(valid_keywords)[:MAX_FOLLOWUP_KEYWORDS]


def _coalesce_followups(followups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []

    for item in followups or []:
        agent = str(item.get("agent", "") or "").strip()
        if not agent:
            continue

        if agent not in merged:
            merged[agent] = {
                "agent": agent,
                "table": str(item.get("table", "") or "").strip(),
                "requirements": _dedupe_keep_order(item.get("requirements", []) or []),
                "reason": str(item.get("reason", "") or "").strip(),
            }
            order.append(agent)
            continue

        current = merged[agent]
        if not current.get("table") and item.get("table"):
            current["table"] = str(item.get("table", "") or "").strip()
        current["requirements"] = _dedupe_keep_order(
            list(current.get("requirements", []) or [])
            + list(item.get("requirements", []) or [])
        )[:MAX_FOLLOWUP_KEYWORDS]
        if not current.get("reason") and item.get("reason"):
            current["reason"] = str(item.get("reason", "") or "").strip()

    return [merged[agent] for agent in order]


def _sanitize_followups(
    state: dict,
    decision: Dict[str, Any],
    normalized_worker_results: Dict[str, NormalizedWorkerResult],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    if str(decision.get("status", "") or "").strip().lower() != "need_more":
        return decision, None

    planned_targets = _planned_target_keywords_by_table(state)
    if not planned_targets:
        return decision, None

    raw_followups = decision.get("followups", []) or []
    facts_n_by_table = _facts_count_by_table(normalized_worker_results)
    empty_tables = [
        table
        for table in planned_targets
        if facts_n_by_table.get(table, 0) == 0
    ]

    kept_followups: List[Dict[str, Any]] = []
    dropped_samples: List[Dict[str, Any]] = []
    added_samples: List[Dict[str, Any]] = []
    seen_tables = set()

    for raw in raw_followups:
        if not isinstance(raw, dict):
            continue

        table = str(raw.get("table", "") or "").strip()
        agent = str(raw.get("agent", "") or "").strip()

        if not table:
            table = AGENT_TO_TABLE.get(agent, "")
        if table in TABLE_TO_AGENT:
            agent = TABLE_TO_AGENT[table]

        if not agent or (agent != "agent_web" and table not in planned_targets):
            if len(dropped_samples) < 3:
                dropped_samples.append(
                    {
                        "agent": agent,
                        "table": table,
                        "reason": "followup_ngoai_ke_hoach",
                    }
                )
            continue

        if agent == "agent_web":
            kept_followups.append(raw)
            continue

        requirements = _followup_requirements_for_table(
            state,
            table,
            preferred=list(raw.get("requirements", []) or []),
        )
        if not requirements:
            if len(dropped_samples) < 3:
                dropped_samples.append(
                    {
                        "agent": agent,
                        "table": table,
                        "reason": "followup_khong_ro_yeu_cau_du_lieu",
                    }
                )
            continue

        kept_followups.append(
            {
                "agent": agent,
                "table": table,
                "requirements": requirements[:MAX_FOLLOWUP_KEYWORDS],
                "reason": str(raw.get("reason", "") or "").strip(),
            }
        )
        seen_tables.add(table)

    for table in empty_tables:
        if table in seen_tables:
            continue

        agent = TABLE_TO_AGENT.get(table, "")
        requirements = _followup_requirements_for_table(
            state,
            table,
            preferred=planned_targets.get(table, []),
        )
        if not agent or not requirements:
            continue

        followup = {
            "agent": agent,
            "table": table,
            "requirements": requirements[:MAX_FOLLOWUP_KEYWORDS],
            "reason": "Bang nay dang thieu du lieu can thiet de hoan tat cau tra loi.",
        }
        kept_followups.append(followup)
        seen_tables.add(table)
        if len(added_samples) < 3:
            added_samples.append(followup)

    normalized_followups = _coalesce_followups(kept_followups)
    if normalized_followups == raw_followups:
        return decision, None

    updated = dict(decision)
    updated["followups"] = normalized_followups
    return updated, make_debug_log(
        state,
        "synth:followups_sanitized",
        raw_n=len(raw_followups),
        kept_n=len(normalized_followups),
        dropped_samples=dropped_samples,
        added_samples=added_samples,
    )


def _format_fact_brief(fact: NormalizedFact, fallback_item_name: str = "") -> str:
    item_name = (
        str(fact.get("item_name", "") or "").strip()
        or str(fallback_item_name or "").strip()
    )
    value = str(fact.get("value", "") or "").strip()
    time_hint = str(fact.get("time_hint", "") or "").strip()

    if item_name and value and item_name != value:
        text = f"{item_name}: {value}"
    else:
        text = item_name or value

    if time_hint:
        text = f"{text} ({time_hint})"
    return text


def _capitalize_first(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    return value[0].upper() + value[1:]


def _pick_primary_fact(
    facts: List[NormalizedFact],
    planned_targets: Dict[str, List[str]],
) -> Tuple[Optional[NormalizedFact], str]:
    best_fact: Optional[NormalizedFact] = None
    best_label = ""
    best_score = -1.0

    for fact in facts or []:
        table = str(fact.get("table", "") or "").strip()
        fallback_label = next(iter(planned_targets.get(table, []) or []), "")
        label = str(fact.get("item_name", "") or "").strip() or fallback_label
        score = 0.0
        if label:
            score += 10.0
        if fallback_label and _normalize_text(label) == _normalize_text(fallback_label):
            score += 50.0
        if str(fact.get("time_hint", "") or "").strip():
            score += 5.0
        if str(fact.get("value", "") or "").strip():
            score += 5.0

        if score > best_score:
            best_fact = fact
            best_label = label
            best_score = score

    return best_fact, best_label


def _build_query_aware_heuristic_answer(
    state: dict,
    facts: List[NormalizedFact],
    planned_targets: Dict[str, List[str]],
) -> str:
    if not facts:
        return ""

    planner_plan = state.get("planner_plan", {}) or {}
    company = str(planner_plan.get("company", "") or "").strip()
    plan_time_hint = str(planner_plan.get("time_hint", "") or "").strip()
    primary_fact, primary_label = _pick_primary_fact(facts, planned_targets)

    if primary_fact is None:
        return ""

    value = str(primary_fact.get("value", "") or "").strip()
    fact_time_hint = str(primary_fact.get("time_hint", "") or "").strip()
    effective_time_hint = fact_time_hint or plan_time_hint

    if value:
        subject_parts = []
        if primary_label:
            subject_parts.append(primary_label)
        if company and _normalize_text(company) not in _normalize_text(" ".join(subject_parts)):
            subject_parts.append(f"của {company}")
        if effective_time_hint:
            subject_parts.append(effective_time_hint)
        subject = " ".join(part.strip() for part in subject_parts if str(part).strip())
        if subject:
            return f"{_capitalize_first(subject)} là {value}."

    fact_briefs = []
    for fact in facts[:6]:
        fallback_item_name = ""
        if not str(fact.get("item_name", "") or "").strip():
            fallback_item_name = next(
                iter(planned_targets.get(str(fact.get("table", "") or "").strip(), []) or []),
                "",
            )
        brief = _format_fact_brief(fact, fallback_item_name=fallback_item_name)
        if brief:
            fact_briefs.append(brief)

    if not fact_briefs:
        return ""

    return "Dữ liệu thu được là: " + "; ".join(fact_briefs) + "."


def _build_heuristic_synth_decision(
    state: dict,
    normalized_worker_results: Dict[str, NormalizedWorkerResult],
) -> Dict[str, Any]:
    planned_targets = _planned_target_keywords_by_table(state)
    facts_n_by_table = _facts_count_by_table(normalized_worker_results)
    missing: List[str] = []
    followups: List[Dict[str, Any]] = []

    for table, planned_keywords in planned_targets.items():
        if facts_n_by_table.get(table, 0) > 0:
            continue

        requirements = _followup_requirements_for_table(
            state,
            table,
            preferred=planned_keywords,
        )
        if requirements:
            missing.append(
                f"Thiếu dữ liệu từ {table}."
            )
            followups.append(
                {
                    "agent": TABLE_TO_AGENT.get(table, ""),
                    "table": table,
                    "requirements": requirements[:MAX_FOLLOWUP_KEYWORDS],
                    "reason": "Bang nay chua co du lieu de hoan tat cau tra loi.",
                }
            )
        else:
            missing.append(f"Thiếu dữ liệu từ {table}.")

    if missing or followups:
        return {
            "status": "need_more",
            "answer": "",
            "missing": _dedupe_keep_order(missing),
            "followups": _coalesce_followups(followups),
        }

    facts = _flatten_facts(normalized_worker_results)
    if facts:
        heuristic_answer = _build_query_aware_heuristic_answer(
            state,
            facts,
            planned_targets,
        )
        if heuristic_answer:
            return {
                "status": "answer",
                "answer": heuristic_answer,
                "missing": [],
                "followups": [],
            }

    return {
        "status": "need_more",
        "answer": "",
        "missing": ["Chưa thu thập được dữ liệu phù hợp để trả lời."],
        "followups": [],
    }


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
                return cleaned[start : index + 1]

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


def _is_action_text(text: str) -> bool:
    return (text or "").strip().upper().startswith("ACTION:")


def _empty_worker_result(agent_name: str = "", raw_text: str = "") -> NormalizedWorkerResult:
    return {
        "agent": agent_name,
        "table": "",
        "facts": [],
        "raw_text": raw_text,
        "action_pending": False,
    }


def _fallback_table_for_agent(agent_name: str = "") -> str:
    return str(AGENT_TO_TABLE.get(str(agent_name or "").strip(), "") or "").strip()


def _normalize_fact(raw_fact: Any, fallback_table: str = "") -> Optional[NormalizedFact]:
    if not isinstance(raw_fact, dict):
        return None

    item_name = str(raw_fact.get("item_name", "")).strip()
    time_hint = str(raw_fact.get("time_hint", "")).strip()
    value = raw_fact.get("value", "")
    source = str(raw_fact.get("source", "")).strip()
    table = str(raw_fact.get("table", fallback_table)).strip() or fallback_table

    if not item_name and value in ("", None):
        return None

    return {
        "item_name": item_name,
        "time_hint": time_hint,
        "value": value,
        "source": source,
        "table": table,
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
                "action_pending": False,
            },
            "structured",
        )

    text = _to_text(raw).strip()
    if not text:
        return _empty_worker_result(agent_name=agent_name), "fallback"

    if _is_action_text(text):
        result = _empty_worker_result(agent_name=agent_name, raw_text=text)
        result["action_pending"] = True
        return result, "action_pending"

    parsed = _try_parse_json(text)
    if parsed is None:
        return _empty_worker_result(agent_name=agent_name, raw_text=text), "fallback"

    table = str(parsed.get("table", "")).strip() or _fallback_table_for_agent(agent_name)
    return (
        {
            "agent": agent_name,
            "table": table,
            "facts": _normalize_facts(parsed.get("facts", []), fallback_table=table),
            "raw_text": text,
            "action_pending": False,
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
        should_log = emit_debug_logs or kind != "structured" or item["action_pending"]
        if should_log:
            entry = {
                "event": "synth:normalize_worker_result",
                "agent": agent_name,
                "kind": kind,
                "facts_n": len(item["facts"]),
                "action_pending": item["action_pending"],
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
            str(fact.get("time_hint", "")).strip(),
            str(fact.get("value", "")).strip(),
            str(fact.get("source", "")).strip(),
        )
        if key in seen:
            continue
        deduped.append(fact)
        seen.add(key)

    return deduped


def _cap_facts(facts: List[NormalizedFact], limit: int = MAX_SYNTH_FACTS_PER_TABLE) -> List[NormalizedFact]:
    if limit <= 0 or len(facts) <= limit:
        return facts
    return facts[:limit]


def _build_compact_worker_results(
    normalized_results: Dict[str, NormalizedWorkerResult],
) -> Tuple[Dict[str, CompactWorkerResult], Dict[str, int]]:
    compact: Dict[str, CompactWorkerResult] = {}
    stats = {
        "tables_n": 0,
        "facts_n_raw": 0,
        "facts_n_kept": 0,
        "tables_trimmed": 0,
    }

    for agent_name, item in (normalized_results or {}).items():
        table = str(item.get("table", "")).strip()
        facts = _dedupe_facts(item.get("facts", []))
        facts_raw_n = len(facts)
        facts = _cap_facts(facts)

        if facts_raw_n > len(facts):
            stats["tables_trimmed"] += 1

        compact[agent_name] = {
            "table": table,
            "facts": facts,
        }

        if table:
            stats["tables_n"] += 1
        stats["facts_n_raw"] += facts_raw_n
        stats["facts_n_kept"] += len(facts)

    return compact, stats


def _truncate_text(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit].rstrip()}..."


def _compact_web_summary(value: Any, limit: int = MAX_SYNTH_WEB_CHARS) -> Tuple[str, bool]:
    if value is None:
        return "", False

    data = value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "", False
        try:
            data = json.loads(stripped)
        except Exception:
            compact_text = _truncate_text(stripped, limit)
            return compact_text, len(compact_text) < len(stripped)

    if isinstance(data, dict):
        compact: Dict[str, Any] = {}
        trimmed = False
        if "table" in data:
            compact["table"] = data.get("table", "")
        if isinstance(data.get("facts"), list):
            facts = _dedupe_facts(
                _normalize_facts(
                    data.get("facts", []),
                    fallback_table=str(data.get("table", "")).strip(),
                )
            )
            compact_facts = _cap_facts(facts, limit=4)
            compact["facts"] = compact_facts
            if len(compact_facts) < len(facts):
                trimmed = True
        for key in ("answer", "summary", "notes", "error"):
            if key in data and data.get(key):
                raw_text = str(data.get(key, ""))
                compact_text = _truncate_text(raw_text, 240)
                compact[key] = compact_text
                if compact_text != " ".join(raw_text.split()):
                    trimmed = True
        text = _safe_json_dumps(compact or data)
        compact_text = _truncate_text(text, limit)
        if compact_text != text:
            trimmed = True
        return compact_text, trimmed

    text = _safe_json_dumps(data)
    compact_text = _truncate_text(text, limit)
    return compact_text, compact_text != text


def prepare_synth_context(state: dict) -> dict:
    raw_worker_results = state.get("worker_results", {}) or {}
    normalized_worker_results, normalize_logs = _normalize_all_worker_results(
        raw_worker_results,
        emit_debug_logs=debug_enabled(state),
    )
    compact_worker_results, stats = _build_compact_worker_results(normalized_worker_results)
    compact_web_summary, web_trimmed = _compact_web_summary(state.get("web_summary", ""))

    prepared_log = make_log(
        state,
        "synth_context:prepared",
        tables_n=stats["tables_n"],
        facts_n_raw=stats["facts_n_raw"],
        facts_n_kept=stats["facts_n_kept"],
        tables_trimmed=stats["tables_trimmed"],
        web_trimmed=web_trimmed,
    )

    return {
        "synth_context": compact_worker_results,
        "synth_web_summary": compact_web_summary,
        "trace": [*normalize_logs, prepared_log],
    }


def _coerce_decision(value: Any) -> Dict[str, Any]:
    data = value
    if hasattr(value, "model_dump"):
        data = value.model_dump()

    if not isinstance(data, dict):
        return dict(DEFAULT_DECISION)

    decision = dict(DEFAULT_DECISION)
    decision.update(data)
    decision["status"] = (
        str(decision.get("status", DEFAULT_DECISION["status"]) or "").strip().lower()
    )
    if not decision["status"]:
        decision["status"] = DEFAULT_DECISION["status"]
    decision["answer"] = str(decision.get("answer", DEFAULT_DECISION["answer"]) or "").strip()
    decision["missing"] = decision.get("missing") or []
    decision["followups"] = decision.get("followups") or []
    return decision


def _extract_synth_usage(raw: Any) -> Optional[SynthUsage]:
    usage = extract_usage_metadata(raw)
    return usage or None


def _build_payload(
    state: dict,
    profile: Dict[str, Any],
    normalized_worker_results: Dict[str, NormalizedWorkerResult],
) -> SynthPayload:
    return {
        "role": profile["role"],
        "tools_list": "",
        "system_instruction": profile["system_instruction"],
        "user_query": state.get("user_query", ""),
        "worker_query": "",
        "plan_json": _safe_json_dumps(state.get("worker_plan", {})),
        "worker_results_json": _safe_json_dumps(normalized_worker_results),
        "allowed_keywords_json": "{}",
        "web_summary": state.get("synth_web_summary", state.get("web_summary", "")) or "",
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
                    "missing": [],
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
                    "missing": [],
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
                "missing": [],
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
                "missing": [],
                "followups": [],
            },
            None,
            "structured",
        )


def run_synth(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_synth"]
    raw_worker_results = state.get("synth_context", state.get("worker_results", {})) or {}
    trace = []
    started_at = time.perf_counter()

    start_log = make_debug_log(
        state,
        "synth:start",
        followup_rounds=state.get("followup_rounds", 0),
    )
    if start_log:
        trace.append(start_log)

    normalized_worker_results: Dict[str, NormalizedWorkerResult] = {}
    normalize_logs: List[Dict[str, Any]] = []

    if state.get("synth_context") is not None:
        for agent_name, raw in (raw_worker_results or {}).items():
            item, _ = _normalize_worker_result(raw, agent_name=agent_name)
            normalized_worker_results[agent_name] = item
    else:
        normalized_worker_results, normalize_logs = _normalize_all_worker_results(
            raw_worker_results,
            emit_debug_logs=debug_enabled(state),
        )

    facts = _flatten_facts(normalized_worker_results)
    payload = _build_payload(state, profile, normalized_worker_results)
    decision, usage, invoke_mode = _invoke_synth(payload)
    heuristic_log = None

    heuristic_reason = ""
    if str(decision.get("status", "") or "").strip().lower() == "error":
        heuristic_reason = "error"
    elif (
        str(decision.get("status", "") or "").strip().lower() == "answer"
        and not str(decision.get("answer", "") or "").strip()
    ):
        heuristic_reason = "blank_answer"

    if heuristic_reason:
        heuristic_decision = _build_heuristic_synth_decision(
            state,
            normalized_worker_results,
        )
        heuristic_log = make_log(
            state,
            "synth:heuristic_fallback",
            reason=heuristic_reason,
            original_status=decision.get("status", ""),
            original_answer=(decision.get("answer", "") or "")[:200],
            fallback_status=heuristic_decision.get("status", ""),
            fallback_followups_n=len(heuristic_decision.get("followups", []) or []),
        )
        decision = heuristic_decision

    decision, followup_sanitize_log = _sanitize_followups(
        state,
        decision,
        normalized_worker_results,
    )

    if invoke_mode != "structured":
        fallback_log = make_debug_log(
            state,
            "synth:structured_output_fallback",
            mode=invoke_mode,
        )
        if fallback_log:
            trace.append(fallback_log)
    if heuristic_log:
        trace.append(heuristic_log)
    if followup_sanitize_log:
        trace.append(followup_sanitize_log)

    done_log = make_log(
        state,
        "synth:done",
        status=decision.get("status", ""),
        followups_n=len(decision.get("followups", []) or []),
        facts_n=len(facts),
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        answer_preview=(decision.get("answer", "") or "")[:200],
        **(usage or {}),
    )

    return {
        "synth_decision": decision,
        "followup_requests": decision.get("followups", []) or [],
        "last_agent_response": decision.get("answer", ""),
        "normalized_worker_results": normalized_worker_results,
        "trace": [*trace, *normalize_logs, done_log],
    }
