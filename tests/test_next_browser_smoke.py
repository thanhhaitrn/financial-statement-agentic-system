"""Browser smoke test of the Next.js UI (web/) through its /api proxy.

Needs a production build (``cd web && npm ci && npm run build``) and
Playwright; skipped otherwise, unless ``FINX_REQUIRE_NEXT_SMOKE=1`` (CI), where
a missing build fails. The build compiles in the FastAPI address
(``FINX_API_URL``), so the test reads it from the build and serves the API
there. The pipeline is a fake that echoes the question; nothing leaves the
machine.
"""

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")
uvicorn = pytest.importorskip("uvicorn")
sync_api = pytest.importorskip("playwright.sync_api")

from dataset_catalog import registry  # noqa: E402
from webapp import auth  # noqa: E402
from webapp import db as webdb  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.chat_runner import ChatDependencies  # noqa: E402
from webapp.settings import WebSettings  # noqa: E402
from webapp.news_updates import run_news_update_once  # noqa: E402
from webapp.worker import run_chat_once  # noqa: E402

WEB = Path(__file__).resolve().parents[1] / "web"
REQUIRED = os.environ.get("FINX_REQUIRE_NEXT_SMOKE") == "1"
PASSWORD = "correct horse battery"


def _unavailable(reason: str):
    if REQUIRED:
        pytest.fail(reason)
    pytest.skip(reason)


def _baked_api_port() -> int:
    manifest = WEB / ".next" / "routes-manifest.json"
    if not (WEB / ".next" / "BUILD_ID").exists() or not manifest.exists():
        _unavailable("no Next.js production build; run `npm ci && npm run build` in web/")
    rewrites = json.loads(manifest.read_text(encoding="utf-8")).get("rewrites", [])
    if isinstance(rewrites, dict):
        rewrites = [*rewrites.get("beforeFiles", []), *rewrites.get("afterFiles", []),
                    *rewrites.get("fallback", [])]
    for rule in rewrites:
        if rule.get("source") == "/api/:path*":
            target = urlsplit(rule["destination"])
            if target.hostname in {"127.0.0.1", "localhost"}:
                return target.port or 80
    _unavailable("the Next.js build does not proxy /api to a local FastAPI")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        # As uvicorn binds: a closed server's TIME_WAIT sockets do not count.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _wait_http(url: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except Exception:
            time.sleep(0.2)
    return False


@pytest.fixture
def stack(monkeypatch, tmp_path):
    api_port = _baked_api_port()
    next_bin = WEB / "node_modules" / ".bin" / "next"
    if not next_bin.exists() or shutil.which("node") is None:
        _unavailable("Node.js or web/node_modules is missing")
    if not _port_is_free(api_port):
        _unavailable(f"port {api_port} (FINX_API_URL of the build) is in use")

    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    shared = registry.save_dataset(
        registry.build_dataset_record(file_path="data/document.md", company="Công ty Mẫu", fiscal_year=2025)
        .model_copy(update={"status": "ready"})
    )

    next_port = _free_port()
    origin = f"http://127.0.0.1:{next_port}"
    settings = WebSettings(
        secret_key="next-smoke-secret-" + "n" * 40,
        db_path=tmp_path / "web.sqlite3",
        port=api_port,
        allowed_origins=frozenset({origin}),
        shared_dataset_ids=frozenset({shared.dataset_id}),
        heartbeat_interval_seconds=60,
    )
    app = create_app(settings, generation_inspector=lambda _record: "gen-1",
                     question_admitter=lambda _owner_id: None)
    database = app.state.db
    deps = ChatDependencies(
        shared_dataset_ids=settings.shared_dataset_ids,
        activate=lambda _dataset: SimpleNamespace(generation="gen-1", collection=object()),
        execute=lambda _ds, _col, question, **_kwargs: {"question": question},
        collect_errors=lambda _state: [],
        format_answer=lambda state: f"**Trả lời** cho: {state['question']}",
    )
    worker_open = threading.Event()
    worker_open.set()
    stop = threading.Event()

    def work():
        while not stop.is_set():
            worked = worker_open.is_set() and (
                run_chat_once(database, "next-smoke", deps)
                or run_news_update_once(database, "next-smoke", provider_factory=deps.news_provider,
                                        invoke=deps.news_model))
            if not worked:
                stop.wait(0.05)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    threads = [threading.Thread(target=work, daemon=True), threading.Thread(target=server.run, daemon=True)]
    for thread in threads:
        thread.start()
    web = subprocess.Popen(
        [str(next_bin), "start", "-p", str(next_port), "-H", "127.0.0.1"],
        cwd=WEB, env={**os.environ, "NEXT_TELEMETRY_DISABLED": "1"},
        stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
    )
    try:
        assert _wait_http(f"{origin}/login", 30), "next start did not answer"
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "FastAPI did not start"
        yield SimpleNamespace(origin=origin, db=database, shared=shared, worker_open=worker_open, deps=deps)
    finally:
        web.terminate()
        try:
            web.wait(timeout=10)
        except subprocess.TimeoutExpired:
            web.kill()
        server.should_exit = True
        stop.set()
        for thread in threads:
            thread.join(timeout=5)


def _invite(database, email):
    code = auth.new_invite_code()
    with database.transaction() as conn:
        webdb.create_invite(conn, code_hash=auth.hash_invite_code(code), email=email,
                            expires_at=webdb.iso(datetime.now(timezone.utc) + timedelta(days=1)))
    return code


def _ask(page, text):
    page.fill("textarea", text)
    page.keyboard.press("Enter")


def _answer(page, question):
    return page.locator(".bubble.bot.rich", has_text=f"cho: {question}")


def test_invite_sign_up_conversation_isolation_and_reload(stack):
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            # Invite-only sign-up: the code arrives in the link.
            code = _invite(stack.db, "an@example.com")
            page.goto(f"{stack.origin}/signup?invite={code}")
            assert page.input_value("#invite_code") == code
            page.fill("#name", "Người Thử")
            page.fill("#email", "an@example.com")
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{stack.origin}/chat")
            assert page.evaluate("document.cookie") == ""

            # Two conversations, A and B, each with one answer.
            page.select_option("#dataset-select", stack.shared.dataset_id)
            _ask(page, "Câu A1")
            _answer(page, "Câu A1").wait_for(timeout=15_000)
            assert "?session=" in page.url
            url_a = page.url
            page.get_by_role("button", name="+ Cuộc trò chuyện mới").click()
            page.select_option("#dataset-select", stack.shared.dataset_id)
            _ask(page, "Câu B1")
            _answer(page, "Câu B1").wait_for(timeout=15_000)
            url_b = page.url
            assert url_a != url_b

            # Send in A, switch to B before POST /api/chat returns: A's answer
            # must stay in A.
            page.locator(".history-title", has_text="Câu A1").click()
            _answer(page, "Câu A1").wait_for(timeout=10_000)
            held = []
            page.route("**/api/chat", lambda route: held.append(route))
            _ask(page, "Câu A2")
            page.wait_for_timeout(500)
            assert len(held) == 1
            page.locator(".history-title", has_text="Câu B1").click()
            _answer(page, "Câu B1").wait_for(timeout=10_000)
            held[0].continue_()
            page.unroute("**/api/chat")
            page.wait_for_timeout(3_000)
            assert _answer(page, "Câu A2").count() == 0
            assert page.locator(".row.user .bubble", has_text="Câu A2").count() == 0
            page.locator(".history-title", has_text="Câu A1").click()
            _answer(page, "Câu A2").wait_for(timeout=15_000)

            # Reload restores the conversation in the URL and resumes polling
            # a run that is still queued.
            stack.worker_open.clear()
            _ask(page, "Câu A3")
            page.locator(".dots-text", has_text="Đang chờ tới lượt xử lý").wait_for(timeout=10_000)
            page.reload()
            page.locator(".row.user .bubble", has_text="Câu A3").wait_for(timeout=15_000)
            page.locator(".dots-text").wait_for(timeout=10_000)
            stack.worker_open.set()
            _answer(page, "Câu A3").wait_for(timeout=15_000)

            # Back/forward follow the address bar.
            page.locator(".history-title", has_text="Câu B1").click()
            page.wait_for_url(url_b)
            page.go_back()
            _answer(page, "Câu A3").wait_for(timeout=10_000)

            # A conversation id that is not this user's falls back to the start.
            page.goto(f"{stack.origin}/chat?session=not-mine")
            page.locator("#dataset-select").wait_for(timeout=10_000)
            page.wait_for_function("() => !location.search.includes('session=')", timeout=10_000)
            assert not errors, errors
        finally:
            browser.close()


def test_backend_outage_is_not_shown_as_a_logout(stack):
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            code = _invite(stack.db, "binh@example.com")
            page.goto(f"{stack.origin}/signup?invite={code}")
            page.fill("#name", "Bình")
            page.fill("#email", "binh@example.com")
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{stack.origin}/chat")

            # FastAPI answers 500 for /api/me: the page says the server is
            # unreachable and keeps the session instead of sending to /login.
            stack.worker_open.clear()
            stack.db.path.rename(stack.db.path.with_suffix(".moved"))
            stack.db.path.mkdir()
            try:
                page.goto(f"{stack.origin}/chat")
                page.locator(".service-down").wait_for(timeout=10_000)
                assert page.url.endswith("/chat")
            finally:
                stack.db.path.rmdir()
                stack.db.path.with_suffix(".moved").rename(stack.db.path)
                stack.worker_open.set()
            page.goto(f"{stack.origin}/chat")
            page.locator("#dataset-select").wait_for(timeout=10_000)
        finally:
            browser.close()


def test_news_section_shows_sources_and_limits_and_survives_reload(stack):
    """The answer's company-news section (WEB_ANALYSIS_ENABLED) as the chat
    renders it: verified quote, Vietstock link, limitations; then a reload."""

    from output_formatter import format_final_answer

    analysis = {
        "status": "partial",
        "context": {"status": "ready", "ticker": "VNM", "company": "Vinamilk",
                    "date_from": "2026-09-11T03:00:00+00:00", "date_to": "2026-10-11T03:00:00+00:00"},
        "events": [{"event_id": "e1", "title": "Lợi nhuận quý 3 tăng", "article_ids": ["a1"],
                    "claim_type": "confirmed", "label": "positive", "score": 1,
                    "rationale": "Kết quả kinh doanh cải thiện.",
                    "quotes": [{"article_id": "a1", "text": "lợi nhuận quý 3 tăng 12%"}]}],
        "aggregate": {"score": 1.0, "scored_events": 1, "rubric_version": "news-sentiment-rubric-1.0"},
        "sources": [{"article_id": "a1", "title": "Vinamilk lãi quý 3 [tăng]",
                     "url": "https://vietstock.vn/2026/10/vnm-lai-quy-3.htm",
                     "published_at": "2026-10-05T02:00:00+00:00"}],
        "limitations": ["Nguồn tin mới được đọc từ 2026-09-20; tin trước đó chưa được thu thập."],
    }
    stack.deps.format_answer = lambda state: format_final_answer({
        "synth_decision": {"status": "answer", "answer": f"**Trả lời** cho: {state['question']}"},
        "web_analysis": analysis,
    })
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            code = _invite(stack.db, "news@example.com")
            page.goto(f"{stack.origin}/signup?invite={code}")
            page.fill("#name", "Tin Tức")
            page.fill("#email", "news@example.com")
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{stack.origin}/chat")
            page.select_option("#dataset-select", stack.shared.dataset_id)
            _ask(page, "Lợi nhuận và tin gần đây?")

            for attempt in ("before reload", "after reload"):
                bubble = page.locator(".bubble.bot.rich", has_text="Tin tức doanh nghiệp VNM")
                bubble.wait_for(timeout=15_000)
                link = bubble.locator("a", has_text="Vinamilk lãi quý 3 [tăng]")
                assert link.get_attribute("href") == "https://vietstock.vn/2026/10/vnm-lai-quy-3.htm", attempt
                assert link.get_attribute("target") == "_blank"
                assert "lợi nhuận quý 3 tăng 12%" in bubble.locator("blockquote").inner_text()
                assert "Giới hạn" in bubble.inner_text() and "2026-09-20" in bubble.inner_text()
                if attempt == "before reload":
                    page.reload()
            assert not errors, errors
        finally:
            browser.close()


def test_news_that_was_not_ready_arrives_by_itself(stack, monkeypatch):
    """The answer goes out while the news list is still being read; the news
    follows as its own message without the user asking again, also after a
    reload in between."""

    import json as jsonlib
    from datetime import timedelta as delta

    import webapp.news_updates as news_updates
    from output_formatter import format_final_answer
    from schemas.web_evidence import WebEvidence, WebEvidenceBatch

    monkeypatch.setattr(news_updates, "RETRY_AFTER", delta(seconds=1))
    now = datetime.now(timezone.utc)
    context = {"status": "ready", "company": "Vinamilk", "ticker": "VNM", "question": "Tin gần đây?",
               "as_of": now.isoformat(), "date_from": (now - delta(days=30)).isoformat(),
               "date_to": now.isoformat(), "window_source": "default"}
    pending = {"status": "pending", "context": context, "collection": {"pending": True},
               "limitations": ["Hệ thống đang đọc danh sách tin của VNM trên Vietstock."]}
    stack.deps.execute = lambda _ds, _col, question, **_kwargs: {
        "question": question, "web_analysis": {**pending, "context": {**context, "question": question}}}
    stack.deps.format_answer = lambda state: format_final_answer({
        "synth_decision": {"status": "answer", "answer": f"**Trả lời** cho: {state['question']}"},
        "web_analysis": state["web_analysis"]})
    body = "Vinamilk công bố lợi nhuận quý 3 tăng 12% so với cùng kỳ nhờ giá sữa bột nguyên liệu giảm."

    from threading import Event as Gate

    news_gate = Gate()

    class Provider:
        def search(self, _request):
            news_gate.wait(20)  # held until the page shows the update is pending
            return WebEvidenceBatch(status="found", coverage={"complete": True}, evidence=[WebEvidence(
                ticker="VNM", title="Vinamilk lãi quý 3 tăng 12%", content=body, publisher="Vietstock",
                source_url="https://vietstock.vn/2026/10/vnm-q3.htm", published_at=now.isoformat())])

    replies = []

    def model(_system, _user, _timeout):
        return jsonlib.dumps(replies.pop(0), ensure_ascii=False)

    stack.deps.news_provider = Provider
    stack.deps.news_model = model
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            code = _invite(stack.db, "update@example.com")
            page.goto(f"{stack.origin}/signup?invite={code}")
            page.fill("#name", "Cập Nhật")
            page.fill("#email", "update@example.com")
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{stack.origin}/chat")
            page.select_option("#dataset-select", stack.shared.dataset_id)

            for round_, question in enumerate(("Câu U1", "Câu U2")):
                replies[:] = [
                    {"relevant_article_ids": ["a1"],
                     "events": [{"title": "Lợi nhuận quý 3 tăng 12%", "article_ids": ["a1"], "event_date": ""}]},
                    {"events": [{"event_id": "e1", "label": "positive", "explanation": "", "uncertainties": [],
                                 "quotes": [{"article_id": "a1", "text": "Vinamilk công bố lợi nhuận quý 3 tăng 12%"}]}]},
                ]
                stack.worker_open.clear() if round_ == 1 else None
                _ask(page, question)
                if round_ == 1:
                    # Reload while the update is still waiting: it resumes by itself.
                    page.locator(".dots-text").wait_for(timeout=10_000)
                    stack.worker_open.set()
                    _answer(page, question).wait_for(timeout=15_000)
                    page.reload()
                else:
                    _answer(page, question).wait_for(timeout=15_000)
                page.locator(".news-pending").wait_for(timeout=10_000)
                news_gate.set()
                update = page.locator(".bubble.bot.rich", has_text=f"Cập nhật tin tức cho câu hỏi “{question}”")
                update.wait_for(timeout=30_000)
                assert "lợi nhuận quý 3 tăng 12%" in update.locator("blockquote").inner_text()
                assert update.locator("a", has_text="Vinamilk lãi quý 3").get_attribute("href").startswith(
                    "https://vietstock.vn/")
                page.locator(".news-pending").wait_for(state="detached", timeout=10_000)
                news_gate.clear()
            page.reload()
            page.locator(".bubble.bot.rich", has_text="Cập nhật tin tức").first.wait_for(timeout=10_000)
            assert page.locator(".bubble.bot.rich", has_text="Cập nhật tin tức").count() == 2
            assert page.locator(".news-pending").count() == 0
            assert not errors, errors
        finally:
            browser.close()
