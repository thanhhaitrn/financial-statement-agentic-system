# graph/logger.py
from __future__ import annotations
from datetime import datetime
from typing import Any, Dict

def log_step(state: dict, event: str, **data: Any) -> dict:
    state.setdefault("trace", [])
    state.setdefault("run_id", "run_unknown")

    entry: Dict[str, Any] = {
        "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "run_id": state["run_id"],
        "event": event,
        "agent": state.get("last_agent", ""),
        "num_steps": state.get("num_steps", 0),
    }
    entry.update(data)
    state["trace"].append(entry)
    return state