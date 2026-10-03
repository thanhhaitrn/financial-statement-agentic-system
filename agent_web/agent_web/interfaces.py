"""
Các interface (Protocol) mà agent_web phụ thuộc vào — KHÔNG import bất kỳ
provider cụ thể nào ở đây. Điều này cho phép:

- Thay search provider (Vietstock, Google News, v.v.) mà không sửa pipeline.
- Mock toàn bộ để unit test chạy không cần API key / network (mục 9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable

from .config import AgentWebConfig


@dataclass
class RawSearchHit:
    """Một kết quả tìm kiếm thô, trước khi fetch nội dung đầy đủ."""
    title: str
    url: str
    source: str
    snippet: str = ""
    published_at: datetime | None = None


@dataclass
class FetchedArticle:
    """Nội dung một bài báo sau khi fetch."""
    url: str
    final_url: str  # sau redirect
    title: str
    source: str
    content: str
    content_scope: str  # "full_text" | "snippet"
    published_at: datetime | None = None
    fetch_error: str | None = None


@runtime_checkable
class SearchProvider(Protocol):
    """Thực hiện tìm kiếm bài báo. KHÔNG chịu trách nhiệm đánh giá relevance
    hay sentiment — chỉ trả kết quả tìm kiếm thô (mục 4: 'Provider thực
    hiện search/fetch')."""

    async def search(
        self,
        query: str,
        *,
        date_from: date | None,
        date_to: date | None,
        max_results: int,
    ) -> list[RawSearchHit]:
        ...


@runtime_checkable
class ArticleFetcher(Protocol):
    """Tải nội dung đầy đủ của 1 URL. Phải tự chặn địa chỉ mạng nội bộ,
    giới hạn dung lượng, không bypass paywall/CAPTCHA (mục 8)."""

    async def fetch(self, url: str, *, timeout_s: float) -> FetchedArticle:
        ...


@runtime_checkable
class ModelClient(Protocol):
    """Bọc lời gọi LLM. Model quyết định query/relevance/nhóm sự kiện/
    sentiment (mục 4) — code chỉ validate schema JSON trả về."""

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2000,
    ) -> dict:
        """Trả một dict đã parse từ JSON. Phải raise ValueError nếu model
        trả JSON không hợp lệ (pipeline sẽ retry có giới hạn)."""
        ...

    @property
    def model_name(self) -> str:
        ...


@dataclass
class WebAgentServices:
    """Bundle mọi dependency mà run_agent_web cần — inject 1 lần duy nhất.
    Đây là điểm để test truyền mock, hoặc production truyền provider thật."""

    search_provider: SearchProvider
    fetcher: ArticleFetcher
    model_client: ModelClient
    config: AgentWebConfig = field(default_factory=AgentWebConfig)
