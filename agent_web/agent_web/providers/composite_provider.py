"""
Ghép nhiều SearchProvider/ArticleFetcher thành một, để pipeline.py (vốn chỉ
biết tới MỘT search_provider và MỘT fetcher trong WebAgentServices) có thể
dùng đồng thời cả 3 nguồn dữ liệu thật:

    1. Vietstock news       (real_crawlers.crawl_vietstock_news/_article)
    2. Cafef báo cáo TC     (real_crawlers.crawl_financial_reports)
    3. Cafef danh mục mã CK (real_crawlers.crawl_stock_directory)
       -> dùng để RESOLVE company_name/ticker, không tham gia search/fetch
          trực tiếp (xem resolve_target() bên dưới), vì nó là dữ liệu tra
          cứu tĩnh chứ không phải "tin tức" theo thời gian.

Thiết kế: fan-out song song tới từng sub-provider trong search(), gộp kết
quả; fetch() thì định tuyến theo domain của URL tới đúng sub-fetcher — vì
FetchedArticle không tự mang theo "provider nào đã tìm ra nó", nên phải suy
ra lại từ URL (nguồn dữ liệu duy nhất đáng tin ở bước này).
"""

from __future__ import annotations

import asyncio
from datetime import date
from urllib.parse import urlparse

from ..interfaces import ArticleFetcher, FetchedArticle, RawSearchHit, SearchProvider


class CompositeSearchProvider:
    def __init__(self, providers: list[SearchProvider]):
        self.providers = providers

    async def search(
        self,
        query: str,
        *,
        date_from: date | None,
        date_to: date | None,
        max_results: int,
    ) -> list[RawSearchHit]:
        results_per_provider = await asyncio.gather(
            *[
                p.search(query, date_from=date_from, date_to=date_to, max_results=max_results)
                for p in self.providers
            ],
            return_exceptions=True,
        )
        merged: list[RawSearchHit] = []
        for result in results_per_provider:
            if isinstance(result, Exception):
                # 1 nguồn lỗi không được kéo sập cả search (mục 8: chịu lỗi
                # từng phần) — lỗi này sẽ khiến evidence ít hơn, pipeline vẫn
                # trả partial/insufficient_evidence đúng bản chất, không crash.
                continue
            merged.extend(result)
        return merged[:max_results]


class CompositeArticleFetcher:
    """Định tuyến fetch() theo hostname của URL tới đúng fetcher con."""

    def __init__(self, fetchers_by_host_substring: dict[str, ArticleFetcher], default: ArticleFetcher):
        self.fetchers_by_host_substring = fetchers_by_host_substring
        self.default = default

    async def fetch(self, url: str, *, timeout_s: float) -> FetchedArticle:
        host = (urlparse(url).hostname or "").lower()
        for substring, fetcher in self.fetchers_by_host_substring.items():
            if substring in host:
                return await fetcher.fetch(url, timeout_s=timeout_s)
        return await self.default.fetch(url, timeout_s=timeout_s)


def build_real_services_providers(*, headless: bool = True, default_exchange: str = "HOSE"):
    """Factory tiện dụng: dựng sẵn CompositeSearchProvider + CompositeArticleFetcher
    ghép cả 3 crawl function thật, để cli.py / code gọi agent chỉ cần 1 dòng.
    """
    from .cafef_reports_provider import CafefFinancialReportsFetcher, CafefFinancialReportsProvider
    from .vietstock_provider import VietstockFetcher, VietstockSearchProvider

    vietstock_search = VietstockSearchProvider(headless=headless)
    vietstock_fetch = VietstockFetcher(headless=headless)
    cafef_reports_search = CafefFinancialReportsProvider(default_exchange=default_exchange)
    cafef_reports_fetch = CafefFinancialReportsFetcher()

    search_provider = CompositeSearchProvider([vietstock_search, cafef_reports_search])
    fetcher = CompositeArticleFetcher(
        fetchers_by_host_substring={"cafef.vn": cafef_reports_fetch},
        default=vietstock_fetch,  # mọi domain khác (vietstock.vn...) đi qua fetcher Vietstock
    )
    return search_provider, fetcher
