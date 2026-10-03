"""
Loại trùng — lớp 1: URL / nội dung giống nhau (mục 5).
Lớp 2 (nhiều bài cùng 1 sự kiện) do MODEL quyết định khi group theo sự kiện
(pipeline.py, bước group_into_events) — code ở đây chỉ đảm bảo lớp 1, vì
lớp 2 cần hiểu ngữ nghĩa mà code không hardcode được.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from .interfaces import FetchedArticle


def normalize_url(url: str) -> str:
    """Chuẩn hoá URL để so khớp trùng: bỏ query tracking phổ biến, fragment,
    trailing slash, lowercase host."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    # bỏ các query param tracking phổ biến (utm_*, fbclid, gclid...)
    query_pairs = [
        kv for kv in parts.query.split("&")
        if kv and not re.match(r"^(utm_|fbclid|gclid|ref)", kv, re.I)
    ]
    query = "&".join(sorted(query_pairs))
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def content_hash(content: str) -> str:
    """Hash nội dung sau khi chuẩn hoá khoảng trắng — dùng để phát hiện
    đăng lại (republish) với URL khác nhau nhưng nội dung giống nhau."""
    normalized = re.sub(r"\s+", " ", content.strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def dedup_articles(
    articles: list[FetchedArticle],
) -> tuple[list[FetchedArticle], int]:
    """Loại trùng theo URL chuẩn hoá hoặc content_hash giống nhau.
    Trả về (danh sách sau khi lọc, số bài đã loại)."""
    seen_urls: set[str] = set()
    seen_hashes: set[str] = set()
    kept: list[FetchedArticle] = []
    removed = 0

    for art in articles:
        norm_url = normalize_url(art.final_url or art.url)
        h = content_hash(art.content) if art.content else None

        is_dup = norm_url in seen_urls or (h is not None and h in seen_hashes)
        if is_dup:
            removed += 1
            continue

        seen_urls.add(norm_url)
        if h is not None:
            seen_hashes.add(h)
        kept.append(art)

    return kept, removed
