"""Company news finished after the answer: scheduled by the run, completed by
the chat worker between questions, shown through the run and session API."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")

from fastapi.testclient import TestClient  # noqa: E402

from dataset_catalog import registry  # noqa: E402
from schemas.web_evidence import WebEvidence, WebEvidenceBatch  # noqa: E402
from webapp import db as webdb  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.chat_runner import ChatDependencies  # noqa: E402
from webapp.news_updates import EXPIRES_AFTER, RETRY_AFTER, needs_update, run_news_update_once  # noqa: E402
from webapp.settings import WebSettings  # noqa: E402
from webapp.worker import run_chat_once  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"
NOW = datetime(2026, 10, 11, 3, 0, tzinfo=timezone.utc)
CONTEXT = {"status": "ready", "company": "Vinamilk", "ticker": "VNM", "question": "Tin gần đây của Vinamilk?",
           "as_of": NOW.isoformat(), "date_from": (NOW - timedelta(days=30)).isoformat(),
           "date_to": NOW.isoformat(), "window_source": "default"}
PENDING = {"status": "pending", "context": CONTEXT, "limitations": ["Hệ thống đang đọc danh sách tin"],
           "collection": {"pending": True}, "diagnostics": {}}
BODY = "Vinamilk công bố lợi nhuận quý 3 tăng 12% so với cùng kỳ nhờ giá sữa bột nguyên liệu giảm."


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


class Provider:
    def __init__(self, pending=False):
        self.pending, self.calls = pending, 0

    def search(self, request):
        self.calls += 1
        if self.pending:
            return WebEvidenceBatch(status="not_found_after_search", pending=True, coverage={"complete": False})
        article = WebEvidence(ticker="VNM", title="Vinamilk lãi quý 3 tăng 12%", content=BODY,
                              source_url="https://vietstock.vn/a1.htm", publisher="Vietstock",
                              published_at=(NOW - timedelta(days=2)).isoformat())
        return WebEvidenceBatch(status="found", evidence=[article], coverage={"complete": True},
                                stats={"listed_in_window": 1, "full_text_in_window": 1})


def model(*replies):
    queue = list(replies)
    return lambda system, user, timeout: json.dumps(queue.pop(0), ensure_ascii=False)


SELECT = {"relevant_article_ids": ["a1"],
          "events": [{"title": "Lợi nhuận quý 3 tăng 12%", "article_ids": ["a1"], "event_date": ""}]}
EVIDENCE = {"events": [{"event_id": "e1", "label": "positive", "explanation": "", "uncertainties": [],
                        "quotes": [{"article_id": "a1", "text": "Vinamilk công bố lợi nhuận quý 3 tăng 12%"}]}]}


@pytest.fixture
def web(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(registry, "DATASETS_DIR", tmp_path / "dataset_store")
    monkeypatch.setattr(registry, "REGISTRY_PATH", registry.DATASETS_DIR / "registry.json")
    monkeypatch.setattr(registry, "MANIFESTS_DIR", registry.DATASETS_DIR / "manifests")
    monkeypatch.setattr(registry, "RAW_TABLES_DIR", registry.DATASETS_DIR / "raw_tables")
    monkeypatch.setattr(registry, "SQLITE_DIR", registry.DATASETS_DIR / "sqlite")
    shared = registry.save_dataset(registry.build_dataset_record(
        file_path="data/document.md", company="Vinamilk", fiscal_year=2025).model_copy(update={"status": "ready"}))
    settings = WebSettings(secret_key="news-secret-" + "n" * 40, db_path=tmp_path / "web.sqlite3",
                           registration_mode="open", allowed_origins=frozenset({ORIGIN}),
                           shared_dataset_ids=frozenset({shared.dataset_id}))
    app = create_app(settings, generation_inspector=lambda _r: "gen-1", question_admitter=lambda _o: None)
    client = TestClient(app, base_url=ORIGIN)
    client.post("/api/register", json={"name": "A", "email": "a@example.com", "password": "correct horse"},
                headers={"Origin": ORIGIN})
    session = client.post("/api/sessions", json={"dataset_id": shared.dataset_id}, headers={"Origin": ORIGIN}).json()
    return SimpleNamespace(app=app, db=app.state.db, client=client, session=session, settings=settings)


def _ask(web, final_state):
    run = web.client.post("/api/chat", json={"session_id": web.session["id"], "message": "Lợi nhuận và tin gần đây?",
                                             "idempotency_key": "key-" + "1" * 10},
                          headers={"Origin": ORIGIN}).json()
    deps = ChatDependencies(
        shared_dataset_ids=web.settings.shared_dataset_ids,
        activate=lambda _d: SimpleNamespace(generation="gen-1", collection=object()),
        execute=lambda *_args, **_kwargs: final_state,
        collect_errors=lambda _state: [],
        format_answer=lambda _state: "Lợi nhuận năm 2025 là 9.359 tỷ đồng.",
    )
    assert run_chat_once(web.db, "worker", deps)
    return run["run_id"]


def test_only_incomplete_news_is_finished_later():
    assert needs_update({"web_analysis": PENDING}) == CONTEXT
    assert needs_update({"web_analysis": {**PENDING, "status": "ok", "diagnostics": {"unfinished": True}}})
    assert needs_update({"web_analysis": {**PENDING, "status": "ok", "collection": {"pending": True}}})
    assert needs_update({"web_analysis": {**PENDING, "status": "ok", "collection": {}}}) is None
    assert needs_update({"web_analysis": {**PENDING, "context": {**CONTEXT, "status": "needs_clarification"}}}) is None
    assert needs_update({}) is None


def test_the_answer_goes_out_and_the_news_follows_on_its_own(web):
    run_id = _ask(web, {"web_analysis": PENDING})
    run = web.client.get(f"/api/chat-runs/{run_id}").json()
    assert run["status"] == "succeeded" and run["news_update"] == {"status": "pending", "message": None}
    [session] = web.client.get("/api/sessions").json()
    assert session["news_update_run_id"] == run_id  # a reloaded page resumes waiting

    clock = Clock()
    clock.now = datetime.now(timezone.utc)
    assert not run_news_update_once(web.db, "worker", provider_factory=Provider, now=clock)  # not due yet
    clock.now += RETRY_AFTER
    waiting = Provider(pending=True)
    assert run_news_update_once(web.db, "worker", provider_factory=lambda: waiting, now=clock)
    assert waiting.calls == 1 and web.client.get(f"/api/chat-runs/{run_id}").json()["news_update"]["status"] == "pending"

    clock.now += RETRY_AFTER
    assert run_news_update_once(web.db, "worker", provider_factory=Provider, invoke=model(SELECT, EVIDENCE), now=clock)
    update = web.client.get(f"/api/chat-runs/{run_id}").json()["news_update"]
    assert update["status"] == "done" and update["message"]["kind"] == "news_update"
    assert "Cập nhật tin tức" in update["message"]["content"]
    assert "lợi nhuận quý 3 tăng 12%" in update["message"]["content"]
    assert update["message"]["payload"]["news"]["events"][0]["confirmation"] == "attributed_report"
    messages = web.client.get(f"/api/sessions/{web.session['id']}/messages").json()
    assert [message["kind"] for message in messages] == ["text", "text", "news_update"]
    assert web.client.get("/api/sessions").json()[0]["news_update_run_id"] is None


def test_an_update_that_never_gets_news_says_so_and_stops(web):
    run_id = _ask(web, {"web_analysis": PENDING})
    clock = Clock()
    clock.now = datetime.now(timezone.utc) + EXPIRES_AFTER + timedelta(seconds=1)
    assert run_news_update_once(web.db, "worker", provider_factory=lambda: Provider(pending=True), now=clock)
    update = web.client.get(f"/api/chat-runs/{run_id}").json()["news_update"]
    assert update["status"] == "expired" and "hỏi lại sau" in update["message"]["content"]
    assert not run_news_update_once(web.db, "worker", provider_factory=Provider, now=clock)


def test_a_complete_answer_schedules_nothing(web):
    run_id = _ask(web, {"web_analysis": {**PENDING, "status": "ok", "collection": {}}})
    assert web.client.get(f"/api/chat-runs/{run_id}").json()["news_update"] is None


def test_a_worker_that_stopped_mid_update_runs_it_again(web):
    _ask(web, {"web_analysis": PENDING})
    with web.db.transaction() as conn:
        conn.execute("UPDATE news_updates SET status = 'running', due_at = ?", (webdb.iso(NOW),))
        assert webdb.recover_news_updates(conn) == 1
        assert conn.execute("SELECT status FROM news_updates").fetchone()["status"] == "pending"


def test_deleting_the_conversation_drops_its_update(web):
    _ask(web, {"web_analysis": PENDING})
    web.client.delete(f"/api/sessions/{web.session['id']}", headers={"Origin": ORIGIN})
    with web.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM news_updates").fetchone()[0] == 0
