"""Event scores and the aggregate, computed in code from labels.

aggregate = Σ(score × weight) / Σ(weight) over events that have a score and a
non-zero confirmation weight (``schemas.web_analysis.CONFIRMATION_WEIGHTS``).
It is secondary information: the tone of the news only, not a price signal
and not the cause of any figure.
"""

from __future__ import annotations

from collections import Counter

from schemas.web_analysis import (AGGREGATION_VERSION, CONFIRMATION_WEIGHTS,
                                  RUBRIC_VERSION, SENTIMENT_SCORES,
                                  AggregateScore, NewsEvent)


def aggregate(events: list[NewsEvent]) -> AggregateScore:
    weighted = [
        (event.score, CONFIRMATION_WEIGHTS.get(event.confirmation, 0.0))
        for event in events
        if event.score is not None and CONFIRMATION_WEIGHTS.get(event.confirmation, 0.0) > 0
    ]
    total_weight = sum(weight for _score, weight in weighted)
    score = None
    if total_weight > 0:
        score = round(sum(score * weight for score, weight in weighted) / total_weight, 2)
    return AggregateScore(
        score=score,
        scored_events=len(weighted),
        unscored_events=len(events) - len(weighted),
        label_distribution=dict(Counter(event.label for event in events)),
        rubric_version=RUBRIC_VERSION,
        aggregation_version=AGGREGATION_VERSION,
    )


def score_for(label: str):
    return SENTIMENT_SCORES.get(label)
