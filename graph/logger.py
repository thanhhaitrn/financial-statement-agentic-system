"""Small structured logging helpers used across graph nodes."""
# Code note: Graph modules mutate LangGraph state; comments here highlight routing and collection boundaries.

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


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
    "args_preview",
    "source_item",
    "value",
}
_SECRET_FIELDS = {
    "api_key",
    "ollama_api_key",
    "qdrant_api_key",
    "authorization",
    "access_token",
    "refresh_token",
    "secret",
    "password",
}


def debug_enabled(state: dict) -> bool:
    return bool((state or {}).get("debug_trace", False))


def make_log(state: dict, event: str, **data: Any) -> dict:
    entry: Dict[str, Any] = {
        "event": event,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    run_id = str((state or {}).get("run_id", "") or "").strip()
    if run_id:
        entry["run_id"] = run_id

    debug = debug_enabled(state)
    for key, value in data.items():
        normalized_key = str(key).lower()
        if normalized_key in _SECRET_FIELDS or normalized_key.endswith("_api_key"):
            entry[key] = "<redacted>"
            continue
        if not debug and normalized_key in _SENSITIVE_DATA_FIELDS:
            if value not in ("", None, [], {}):
                entry[f"{key}_redacted"] = True
            continue
        entry[key] = value
    return entry


def make_debug_log(state: dict, event: str, **data: Any) -> Optional[dict]:
    if not debug_enabled(state):
        return None

    entry = make_log(state, event, **data)
    entry["debug"] = True
    return entry
