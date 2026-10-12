"""Live Vietstock check: the JSON listing and article parser against today's
site. Opt-in (``FINX_LIVE_WEB_SMOKE=1``); never part of the default run, so CI
does not depend on the network. Read-only requests to public pages."""

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("FINX_LIVE_WEB_SMOKE") != "1", reason="set FINX_LIVE_WEB_SMOKE=1 to call Vietstock"
)


def test_live_listing_and_articles_parse_for_a_large_cap():
    from agentfinx.news.collect.dates import parse_published
    from agentfinx.news.collect.vietstock import VietstockHttpFetcher

    ticker = os.environ.get("FINX_LIVE_WEB_TICKER", "VNM")
    fetcher = VietstockHttpFetcher(deadline_seconds=60)
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=60)
    result = fetcher(ticker, 6, since=since, until=now)

    items = result["items"]
    assert items, f"no {ticker} news in 60 days; diagnostics={dict(fetcher.diagnostics)}"
    for item in items:
        published = parse_published(item["published_at"])
        assert published is not None and since <= published <= now
        assert item["source_url"].startswith("https://vietstock.vn/")
    # Disclosure notices are short (a few hundred characters); the parser
    # must still read the article body, not fall back to the listing summary.
    assert any(item["content_status"] == "full" for item in items), (
        f"article parser read no article body; diagnostics={dict(fetcher.diagnostics)}"
    )
    # A busy ticker may need more listing pages than the cap to reach 60 days
    # back: that is reported partial coverage, not a failure.
    assert result["covered_from"] <= now


def test_live_paging_reaches_an_older_window_with_matching_dates():
    from agentfinx.news.collect.dates import parse_published
    from agentfinx.news.collect.vietstock import VietstockHttpFetcher, parse_vietstock_article

    ticker = os.environ.get("FINX_LIVE_WEB_TICKER", "VNM")
    fetcher = VietstockHttpFetcher(deadline_seconds=90)
    pages = []
    listing = fetcher._listing_page

    def counted(code, page, deadline):
        pages.append(page)
        return listing(code, page, deadline)

    # A window that ends before the oldest article on page 1 forces paging,
    # however busy the ticker is.
    first_page = listing(ticker, 1, None)
    oldest_on_page_1 = min(parse_published(row["PublishTime"]) for row in first_page)
    until = oldest_on_page_1 - timedelta(days=1)
    since = until - timedelta(days=60)
    fetcher._listing_page = counted
    result = fetcher(ticker, 3, since=since, until=until)

    assert max(pages) >= 2, f"an older window must page past page 1; pages={pages}"
    items = result["items"]
    assert items, f"no {ticker} news in {since:%d/%m}-{until:%d/%m}; pages={pages}, diagnostics={dict(fetcher.diagnostics)}"
    for item in items:
        assert since <= parse_published(item["published_at"]) <= until
    # The listing's publish time agrees with the article page's own metadata.
    html, _url = fetcher._get(items[0]["source_url"])
    page_time = parse_published(parse_vietstock_article(html)["published_at"])
    assert page_time is not None
    assert abs((page_time - parse_published(items[0]["published_at"])).total_seconds()) < 3600
    assert result["covered_from"] <= since or fetcher.diagnostics.get("deadline_reached")
