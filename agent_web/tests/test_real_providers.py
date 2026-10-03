from __future__ import annotations

from datetime import date, datetime

import pytest

from agent_web.providers import real_crawlers as rc
from agent_web.providers.cafef_reports_provider import (
    CafefFinancialReportsFetcher,
    CafefFinancialReportsProvider,
)
from agent_web.providers.composite_provider import (
    CompositeArticleFetcher,
    CompositeSearchProvider,
)
from agent_web.providers.vietstock_provider import VietstockFetcher, VietstockSearchProvider


# ---------------------------------------------------------------------------
# VietstockSearchProvider / VietstockFetcher — monkeypatch real_crawlers,
# không mở Selenium thật.
# ---------------------------------------------------------------------------

async def test_vietstock_search_filters_by_date(monkeypatch):
    def fake_crawl_vietstock_news(stock_code, *, target_count, headless):
        assert stock_code == "FPT"
        return [
            rc.NewsHit(title="Tin cũ", link="https://finance.vietstock.vn/FPT/2020/01/01/a.htm", index=1),
            rc.NewsHit(title="Tin mới", link="https://finance.vietstock.vn/FPT/2026/06/01/b.htm", index=2),
        ]

    monkeypatch.setattr(rc, "crawl_vietstock_news", fake_crawl_vietstock_news)

    provider = VietstockSearchProvider()
    hits = await provider.search(
        "FPT", date_from=date(2026, 1, 1), date_to=date(2026, 12, 31), max_results=10
    )

    assert len(hits) == 1
    assert hits[0].title == "Tin mới"


async def test_vietstock_fetch_blocks_unsafe_url():
    fetcher = VietstockFetcher()
    result = await fetcher.fetch("http://127.0.0.1/internal", timeout_s=5.0)
    assert result.fetch_error == "blocked_unsafe_url"
    assert result.content == ""


async def test_vietstock_fetch_handles_crawler_error(monkeypatch):
    def boom(url, *, headless, timeout_s):
        raise TimeoutError("selenium timeout")

    monkeypatch.setattr(rc, "crawl_vietstock_article", boom)
    fetcher = VietstockFetcher()
    result = await fetcher.fetch("https://finance.vietstock.vn/FPT/a.htm", timeout_s=5.0)
    assert result.fetch_error is not None
    assert result.content_scope == "snippet"


# ---------------------------------------------------------------------------
# CafefFinancialReportsProvider
# ---------------------------------------------------------------------------

async def test_cafef_reports_provider_maps_to_hits(monkeypatch):
    def fake_reports(stock_code, exchange):
        return [
            rc.FinancialReportItem(
                name="BCTC hop nhat quy 1 nam 2026",
                link="https://cafef.vn/x.pdf",
                year=2026,
                report_type="consolidated",
            )
        ]

    monkeypatch.setattr(rc, "crawl_financial_reports", fake_reports)
    provider = CafefFinancialReportsProvider()
    hits = await provider.search("FPT", date_from=None, date_to=None, max_results=10)

    assert len(hits) == 1
    assert hits[0].source == "Cafef-BCTC"
    assert hits[0].url == "https://cafef.vn/x.pdf"


async def test_cafef_reports_provider_swallows_errors(monkeypatch):
    def boom(stock_code, exchange):
        raise RuntimeError("API down")

    monkeypatch.setattr(rc, "crawl_financial_reports", boom)
    provider = CafefFinancialReportsProvider()
    hits = await provider.search("FPT", date_from=None, date_to=None, max_results=10)
    assert hits == []  # lỗi 1 nguồn không được raise ra ngoài


async def test_cafef_reports_fetcher_returns_snippet_metadata():
    fetcher = CafefFinancialReportsFetcher()
    result = await fetcher.fetch("https://cafef.vn/report.pdf", timeout_s=5.0)
    assert result.content_scope == "snippet"
    assert "PDF" in result.content


# ---------------------------------------------------------------------------
# CompositeSearchProvider / CompositeArticleFetcher
# ---------------------------------------------------------------------------

class _StubProvider:
    def __init__(self, hits=None, raise_error=False):
        self.hits = hits or []
        self.raise_error = raise_error

    async def search(self, query, *, date_from, date_to, max_results):
        if self.raise_error:
            raise RuntimeError("provider lỗi")
        return self.hits


async def test_composite_search_merges_and_isolates_failures():
    from agent_web.interfaces import RawSearchHit

    ok_provider = _StubProvider(hits=[RawSearchHit(title="A", url="u1", source="S1")])
    bad_provider = _StubProvider(raise_error=True)

    composite = CompositeSearchProvider([ok_provider, bad_provider])
    hits = await composite.search("FPT", date_from=None, date_to=None, max_results=10)

    assert len(hits) == 1  # provider lỗi bị bỏ qua, không làm hỏng kết quả provider kia
    assert hits[0].title == "A"


async def test_composite_fetcher_routes_by_host():
    class _FakeFetcher:
        def __init__(self, tag):
            self.tag = tag

        async def fetch(self, url, *, timeout_s):
            from agent_web.interfaces import FetchedArticle

            return FetchedArticle(
                url=url, final_url=url, title=self.tag, source=self.tag,
                content="", content_scope="snippet",
            )

    composite = CompositeArticleFetcher(
        fetchers_by_host_substring={"cafef.vn": _FakeFetcher("cafef")},
        default=_FakeFetcher("default"),
    )

    r1 = await composite.fetch("https://cafef.vn/x.pdf", timeout_s=5.0)
    r2 = await composite.fetch("https://finance.vietstock.vn/y.htm", timeout_s=5.0)
    assert r1.title == "cafef"
    assert r2.title == "default"
