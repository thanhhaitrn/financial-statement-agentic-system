from agent_web.aggregate import compute_aggregate
from agent_web.models import EventSentiment, SentimentLabel


def _event(label: SentimentLabel, eid: str = "e1") -> EventSentiment:
    return EventSentiment(
        event_id=eid,
        event_summary="sự kiện",
        evidence_ids=["ev_1"],
        sentiment_label=label,
        rationale="vì lý do X",
    )


def test_aggregate_simple_mean():
    events = [
        _event(SentimentLabel.POSITIVE, "e1"),   # +1
        _event(SentimentLabel.VERY_NEGATIVE, "e2"),  # -2
        _event(SentimentLabel.NEUTRAL, "e3"),  # 0
    ]
    agg = compute_aggregate(events)
    assert agg.scored_event_count == 3
    assert agg.unscored_event_count == 0
    assert agg.score == (1 + (-2) + 0) / 3


def test_aggregate_excludes_insufficient_and_mixed_from_score():
    events = [
        _event(SentimentLabel.POSITIVE, "e1"),
        _event(SentimentLabel.INSUFFICIENT, "e2"),
        _event(SentimentLabel.MIXED, "e3"),
    ]
    agg = compute_aggregate(events)
    assert agg.scored_event_count == 1
    assert agg.unscored_event_count == 2
    assert agg.score == 1.0
    assert agg.label_distribution["insufficient"] == 1
    assert agg.label_distribution["mixed"] == 1


def test_aggregate_all_null_returns_none_not_zero():
    events = [_event(SentimentLabel.INSUFFICIENT, "e1"), _event(SentimentLabel.MIXED, "e2")]
    agg = compute_aggregate(events)
    assert agg.score is None
    assert agg.scored_event_count == 0


def test_aggregate_empty_events_returns_none():
    agg = compute_aggregate([])
    assert agg.score is None
    assert agg.scored_event_count == 0
    assert agg.unscored_event_count == 0
