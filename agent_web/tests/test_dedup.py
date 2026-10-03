from datetime import datetime, timezone

from agent_web.dedup import dedup_articles, normalize_url, content_hash
from agent_web.interfaces import FetchedArticle


def _art(url: str, content: str) -> FetchedArticle:
    return FetchedArticle(
        url=url, final_url=url, title="t", source="s", content=content,
        content_scope="full_text", published_at=datetime.now(timezone.utc),
    )


def test_normalize_url_strips_tracking_params_and_trailing_slash():
    a = normalize_url("https://Example.com/news/abc/?utm_source=fb&ref=home")
    b = normalize_url("https://example.com/news/abc")
    assert a == b


def test_dedup_removes_same_url():
    articles = [_art("https://x.com/a?utm_source=x", "nội dung 1"), _art("https://x.com/a", "nội dung khác")]
    kept, removed = dedup_articles(articles)
    assert len(kept) == 1
    assert removed == 1


def test_dedup_removes_same_content_different_url():
    articles = [
        _art("https://a.com/1", "Nội dung   giống hệt nhau   với khoảng trắng khác."),
        _art("https://b.com/2", "Nội dung giống hệt nhau với khoảng trắng khác."),
    ]
    kept, removed = dedup_articles(articles)
    assert len(kept) == 1
    assert removed == 1


def test_dedup_keeps_distinct_articles():
    articles = [_art("https://a.com/1", "bài A"), _art("https://b.com/2", "bài B")]
    kept, removed = dedup_articles(articles)
    assert len(kept) == 2
    assert removed == 0


def test_content_hash_stable_across_whitespace():
    assert content_hash("Hello   World") == content_hash("hello world")
