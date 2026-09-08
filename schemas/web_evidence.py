"""Typed contracts for external, non-BCTC evidence providers."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

WebIntent = Literal["company_news", "unsupported_external"]
WebRetrievalStatus = Literal[
    "found",
    "not_found_after_search",
    "unsupported",
    "error",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class WebEvidenceRequest(BaseModel):
    query: str
    intent: WebIntent = "unsupported_external"
    owner_id: str = ""
    ticker: str = ""
    company: str = ""
    as_of: str = ""
    limit: int = Field(default=10, ge=1, le=50)
    force_refresh: bool = False

    @field_validator("query", "owner_id", "ticker", "company", "as_of", mode="before")
    @classmethod
    def normalize_text(cls, value):
        return " ".join(str(value or "").split())

    @field_validator("ticker", mode="after")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()


class WebEvidence(BaseModel):
    evidence_id: str = ""
    intent: WebIntent = "company_news"
    ticker: str = ""
    company: str = ""
    title: str
    content: str
    source_url: str
    publisher: str
    published_at: str = ""
    retrieved_at: str = Field(default_factory=utc_now_iso)
    query: str = ""
    content_hash: str = ""

    @field_validator(
        "evidence_id",
        "ticker",
        "company",
        "title",
        "content",
        "source_url",
        "publisher",
        "published_at",
        "retrieved_at",
        "query",
        "content_hash",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value):
        return " ".join(str(value or "").split())

    @field_validator("ticker", mode="after")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()

    def model_post_init(self, __context) -> None:
        if not self.content_hash:
            self.content_hash = sha256(self.content.encode("utf-8")).hexdigest()
        if not self.evidence_id:
            identity = f"{self.source_url}\x1f{self.content_hash}"
            self.evidence_id = sha256(identity.encode("utf-8")).hexdigest()[:24]

    def as_fact(self) -> dict:
        value = self.title
        if self.content:
            value = f"{self.title}\n\n{self.content}"
        return {
            "source_kind": "web",
            "content_type": "web_fact",
            "fact_id": self.evidence_id,
            "item_name": self.title,
            "ticker": self.ticker,
            "company": self.company,
            "time_hint": self.published_at,
            "published_at": self.published_at,
            "retrieved_at": self.retrieved_at,
            "value": value,
            "source": self.publisher,
            "source_url": self.source_url,
            "publisher": self.publisher,
            "content_hash": self.content_hash,
            "query": self.query,
            "status": "found",
            "retrieval_status": "found",
            "evidence_text": value,
        }


class WebEvidenceBatch(BaseModel):
    status: WebRetrievalStatus
    diagnostics: dict[str, int] = Field(default_factory=dict)
    evidence: list[WebEvidence] = Field(default_factory=list)
    message: str = ""
    provider: str = ""
    cache_status: Literal["fresh", "stale", "miss", "disabled", "unsupported"] = (
        "miss"
    )
    refresh_scheduled: bool = False


@runtime_checkable
class WebEvidenceProvider(Protocol):
    @property
    def provider_identity(self) -> str: ...

    def search(self, request: WebEvidenceRequest) -> WebEvidenceBatch: ...
