"""Contracts for report discovery and asynchronous dataset imports."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator, model_validator

ReportOrigin = Literal["upload", "cafef_discovery"]
# What the user asked for. ``any`` returns every period type.
ReportPeriod = Literal["any", "annual", "quarter", "half_year"]
# What a candidate is known to be; ``unknown`` unless the period is verified.
CandidatePeriodType = Literal["annual", "quarter", "half_year", "unknown"]
ImportStatus = Literal[
    "queued",
    "downloading",
    "parsing",
    "validating",
    "building",
    "ready",
    "failed",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ReportQuery(BaseModel):
    owner_id: str
    session_id: str
    ticker: str
    company: str = ""
    # Display metadata only: the CafeF report endpoint does not take an exchange.
    exchange: str = ""
    fiscal_year: int | None = None
    period: ReportPeriod = "any"
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    fiscal_half: int | None = Field(default=None, ge=1, le=2)
    report_type: str = "financial_statement"
    scope: str = ""
    audit_status: str = ""

    @field_validator(
        "owner_id",
        "session_id",
        "ticker",
        "company",
        "exchange",
        "report_type",
        "scope",
        "audit_status",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value):
        return " ".join(str(value or "").split())

    @field_validator("ticker", "exchange", mode="after")
    @classmethod
    def uppercase_market_fields(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def check_period(self):
        # A quarter or half number is only meaningful for its own period type.
        # An explicit quarter with no period chosen means the quarter period.
        if self.fiscal_quarter is not None:
            if self.period == "any":
                self.period = "quarter"
            elif self.period != "quarter":
                raise ValueError("fiscal_quarter requires period=quarter")
        if self.fiscal_half is not None:
            if self.period == "any":
                self.period = "half_year"
            elif self.period != "half_year":
                raise ValueError("fiscal_half requires period=half_year")
        return self


class ReportCandidate(BaseModel):
    candidate_id: str
    owner_id: str
    session_id: str
    provider: str = "cafef"
    ticker: str
    company: str = ""
    exchange: str = ""
    title: str
    source_url: str
    # Period fields stay unknown/None unless ``period_verified``: they are
    # never filled in from the user's query.
    period_type: CandidatePeriodType = "unknown"
    fiscal_year: int | None = None
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    fiscal_half: int | None = Field(default=None, ge=1, le=2)
    period_verified: bool = False
    # The provider's own period text (e.g. "Q2/2025"), shown as-is.
    period_label: str = ""
    verification_note: str = ""
    scope: str = "unknown"
    audit_status: str = "unknown"
    report_type: str = "financial_statement"
    created_at: str = Field(default_factory=utc_now_iso)
    expires_at: str

    @field_validator(
        "candidate_id",
        "owner_id",
        "session_id",
        "provider",
        "ticker",
        "company",
        "exchange",
        "title",
        "source_url",
        "scope",
        "audit_status",
        "report_type",
        "created_at",
        "expires_at",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value):
        return " ".join(str(value or "").split())


class PdfArtifact(BaseModel):
    path: str
    source_sha256: str
    size_bytes: int = Field(ge=1)
    page_count: int = Field(ge=1)
    origin: ReportOrigin
    source_url: str = ""
    public_source: bool = False
    cache_namespace: str = ""


class ConvertedPage(BaseModel):
    page_number: int = Field(ge=1)
    markdown: str
    tier: str = "cost_effective"
    quality_issues: list[str] = Field(default_factory=list)


# Billing is tracked independently of processing: a job can be ``ready`` while
# the provider has not yet reported its usage.
#   not_submitted     nothing was sent to a billing provider
#   pending           a remote parse was (or may have been) submitted; usage unknown
#   confirmed         provider-reported usage is recorded (0 only with evidence)
#   estimated_legacy  written before the attempt ledger; amounts are estimates
BillingStatus = Literal["not_submitted", "pending", "confirmed", "estimated_legacy"]
TERMINAL_REMOTE_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class ConvertedDocument(BaseModel):
    markdown: str
    pages: list[ConvertedPage]
    provider: str
    provider_version: str
    tier: str
    source_sha256: str
    billed_pages: int = Field(default=0, ge=0)
    # Provider-confirmed credits rounded up for budget arithmetic; 0 while the
    # provider has not reported usage (see ``credits_confirmed``).
    billed_credits: int = Field(default=0, ge=0)
    # Pages × tier rate. Reservation/budget input only, never a billed amount.
    estimated_credits: int = Field(default=0, ge=0)
    # Exact provider-reported credits as a decimal string; None = unknown.
    confirmed_credits: str | None = None
    credits_confirmed: bool = True
    cache_hit: bool = False
    targeted_retry_pages: list[int] = Field(default_factory=list)
    quality_issues: list[str] = Field(default_factory=list)


class ImportJob(BaseModel):
    billing_pending: bool = False
    billing_status: BillingStatus = "not_submitted"
    # Pending billing that blocks the owner's next import: this job's own
    # remote parse is unsettled. Waiting only on another job's payer does not.
    billing_blocking: bool = False
    job_id: str
    owner_id: str
    session_id: str
    origin: ReportOrigin
    status: ImportStatus = "queued"
    candidate_id: str = ""
    upload_path: str = ""
    dataset_id: str = ""
    source_sha256: str = ""
    converted_path: str = ""
    page_count: int = Field(default=0, ge=0)
    # Consent to send this job's PDF to the cloud parser. Stored per job and
    # passed to every conversion call, targeted retries included.
    allow_cloud_parse: bool = False
    consented_at: str = ""
    billed_pages: int = Field(default=0, ge=0)
    reserved_credits: int = Field(default=0, ge=0)
    # Pages × tier rate for the parse actually run; never a charge.
    estimated_credits: int = Field(default=0, ge=0)
    # Exact provider-confirmed credits (decimal string); "" while unknown.
    confirmed_credits: str = ""
    # ceil(confirmed_credits) for budget arithmetic; 0 while unknown.
    billed_credits: int = Field(default=0, ge=0)
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    session_attached: bool = False
    session_attachment_error: str = ""
    # Request identity for idempotency, immutable once written: the source
    # (web upload ID, verified file SHA or candidate ID), the session it came
    # from and a fingerprint of everything that shapes the import. Empty on
    # jobs written before identities existed; replaying those is refused.
    source_kind: Literal["upload", "candidate", ""] = ""
    source_id: str = ""
    source_session_id: str = ""
    request_fingerprint: str = ""
    # Set while the web confirmation hand-off links the job to its session;
    # the worker does not claim a held job.
    dispatch_hold: bool = False
    metadata: dict = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


@runtime_checkable
class ReportDiscoveryProvider(Protocol):
    @property
    def provider_identity(self) -> str: ...

    def discover(self, query: ReportQuery) -> list[dict]: ...


@runtime_checkable
class DocumentConverter(Protocol):
    @property
    def converter_identity(self) -> str: ...

    # Converters that set ``supports_attempt_ledger = True`` also accept
    # ``consent: bool`` and ``recorder`` (the job's parse-attempt ledger).
    def convert(self, artifact: PdfArtifact, *, private: bool) -> ConvertedDocument: ...
