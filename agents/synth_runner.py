from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple, TypedDict

from pydantic import ValidationError

from agents.profiles import AGENT_PROFILES
from agents.prompts import PROMPT_TEMPLATE
from graph.logger import debug_enabled, make_debug_log, make_log
from llm.client import llm
from schemas.agent_outputs import SynthDecision


synth_chain = PROMPT_TEMPLATE | llm.with_structured_output(SynthDecision)

DEFAULT_DECISION = {
    "status": "error",
    "answer": "Chưa đủ dữ liệu để trả lời.",
    "missing": [],
    "followups": [],
}

MAX_SYNTH_FACTS_PER_TABLE = 6
MAX_SYNTH_WEB_CHARS = 1200


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


def _normalize_table_name(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


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
        table = str(raw.get("table", "")).strip()
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

    table = str(parsed.get("table", "")).strip()
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
        should_log = emit_debug_logs or kind != "structured" or item["action_pending"] or len(item["facts"]) == 0
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
    decision["missing"] = decision.get("missing") or []
    decision["followups"] = decision.get("followups") or []
    return decision


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
        "plan_json": _safe_json_dumps(state.get("plan", {})),
        "worker_results_json": _safe_json_dumps(normalized_worker_results),
        "allowed_keywords_json": "{}",
        "web_summary": state.get("synth_web_summary", state.get("web_summary", "")) or "",
        "last_agent_response": state.get("last_agent_response", "") or "",
        "tool_observations": "",
    }


def _invoke_synth(payload: SynthPayload) -> Dict[str, Any]:
    try:
        return _coerce_decision(synth_chain.invoke(payload))
    except ValidationError as exc:
        return {
            "status": "error",
            "answer": f"Synth trả về sai schema: {exc}",
            "missing": [],
            "followups": [],
        }
    except Exception as exc:
        return {
            "status": "error",
            "answer": f"Lỗi khi chạy synth: {exc}",
            "missing": [],
            "followups": [],
        }


def run_synth(state: dict) -> dict:
    profile = AGENT_PROFILES["agent_synth"]
    raw_worker_results = state.get("synth_context", state.get("worker_results", {})) or {}
    trace = []

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
    decision = _invoke_synth(payload)

    done_log = make_log(
        state,
        "synth:done",
        status=decision.get("status", ""),
        followups_n=len(decision.get("followups", []) or []),
        facts_n=len(facts),
        answer_preview=(decision.get("answer", "") or "")[:200],
    )

    return {
        "synth_decision": decision,
        "followup_requests": decision.get("followups", []) or [],
        "last_agent_response": decision.get("answer", ""),
        "normalized_worker_results": normalized_worker_results,
        "trace": [*trace, *normalize_logs, done_log],
    }
