"""Contracts for report discovery and asynchronous dataset imports."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

ReportOrigin = Literal["upload", "cafef_discovery"]
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
    exchange: str = ""
    fiscal_year: int | None = None
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
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


class ReportCandidate(BaseModel):
    candidate_id: str
    owner_id: str
    session_id: str
    provider: str = "cafef"
    ticker: str
    company: str = ""
    exchange: str
    title: str
    source_url: str
    fiscal_year: int | None = None
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
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


class ConvertedDocument(BaseModel):
    markdown: str
    pages: list[ConvertedPage]
    provider: str
    provider_version: str
    tier: str
    source_sha256: str
    billed_pages: int = Field(default=0, ge=0)
    billed_credits: int = Field(default=0, ge=0)
    cache_hit: bool = False
    targeted_retry_pages: list[int] = Field(default_factory=list)
    quality_issues: list[str] = Field(default_factory=list)


class ImportJob(BaseModel):
    billing_pending: bool = False
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
    billed_pages: int = Field(default=0, ge=0)
    reserved_credits: int = Field(default=0, ge=0)
    billed_credits: int = Field(default=0, ge=0)
    error_code: str = ""
    error_message: str = ""
    retryable: bool = False
    session_attached: bool = False
    session_attachment_error: str = ""
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

    def convert(self, artifact: PdfArtifact, *, private: bool) -> ConvertedDocument: ...
