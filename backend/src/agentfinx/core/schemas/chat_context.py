"""Web-only follow-up contract. No financial answer or fact values belong here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTEXT_CONTRACT_VERSION = "chat-context-v1"
CONTEXT_FIELDS = ("metric", "period", "entity", "operation", "scope")


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InheritedSpan(ContextModel):
    source_run_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)


class FieldResolution(ContextModel):
    # Empty lists explicitly mean that this field is not specified.
    kept: list[InheritedSpan] = Field(default_factory=list, max_length=10)
    changed: list[str] = Field(default_factory=list, max_length=10)


class ContextResolution(ContextModel):
    status: Literal["standalone", "resolved", "needs_clarification", "failed"]
    question: str = Field(default="", max_length=4000)
    clarification_question: str = Field(default="", max_length=2000)
    # Required even for standalone/clarification: omitted claims are not proof.
    metric: FieldResolution
    period: FieldResolution
    entity: FieldResolution = Field(default_factory=FieldResolution)
    operation: FieldResolution = Field(default_factory=FieldResolution)
    scope: FieldResolution = Field(default_factory=FieldResolution)


class ContextTurn(ContextModel):
    run_id: str
    effective_question: str
    legacy: bool = False
    snapshot: dict = Field(default_factory=dict)
