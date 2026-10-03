"""
Resolve company_name <-> ticker bằng crawl function #2 (danh mục mã CK
Cafef), dùng TRƯỚC khi gọi run_agent_web — không đụng vào pipeline.py, vì
pipeline chủ trương "không đoán doanh nghiệp mục tiêu" (mục 3) khi thiếu cả
ticker lẫn company_name. Đây là bước bổ sung ở tầng gọi (CLI/API) để tăng
tỉ lệ resolve thành công MÀ VẪN giữ nguyên tắc "không chắc thì hỏi lại":
nếu khớp mơ hồ (0 hoặc nhiều kết quả), trả None và để caller tự quyết định
hỏi lại người dùng, thay vì đoán.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from .providers import real_crawlers as rc

_CACHE: dict[str, tuple[float, list[rc.StockDirectoryItem]]] = {}
_CACHE_TTL_S = 3600.0  # danh mục mã CK ít đổi trong ngày -> cache 1 giờ


async def _get_directory(url: str = rc.DEFAULT_SCREENER_URL) -> list[rc.StockDirectoryItem]:
    cached = _CACHE.get(url)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_S:
        return cached[1]
    directory = await asyncio.to_thread(rc.crawl_stock_directory, url)
    _CACHE[url] = (time.monotonic(), directory)
    return directory


@dataclass
class ResolvedTarget:
    ticker: str
    company_name: str
    exchange: str


async def resolve_target(query: str) -> ResolvedTarget | None:
    """query có thể là ticker (VD "FPT") hoặc tên công ty (VD "FPT Corp").
    Trả None nếu không resolve chắc chắn được — caller nên trả
    needs_clarification thay vì tự đoán."""
    directory = await _get_directory()
    match = rc.resolve_company(query, directory)
    if match is None:
        return None
    return ResolvedTarget(ticker=match.symbol, company_name=match.name, exchange=match.exchange)
