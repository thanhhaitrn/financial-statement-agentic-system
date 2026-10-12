"""Small, allowlisted intent snapshots from graph state, never answer values.

The requirement text is referenced by digest, not copied: it can itself contain
numeric premises. The resolver receives only typed query slots, not raw facts,
selected_facts, the calculation ledger, or Synth's answer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

SNAPSHOT_VERSION = "chat-intent-v1"
MAX_REQUIREMENTS = 20
SLOT_FIELDS = ("metric", "entity", "period", "period_role", "operation",
               "reporting_basis", "scope_label")


class SnapshotModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IntentSlots(SnapshotModel):
    metric: str = Field(default="", max_length=500)
    entity: str = Field(default="", max_length=500)
    period: str = Field(default="", max_length=200)
    period_role: str = Field(default="", max_length=100)
    operation: str = Field(default="", max_length=100)
    reporting_basis: str = Field(default="", max_length=100)
    scope_label: str = Field(default="", max_length=200)


class RequirementSnapshot(SnapshotModel):
    ledger_index: int = Field(ge=0)
    requirement_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    table: str = Field(max_length=100)
    parsed_query_slots: IntentSlots
    operands: list[IntentSlots] = Field(default_factory=list, max_length=20)


class IntentSnapshot(SnapshotModel):
    version: Literal["chat-intent-v1"] = SNAPSHOT_VERSION
    source_run_id: str
    dataset_id: str
    dataset_generation: str
    company: str = Field(default="", max_length=500)
    time_hint: str = Field(default="", max_length=500)
    requirements: list[RequirementSnapshot] = Field(default_factory=list, max_length=MAX_REQUIREMENTS)
    truncated: bool = False


def _slots(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in SLOT_FIELDS if isinstance(value.get(key), str)}


def build_context_snapshot(state: dict, run) -> dict:
    if not isinstance(state, dict):
        return {}
    plan = state.get("planner_plan") or {}
    ledger = state.get("evidence_ledger") or {}
    if not isinstance(plan, dict) or not isinstance(ledger, dict):
        return {}
    requirements = []
    truncated = False
    entries = ledger.get("entries") or []
    if not isinstance(entries, list):
        return {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get("kind") != "retrieval_requirement":
            continue
        slots = entry.get("parsed_query_slots") or {}
        if not isinstance(slots, dict):
            continue
        operands = slots.get("operands") or []
        if not isinstance(operands, (list, tuple)) or len(operands) > 20:
            operands = []
            truncated = True
        requirements.append({
            "ledger_index": index,
            "requirement_sha256": hashlib.sha256(str(entry.get("requirement") or "").encode()).hexdigest(),
            "table": entry.get("table") or "",
            "parsed_query_slots": _slots(slots),
            "operands": [_slots(operand) for operand in operands if isinstance(operand, dict)],
        })
    if len(requirements) > MAX_REQUIREMENTS:
        # Do not turn a truncated list into a false single/first focus.
        requirements = []
        truncated = True
    try:
        snapshot = IntentSnapshot(
            source_run_id=run["id"], dataset_id=run["dataset_id"] or "",
            dataset_generation=run["dataset_generation"] or "",
            company=plan.get("company") or "", time_hint=plan.get("time_hint") or "",
            requirements=requirements, truncated=truncated,
        )
    except ValidationError:
        # Supplemental metadata cannot turn a valid financial answer into an error.
        return {}
    return snapshot.model_dump(exclude_defaults=True) | {"version": SNAPSHOT_VERSION}


def read_context_snapshot(raw: str, *, run_id: str, dataset_id: str, generation: str) -> dict:
    try:
        snapshot = IntentSnapshot.model_validate(json.loads(raw or "{}"))
    except (ValidationError, TypeError, ValueError):
        return {}
    if (snapshot.source_run_id, snapshot.dataset_id, snapshot.dataset_generation) != (
        run_id, dataset_id or "", generation or "",
    ):
        return {}
    return snapshot.model_dump(exclude_defaults=True) | {"version": SNAPSHOT_VERSION}
