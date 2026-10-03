"""
Mock provider — dùng cho unit test (mục 9: 'Unit test dùng fixture/mock và
chạy được không cần API key'). Nhận sẵn danh sách bài (fixture) và trả lại
đúng dữ liệu đó khi search/fetch được gọi, không đi ra network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ..interfaces import FetchedArticle, RawSearchHit


@dataclass
class MockArticleFixture:
    title: str
    url: str
    source: str
    content: str
    published_at: datetime | None = None
    content_scope: str = "full_text"
    fetch_error: str | None = None


@dataclass
class MockSearchProvider:
    """Trả về mọi fixture khớp full-text search đơn giản theo `query` trong
    title/content, để test kiểm soát được không cần gọi search thật."""

    fixtures: list[MockArticleFixture] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)

    async def search(
        self,
        query: str,
        *,
        date_from: date | None,
        date_to: date | None,
        max_results: int,
    ) -> list[RawSearchHit]:
        self.calls.append(query)
        hits = []
        for fx in self.fixtures:
            if fx.published_at and date_from and fx.published_at.date() < date_from:
                continue
            if fx.published_at and date_to and fx.published_at.date() > date_to:
                continue
            hits.append(
                RawSearchHit(
                    title=fx.title,
                    url=fx.url,
                    source=fx.source,
                    snippet=fx.content[:200],
                    published_at=fx.published_at,
                )
            )
        return hits[:max_results]


@dataclass
class MockFetcher:
    fixtures_by_url: dict[str, MockArticleFixture]

    async def fetch(self, url: str, *, timeout_s: float) -> FetchedArticle:
        fx = self.fixtures_by_url.get(url)
        if fx is None:
            return FetchedArticle(
                url=url, final_url=url, title="", source="", content="",
                content_scope="snippet", fetch_error="not_found",
            )
        return FetchedArticle(
            url=url,
            final_url=url,
            title=fx.title,
            source=fx.source,
            content=fx.content,
            content_scope=fx.content_scope,
            published_at=fx.published_at,
            fetch_error=fx.fetch_error,
        )


def build_mock_services(fixtures: list[MockArticleFixture]):
    provider = MockSearchProvider(fixtures=fixtures)
    fetcher = MockFetcher(fixtures_by_url={fx.url: fx for fx in fixtures})
    return provider, fetcher
