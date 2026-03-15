from __future__ import annotations
from datetime import datetime
from typing import Any, Dict


def make_log(state: dict, event: str, **data: Any) -> dict:
    entry: Dict[str, Any] = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "run_id": state.get("run_id", "run_unknown"),
        "event": event,
        "agent": state.get("last_agent", ""),
    }
    entry.update(data)
    return entry