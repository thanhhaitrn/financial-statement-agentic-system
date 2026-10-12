"""The JSON shape of a chat message for the browser."""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import json
from typing import Any


def message_out(row: Any) -> dict:
    """``payload`` is server-built JSON for structured kinds (report
    candidates, clarifications, errors); the browser renders it as text."""

    keys = row.keys()
    kind = (row["kind"] if "kind" in keys else "text") or "text"
    payload = None
    if "payload" in keys and row["payload"]:
        try:
            payload = json.loads(row["payload"])
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        payload = None
    context = payload or {}
    return {
        "id": row["id"],
        "role": row["role"],
        "content": row["content"],
        "run_id": row["run_id"],
        "created_at": row["created_at"],
        "kind": kind,
        "payload": payload if kind != "text" else None,
        "context_status": context.get("context_status", ""),
        "context_reason": context.get("context_reason", ""),
        "interpreted_question": context.get("interpreted_question", ""),
    }
