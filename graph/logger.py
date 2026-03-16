from __future__ import annotations
from datetime import datetime
from typing import Any, Dict


def make_log(state: dict, event: str, **data: Any) -> dict:
    entry: Dict[str, Any] = {
        "event": event
    }
    entry.update(data)
    return entry