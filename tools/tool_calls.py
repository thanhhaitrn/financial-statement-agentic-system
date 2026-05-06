"""Helpers for normalizing LangChain native tool call payloads."""
# Code note: Tool modules bridge agent requests to retrieval helpers; comments here mark guardrails around external calls.

from __future__ import annotations

import json
import uuid
from typing import Any


def coerce_tool_args(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def normalize_tool_call(call: Any) -> dict:
    if hasattr(call, "model_dump"):
        call = call.model_dump()

    if not isinstance(call, dict):
        return {"name": "", "args": {}, "id": "", "type": "", "raw": str(call)}

    name = str(call.get("name", "") or "").strip()
    args = call.get("args")

    function_payload = call.get("function")
    if isinstance(function_payload, dict):
        name = name or str(function_payload.get("name", "") or "").strip()
        if args is None:
            args = function_payload.get("arguments")

    return {
        "name": name,
        "args": coerce_tool_args(args),
        "id": str(call.get("id", "") or "").strip(),
        "type": str(call.get("type", "") or "").strip(),
    }


def response_tool_calls(response: Any) -> list[dict]:
    calls = getattr(response, "tool_calls", None)
    if calls is None:
        additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
        calls = additional_kwargs.get("tool_calls", [])
    return [normalize_tool_call(call) for call in (calls or [])]


def invalid_tool_calls(response: Any) -> list[Any]:
    invalid = getattr(response, "invalid_tool_calls", None)
    if invalid is not None:
        return list(invalid or [])

    additional_kwargs = getattr(response, "additional_kwargs", {}) or {}
    return list(additional_kwargs.get("invalid_tool_calls", []) or [])


def synthetic_tool_call(tool_name: str, args: dict) -> dict:
    return {
        "name": str(tool_name or "").strip(),
        "args": dict(args or {}),
        "id": f"synthetic-{uuid.uuid4()}",
        "type": "tool_call",
    }
