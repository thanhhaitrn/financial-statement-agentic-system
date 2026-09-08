from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

from graph.evidence import build_evidence_pack
from graph.state import WorkflowServices
from graph.workflow import _bind_services
from schemas.web_evidence import WebEvidenceRequest
from web_evidence.cache import WebEvidenceCache
from web_evidence.symbols import (CafeFSymbolCatalogJob, load_symbol_catalog,
                                  parse_cafef_symbols)
from web_evidence.vietstock import (VietstockNewsProvider,
                                    parse_vietstock_article)


def _request(**updates):
    payload = {
        "query": "Tin mới của APEC",
        "intent": "company_news",
        "ticker": "APS",
        "company": "Chứng khoán Châu Á Thái Bình Dương",
        "limit": 5,
    }
    payload.update(updates)
    return WebEvidenceRequest(**payload)


def _article(title="APEC công bố thông tin"):
    return {
        "title": title,
        "content": "Nội dung bài viết đủ dài và có thông tin liên quan đến doanh nghiệp.",
        "source_url": "https://finance.vietstock.vn/APS/tin-tuc-su-kien.htm",
        "published_at": "2026-09-05T08:00:00+00:00",
    }


def test_vietstock_provider_is_cache_first(tmp_path: Path):
    calls = []

    def fetcher(ticker, limit):
        calls.append((ticker, limit))
        return [_article()]

    provider = VietstockNewsProvider(
        cache=WebEvidenceCache(tmp_path / "web.db"),
        fetcher=fetcher,
        miss_wait_seconds=1,
    )

    first = provider.search(_request())
    second = provider.search(_request())

    assert first.status == "found"
    assert first.cache_status == "miss"
    assert second.status == "found"
    assert second.cache_status == "fresh"
    assert calls == [("APS", 20)]
    assert second.evidence[0].publisher == "Vietstock"
    assert second.evidence[0].source_url.startswith("https://finance.vietstock.vn/")


def test_stale_cache_is_served_while_refresh_runs(tmp_path: Path):
    old_time = datetime(2026, 9, 5, tzinfo=timezone.utc)
    now = old_time + timedelta(hours=7)
    cache = WebEvidenceCache(tmp_path / "web.db")
    seed_provider = VietstockNewsProvider(
        cache=cache,
        fetcher=lambda _ticker, _limit: [_article("Bản cũ")],
        now=lambda: old_time,
        miss_wait_seconds=1,
    )
    seed_provider.search(_request())

    provider = VietstockNewsProvider(
        cache=cache,
        fetcher=lambda _ticker, _limit: [_article("Bản mới")],
        cache_ttl_seconds=6 * 60 * 60,
        now=lambda: now,
        miss_wait_seconds=1,
    )
    result = provider.search(_request())

    assert result.status == "found"
    assert result.cache_status == "stale"
    assert result.refresh_scheduled is True
    assert result.evidence[0].title == "Bản cũ"


def test_provider_rejects_non_company_news_without_fetching(tmp_path: Path):
    calls = []
    provider = VietstockNewsProvider(
        cache=WebEvidenceCache(tmp_path / "web.db"),
        fetcher=lambda ticker, limit: calls.append((ticker, limit)) or [],
    )

    result = provider.search(_request(intent="unsupported_external"))

    assert result.status == "unsupported"
    assert calls == []


def test_only_proactive_refresh_uses_account_quota(tmp_path: Path):
    admitted = []
    provider = VietstockNewsProvider(
        cache=WebEvidenceCache(tmp_path / "web.db"),
        fetcher=lambda _ticker, _limit: [_article()],
        proactive_refresh_admitter=admitted.append,
        miss_wait_seconds=1,
    )

    provider.search(_request(owner_id="owner-a"))
    provider.search(_request(owner_id="owner-a"))
    provider.search(_request(owner_id="owner-a", force_refresh=True))

    assert admitted == ["owner-a"]


def test_cache_miss_returns_bounded_not_found_while_refresh_continues(tmp_path: Path):
    release = Event()

    def delayed_fetcher(_ticker, _limit):
        release.wait(timeout=1)
        return [_article()]

    provider = VietstockNewsProvider(
        cache=WebEvidenceCache(tmp_path / "web.db"),
        fetcher=delayed_fetcher,
        miss_wait_seconds=0.01,
    )

    result = provider.search(_request())
    release.set()

    assert result.status == "not_found_after_search"
    assert result.cache_status == "miss"
    assert result.refresh_scheduled is True


def test_refresh_deduplicates_duplicate_source_urls(tmp_path: Path):
    provider = VietstockNewsProvider(
        cache=WebEvidenceCache(tmp_path / "web.db"),
        fetcher=lambda _ticker, _limit: [
            _article("Bản đầu"),
            _article("Bản trùng URL"),
        ],
        miss_wait_seconds=1,
    )

    result = provider.search(_request())

    assert len(result.evidence) == 1
    assert result.evidence[0].title == "Bản đầu"


def test_graph_injects_structured_web_facts_without_a_fifth_agent(tmp_path: Path):
    def fetcher(_ticker, _limit):
        return [_article()]
    fetcher.diagnostics = {"article_dropped_short": 2}
    provider = VietstockNewsProvider(
        cache=WebEvidenceCache(tmp_path / "web.db"),
        fetcher=fetcher,
        miss_wait_seconds=1,
    )
    node = _bind_services(
        lambda state, *, web_provider=None: build_evidence_pack(
            state,
            web_provider=web_provider,
        ),
        WorkflowServices(web_provider=provider),
        inject_web_provider=True,
    )

    updates = node(
        {
            "dataset_id": "apec",
            "dataset_ticker": "APS",
            "dataset_company": "APEC",
            "planner_plan": {"need_web": True, "web_intent": "company_news"},
            "worker_plan": {
                "need_web": True,
                "evidence_plan": [
                    {
                        "table": "",
                        "query": "Tin mới của APEC",
                        "needby": ["agent_profitability"],
                        "web_intent": "company_news",
                    }
                ],
                "analysis_plan": [
                    {"agent": "agent_profitability", "objective": "Đánh giá tác động"}
                ],
            },
        }
    )

    facts = updates["worker_results"]["WEB"]["facts"]
    assert len(facts) == 1
    assert facts[0]["source_url"].startswith("https://finance.vietstock.vn/")
    assert facts[0]["needby"] == ["agent_profitability"]
    assert updates["evidence_pack"]["stats"]["web_unsupported_n"] == 0
    assert updates["evidence_pack"]["stats"]["retrieval_calls_n"] == 1
    web_log = next(entry for entry in updates["trace"] if entry.get("tool") == "web_search")
    assert web_log["provider_diagnostics"] == {"article_dropped_short": 2}


def test_vietstock_article_parser_keeps_plain_paragraphs():
    parsed = parse_vietstock_article(
        """
        <html><body><h1>Doanh nghiệp công bố báo cáo</h1>
        <time datetime="2026-09-05T08:00:00+07:00"></time>
        <div class="news-detail"><p>Đây là đoạn nội dung không có CSS class riêng.</p>
        <p>Đoạn thứ hai chứa thông tin tài chính cần giữ lại.</p></div></body></html>
        """
    )

    assert parsed["title"] == "Doanh nghiệp công bố báo cáo"
    assert "không có CSS class riêng" in parsed["content"]
    assert parsed["published_at"] == "2026-09-05T08:00:00+07:00"


def test_cafef_symbol_parser_handles_embedded_json_and_deduplicates():
    symbols = parse_cafef_symbols(
        """
        <script>var jsonData = [
          {"Symbol":"VNM","CompanyName":"Vinamilk","TradeCenter":"HOSE"},
          {"Symbol":"VNM","CompanyName":"Duplicate","TradeCenter":"HOSE"},
          {"Symbol":"FPT","CompanyName":"FPT","TradeCenter":"HNX"}
        ];</script>
        """
    )

    assert [(item.ticker, item.exchange) for item in symbols] == [
        ("VNM", "HSX"),
        ("FPT", "HNX"),
    ]


def test_symbol_catalog_job_writes_structured_daily_artifact(tmp_path: Path):
    job = CafeFSymbolCatalogJob(
        fetch_html=lambda: '<script>var jsonData=[{"Symbol":"VNM",'
        '"CompanyName":"Vinamilk","TradeCenter":"HOSE"}];</script>'
    )

    payload = job.refresh(tmp_path / "symbols.json")

    assert payload["symbols"] == [
        {"ticker": "VNM", "company": "Vinamilk", "exchange": "HSX"}
    ]
    assert len(payload["content_hash"]) == 64
    assert (tmp_path / "symbols.json").exists()
    assert load_symbol_catalog(tmp_path / "symbols.json")["VNM"].company == "Vinamilk"


def test_malformed_article_excludes_navigation_footer_and_script():
    html = (Path(__file__).parent / "fixtures/vietstock_malformed_article.html").read_text()
    parsed = parse_vietstock_article(html)
    assert "Dòng tiền" in parsed["content"]
    assert "credentials" not in parsed["content"]
    assert "quảng cáo" not in parsed["content"]
    assert parsed["published_at"] == "2026-09-01T08:00:00+07:00"


def test_provider_cleans_futures_and_closes_executor(tmp_path):
    provider = VietstockNewsProvider(cache=WebEvidenceCache(tmp_path / "cache.db"),
                                    fetcher=lambda *_: [_article()], miss_wait_seconds=1)
    with provider:
        assert provider.search(_request()).status == "found"
    assert provider._pending == {}
    assert provider._executor._shutdown


def test_cache_replaces_revised_url_and_uses_wal(tmp_path):
    import sqlite3
    from schemas.web_evidence import WebEvidence
    cache = WebEvidenceCache(tmp_path / "cache.db")
    first = WebEvidence(ticker="VNM", title="News", content="First", source_url="https://vietstock.vn/news.htm", publisher="Vietstock")
    second = WebEvidence(**{**first.model_dump(), "evidence_id": "", "content_hash": "", "content": "Revised"})
    cache.put_many([first])
    cache.put_many([second])
    assert [item.content for item in cache.get(ticker="VNM", limit=5)] == ["Revised"]
    with sqlite3.connect(cache.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_short_article_drop_is_observable(monkeypatch):
    from web_evidence.vietstock import VietstockHttpFetcher
    fetcher = VietstockHttpFetcher()
    def get(url):
        if url.endswith("tin-tuc-su-kien.htm"):
            return '<a class="stock-news__title" href="https://vietstock.vn/news.htm">Tin tức</a>', url
        return '<article><p>Short</p></article>', url
    monkeypatch.setattr(fetcher, "_get", get)
    assert fetcher("VNM", 3) == []
    assert fetcher.diagnostics["article_dropped_short"] == 1


def test_need_web_false_never_calls_provider():
    class Provider:
        provider_identity = "never"
        def search(self, _request):
            raise AssertionError("need_web=false must not perform retrieval")
    updates = build_evidence_pack({"worker_plan": {"need_web": False, "evidence_plan": [
        {"table": "", "query": "News", "needby": ["agent_profitability"]},
    ]}}, web_provider=Provider())
    assert "WEB" not in updates.get("worker_results", {})
