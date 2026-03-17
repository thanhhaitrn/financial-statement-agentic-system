from __future__ import annotations
from datetime import datetime
from typing import Any, Dict, Optional


def debug_enabled(state: dict) -> bool:
    return bool((state or {}).get("debug_trace", False))


def make_log(state: dict, event: str, **data: Any) -> dict:
    entry: Dict[str, Any] = {
        "event": event
    }
    entry.update(data)
    return entry


def make_debug_log(state: dict, event: str, **data: Any) -> Optional[dict]:
    if not debug_enabled(state):
        return None

    entry = make_log(state, event, **data)
    entry["debug"] = True
    return entry
