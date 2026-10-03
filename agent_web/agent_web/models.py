"""
Schema input / output cho agent_web.

Toàn bộ field bám theo mục 3, 5, 6, 7 của file requirement:
- WebAnalysisRequest: input.
- Evidence: 1 bài báo đã crawl, đủ để truy ngược nguồn.
- EventSentiment: sentiment chấm theo TỪNG SỰ KIỆN (không phải theo bài).
- AggregateSentiment: điểm tổng hợp, có version rubric.
- WebAnalysisResult: output cuối cùng, structured, không phụ thuộc Markdown.
"""

from __future__ import annotations

from datetime import datetime, date
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ResultStatus(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class SentimentLabel(str, Enum):
    VERY_NEGATIVE = "very_negative"   # -2
    NEGATIVE = "negative"             # -1
    NEUTRAL = "neutral"               # 0
    POSITIVE = "positive"             # +1
    VERY_POSITIVE = "very_positive"   # +2
    INSUFFICIENT = "insufficient"     # không đủ evidence -> score = None
    MIXED = "mixed"                   # tín hiệu trái chiều -> score = None


SENTIMENT_SCORE_MAP = {
    SentimentLabel.VERY_NEGATIVE: -2,
    SentimentLabel.NEGATIVE: -1,
    SentimentLabel.NEUTRAL: 0,
    SentimentLabel.POSITIVE: 1,
    SentimentLabel.VERY_POSITIVE: 2,
    SentimentLabel.INSUFFICIENT: None,
    SentimentLabel.MIXED: None,
}


class ContentScope(str, Enum):
    FULL_TEXT = "full_text"
    SNIPPET = "snippet"


class ClaimType(str, Enum):
    """Phân biệt tin đồn / dự báo / sự kiện đã xác nhận (mục 6)."""
    CONFIRMED = "confirmed"
    FORECAST = "forecast"
    RUMOR = "rumor"


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------

class WebAnalysisRequest(BaseModel):
    question: str = Field(..., min_length=1)

    ticker: Optional[str] = None
    company_name: Optional[str] = None

    topics: list[str] = Field(default_factory=list)

    date_from: Optional[date] = None
    date_to: Optional[date] = None
    as_of: Optional[datetime] = None

    language: str = "vi"

    @model_validator(mode="after")
    def _require_target(self) -> "WebAnalysisRequest":
        # question và doanh nghiệp mục tiêu là bắt buộc (mục 3).
        # Không đoán doanh nghiệp nếu thiếu cả ticker lẫn company_name -
        # pipeline sẽ trả needs_clarification thay vì raise ở đây, để lỗi
        # này đi qua đúng "status" trong output chứ không phải exception.
        return self


class TargetCompany(BaseModel):
    ticker: Optional[str] = None
    company_name: Optional[str] = None
    resolved: bool = True
    resolution_note: Optional[str] = None


class TimeRange(BaseModel):
    date_from: date
    date_to: date
    is_default_window: bool = False


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class Evidence(BaseModel):
    evidence_id: str
    title: str
    url: str
    source: str
    published_at: Optional[datetime] = None
    retrieved_at: datetime
    event_date: Optional[date] = None
    content: str
    content_scope: ContentScope
    query: str
    content_hash: str


# ---------------------------------------------------------------------------
# Sentiment theo sự kiện
# ---------------------------------------------------------------------------

class SupportingQuote(BaseModel):
    evidence_id: str
    quote: str


class EventSentiment(BaseModel):
    event_id: str
    event_summary: str
    evidence_ids: list[str]

    sentiment_label: SentimentLabel
    sentiment_score: Optional[int] = None  # -2..2, None nếu insufficient/mixed
    claim_type: ClaimType = ClaimType.CONFIRMED

    rationale: str
    supporting_quotes: list[SupportingQuote] = Field(default_factory=list)
    caveats: Optional[str] = None  # giới hạn hoặc thông tin mâu thuẫn

    @model_validator(mode="after")
    def _score_matches_label(self) -> "EventSentiment":
        # Code không tin model mù quáng: điểm số LUÔN được suy ra từ nhãn
        # theo bảng tra cứu cố định, bất kể model trả gì cho sentiment_score.
        # insufficient/mixed luôn ép về None, kể cả khi model lỡ điền số.
        self.sentiment_score = SENTIMENT_SCORE_MAP[self.sentiment_label]
        return self


class AggregateSentiment(BaseModel):
    score: Optional[float] = None
    scored_event_count: int = 0
    unscored_event_count: int = 0
    label_distribution: dict[str, int] = Field(default_factory=dict)
    rubric_version: str = "sentiment-rubric-1.0"
    aggregation_version: str = "unweighted-mean-1.0"


# ---------------------------------------------------------------------------
# Diagnostics / limitations
# ---------------------------------------------------------------------------

class Diagnostics(BaseModel):
    search_queries_used: list[str] = Field(default_factory=list)
    search_calls: int = 0
    fetch_calls: int = 0
    fetch_failures: int = 0
    followup_rounds_used: int = 0
    articles_considered: int = 0
    articles_after_dedup: int = 0
    duplicate_url_count: int = 0
    duplicate_event_merge_count: int = 0
    model_calls: int = 0
    total_latency_ms: Optional[float] = None
    model: Optional[str] = None


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class WebAnalysisResult(BaseModel):
    status: ResultStatus

    target: TargetCompany
    question: str
    time_range: Optional[TimeRange] = None
    as_of: Optional[datetime] = None

    summary_assessment: str = ""

    events: list[EventSentiment] = Field(default_factory=list)
    aggregate: AggregateSentiment = Field(default_factory=AggregateSentiment)
    evidence: list[Evidence] = Field(default_factory=list)

    limitations: list[str] = Field(default_factory=list)
    diagnostics: Diagnostics = Field(default_factory=Diagnostics)

    @classmethod
    def needs_clarification(cls, question: str, note: str) -> "WebAnalysisResult":
        return cls(
            status=ResultStatus.NEEDS_CLARIFICATION,
            target=TargetCompany(resolved=False, resolution_note=note),
            question=question,
            limitations=[note],
        )

    @classmethod
    def error(cls, question: str, target: TargetCompany, message: str) -> "WebAnalysisResult":
        return cls(
            status=ResultStatus.ERROR,
            target=target,
            question=question,
            limitations=[message],
        )
