"""
Adapter search/fetch cho tin tức Vietstock — dùng crawl function #3
(real_crawlers.crawl_vietstock_news / crawl_vietstock_article).

Yêu cầu chạy thật: `pip install selenium` + có chromedriver tương thích
Chrome trên máy chạy agent. Vì đây là phụ thuộc nặng (mở trình duyệt),
provider này chỉ import selenium khi thật sự gọi search()/fetch(), để
agent_web vẫn import được bình thường ở nơi không cài selenium (vd CI
chỉ chạy unit test với mock).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from datetime import date, datetime
from urllib.parse import urlparse

from ..interfaces import FetchedArticle, RawSearchHit
from . import real_crawlers as rc

MAX_CONTENT_CHARS = 50_000  # giới hạn dung lượng nội dung giữ lại (mục 8)


def _is_internal_host(host: str) -> bool:
    """Chặn địa chỉ mạng nội bộ / loopback / link-local (SSRF guard, mục 8)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True  # không resolve được -> an toàn hơn là chặn
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return True
    return False


def is_safe_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.hostname:
        return False
    return not _is_internal_host(parsed.hostname)


class VietstockSearchProvider:
    """SearchProvider thật: liệt kê tin theo mã CK trên finance.vietstock.vn.

    Lưu ý: Vietstock tổ chức tin theo MÃ CHỨNG KHOÁN, không theo từ khoá tự
    do như Google. Vì vậy `query` ở đây được hiểu là ticker (VD "FPT"), còn
    phần lọc theo chủ đề/từ khoá trong `query` do bước relevance-filter của
    model đảm nhiệm ở pipeline.py — provider chỉ trả TOÀN BỘ tin gần đây của
    mã đó, không tự lọc theo nghĩa của query.
    """

    def __init__(self, *, headless: bool = True, max_fetch_per_search: int = 30):
        self.headless = headless
        self.max_fetch_per_search = max_fetch_per_search

    async def search(
        self,
        query: str,
        *,
        date_from: date | None,
        date_to: date | None,
        max_results: int,
    ) -> list[RawSearchHit]:
        stock_code = query.strip().split()[0].upper() if query.strip() else ""
        if not stock_code:
            return []

        target_count = min(max(max_results, 10), self.max_fetch_per_search)
        hits = await asyncio.to_thread(
            rc.crawl_vietstock_news,
            stock_code,
            target_count=target_count,
            headless=self.headless,
        )

        results: list[RawSearchHit] = []
        for hit in hits:
            published = rc.extract_date_from_url(hit.link)
            published_at = (
                datetime(published.year, published.month, published.day)
                if published
                else None
            )
            if date_from and published and published < date_from:
                continue
            if date_to and published and published > date_to:
                continue
            results.append(
                RawSearchHit(
                    title=hit.title,
                    url=hit.link,
                    source="Vietstock",
                    snippet="",
                    published_at=published_at,
                )
            )
        return results[:max_results]


class VietstockFetcher:
    """ArticleFetcher thật: tải nội dung chi tiết 1 bài Vietstock."""

    def __init__(self, *, headless: bool = True):
        self.headless = headless

    async def fetch(self, url: str, *, timeout_s: float) -> FetchedArticle:
        if not is_safe_url(url):
            return FetchedArticle(
                url=url,
                final_url=url,
                title="",
                source="Vietstock",
                content="",
                content_scope="snippet",
                fetch_error="blocked_unsafe_url",
            )
        try:
            data = await asyncio.to_thread(
                rc.crawl_vietstock_article, url, headless=self.headless, timeout_s=timeout_s
            )
        except Exception as exc:  # noqa: BLE001 — không để lỗi crawler làm sập pipeline
            return FetchedArticle(
                url=url,
                final_url=url,
                title="",
                source="Vietstock",
                content="",
                content_scope="snippet",
                fetch_error=f"{type(exc).__name__}: {exc}",
            )

        content = (data.get("content") or "")[:MAX_CONTENT_CHARS]
        return FetchedArticle(
            url=url,
            final_url=url,
            title=data.get("title", ""),
            source="Vietstock",
            content=content,
            content_scope="full_text" if len(content) > 100 else "snippet",
            published_at=None,  # ngày hiển thị dạng text tự do, không parse chắc chắn -> để None thay vì đoán
        )
