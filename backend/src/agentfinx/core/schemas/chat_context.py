"""Web-only follow-up contract. No financial answer or fact values belong here."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTEXT_CONTRACT_VERSION = "chat-context-v1"
CONTEXT_FIELDS = ("metric", "period", "entity", "operation", "scope")


class ContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportContext(ContextModel):
    """Allowlisted report metadata; never financial facts or filesystem paths."""

    company: str = Field(default="", max_length=500)
    ticker: str = Field(default="", max_length=50)
    fiscal_year: int | None = None
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    scope: str = Field(default="unknown", max_length=100)

    @classmethod
    def from_dataset(cls, dataset) -> "ReportContext":
        return cls(**{
            name: getattr(dataset, name, field.default)
            for name, field in cls.model_fields.items()
        })

    def period_label(self) -> str:
        if self.fiscal_year is None:
            return ""
        if self.fiscal_quarter is not None:
            return f"quý {self.fiscal_quarter}/{self.fiscal_year}"
        return f"năm {self.fiscal_year}"


class InheritedSpan(ContextModel):
    source_run_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)


class FieldResolution(ContextModel):
    # Empty lists explicitly mean that this field is not specified.
    kept: list[InheritedSpan] = Field(
        default_factory=list, max_length=10,
        description="Array of objects with source_run_id and text. Never an array of strings.",
    )
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
    # A still-open clarification includes the user's question and the question
    # we asked back. Financial answers are never sent as resolver history.
    clarification_question: str = Field(default="", max_length=2000)
