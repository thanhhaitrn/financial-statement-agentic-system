"""Small structured logging helpers used across graph nodes.

The default trace is intentionally operational: it keeps timings, counts, routing
metadata, and statuses while removing prompt/evidence payloads. ``debug_trace`` is
the explicit opt-in for payload detail, but credentials are redacted in every mode
and pathological values are still bounded so one event cannot dominate a run log.
"""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import json
import re
from typing import Any, Dict, Optional

from config.runtime_policy import DEFAULT_POLICY


_SENSITIVE_DATA_FIELDS = {
    "query",
    "user_query",
    "worker_query",
    "context",
    "context_preview",
    "answer",
    "answer_preview",
    "response",
    "response_preview",
    "parsed_result",
    "raw_result",
    "result",
    "results",
    "analysis_outputs",
    "worker_results",
    "evidence_pack",
    "documents",
    "metadatas",
    "messages",
    "worker_messages",
    "tool_observations",
    "tool_calls",
    "args",
    "arguments",
    "kwargs",
    "requirements",
    "followups",
    "planner_plan",
    "worker_plan",
    "analysis_plan",
    "prompt",
    "prompts",
    "args_preview",
    "facts",
    "fact_preview",
    "facts_preview",
    "payload",
    "raw_payload",
    "source_item",
    "value",
    "raw_value",
    "raw_values",
    "parsed_value",
    "parsed_values",
}
_SECRET_FIELDS = {
    "api_key",
    "ollama_api_key",
    "qdrant_api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "token",
    "client_secret",
    "private_key",
    "secret",
    "password",
}
_PATH_FIELDS = {
    "path",
    "file",
    "file_path",
    "source_path",
    "source_file",
    "document_path",
    "document_file",
    "filename",
}
_SOURCE_PATH_RE = re.compile(
    r"(?:^[/~.]|[\\/]|\.(?:csv|docx?|jsonl?|md|pdf|txt|xlsx?)$|^[a-z]+://)",
    re.IGNORECASE,
)

_DEFAULT_MAX_STRING_CHARS = 384
_DEBUG_MAX_STRING_CHARS = 4096
_DEFAULT_MAX_LIST_ITEMS = 12
_DEBUG_MAX_LIST_ITEMS = 64
_DEFAULT_MAX_DICT_ITEMS = 32
_DEBUG_MAX_DICT_ITEMS = 128
_DEFAULT_MAX_DEPTH = 5
_DEBUG_MAX_DEPTH = 8
_DEFAULT_MAX_EVENT_BYTES = DEFAULT_POLICY.observability.max_event_bytes
_DEBUG_MAX_EVENT_BYTES = DEFAULT_POLICY.observability.debug_max_event_bytes

_CORE_EVENT_FIELDS = {"event", "timestamp", "run_id"}
_OPERATIONAL_FIELDS = {
    "agent",
    "agent_name",
    "cache_key",
    "debug",
    "empty",
    "error_type",
    "mode",
    "phase",
    "reason",
    "retrieval_status",
    "round",
    "scope",
    "source",
    "status",
    "strict_table",
    "table",
    "tool",
    "tool_call_id",
}


def debug_enabled(state: dict) -> bool:
    return bool((state or {}).get("debug_trace", False))


def _normalized_key(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def _is_secret_field(key: Any) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SECRET_FIELDS or normalized.endswith(
        (
            "_api_key",
            "_access_token",
            "_refresh_token",
            "_token",
            "_password",
            "_private_key",
            "_secret",
        )
    )


def _is_sensitive_data_field(key: Any) -> bool:
    normalized = _normalized_key(key)
    return (
        normalized in _SENSITIVE_DATA_FIELDS
        or normalized == "preview"
        or normalized.endswith(("_query", "_queries"))
        or normalized.endswith("_preview")
        or normalized.endswith("_payload")
        or normalized.endswith(("_result", "_results"))
        or normalized.endswith("_facts")
        or normalized.endswith(("_answer", "_response", "_context"))
        or normalized.endswith(("_value", "_values"))
        or normalized.endswith(("_prompt", "_messages", "_observations"))
        or normalized.endswith(("_requirements", "_args", "_calls", "_plan"))
        or normalized.endswith("_raw_value")
    )


def _looks_like_source_path(key: Any, value: Any) -> bool:
    normalized = _normalized_key(key)
    if normalized in _PATH_FIELDS or normalized.endswith(
        ("_path", "_file", "_filename")
    ):
        return True
    return (
        normalized == "source"
        and isinstance(value, str)
        and bool(_SOURCE_PATH_RE.search(value.strip()))
    )


def _is_operational_field(key: Any) -> bool:
    normalized = _normalized_key(key)
    return (
        normalized in _OPERATIONAL_FIELDS
        or normalized.endswith(
            (
                "_count",
                "_duration",
                "_len",
                "_ms",
                "_n",
                "_status",
                "_redacted",
            )
        )
    )


def _has_payload_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, (Mapping, list, tuple, set, frozenset)):
        return bool(value)
    return True


def _truncate_string(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    suffix = "…<truncated>"
    if limit <= len(suffix):
        return suffix[:limit]
    return f"{value[: limit - len(suffix)]}{suffix}"


def _cap_mapping_items(value: Dict[Any, Any], limit: int) -> Dict[Any, Any]:
    if len(value) <= limit:
        return value

    keys = list(value)
    priority = [key for key in keys if _is_operational_field(key)]
    selected = priority[:limit]
    if len(selected) < limit:
        selected_set = set(selected)
        selected.extend(key for key in keys if key not in selected_set)
        selected = selected[:limit]

    compact = {key: value[key] for key in selected}
    compact["_truncated_items"] = len(value) - len(selected)
    return compact


def _sanitize_value(
    value: Any,
    *,
    debug: bool,
    depth: int,
    seen: set[int],
) -> Any:
    max_depth = _DEBUG_MAX_DEPTH if debug else _DEFAULT_MAX_DEPTH
    if depth > max_depth:
        return "<max-depth>"

    if isinstance(value, str):
        limit = _DEBUG_MAX_STRING_CHARS if debug else _DEFAULT_MAX_STRING_CHARS
        return _truncate_string(value, limit)
    if value is None or isinstance(value, (bool, int, float)):
        return value

    is_container = isinstance(value, (Mapping, list, tuple, set, frozenset))
    if is_container:
        identity = id(value)
        if identity in seen:
            return "<cycle>"
        seen.add(identity)

    try:
        if isinstance(value, Mapping):
            sanitized: Dict[Any, Any] = {}
            for key, item in value.items():
                if _is_secret_field(key):
                    sanitized[key] = "<redacted>"
                    continue
                if not debug and (
                    _is_sensitive_data_field(key) or _looks_like_source_path(key, item)
                ):
                    if _has_payload_content(item):
                        sanitized[f"{key}_redacted"] = True
                    continue
                sanitized[key] = _sanitize_value(
                    item,
                    debug=debug,
                    depth=depth + 1,
                    seen=seen,
                )
            limit = _DEBUG_MAX_DICT_ITEMS if debug else _DEFAULT_MAX_DICT_ITEMS
            return _cap_mapping_items(sanitized, limit)

        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(value)
            if isinstance(value, (set, frozenset)):
                items.sort(key=repr)
            limit = _DEBUG_MAX_LIST_ITEMS if debug else _DEFAULT_MAX_LIST_ITEMS
            kept = [
                _sanitize_value(item, debug=debug, depth=depth + 1, seen=seen)
                for item in items[:limit]
            ]
            if len(items) > limit:
                kept.append({"_truncated_items": len(items) - limit})
            return kept

        return value
    finally:
        if is_container:
            seen.discard(id(value))


def _json_bytes(value: Any) -> int:
    try:
        return len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        return len(str(value).encode("utf-8", errors="replace"))


def _field_summary(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {"_truncated": True, "items_n": len(value)}
    if isinstance(value, list):
        return {"_truncated": True, "items_n": len(value)}
    if isinstance(value, str):
        return _truncate_string(value, 96)
    return "<truncated>"


def _enforce_event_budget(entry: Dict[str, Any], *, debug: bool) -> Dict[str, Any]:
    limit = _DEBUG_MAX_EVENT_BYTES if debug else _DEFAULT_MAX_EVENT_BYTES
    original_bytes = _json_bytes(entry)
    if original_bytes <= limit:
        return entry

    # Compact content-heavy fields first. Numeric counters, booleans, statuses,
    # and core identity fields remain available for run monitoring.
    candidates = [
        key
        for key in entry
        if key not in _CORE_EVENT_FIELDS and not _is_operational_field(key)
    ]
    candidates.extend(
        key
        for key in entry
        if key not in _CORE_EVENT_FIELDS
        and _is_operational_field(key)
        and isinstance(entry[key], (Mapping, list, str))
        and len(str(entry[key])) > 128
    )
    candidates = sorted(
        dict.fromkeys(candidates),
        key=lambda key: _json_bytes(entry.get(key)),
        reverse=True,
    )

    for key in candidates:
        if _json_bytes(entry) <= limit - 80:
            break
        summary = _field_summary(entry[key])
        if _json_bytes(summary) < _json_bytes(entry[key]):
            entry[key] = summary

    entry["log_truncated"] = True
    entry["log_original_bytes"] = original_bytes

    # This is a last-resort bound for adversarial input with hundreds of small
    # fields. Prefer dropping non-operational fields over losing counters/status.
    if _json_bytes(entry) > limit:
        removable = [
            key
            for key in list(entry)
            if key not in _CORE_EVENT_FIELDS
            and key not in {"log_truncated", "log_original_bytes"}
            and not _is_operational_field(key)
        ]
        for key in reversed(removable):
            entry.pop(key, None)
            if _json_bytes(entry) <= limit:
                break

    # Adversarial input can use hundreds of count-like fields or giant field
    # names. Retain the fixed event identity and enforce the bound regardless.
    if _json_bytes(entry) > limit:
        removable = [
            key
            for key in entry
            if key not in _CORE_EVENT_FIELDS
            and key not in {"log_truncated", "log_original_bytes"}
        ]
        removable.sort(key=lambda key: _json_bytes({key: entry[key]}), reverse=True)
        for key in removable:
            entry.pop(key, None)
            if _json_bytes(entry) <= limit:
                break

    return entry


def make_log(state: dict, event: str, **data: Any) -> dict:
    entry: Dict[str, Any] = {
        "event": _truncate_string(str(event), _DEFAULT_MAX_STRING_CHARS),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    run_id = str((state or {}).get("run_id", "") or "").strip()
    if run_id:
        entry["run_id"] = _truncate_string(run_id, _DEFAULT_MAX_STRING_CHARS)

    debug = debug_enabled(state)
    sanitized = _sanitize_value(data, debug=debug, depth=0, seen=set())
    if isinstance(sanitized, Mapping):
        entry.update(sanitized)
    return _enforce_event_budget(entry, debug=debug)


def make_debug_log(state: dict, event: str, **data: Any) -> Optional[dict]:
    if not debug_enabled(state):
        return None

    entry = make_log(state, event, **data)
    entry["debug"] = True
    return _enforce_event_budget(entry, debug=True)
