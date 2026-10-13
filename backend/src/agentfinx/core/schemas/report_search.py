"""Planner output for the web entry point with report discovery enabled.

Kept apart from ``schemas.agent_outputs`` on purpose: the QA planner's schema
(``PlannerEvidencePlan``) is unchanged whether discovery is on or off. The web
entry extends it with the task kind and what to search for.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from agentfinx.core.schemas.agent_outputs import PlannerEvidencePlan

_PERIOD_ALIASES = {
    "annual": "annual", "year": "annual", "yearly": "annual", "nam": "annual", "năm": "annual",
    "quarter": "quarter", "quarterly": "quarter", "quy": "quarter", "quý": "quarter",
    "half_year": "half_year", "half": "half_year", "semiannual": "half_year",
    "ban_nien": "half_year", "bán niên": "half_year",
}


class ReportSearchRequest(BaseModel):
    """What the user asked to find. ``subject`` is the company name or ticker
    as the user wrote it; ``ticker_guess`` is the model's guess and is only
    ever offered back to the user for confirmation."""

    subject: str = ""
    ticker_guess: str = ""
    fiscal_year: Optional[int] = None
    period: str = "any"
    fiscal_quarter: Optional[int] = None
    fiscal_half: Optional[int] = None
    scope: str = "unknown"
    audit_status: str = "unknown"

    @field_validator("subject", "ticker_guess", mode="before")
    @classmethod
    def normalize_text(cls, value):
        return " ".join(str(value or "").split())

    @field_validator("fiscal_year", "fiscal_quarter", "fiscal_half", mode="before")
    @classmethod
    def normalize_number(cls, value):
        try:
            return int(value) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None

    @field_validator("period", mode="before")
    @classmethod
    def normalize_period(cls, value):
        return _PERIOD_ALIASES.get(str(value or "").strip().lower(), "any")

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, value):
        text = str(value or "").strip().lower()
        return text if text in {"consolidated", "separate"} else "unknown"

    @field_validator("audit_status", mode="before")
    @classmethod
    def normalize_audit(cls, value):
        text = str(value or "").strip().lower()
        return text if text in {"audited", "reviewed", "unaudited"} else "unknown"


class WebPlannerPlan(PlannerEvidencePlan):
    task_kind: Literal["report_qa", "report_discovery"] = "report_qa"
    report_search: ReportSearchRequest = Field(default_factory=ReportSearchRequest)

    @field_validator("task_kind", mode="before")
    @classmethod
    def normalize_task_kind(cls, value):
        return "report_discovery" if str(value or "").strip().lower() == "report_discovery" else "report_qa"
