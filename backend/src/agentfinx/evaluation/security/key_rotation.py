"""Persist credential selection, never credential values, across pilot resumes."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from agentfinx.evaluation.contracts import atomic_write_json, provider_limit_reason


def quota_reason(result: dict) -> str:
    """Inspect transport failures only, never model answers or attack text."""
    if result.get("status") == "budget_exhausted":
        return ""
    if result.get("status") == "provider_quota" and result.get("quota_reason"):
        return result["quota_reason"]
    errors = [f"{row.get('error_type', '')}: {row.get('error', '')}"
              for row in result.get("model_call_errors", [])]
    if result.get("status") in {"runtime_error", "infra_error", "provider_quota"}:
        errors.extend(str(error) for error in result.get("errors", [])
                      if not re.search(r"qdrant|unexpectedresponse|unexpected response", str(error), re.I))
    for error in errors:
        # Local workflow follow-up limits are not provider quota.
        reason = provider_limit_reason(error)
        if reason and reason not in {"follow-up limit", "follow up limit", "followup limit"}:
            return reason
        if re.search(r"\b(?:status(?:\s+code)?\s*[:=]?\s*429|429\s+too many requests)\b",
                     error, re.I):
            return "http 429"
        if re.search(r"\b(?:usage limit|quota (?:exceeded|exhausted)|insufficient quota)\b", error, re.I):
            return "provider usage limit"
    return ""


class KeyPool:
    def __init__(self, keys: list[str], state_path: Path):
        import json

        self._keys = {hashlib.sha256(key.encode()).hexdigest(): key for key in keys if key}
        self.path = state_path
        self.state = (json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists()
                      else {"version": 1, "active": None, "exhausted": [], "quota_runs": []})

    @property
    def count(self) -> int:
        return len(self._keys)

    def select(self) -> tuple[str, str] | None:
        available = [key_id for key_id in self._keys if key_id not in self.state["exhausted"]]
        if not available:
            return None
        key_id = self.state["active"] if self.state["active"] in available else available[0]
        self.state["active"] = key_id
        self.save()
        return key_id, self._keys[key_id]

    def exhausted(self, key_id: str | None, run_id: str) -> None:
        if key_id and key_id not in self.state["exhausted"]:
            self.state["exhausted"].append(key_id)
        if run_id not in self.state["quota_runs"]:
            self.state["quota_runs"].append(run_id)
        self.save()

    def save(self) -> None:
        atomic_write_json(self.path, self.state)

    def reload(self, *, retry_exhausted: bool = False) -> None:
        import json

        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        if retry_exhausted:
            # Keep processed run IDs: historical failures must not exhaust keys again.
            self.state["exhausted"] = []
            self.state["active"] = None
            self.save()
