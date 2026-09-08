"""Cache-first Vietstock company-news provider.

The provider owns freshness and structured provenance. Network/browser work is
hidden behind an injected fetcher so the LangGraph node never launches Chrome.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from threading import Lock
from typing import Callable, Iterable
from urllib.parse import urljoin
from urllib.request import Request

from tools.http_safety import allowed_https_url, allowlisted_opener

from schemas.web_evidence import (WebEvidence, WebEvidenceBatch,
                                  WebEvidenceRequest)
from web_evidence.cache import WebEvidenceCache

VIETSTOCK_HOSTS = {"vietstock.vn", "finance.vietstock.vn"}
VIETSTOCK_URL = "https://finance.vietstock.vn/{ticker}/tin-tuc-su-kien.htm"
USER_AGENT = "AgentFinX/0.2 (+financial-evidence-crawler)"


def _allowed_vietstock_url(value: str) -> bool:
    return allowed_https_url(value, ("vietstock.vn",))


class _NewsLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._href = ""
        self._capture = False
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() != "a":
            return
        values = dict(attrs)
        css_class = str(values.get("class", "") or "")
        href = str(values.get("href", "") or "")
        if href and ("stock-news__title" in css_class or "tin-tuc" in href):
            self._href = href
            self._capture = True
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._capture:
            title = " ".join("".join(self._text).split())
            if title and self._href:
                self.links.append({"title": title, "source_url": self._href})
            self._href = ""
            self._capture = False
            self._text = []


class _ArticleParser(HTMLParser):
    CONTENT_TAGS = {"p", "h2", "h3", "li", "td"}
    CONTAINERS = {"news-content", "article-content", "detail-content", "news-detail"}
    EXCLUDED = {"nav", "footer", "aside", "script", "style"}
    VOID_TAGS = {"meta", "link", "img", "br", "hr", "input", "source", "wbr"}

    def __init__(self):
        super().__init__()
        self.title = ""
        self.published_at = ""
        self._stack: list[tuple[str, bool]] = []
        self._text: list[str] = []
        self._title_text: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        values = dict(attrs)
        # HTML permits omitted paragraph end tags.
        if tag == "p" and any(item[0] == "p" for item in self._stack):
            self.handle_endtag("p")
        classes = set(str(values.get("class") or "").split())
        if tag not in self.VOID_TAGS:
            self._stack.append((tag, tag == "article" or bool(classes & self.CONTAINERS)))
        if tag == "time":
            self.published_at = str(values.get("datetime", "") or "").strip()
        if tag == "meta" and values.get("property") == "article:published_time":
            self.published_at = str(values.get("content") or "").strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "h1":
            self.title = " ".join("".join(self._title_text).split())
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        tags = {item[0] for item in self._stack}
        if tags & self.EXCLUDED:
            return
        if "h1" in tags:
            self._title_text.append(data)
        if tags & self.CONTENT_TAGS and any(item[1] for item in self._stack):
            text = " ".join(data.split())
            if text:
                self._text.append(text)

    @property
    def content(self) -> str:
        return "\n".join(self._text)


class VietstockHttpFetcher:
    """Bounded HTTP fallback suitable for a refresh worker.

    A deployment may inject a pooled-browser fetcher when Vietstock requires
    dynamic load-more behaviour; the provider contract stays unchanged.
    """

    def __init__(self, *, timeout_seconds: int = 15, max_body_bytes: int = 50_000, opener=None):
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.opener = opener or allowlisted_opener(_allowed_vietstock_url)
        self.diagnostics = Counter()

    def _get(self, url: str) -> tuple[str, str]:
        if not _allowed_vietstock_url(url):
            raise ValueError("Vietstock fetch URL is outside the allowed hosts")
        request = Request(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "vi,en;q=0.8"},
        )
        with self.opener(request, timeout=self.timeout_seconds) as response:
            final_url = response.geturl()
            if not _allowed_vietstock_url(final_url):
                raise ValueError("Vietstock redirected outside the allowed hosts")
            raw = response.read(self.max_body_bytes + 1)
            if len(raw) > self.max_body_bytes:
                raw = raw[: self.max_body_bytes]
            charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace"), final_url

    def __call__(self, ticker: str, limit: int) -> list[dict]:
        listing_url = VIETSTOCK_URL.format(ticker=ticker.upper())
        listing_html, _ = self._get(listing_url)
        parser = _NewsLinkParser()
        parser.feed(listing_html)
        output = []
        seen = set()
        for item in parser.links:
            url = urljoin(listing_url, item["source_url"])
            if url in seen or not _allowed_vietstock_url(url):
                continue
            seen.add(url)
            try:
                article_html, final_url = self._get(url)
                parsed_article = parse_vietstock_article(article_html)
            except Exception:
                self.diagnostics["article_fetch_failed"] += 1
                continue
            content = parsed_article["content"]
            if len(content) < 80:
                self.diagnostics["article_dropped_short"] += 1
                continue
            output.append(
                {
                    "title": parsed_article["title"] or item["title"],
                    "content": content,
                    "source_url": final_url,
                    "published_at": parsed_article["published_at"],
                }
            )
            if len(output) >= limit:
                break
        return output


def parse_vietstock_article(html: str) -> dict[str, str]:
    parser = _ArticleParser()
    parser.feed(html)
    return {
        "title": parser.title or " ".join("".join(parser._title_text).split()),
        "content": parser.content,
        "published_at": parser.published_at,
    }


class VietstockNewsProvider:
    def __init__(
        self,
        *,
        cache: WebEvidenceCache,
        fetcher: Callable[[str, int], Iterable[dict]],
        cache_ttl_seconds: int = 6 * 60 * 60,
        miss_wait_seconds: int = 10,
        max_refresh_articles: int = 20,
        refresh_concurrency: int = 1,
        proactive_refresh_admitter: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.cache = cache
        self.fetcher = fetcher
        self.cache_ttl_seconds = cache_ttl_seconds
        self.miss_wait_seconds = miss_wait_seconds
        self.max_refresh_articles = max_refresh_articles
        self.proactive_refresh_admitter = proactive_refresh_admitter
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._executor = ThreadPoolExecutor(
            max_workers=refresh_concurrency,
            thread_name_prefix="vietstock-refresh",
        )
        self._pending: dict[str, Future] = {}
        self._pending_lock = Lock()
        self._closed = False

    def close(self, *, wait: bool = True) -> None:
        with self._pending_lock:
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    @property
    def provider_identity(self) -> str:
        return "vietstock-news-v2"

    def _refresh(self, request: WebEvidenceRequest) -> list[WebEvidence]:
        raw_items = list(self.fetcher(request.ticker, self.max_refresh_articles))
        retrieved_at = self._now().replace(microsecond=0).isoformat()
        evidence = []
        seen_urls = set()
        for raw in raw_items:
            url = str(raw.get("source_url") or raw.get("url") or "").strip()
            title = str(raw.get("title", "") or "").strip()
            content = str(raw.get("content", "") or "").strip()
            if not url or url in seen_urls or not title or not content:
                continue
            if not _allowed_vietstock_url(url):
                continue
            seen_urls.add(url)
            evidence.append(
                WebEvidence(
                    ticker=request.ticker,
                    company=request.company,
                    title=title,
                    content=content,
                    source_url=url,
                    publisher="Vietstock",
                    published_at=str(raw.get("published_at", "") or ""),
                    retrieved_at=retrieved_at,
                    query=request.query,
                )
            )
        self.cache.put_many(evidence)
        return evidence

    def _schedule(self, request: WebEvidenceRequest) -> tuple[Future, bool]:
        key = request.ticker
        with self._pending_lock:
            if self._closed:
                raise RuntimeError("Vietstock provider is closed")
            pending = self._pending.get(key)
            if pending is not None and not pending.done():
                return pending, False
            future = self._executor.submit(self._refresh, request)
            self._pending[key] = future
        # A completed Future invokes callbacks synchronously: register outside
        # the lock to avoid deadlocking fast refreshes.
        def cleanup(done):
            with self._pending_lock:
                if self._pending.get(key) is done:
                    self._pending.pop(key, None)
        future.add_done_callback(cleanup)
        return future, True

    def search(self, request: WebEvidenceRequest) -> WebEvidenceBatch:
        if request.intent != "company_news":
            return self._batch(
                status="unsupported",
                message="The configured provider only supports company_news.",
                provider=self.provider_identity,
                cache_status="unsupported",
            )
        if not request.ticker:
            return self._batch(
                status="unsupported",
                message="A ticker is required for Vietstock company news.",
                provider=self.provider_identity,
                cache_status="unsupported",
            )

        if (
            request.force_refresh
            and request.owner_id
            and self.proactive_refresh_admitter is not None
        ):
            self.proactive_refresh_admitter(request.owner_id)

        cached = self.cache.get(ticker=request.ticker, limit=request.limit)
        age = self.cache.age_seconds(cached, now=self._now())
        fresh = age is not None and age <= self.cache_ttl_seconds
        if cached and fresh and not request.force_refresh:
            return self._batch(
                status="found",
                evidence=cached,
                provider=self.provider_identity,
                cache_status="fresh",
            )

        future, scheduled = self._schedule(request)
        if cached and not request.force_refresh:
            return self._batch(
                status="found",
                evidence=cached,
                message="Serving stale evidence while a refresh runs.",
                provider=self.provider_identity,
                cache_status="stale",
                refresh_scheduled=True,
            )

        try:
            refreshed = future.result(timeout=self.miss_wait_seconds)
        except TimeoutError:
            return self._batch(
                status="not_found_after_search",
                message="News refresh is still running.",
                provider=self.provider_identity,
                cache_status="miss",
                refresh_scheduled=True,
            )
        except Exception as exc:
            return self._batch(
                status="error",
                message=f"News refresh failed: {type(exc).__name__}",
                provider=self.provider_identity,
                cache_status="miss",
            )

        return self._batch(
            status="found" if refreshed else "not_found_after_search",
            evidence=refreshed[: request.limit],
            message="" if refreshed else "No company news was found.",
            provider=self.provider_identity,
            cache_status="miss",
            refresh_scheduled=scheduled,
        )

    def _batch(self, **kwargs) -> WebEvidenceBatch:
        return WebEvidenceBatch(
            diagnostics=dict(getattr(self.fetcher, "diagnostics", {}) or {}), **kwargs,
        )
