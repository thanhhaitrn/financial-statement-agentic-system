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
    # Optional publication window (ISO 8601, UTC). Without it the provider
    # keeps its original "latest articles" behaviour.
    date_from: str = ""
    date_to: str = ""
    # What the question is about, to rank articles and paragraphs. Used in
    # memory for this request only; never stored or logged.
    topic: str = ""
    limit: int = Field(default=10, ge=1, le=50)
    force_refresh: bool = False

    @field_validator("query", "owner_id", "ticker", "company", "as_of", "date_from", "date_to",
                     mode="before")
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
    # Source summary (sapo) and how much of the article is held:
    # "full" article text, "summary_only" (the page could not be read) or
    # "listing_only" (seen in the source's news list, page not fetched yet).
    summary: str = ""
    content_status: Literal["full", "summary_only", "listing_only"] = "full"

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
        "summary",
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

    def as_structured_fact(self, figure: dict) -> dict:
        """One figure of the article as a typed fact: the identity fields a
        report fact has for a calculation (``STRUCTURED_FACT_FIELDS``) and the
        number itself in ``value``/``parsed_value``, never the article text.
        It stays a WEB fact: ``source_kind="web"``, ``content_type="web_fact"``,
        the article's ``source_url`` and publisher, so the provenance gate sees
        exactly what it sees for ``as_fact``. Used only by the security
        experiment's controlled exposure."""

        fact = self.as_fact()
        number = str(figure["value"]).strip()
        if not number:
            raise ValueError("a structured fact needs a value")
        fact.update({key: figure[key] for key in STRUCTURED_FACT_FIELDS if figure.get(key) not in (None, "")})
        fact.update(value=number, parsed_value=number, evidence_text=self.title,
                    fact_id=f"{self.evidence_id}:{figure.get('key') or fact.get('item_name', '')}")
        return fact


# What a typed figure from an article carries so that the calculation step can
# rank it like a report fact (security experiment, phase 2). Provenance fields
# are not among them: they stay those of the web.
STRUCTURED_FACT_FIELDS = (
    "table", "row_label", "column_label", "item_name", "metric_label", "period_label", "period_role",
    "fiscal_year", "unit", "value_type", "reporting_basis", "aggregation_level", "evidence_role", "statement",
)


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
    # For a windowed request: the part of the window the source was actually
    # read for ({"requested_from", "requested_to", "covered_from",
    # "covered_to", "complete"}), and whether a refresh is still collecting.
    coverage: dict = Field(default_factory=dict)
    pending: bool = False
    # Collection steps kept apart: articles of the window seen in the news
    # list, of those how many have full text stored, and how many were
    # selected for this request.
    stats: dict = Field(default_factory=dict)


@runtime_checkable
class WebEvidenceProvider(Protocol):
    @property
    def provider_identity(self) -> str: ...

    def search(self, request: WebEvidenceRequest) -> WebEvidenceBatch: ...
