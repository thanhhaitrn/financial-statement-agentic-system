"""
SearchProvider dùng crawl function #1 (real_crawlers.crawl_financial_reports).

Khác với tin tức, mỗi "báo cáo tài chính mới công bố" tự nó là MỘT SỰ KIỆN
(event) đáng đưa vào phân tích sentiment (VD: công bố lãi/lỗ). Ta đóng gói
nó thành RawSearchHit / FetchedArticle bình thường để tái dùng đúng luồng
pipeline hiện có (search -> fetch -> relevance -> group -> score) mà không
cần sửa pipeline.py.

Vì đây không phải file PDF đã OCR/parse nội dung, `content` chỉ gồm
metadata (tên báo cáo, loại, năm) — KHÔNG bịa nội dung tài chính. content
này chỉ đủ để model biết "có báo cáo Quý x/Năm y vừa được công bố", không
đủ để chấm sentiment chi tiết số liệu — provider set content_scope="snippet"
để pipeline hiển thị đúng giới hạn này trong limitations (mục 5).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

from ..interfaces import FetchedArticle, RawSearchHit
from . import real_crawlers as rc


class CafefFinancialReportsProvider:
    """SearchProvider: `query` được hiểu là ticker (VD "FPT"), exchange lấy
    từ `default_exchange` (không có trong RawSearchHit protocol nên phải
    cấu hình sẵn ở constructor thay vì suy luận)."""

    def __init__(self, *, default_exchange: str = "HOSE"):
        self.default_exchange = default_exchange

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

        try:
            reports = await asyncio.to_thread(
                rc.crawl_financial_reports, stock_code, self.default_exchange
            )
        except Exception:
            # Provider lỗi -> trả rỗng, KHÔNG raise để không làm hỏng cả vòng
            # search (các provider khác trong composite vẫn chạy tiếp).
            return []

        hits: list[RawSearchHit] = []
        for report in reports:
            if report.year is None:
                published_at = None
            else:
                published_at = datetime(report.year, 12, 31)  # chỉ biết năm, không biết ngày chính xác
            if date_from and published_at and published_at.date() < date_from:
                continue
            if date_to and published_at and published_at.date() > date_to:
                continue
            hits.append(
                RawSearchHit(
                    title=f"Báo cáo tài chính: {report.name}",
                    url=report.link,
                    source="Cafef-BCTC",
                    snippet=f"Loại: {report.report_type}",
                    published_at=published_at,
                )
            )
        return hits[:max_results]


class CafefFinancialReportsFetcher:
    """ArticleFetcher tương ứng: KHÔNG tải/parse nội dung PDF (ngoài phạm
    vi agent này) — chỉ trả lại metadata làm content, và luôn đánh dấu
    content_scope="snippet" để pipeline biết đây không phải toàn văn."""

    async def fetch(self, url: str, *, timeout_s: float) -> FetchedArticle:
        return FetchedArticle(
            url=url,
            final_url=url,
            title=url.rsplit("/", 1)[-1],
            source="Cafef-BCTC",
            content=(
                "Đây là liên kết tới file PDF báo cáo tài chính gốc trên "
                "Cafef. Agent chưa OCR/parse nội dung PDF — chỉ xác nhận "
                "SỰ KIỆN công bố báo cáo, không có số liệu chi tiết."
            ),
            content_scope="snippet",
        )
