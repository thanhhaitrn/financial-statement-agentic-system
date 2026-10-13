"""Typed contracts for company-news analysis (WEB_ANALYSIS_ENABLED).

``WebContext`` is what the analysis is about, ``NewsArticle`` the bounded
article text it may read and quote, ``WebAnalysis`` what it concluded.

An event answers, in this order: what happened, when, which figures it
relates to, which quotes prove it, and what is still uncertain. Everything a
reader may take as fact is checked in code: quotes verbatim against the
article, the title and explanation against the quotes, the confirmation level
by fixed source rules. The sentiment label is secondary and its score is
derived from the label, never set by the model.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

RUBRIC_VERSION = "news-sentiment-rubric-2.0"
AGGREGATION_VERSION = "confirmation-weighted-mean-2.0"

AnalysisStatus = Literal[
    "ok",
    "partial",
    "pending",
    "insufficient_evidence",
    "needs_clarification",
    "unsupported",
    "error",
]
SentimentLabel = Literal[
    "very_negative", "negative", "neutral", "positive", "very_positive", "mixed", "insufficient",
]
# Decided by code from the source, not by the model:
# official_disclosure  the company's own disclosure, republished by the source;
# attributed_report    a news report quoting the company or an official body;
# forecast             a plan, target or projection;
# unconfirmed          the quote itself says it is a rumour or unconfirmed;
# unattributed         none of the above can be shown from the quotes.
Confirmation = Literal["official_disclosure", "attributed_report", "forecast", "unconfirmed", "unattributed"]

SENTIMENT_SCORES: dict[str, Optional[int]] = {
    "very_negative": -2,
    "negative": -1,
    "neutral": 0,
    "positive": 1,
    "very_positive": 2,
    "mixed": None,
    "insufficient": None,
}
# How much an event's sentiment counts in the aggregate, by confirmation.
CONFIRMATION_WEIGHTS: dict[str, float] = {
    "official_disclosure": 1.0,
    "attributed_report": 0.75,
    "forecast": 0.5,
    "unattributed": 0.5,
    "unconfirmed": 0.0,
}


class WebContext(BaseModel):
    status: Literal["ready", "needs_clarification", "unsupported"] = "ready"
    reason: str = ""
    message: str = ""
    company: str = ""
    ticker: str = ""
    # "report": the report's own ticker; "catalog_name": the report has none
    # and its exact company name matched exactly one listed company.
    ticker_source: Literal["report", "catalog_name", ""] = ""
    question: str = ""
    date_from: str = ""
    date_to: str = ""
    as_of: str = ""
    window_source: Literal["default", "explicit", "relative", ""] = ""


class NewsArticle(BaseModel):
    article_id: str
    evidence_id: str = ""
    title: str
    url: str
    publisher: str = ""
    published_at: str = ""
    retrieved_at: str = ""
    content: str = ""
    summary: str = ""
    content_status: Literal["full", "summary_only", "listing_only"] = "full"


class Quote(BaseModel):
    article_id: str
    text: str


class NewsEvent(BaseModel):
    event_id: str
    # What happened. "model_checked": the model's title, supported by the
    # quotes; "source_title": the article's own title, used instead.
    title: str
    title_source: Literal["model_checked", "source_title"] = "source_title"
    article_ids: list[str] = Field(default_factory=list)
    # When: the event date only if the article text states it; otherwise
    # just when it was reported.
    event_date: str = ""
    reported_at: str = ""
    # Which figures it relates to (financial terms present in its quotes).
    related_metrics: list[str] = Field(default_factory=list)
    explanation: str = ""
    quotes: list[Quote] = Field(default_factory=list)
    confirmation: Confirmation = "unattributed"
    confirmation_basis: str = ""
    uncertainties: list[str] = Field(default_factory=list)
    # Secondary: tone of the news for the company.
    label: SentimentLabel = "insufficient"
    score: Optional[int] = None


class AggregateScore(BaseModel):
    score: Optional[float] = None
    scored_events: int = 0
    unscored_events: int = 0
    label_distribution: dict[str, int] = Field(default_factory=dict)
    rubric_version: str = RUBRIC_VERSION
    aggregation_version: str = AGGREGATION_VERSION


class Collection(BaseModel):
    """Collection steps kept apart; "not found" is only claimed when the
    news list was read for the whole window."""

    list_read_for_window: bool = False
    listed_in_window: int = 0
    full_text_in_window: int = 0
    selected: int = 0
    analyzed_full_text: int = 0
    analyzed_summary_only: int = 0
    relevant: int = 0
    ranked_by_topic: bool = False
    covered_from: str = ""
    pending: bool = False


class WebAnalysis(BaseModel):
    status: AnalysisStatus
    context: WebContext = Field(default_factory=WebContext)
    events: list[NewsEvent] = Field(default_factory=list)
    aggregate: AggregateScore = Field(default_factory=AggregateScore)
    # Articles an event cites, for the source list (no article text).
    sources: list[dict] = Field(default_factory=list)
    collection: Collection = Field(default_factory=Collection)
    limitations: list[str] = Field(default_factory=list)
    coverage: dict = Field(default_factory=dict)
    diagnostics: dict = Field(default_factory=dict)
    memo_key: str = ""
