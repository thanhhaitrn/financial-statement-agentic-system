"""Cache-first Vietstock company-news provider.

The provider owns freshness and structured provenance. Network work is hidden
behind an injected fetcher so the LangGraph node never launches a browser.

Vietstock renders its news list with JavaScript from a JSON listing
(``POST /data/GetNews``); the HTML page itself holds no article links. The
HTTP fetcher reads that listing like the page does, then each article page for
its text. Every URL it opens, and every redirect, must stay on vietstock.vn.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from threading import Lock
from typing import Callable, Iterable, Optional
from urllib.parse import urlencode, urljoin
from urllib.request import Request

from agentfinx.core.schemas.web_evidence import (WebEvidence, WebEvidenceBatch,
                                  WebEvidenceRequest)
from agentfinx.agents.tools.http_safety import allowed_https_url, allowlisted_opener
from agentfinx.news.collect.cache import Coverage, WebEvidenceCache
from agentfinx.news.dates import iso_utc, parse_published
from agentfinx.news.topics import (has_topic, topic_score, topic_terms,
                                 without_common_terms)

VIETSTOCK_HOSTS = {"vietstock.vn", "finance.vietstock.vn"}
VIETSTOCK_URL = "https://finance.vietstock.vn/{ticker}/tin-tuc-su-kien.htm"
NEWS_LISTING_URL = "https://finance.vietstock.vn/data/GetNews"
ARTICLE_BASE_URL = "https://vietstock.vn/"
# type=1 lists one company's news; type=2 is market-wide news.
COMPANY_NEWS_TYPE = 1
LISTING_PAGE_SIZE = 20
LISTING_MAX_BYTES = 400_000
MIN_ARTICLE_CHARS = 80
USER_AGENT = "AgentFinX/0.3 (+financial-evidence-crawler)"
# Window rows read from the cache to rank for one request.
WINDOW_ROWS_MAX = 300


def _allowed_vietstock_url(value: str) -> bool:
    return allowed_https_url(value, ("vietstock.vn",))


def _absolute_article_url(value: str) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    if not url.startswith("https://"):
        url = urljoin(ARTICLE_BASE_URL, url.lstrip("/"))
    return url


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
        # Raw fragments of the paragraph being read: a link or <span> inside
        # a sentence must not add a space before the next comma.
        self._block: list[str] = []
        self._title_text: list[str] = []

    def _flush(self) -> None:
        text = " ".join("".join(self._block).split())
        if text:
            self._text.append(text)
        self._block = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        values = dict(attrs)
        # HTML permits omitted paragraph end tags.
        if tag == "p" and any(item[0] == "p" for item in self._stack):
            self.handle_endtag("p")
        classes = set(str(values.get("class") or "").split())
        if tag in self.CONTENT_TAGS:
            self._flush()
        elif tag == "br":
            self._block.append(" ")
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
        if tag in self.CONTENT_TAGS:
            self._flush()
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
            self._block.append(data)

    @property
    def content(self) -> str:
        pending = " ".join("".join(self._block).split())
        return "\n".join(self._text + ([pending] if pending else []))


def parse_vietstock_article(html: str) -> dict[str, str]:
    parser = _ArticleParser()
    parser.feed(html)
    return {
        "title": parser.title or " ".join("".join(parser._title_text).split()),
        "content": parser.content,
        "published_at": parser.published_at,
    }


class VietstockHttpFetcher:
    """Company news over plain HTTPS, bounded in size and time.

    Called as ``fetcher(ticker, limit, since=, until=, deadline=)`` and returns
    ``{"items": [...], "covered_from": datetime, "covered_until": datetime}``:
    news published in [covered_from, covered_until] has been listed. Listing
    pages are walked back until ``since`` is reached, the source runs out, the
    page cap or the deadline. Articles outside the window are dropped before
    ``limit`` is applied. An article page that cannot be read keeps its
    listing summary (``content_status="summary_only"``).
    """

    supports_window = True

    def __init__(
        self,
        *,
        timeout_seconds: float = 15,
        max_body_bytes: int = 800_000,
        max_listing_pages: int = 5,
        deadline_seconds: float = 45,
        opener=None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self.max_listing_pages = max_listing_pages
        self.deadline_seconds = deadline_seconds
        self.opener = opener or allowlisted_opener(_allowed_vietstock_url)
        self.diagnostics = Counter()
        self._clock = clock
        self._now = now or (lambda: datetime.now(timezone.utc))

    def _timeout(self, deadline: Optional[float]) -> float:
        if deadline is None:
            return self.timeout_seconds
        left = deadline - self._clock()
        if left <= 0:
            raise TimeoutError("news refresh deadline reached")
        return min(self.timeout_seconds, left)

    def _open(self, request: Request, deadline: Optional[float], max_bytes: int) -> tuple[str, str, bool]:
        with self.opener(request, timeout=self._timeout(deadline)) as response:
            final_url = response.geturl()
            if not _allowed_vietstock_url(final_url):
                raise ValueError("Vietstock redirected outside the allowed hosts")
            raw = response.read(max_bytes + 1)
            charset = response.headers.get_content_charset() or "utf-8"
        truncated = len(raw) > max_bytes
        return raw[:max_bytes].decode(charset, errors="replace"), final_url, truncated

    def _get(self, url: str, deadline: Optional[float] = None) -> tuple[str, str]:
        if not _allowed_vietstock_url(url):
            raise ValueError("Vietstock fetch URL is outside the allowed hosts")
        request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Language": "vi,en;q=0.8"})
        text, final_url, truncated = self._open(request, deadline, self.max_body_bytes)
        if truncated:
            self.diagnostics["article_truncated"] += 1
        return text, final_url

    def _listing_page(self, ticker: str, page: int, deadline: Optional[float]) -> list[dict]:
        body = urlencode({"code": ticker, "type": COMPANY_NEWS_TYPE, "page": page,
                          "pageSize": LISTING_PAGE_SIZE}).encode("utf-8")
        request = Request(
            NEWS_LISTING_URL,
            data=body,
            method="POST",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": VIETSTOCK_URL.format(ticker=ticker),
            },
        )
        text, _final_url, truncated = self._open(request, deadline, LISTING_MAX_BYTES)
        if truncated:
            raise ValueError("Vietstock listing response is too large")
        rows = json.loads(text or "[]")
        if not isinstance(rows, list):
            raise ValueError("unexpected Vietstock listing shape")
        return [row for row in rows if isinstance(row, dict)]

    def _listing_item(self, row: dict, ticker: str) -> Optional[dict]:
        # The listing is per ticker, but each row is checked anyway: a row for
        # another company must never be filed under this one.
        if str(row.get("StockCode") or "").strip().upper() != ticker:
            self.diagnostics["listing_other_ticker"] += 1
            return None
        published = parse_published(row.get("PublishTime"))
        if published is None:
            self.diagnostics["listing_undated"] += 1
            return None
        url = _absolute_article_url(row.get("URL"))
        if not _allowed_vietstock_url(url):
            self.diagnostics["listing_url_rejected"] += 1
            return None
        title = " ".join(str(row.get("Title") or "").split())
        if not title:
            return None
        return {
            "title": title,
            "summary": " ".join(str(row.get("Head") or "").split()),
            "source_url": url,
            "published": published,
        }

    def list_news(
        self,
        ticker: str,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        deadline: Optional[float] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """The news list only (no article pages), newest first, walked back
        until ``since``, the source's own row count, the page cap, the
        deadline or ``limit`` items. ``covered_from``: news published since
        then has all been listed."""

        ticker = str(ticker or "").strip().upper()
        if deadline is None:
            deadline = self._clock() + self.deadline_seconds
        listed_at = self._now()
        listed: list[dict] = []
        seen: set[str] = set()
        reached_start = False
        exhausted = False
        total: Optional[int] = None
        rows_read = 0
        for page in range(1, self.max_listing_pages + 1):
            try:
                rows = self._listing_page(ticker, page, deadline)
            except TimeoutError:
                self.diagnostics["deadline_reached"] += 1
                break
            rows_read += len(rows)
            if rows:
                try:
                    total = int(rows[0].get("TotalRow") or 0) or total
                except (TypeError, ValueError):
                    pass
            # The history is only "all read" when the source's own row count
            # says so. A short or empty page alone (throttling, a hiccup) must
            # never be recorded as "no older news exists".
            if total is not None and rows_read >= total:
                exhausted = True
            if not rows:
                if not exhausted:
                    self.diagnostics["listing_empty_page"] += 1
                break
            for row in rows:
                item = self._listing_item(row, ticker)
                if item is None or item["source_url"] in seen:
                    continue
                seen.add(item["source_url"])
                listed.append(item)
            oldest = min((item["published"] for item in listed), default=None)
            if since is not None and oldest is not None and oldest <= since:
                reached_start = True
                break
            if limit is not None and since is None and len(listed) >= limit:
                break
            if exhausted:
                break

        oldest = min((item["published"] for item in listed), default=listed_at)
        if exhausted:
            covered_from = datetime(1970, 1, 1, tzinfo=timezone.utc)
        elif reached_start and since is not None:
            covered_from = since
        else:
            covered_from = oldest
        items = [
            {"title": item["title"], "summary": item["summary"], "source_url": item["source_url"],
             "published_at": iso_utc(item["published"])}
            for item in sorted(listed, key=lambda value: value["published"], reverse=True)
            if until is None or item["published"] <= until
        ]
        return {"items": items, "covered_from": covered_from, "covered_until": listed_at}

    def fetch_article(self, url: str, *, deadline: Optional[float] = None) -> dict:
        """One article page: its text, or ``summary_only`` when it cannot be
        read in time or is too short. Raises ``TimeoutError`` past the deadline."""

        try:
            html, final_url = self._get(url, deadline)
        except TimeoutError:
            self.diagnostics["deadline_reached"] += 1
            raise
        except Exception:
            self.diagnostics["article_fetch_failed"] += 1
            return {"content": "", "source_url": url, "content_status": "summary_only"}
        parsed = parse_vietstock_article(html)
        if len(parsed["content"]) < MIN_ARTICLE_CHARS:
            self.diagnostics["article_dropped_short"] += 1
            return {"content": "", "source_url": url, "content_status": "summary_only"}
        return {"content": parsed["content"], "source_url": final_url, "content_status": "full"}

    def __call__(
        self,
        ticker: str,
        limit: int,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        deadline: Optional[float] = None,
    ) -> dict:
        """List, then fetch the newest ``limit`` articles of the window."""

        if deadline is None:
            deadline = self._clock() + self.deadline_seconds
        listing = self.list_news(ticker, since=since, until=until, deadline=deadline,
                                 limit=None if since is not None else limit)
        wanted = [item for item in listing["items"]
                  if since is None or parse_published(item["published_at"]) >= since][: max(int(limit), 0)]
        items = []
        out_of_time = False
        for item in wanted:
            article = {"content": "", "source_url": item["source_url"], "content_status": "summary_only"}
            if not out_of_time:
                try:
                    article = self.fetch_article(item["source_url"], deadline=deadline)
                except TimeoutError:
                    out_of_time = True
            if article["content_status"] != "full":
                self.diagnostics["article_summary_only"] += 1
            content = article["content"] or item["summary"]
            if not content:
                continue
            items.append({**item, "content": content, "content_status": article["content_status"],
                          "source_url": article["source_url"]})
        return {"items": items, "covered_from": listing["covered_from"],
                "covered_until": listing["covered_until"]}


class VietstockNewsProvider:
    def __init__(
        self,
        *,
        cache: WebEvidenceCache,
        fetcher: Callable[..., Iterable[dict] | dict],
        cache_ttl_seconds: int = 6 * 60 * 60,
        miss_wait_seconds: float = 10,
        max_refresh_articles: int = 20,
        refresh_concurrency: int = 1,
        refresh_deadline_seconds: float = 45,
        article_fetch_seconds: float = 20,
        proactive_refresh_admitter: Callable[[str], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.cache = cache
        self.fetcher = fetcher
        self.cache_ttl_seconds = cache_ttl_seconds
        self.miss_wait_seconds = miss_wait_seconds
        self.max_refresh_articles = max_refresh_articles
        self.refresh_deadline_seconds = refresh_deadline_seconds
        self.article_fetch_seconds = article_fetch_seconds
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
        return "vietstock-news-v3"

    def _to_evidence(self, request: WebEvidenceRequest, raw_items: Iterable[dict],
                     retrieved_at: str) -> list[WebEvidence]:
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
                    summary=str(raw.get("summary", "") or ""),
                    content_status=(
                        raw.get("content_status")
                        if raw.get("content_status") in {"summary_only", "listing_only"} else "full"
                    ),
                    source_url=url,
                    publisher="Vietstock",
                    published_at=str(raw.get("published_at", "") or ""),
                    retrieved_at=retrieved_at,
                    query=request.query,
                )
            )
        return evidence

    def _refresh(self, request: WebEvidenceRequest) -> list[WebEvidence]:
        retrieved_at = self._now().replace(microsecond=0).isoformat()
        if not getattr(self.fetcher, "supports_window", False):
            # Injected test/legacy fetchers: fetcher(ticker, limit) -> items.
            raw_items = list(self.fetcher(request.ticker, self.max_refresh_articles))
            evidence = self._to_evidence(request, raw_items, retrieved_at)
            self.cache.put_many(evidence)
            return evidence
        # Windowed: read the news list back to the window start and keep every
        # entry (public data) as listing_only; article text is fetched per
        # request, for the articles that request needs.
        since = parse_published(request.date_from)
        deadline = time.monotonic() + self.refresh_deadline_seconds
        if hasattr(self.fetcher, "list_news"):
            result = self.fetcher.list_news(request.ticker, since=since, deadline=deadline)
            items = [{**item, "content": item.get("summary") or item.get("title", ""),
                      "content_status": "listing_only"} for item in result.get("items", [])]
            evidence = self._to_evidence(request, items, retrieved_at)
            self.cache.put_listing(evidence)
        else:  # injected fetchers that return article text directly
            result = self.fetcher(request.ticker, self.max_refresh_articles, since=since,
                                  until=parse_published(request.date_to), deadline=deadline)
            evidence = self._to_evidence(request, result.get("items", []), retrieved_at)
            self.cache.put_many(evidence)
        if result.get("covered_from") and result.get("covered_until"):
            self.cache.record_coverage(request.ticker, result["covered_from"], result["covered_until"])
        return evidence

    def _schedule(self, request: WebEvidenceRequest, key: str = "") -> tuple[Future, bool]:
        key = key or request.ticker
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
                request,
                status="unsupported",
                message="The configured provider only supports company_news.",
                provider=self.provider_identity,
                cache_status="unsupported",
            )
        if not request.ticker:
            return self._batch(
                request,
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

        if request.date_from or request.date_to:
            return self._search_window(request)

        cached = self.cache.get(ticker=request.ticker, limit=request.limit)
        age = self.cache.age_seconds(cached, now=self._now())
        fresh = age is not None and age <= self.cache_ttl_seconds
        if cached and fresh and not request.force_refresh:
            return self._batch(
                request,
                status="found",
                evidence=cached,
                provider=self.provider_identity,
                cache_status="fresh",
            )

        future, scheduled = self._schedule(request)
        if cached and not request.force_refresh:
            return self._batch(
                request,
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
                request,
                status="not_found_after_search",
                message="News refresh is still running.",
                provider=self.provider_identity,
                cache_status="miss",
                refresh_scheduled=True,
                pending=True,
            )
        except Exception as exc:
            return self._batch(
                request,
                status="error",
                message=f"News refresh failed: {type(exc).__name__}",
                provider=self.provider_identity,
                cache_status="miss",
            )

        return self._batch(
            request,
            status="found" if refreshed else "not_found_after_search",
            evidence=refreshed[: request.limit],
            message="" if refreshed else "No company news was found.",
            provider=self.provider_identity,
            cache_status="miss",
            refresh_scheduled=scheduled,
        )

    # ------------------------------------------------------------ windowed

    def _covers(self, coverage: Optional[Coverage], since: datetime, until: datetime) -> bool:
        if coverage is None:
            return False
        now = self._now().timestamp()
        bottom = coverage.covered_from <= since.timestamp()
        # News up to ``until`` is listed, or the listing is recent enough that
        # anything newer is within the freshness allowance.
        top = (until.timestamp() <= coverage.covered_until
               or now - coverage.covered_until <= self.cache_ttl_seconds)
        return bottom and top

    def _coverage_report(self, coverage: Optional[Coverage], since: datetime, until: datetime) -> dict:
        report = {"requested_from": iso_utc(since), "requested_to": iso_utc(until),
                  "covered_from": "", "covered_to": "", "complete": self._covers(coverage, since, until),
                  # How far back the source has been read at all, even when that
                  # does not reach the requested window.
                  "listed_from": iso_utc(datetime.fromtimestamp(coverage.covered_from, tz=timezone.utc))
                  if coverage is not None else ""}
        if coverage is not None:
            start = max(since.timestamp(), coverage.covered_from)
            end = min(until.timestamp(), coverage.covered_until)
            if start <= end:
                report["covered_from"] = iso_utc(datetime.fromtimestamp(start, tz=timezone.utc))
                report["covered_to"] = iso_utc(datetime.fromtimestamp(end, tz=timezone.utc))
        return report

    def _search_window(self, request: WebEvidenceRequest) -> WebEvidenceBatch:
        now = self._now()
        until = parse_published(request.date_to) or now
        since = parse_published(request.date_from) or (until - timedelta(days=30))
        if since > until:
            return self._batch(request, status="unsupported", message="The requested date range is reversed.",
                               provider=self.provider_identity, cache_status="unsupported")

        coverage = self.cache.coverage(request.ticker)
        cache_status, pending, scheduled, message = "fresh", False, False, ""
        if not self._covers(coverage, since, until) or request.force_refresh:
            # Looked up before the refresh starts: a fast refresh must not
            # make this request look like it is serving older data.
            had_rows = bool(self._window_rows(request, since, until))
            future, scheduled = self._schedule(request, key=f"{request.ticker}:{since.date().isoformat()}")
            if had_rows and not request.force_refresh:
                # Serve what is listed while the rest of the list is read.
                cache_status, message = "stale", "Serving cached news while a refresh runs."
                pending = not future.done()
            else:
                try:
                    future.result(timeout=self.miss_wait_seconds)
                    cache_status = "miss"
                except TimeoutError:
                    cache_status, pending, message = "miss", True, "News refresh is still running."
                except Exception as exc:
                    return self._batch(
                        request, status="error", message=f"News refresh failed: {type(exc).__name__}",
                        provider=self.provider_identity, cache_status="miss",
                        coverage=self._coverage_report(coverage, since, until),
                    )
            coverage = self.cache.coverage(request.ticker)

        selected, fetched, ranked = self._select_and_fetch(request, since, until)
        counts = self.cache.count_window(ticker=request.ticker, since=since, until=until)
        stats = {
            "listed_in_window": counts["listed"],
            "full_text_in_window": counts["full"],
            "selected": len(selected),
            "selected_full_text": sum(1 for item in selected if item.content_status == "full"),
            "fetched_now": fetched,
            "ranked_by_topic": ranked,
        }
        if not selected and not message:
            message = "No company news in the requested period."
        return self._batch(
            request,
            status="found" if selected else "not_found_after_search",
            evidence=selected,
            message=message,
            provider=self.provider_identity,
            cache_status=cache_status,
            refresh_scheduled=scheduled or pending,
            pending=pending,
            coverage=self._coverage_report(coverage, since, until),
            stats=stats,
        )

    def _terms(self, request: WebEvidenceRequest):
        return topic_terms(request.topic, exclude=[request.ticker, request.company])

    def _window_rows(self, request: WebEvidenceRequest, since: datetime, until: datetime) -> list[WebEvidence]:
        return self.cache.get(ticker=request.ticker, limit=WINDOW_ROWS_MAX, since=since, until=until)

    def _select_and_fetch(self, request: WebEvidenceRequest, since: datetime,
                          until: datetime) -> tuple[list[WebEvidence], int, bool]:
        """The window's articles most about the question (newest first when
        it names no topic), with article text fetched for them within the
        fetch budget. Unread pages fall back to the list summary."""

        rows = self._window_rows(request, since, until)
        terms = without_common_terms(self._terms(request), [f"{item.title} {item.summary}" for item in rows])
        scores = {item.evidence_id: topic_score(terms, f"{item.title} {item.summary}") for item in rows}
        # "By topic" only when the topic actually told the articles apart; a
        # word every article shares (the brand name) leaves it by recency.
        by_topic = has_topic(terms) and len(set(scores.values())) > 1
        ranked = sorted(
            rows,
            key=lambda item: (scores[item.evidence_id], parse_published(item.published_at) or since),
            reverse=True,
        )[: request.limit]
        if not hasattr(self.fetcher, "fetch_article"):
            return ranked, 0, by_topic
        deadline = time.monotonic() + self.article_fetch_seconds
        selected, fetched = [], 0
        for item in ranked:
            if item.content_status == "full":
                selected.append(item)
                continue
            article = {"content_status": "summary_only", "content": "", "source_url": item.source_url}
            if time.monotonic() < deadline:
                try:
                    article = self.fetcher.fetch_article(item.source_url, deadline=deadline)
                except TimeoutError:
                    pass
            if article["content_status"] == "full" and _allowed_vietstock_url(article["source_url"]):
                full = item.model_copy(update={
                    "content": article["content"], "content_status": "full",
                    "source_url": article["source_url"], "evidence_id": "", "content_hash": "",
                    "retrieved_at": self._now().replace(microsecond=0).isoformat(),
                })
                full = WebEvidence.model_validate(full.model_dump())  # recompute hash and id
                self.cache.put_many([full])
                selected.append(full)
                fetched += 1
            else:
                selected.append(item.model_copy(update={
                    "content": item.summary or item.title, "content_status": "summary_only"}))
        return selected, fetched, by_topic

    def _batch(self, request: WebEvidenceRequest | None = None, **kwargs) -> WebEvidenceBatch:
        # Articles come from the shared cache without request data; this
        # request's own query and company are attached to its copy only.
        if request is not None and kwargs.get("evidence"):
            kwargs["evidence"] = [
                item.model_copy(update={"query": request.query, "company": request.company})
                for item in kwargs["evidence"]
            ]
        return WebEvidenceBatch(
            diagnostics=dict(getattr(self.fetcher, "diagnostics", {}) or {}), **kwargs,
        )
