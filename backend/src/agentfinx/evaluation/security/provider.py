"""A fixed news source for the security experiment.

``FixtureNewsProvider`` implements the product's ``WebEvidenceProvider``
contract directly and returns a ``WebEvidenceBatch`` built with the app's own
``WebEvidence`` schema. It reads no cache, writes none, and never calls
Vietstock or a search engine. It holds the articles of one ticker and answers
a request only for that ticker and only with the articles published inside the
requested window; the window and every date are compared with the fixed
``as_of`` of the experiment, never with the real clock.

Every search is recorded for the active experiment run (evidence level E1):
the request's ticker and window, what was returned with its content hashes,
and why anything was not.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentfinx.core import experiment
from agentfinx.core.schemas.web_evidence import WebEvidence, WebEvidenceBatch, WebEvidenceRequest

PROVIDER_IDENTITY = "security-fixture-v1"
# How far back the fixture claims to have read the news list for its ticker.
COVERAGE_DAYS = 30


def _parse(value: str) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


class FixtureNewsProvider:
    def __init__(self, articles: list[dict], *, ticker: str, as_of: str):
        self._ticker = ticker.upper()
        self._as_of = _parse(as_of)
        if self._as_of is None:
            raise ValueError("as_of is required")
        self._covered_from = self._as_of - timedelta(days=COVERAGE_DAYS)
        retrieved = _iso(self._as_of)
        self._evidence = [
            WebEvidence(
                intent="company_news",
                ticker=article["ticker"],
                company=article.get("company", ""),
                title=article["title"],
                content=article["content"],
                summary=article.get("summary", ""),
                source_url=article["source_url"],
                publisher=article["publisher"],
                published_at=_iso(_parse(article["published_at"])),
                retrieved_at=retrieved,
                content_status=article.get("content_status", "full"),
            )
            for article in articles
        ]
        for item in self._evidence:
            if item.ticker != self._ticker:
                raise ValueError(f"fixture article {item.source_url} is not about {self._ticker}")
        self.searches: list[dict] = []

    @property
    def provider_identity(self) -> str:
        return PROVIDER_IDENTITY

    @property
    def evidence(self) -> list[WebEvidence]:
        return list(self._evidence)

    def _record(self, request: WebEvidenceRequest, batch: WebEvidenceBatch, reasons: dict) -> None:
        record = {
            "request": {"ticker": request.ticker, "date_from": request.date_from,
                        "date_to": request.date_to, "as_of": request.as_of, "intent": request.intent,
                        "limit": request.limit},
            "status": batch.status,
            "coverage": dict(batch.coverage),
            "reasons": reasons,
            "returned": [{"evidence_id": item.evidence_id, "content_hash": item.content_hash,
                          "source_url": item.source_url, "published_at": item.published_at,
                          "title": item.title} for item in batch.evidence],
        }
        self.searches.append(record)
        experiment.emit("provider_search", provider=PROVIDER_IDENTITY, **record)

    def search(self, request: WebEvidenceRequest) -> WebEvidenceBatch:
        if request.ticker != self._ticker:
            batch = WebEvidenceBatch(
                status="not_found_after_search", provider=PROVIDER_IDENTITY, cache_status="disabled",
                message=f"Nguồn thử nghiệm chỉ có tin của {self._ticker}, không có tin của "
                        f"{request.ticker or '(không rõ mã)'}.",
                coverage={"complete": False, "reason": "ticker_not_in_fixture"},
                diagnostics={"ticker_mismatch": 1})
            self._record(request, batch, {"ticker_mismatch": request.ticker})
            return batch

        start = _parse(request.date_from) or self._covered_from
        end = min(_parse(request.date_to) or self._as_of, self._as_of)
        inside, outside = [], []
        for item in self._evidence:
            published = _parse(item.published_at)
            (inside if published is not None and start <= published <= end else outside).append(item)
        selected = inside[: request.limit]
        # Complete only when the fixture's read span covers the whole window.
        complete = start >= self._covered_from and end <= self._as_of
        coverage = {
            "requested_from": _iso(start), "requested_to": _iso(end),
            "covered_from": _iso(max(start, self._covered_from)), "covered_to": _iso(end),
            "listed_from": _iso(self._covered_from), "complete": complete,
        }
        if not complete:
            coverage["reason"] = "window_starts_before_fixture_coverage"
        batch = WebEvidenceBatch(
            status="found" if selected else "not_found_after_search",
            provider=PROVIDER_IDENTITY,
            evidence=selected,
            cache_status="disabled",
            coverage=coverage,
            stats={"listed_in_window": len(inside), "full_text_in_window": len(inside),
                   "selected": len(selected), "selected_full_text": len(selected),
                   "fetched_now": 0, "ranked_by_topic": False},
            diagnostics={"outside_window": len(outside), "over_limit": len(inside) - len(selected)},
        )
        self._record(request, batch, {"outside_window": [item.source_url for item in outside],
                                      "over_limit": len(inside) - len(selected),
                                      "incomplete_window": not complete})
        return batch
