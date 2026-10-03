"""
Tính aggregate sentiment (mục 6).

aggregate_score = sum(event_score) / scored_event_count

Chỉ tính trên các SỰ KIỆN DUY NHẤT có điểm (không None). insufficient/mixed
không tham gia phép tính nhưng vẫn được đếm và giữ trong label_distribution.
"""

from __future__ import annotations

from .models import EventSentiment, AggregateSentiment


RUBRIC_VERSION = "sentiment-rubric-1.0"
AGGREGATION_VERSION = "unweighted-mean-1.0"


def compute_aggregate(events: list[EventSentiment]) -> AggregateSentiment:
    scored = [e for e in events if e.sentiment_score is not None]
    unscored = [e for e in events if e.sentiment_score is None]

    label_distribution: dict[str, int] = {}
    for e in events:
        label_distribution[e.sentiment_label.value] = (
            label_distribution.get(e.sentiment_label.value, 0) + 1
        )

    if not scored:
        score = None
    else:
        score = sum(e.sentiment_score for e in scored) / len(scored)

    return AggregateSentiment(
        score=score,
        scored_event_count=len(scored),
        unscored_event_count=len(unscored),
        label_distribution=label_distribution,
        rubric_version=RUBRIC_VERSION,
        aggregation_version=AGGREGATION_VERSION,
    )
