"""Browser smoke test of the Next.js UI (web/) through its /api proxy.

Needs a production build (``cd web && npm ci && npm run build``) and
Playwright; skipped otherwise, unless ``FINX_REQUIRE_NEXT_SMOKE=1`` (CI), where
a missing build fails. The build compiles in the FastAPI address
(``FINX_API_URL``), so the test reads it from the build and serves the API
there. The pipeline is a fake that echoes the question; nothing leaves the
machine; report discovery, PDF import and their parser are fakes too.
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

from agentfinx.acquisition.cafef import CafeFReportDiscoveryProvider  # noqa: E402
from agentfinx.acquisition.download import PdfArtifactValidator  # noqa: E402
from agentfinx.acquisition.service import ReportAcquisitionService  # noqa: E402
from agentfinx.acquisition.store import AcquisitionStore  # noqa: E402
from agentfinx.core.config.runtime_policy import AcquisitionPolicy  # noqa: E402
from agentfinx.datasets import registry  # noqa: E402
from agentfinx.core.schemas.acquisition import ConvertedDocument, ConvertedPage  # noqa: E402
from agentfinx.api import auth  # noqa: E402
from agentfinx.api import db as webdb  # noqa: E402
from agentfinx.api.app import create_app  # noqa: E402
from agentfinx.api.chat_runner import ChatDependencies  # noqa: E402
from agentfinx.api.discovery import run_discovery  # noqa: E402
from agentfinx.api.imports import build_import_handler, make_session_attacher  # noqa: E402
from agentfinx.api.news_updates import run_news_update_once  # noqa: E402
from agentfinx.api.settings import WebSettings  # noqa: E402
from agentfinx.api.worker import run_chat_once  # noqa: E402

WEB = Path(__file__).resolve().parents[2] / "web"
REQUIRED = os.environ.get("FINX_REQUIRE_NEXT_SMOKE") == "1"
PASSWORD = "correct horse battery"
PDF = b"%PDF-1.7\nfixture"
MARKDOWN = "# BCTC\n\nNội dung báo cáo tài chính đầy đủ để ingest."
# Markdown the UI must render, plus markup it must never turn into elements.
UNSAFE_ANSWER = (
    "**Tổng tài sản**: 45.952 tỷ đồng\n\n"
    '<img src="x" onerror="window.__xss = 1"><script>window.__xss = 2</script>'
)
DISCOVERY_ROWS = [
    {"Name": "Báo cáo tài chính hợp nhất năm 2025 (đã kiểm toán)",
     "Link": "https://cafef1.mediacdn.vn/vnm-2025.pdf", "Year": 2025, "Quarter": 5, "Time": "CN/2025"},
    {"Name": 'Báo cáo quý 2 năm 2025 <img src="x" onerror="window.__xss = 3">',
     "Link": "https://cafef1.mediacdn.vn/vnm-q2.pdf", "Year": 2025, "Quarter": 2, "Time": "Q2/2025"},
]


def _plan_for(state):
    # Test double for the web planner: in production the model decides.
    if state["user_query"].lower().startswith("tìm"):
        return {"task_kind": "report_discovery", "planner_plan": {}, "trace": [],
                "report_search": {"subject": "VNM", "fiscal_year": 2025, "period": "annual"}}
    return {"task_kind": "report_qa", "planner_plan": {}, "trace": []}


class _Downloader:
    def __init__(self, validator):
        self.validator = validator

    def download(self, candidate, *, target_dir, cache_namespace):
        Path(target_dir).mkdir(parents=True, exist_ok=True)
        path = Path(target_dir) / "downloaded.pdf"
        path.write_bytes(PDF)
        return self.validator.validate(path, origin="cafef_discovery", source_url=candidate.source_url,
                                       public_source=True, cache_namespace=cache_namespace)


class _Converter:
    converter_identity = "fake"
    supports_attempt_ledger = True

    def convert(self, artifact, *, private, consent=None, recorder=None):
        return ConvertedDocument(
            markdown=MARKDOWN, pages=[ConvertedPage(page_number=1, markdown=MARKDOWN)],
            provider="local_text_layer", provider_version="test", tier="local_text",
            source_sha256=artifact.source_sha256, confirmed_credits="0",
        )


def _build_dataset(job, _artifact, _converted_path):
    record = registry.build_dataset_record(
        file_path="data/document.md", company=job.metadata["company"],
        fiscal_year=job.metadata.get("fiscal_year"), owner_id=job.owner_id, dataset_id=f"imp-{job.job_id[:12]}",
    )
    registry.save_dataset(record.model_copy(update={"status": "ready"}))
    return record.dataset_id


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
        report_discovery=True,
    )
    catalog = tmp_path / "symbols.json"
    catalog.write_text(json.dumps({"retrieved_at": datetime.now(timezone.utc).isoformat(), "symbols": [
        {"ticker": "VNM", "company": "Công ty Cổ phần Sữa Việt Nam", "exchange": "HOSE"}]}), encoding="utf-8")
    validator = PdfArtifactValidator(max_file_bytes=1024 * 1024, max_pdf_pages=200, page_counter=lambda _path: 1)
    service = ReportAcquisitionService(
        store=AcquisitionStore(tmp_path / "acquisition" / "acquisition.db"),
        discovery_provider=CafeFReportDiscoveryProvider(fetch_json=lambda _url: {"Success": True, "Data": DISCOVERY_ROWS}),
        converter=_Converter(),
        validator=validator,
        downloader=_Downloader(validator),
        policy=AcquisitionPolicy(monthly_parse_credit_budget=100_000, trial_successful_imports=10,
                                 per_owner_concurrency=3),
        symbol_catalog_path=catalog,
        artifact_dir=tmp_path / "artifacts",
        converted_dir=tmp_path / "converted",
        dataset_builder=_build_dataset,
    )
    app = create_app(settings, generation_inspector=lambda _record: "gen-1",
                     question_admitter=lambda _owner_id: None, import_service=service)
    database = app.state.db
    service.dataset_attacher = make_session_attacher(database, generation_inspector=lambda _record: "gen-1")
    asked: list[str] = []
    deps = ChatDependencies(
        shared_dataset_ids=settings.shared_dataset_ids,
        activate=lambda _dataset: SimpleNamespace(generation="gen-1", collection=object()),
        execute=lambda _ds, _col, question, **_kwargs: asked.append(question) or {"question": question},
        collect_errors=lambda _state: [],
        format_answer=lambda state: f"**Trả lời** cho: {state['question']}",
        discovery_enabled=True,
        plan_task=_plan_for,
        discover=lambda search, **kwargs: run_discovery(service, search, **kwargs),
    )
    import_step = build_import_handler(database, service=service)
    worker_open = threading.Event()
    worker_open.set()
    stop = threading.Event()

    def work():
        while not stop.is_set():
            worked = worker_open.is_set() and (
                run_chat_once(database, "next-smoke", deps)
                or import_step(database, "next-smoke-import")
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
        yield SimpleNamespace(origin=origin, db=database, shared=shared, worker_open=worker_open, deps=deps,
                              asked=asked, service=service)
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


def _sign_up(page, stack, email, name="Người Thử"):
    code = _invite(stack.db, email)
    page.goto(f"{stack.origin}/signup?invite={code}")
    page.fill("#name", name)
    page.fill("#email", email)
    page.fill("#password", PASSWORD)
    page.fill("#password_confirm", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(f"{stack.origin}/chat")


def _ask(page, text):
    page.fill("textarea", text)
    page.keyboard.press("Enter")


def _answer(page, question):
    return page.locator(".bubble.bot.rich", has_text=f"cho: {question}")


def _choose_report(page, dataset_id):
    """Pick the report to ask about through the start screen's report picker."""
    page.click("#dataset-picker")
    page.locator(f'.picker-results [data-dataset-id="{dataset_id}"]').click()
    page.locator(".picker-results").wait_for(state="detached")


@pytest.mark.parametrize("width", [1280, 390])
def test_user_agent_prompts_save_reload_reset_and_discard(stack, width, tmp_path):
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _sign_up(page, stack, "prompts@example.com")

            def open_personalization():
                toggle = page.get_by_role("button", name="Mở thanh bên", exact=True)
                if toggle.count() and toggle.is_visible():
                    toggle.click()
                page.get_by_role("button", name="Tài khoản", exact=True).click()
                page.get_by_role("menuitem", name="Cá nhân hoá agents").click()
                page.locator("#agent-preference-input").wait_for()

            def select(agent_id):
                back = page.get_by_role("button", name="← Chọn agent khác", exact=True)
                if width < 700 and back.is_visible():
                    back.click()
                page.locator(f'[data-agent-id="{agent_id}"]').click()
                if width < 700:
                    page.wait_for_function("""() => {
                        const input = document.querySelector('#agent-preference-input').getBoundingClientRect();
                        const body = document.querySelector('.modal-body').getBoundingClientRect();
                        return input.top >= body.top && input.bottom <= body.bottom;
                    }""")

            open_personalization()
            editor = page.locator("#agent-preference-input")
            assert editor.input_value() == ""
            assert page.locator(".agent-node").count() == 9
            data = page.request.get(f"{stack.origin}/api/me/agent-prompts").json()
            assert all(set(agent) == {"id", "label", "description"} for agent in data["agents"])
            assert page.get_by_text("Xem prompt mặc định", exact=True).count() == 0
            planner = "Ưu tiên lập kế hoạch so sánh với kỳ trước."
            editor.fill(planner)
            select("agent_synth")
            assert editor.input_value() == ""
            custom = "Mở đầu bằng 3 nhận định chính, sau đó giải thích ngắn gọn."
            editor.fill(custom)
            select("agent_planner")
            assert editor.input_value() == planner
            page.get_by_role("button", name="Lưu thay đổi", exact=True).click()
            page.get_by_role("dialog").wait_for(state="detached")
            page.reload()
            open_personalization()
            assert editor.input_value() == planner
            page.screenshot(path=str(tmp_path / f"personal-workflow-{width}.png"), full_page=True, animations="disabled")
            select("agent_synth")
            assert editor.input_value() == custom
            page.screenshot(path=str(tmp_path / f"personal-input-{width}.png"), full_page=True, animations="disabled")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            editor.fill("Thay đổi chưa lưu")
            page.get_by_role("button", name="Đóng", exact=True).last.click()
            page.get_by_text("Bạn có thay đổi chưa lưu.").wait_for()
            page.get_by_role("button", name="Bỏ thay đổi và đóng").click()
            open_personalization()
            select("agent_synth")
            assert editor.input_value() == custom
            page.get_by_role("button", name="Xoá yêu cầu").click()
            assert editor.input_value() == ""
            page.get_by_role("button", name="Lưu thay đổi", exact=True).click()
            page.get_by_role("dialog").wait_for(state="detached")
            page.reload()
            open_personalization()
            assert editor.input_value() == planner
            select("agent_synth")
            assert editor.input_value() == ""
            assert not errors
        finally:
            browser.close()


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
            page.fill("#password_confirm", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{stack.origin}/chat")
            assert page.evaluate("document.cookie") == ""

            # Two conversations, A and B, each with one answer.
            _choose_report(page, stack.shared.dataset_id)
            _ask(page, "Câu A1")
            _answer(page, "Câu A1").wait_for(timeout=15_000)
            assert "?session=" in page.url
            url_a = page.url
            page.get_by_role("button", name="Cuộc trò chuyện mới").click()
            _choose_report(page, stack.shared.dataset_id)
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
            page.locator("#dataset-picker").wait_for(timeout=10_000)
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
            page.fill("#password_confirm", PASSWORD)
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
            page.locator("#dataset-picker").wait_for(timeout=10_000)
        finally:
            browser.close()


def test_news_section_shows_sources_and_limits_and_survives_reload(stack):
    """The answer's company-news section (WEB_ANALYSIS_ENABLED) as the chat
    renders it: verified quote, Vietstock link, limitations; then a reload."""

    from agentfinx.workflow.output_formatter import format_final_answer

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
            page.fill("#password_confirm", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{stack.origin}/chat")
            _choose_report(page, stack.shared.dataset_id)
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

    import agentfinx.api.news_updates as news_updates
    from agentfinx.workflow.output_formatter import format_final_answer
    from agentfinx.core.schemas.web_evidence import WebEvidence, WebEvidenceBatch

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
            page.fill("#password_confirm", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_url(f"{stack.origin}/chat")
            _choose_report(page, stack.shared.dataset_id)

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


def _wait_for_import(page, stack, *, timeout):
    """Wait until the import card says ready, or fail with what the page and store show."""

    try:
        page.locator(".import-card .import-processing", has_text="Sẵn sàng").wait_for(timeout=timeout)
    except sync_api.TimeoutError:
        jobs = [(job.status, job.error_code, job.billing_status, job.session_attached,
                 job.session_attachment_error) for job in stack.service.store._all_jobs()]
        cards = page.locator(".import-card")
        pytest.fail(f"import never became ready; url={page.url}; "
                    f"card={cards.first.inner_text() if cards.count() else None!r}; jobs={jobs}")


@pytest.mark.parametrize("width", [1280, 390])
def test_import_progress_runs_below_messages_and_survives_reload(stack, width, tmp_path):
    stack.worker_open.clear()
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _sign_up(page, stack, f"progress{width}@example.com")
            owner = page.request.get(f"{stack.origin}/api/me").json()["user"]["id"]
            session = page.request.post(f"{stack.origin}/api/sessions", data={},
                                        headers={"Origin": stack.origin}).json()
            with stack.db.transaction() as conn:
                webdb.add_message(conn, session_id=session["id"], role="user", content="Xử lý báo cáo này giúp tôi.")
            pdf = tmp_path / "progress.pdf"
            pdf.write_bytes(PDF)
            job = stack.service.create_upload_import(owner_id=owner, session_id=session["id"],
                upload_path=pdf, idempotency_key="progress-browser-fixture", allow_cloud_parse=True,
                metadata={"company": "Công ty Mẫu", "fiscal_year": 2025})
            with stack.db.transaction() as conn:
                webdb.set_session_import(conn, session["id"], owner, job.job_id)
            page.goto(f"{stack.origin}/chat?session={session['id']}")
            log = page.locator(".import-progress")
            log.get_by_text("Tiếp nhận yêu cầu xử lý", exact=True).wait_for(timeout=10_000)
            assert page.locator(".session-banner", has_text="đang được xử lý").count() == 0
            assert page.locator(".chat-list > .row").last.locator(".import-progress").count() == 1
            sync_api.expect(page.get_by_label("Câu hỏi", exact=True)).to_be_disabled()
            job = stack.service.store.claim_job(job.job_id)
            log.get_by_text("Trích xuất nội dung báo cáo", exact=True).wait_for(timeout=10_000)
            assert log.locator(".step-running").count() == 1
            assert log.locator(".step-done").count() == 1
            page.screenshot(path=str(tmp_path / f"import-parsing-{width}.png"), animations="disabled")
            for status in ("validating", "building"):
                job = stack.service.store.update_job(job.model_copy(update={"status": status}))
            log.get_by_text("Tạo dữ liệu tra cứu cho báo cáo", exact=True).wait_for(timeout=10_000)
            page.reload()
            log.get_by_text("Tạo dữ liệu tra cứu cho báo cáo", exact=True).wait_for(timeout=10_000)
            assert log.locator(".step").count() == 4
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(tmp_path / f"import-building-{width}.png"), animations="disabled")

            dataset_id = _build_dataset(job, None, None)
            stack.service.dataset_attacher(owner, session["id"], dataset_id)
            job = stack.service.store.update_job(job.model_copy(update={"status": "ready", "dataset_id": dataset_id,
                "session_attached": True, "billing_status": "confirmed", "confirmed_credits": "0"}))
            _wait_for_import(page, stack, timeout=10_000)
            assert log.locator(".step-spinner").count() == 0
            assert log.locator(".steps-list").count() == 0
            log.get_by_role("button", name="Xem nhật ký xử lý", exact=False).click()
            assert log.locator(".step").count() == 5
            sync_api.expect(page.get_by_label("Câu hỏi", exact=True)).to_be_enabled()
            stack.worker_open.set()
            _ask(page, "Doanh thu năm 2025?")
            _answer(page, "Doanh thu năm 2025?").wait_for(timeout=15_000)
            assert page.locator(".chat-list > .row").last.locator(".import-progress").count() == 0
            assert not errors
        finally:
            browser.close()


def test_answers_render_safely_and_an_uploaded_pdf_becomes_a_conversation(stack):
    stack.deps.format_answer = lambda _state: UNSAFE_ANSWER
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _sign_up(page, stack, "upload@example.com")
            _choose_report(page, stack.shared.dataset_id)
            _ask(page, "Tổng tài sản cuối kỳ?")
            page.locator(".bubble.bot.rich strong", has_text="Tổng tài sản").wait_for(timeout=15_000)
            assert page.locator(".bubble.bot.rich img, .bubble.bot.rich script").count() == 0
            assert page.evaluate("window.__xss") is None

            page.get_by_role("button", name="Cuộc trò chuyện mới").click()
            page.locator(".start-actions").get_by_role("button", name="Tải PDF lên").click()
            page.set_input_files("#upload-form input[type=file]",
                                 files=[{"name": "bctc.pdf", "mimeType": "application/pdf", "buffer": PDF}])
            page.get_by_label("Tên công ty").fill("Công ty Tải Lên")
            page.get_by_label("Năm tài chính").fill("2025")
            page.get_by_role("button", name="Tải lên và xử lý").click()
            _wait_for_import(page, stack, timeout=20_000)
            page.locator(".import-card .import-billing", has_text="Đã xác nhận").wait_for(timeout=5_000)
            page.locator("textarea:not([disabled])").wait_for(timeout=10_000)

            page.reload()
            page.locator(".history-title", has_text="Công ty Tải Lên").wait_for(timeout=10_000)
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_many_report_candidates_collapse_filter_and_keep_selection(stack, width, tmp_path):
    rows = []
    for year in range(2025, 2021, -1):
        for quarter in [5, 4, 3, 2, 1]:
            for scope, suffix in [("hợp nhất", "consolidated"), ("công ty mẹ", "separate")]:
                period = f"năm {year}" if quarter == 5 else f"quý {quarter} năm {year}"
                rows.append({"Name": f"Báo cáo tài chính {scope} {period} (đã kiểm toán)",
                             "Link": f"https://cafef1.mediacdn.vn/vnm-{year}-{quarter}-{suffix}.pdf",
                             "Year": year, "Quarter": quarter,
                             "Time": f"CN/{year}" if quarter == 5 else f"Q{quarter}/{year}"})
    fetches = []

    def fetch(url):
        fetches.append(url)
        return {"Success": True, "Data": rows}

    stack.service.discovery_provider = CafeFReportDiscoveryProvider(fetch_json=fetch)
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _sign_up(page, stack, f"reports{width}@example.com")
            # Seed a stored discovery result through the same authenticated API;
            # this test exercises the result card independently of entry points.
            response = page.request.post(f"{stack.origin}/api/report-discoveries",
                data={"idempotency_key": "many-reports-fixture", "search": {"subject": "VNM", "period": "any"}},
                headers={"Origin": stack.origin})
            assert response.ok, response.text()
            session_id = response.json()["session_id"]
            page.goto(f"{stack.origin}/chat?session={session_id}")
            toggle = page.get_by_role("button", name="Xem 30 báo cáo", exact=True)
            toggle.wait_for(timeout=15_000)
            card = page.locator(".report-results")
            visible = card.locator(".cand-row:visible")
            assert visible.count() == 0
            assert card.bounding_box()["height"] < 340
            page.screenshot(path=str(tmp_path / f"reports-collapsed-{width}.png"), animations="disabled")

            toggle.click()
            assert visible.count() == 30
            assert card.locator(".cand-scroll").evaluate("el => el.scrollHeight > el.clientHeight")
            search = page.get_by_label("Tìm trong danh sách báo cáo")
            search.fill("2025 hop nhat")
            sync_api.expect(visible).to_have_count(5)
            search.fill("")
            page.get_by_label("Lọc năm báo cáo").click()
            page.get_by_role("option", name="2025", exact=True).click()
            sync_api.expect(visible).to_have_count(10)
            page.get_by_label("Lọc kỳ báo cáo").click()
            page.get_by_role("option", name="Cả năm", exact=True).click()
            sync_api.expect(visible).to_have_count(2)
            page.get_by_label("Lọc phạm vi báo cáo").click()
            page.get_by_role("option", name="Hợp nhất", exact=True).click()
            sync_api.expect(visible).to_have_count(1)
            assert "năm 2025" in visible.inner_text()
            page.screenshot(path=str(tmp_path / f"reports-filtered-{width}.png"), animations="disabled")
            visible.get_by_role("button", name="Chọn báo cáo này").click()
            card.locator(".confirm-panel:visible").wait_for()
            page.get_by_role("button", name="Thu gọn danh sách", exact=True).click()
            toggle.click()
            assert card.locator(".confirm-panel:visible").count() == 1
            assert visible.count() == 1  # filters and confirmation survive collapse

            search.fill("khong co bao cao nay")
            sync_api.expect(visible).to_have_count(0)
            card.get_by_text("Không có báo cáo khớp bộ lọc", exact=False).wait_for()
            card.get_by_role("button", name="Xoá bộ lọc").click()
            sync_api.expect(visible).to_have_count(30)
            assert len(fetches) == 1
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            card.get_by_role("button", name="Tìm lại theo năm hoặc kỳ").click()
            assert page.get_by_label("Mã CK hoặc tên công ty").input_value() == "VNM"
            page.get_by_role("button", name="Đóng", exact=True).click()
            page.reload()
            toggle.wait_for(timeout=15_000)
            assert visible.count() == 0 and len(fetches) == 1
            assert not errors
        finally:
            browser.close()


def test_find_confirm_import_ask_and_answer_on_the_open_report(stack):
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _sign_up(page, stack, "finder@example.com", name="Người Tìm")

            # Search from the start screen: metadata only, shown as a stored card.
            page.locator(".start-actions").get_by_role("button", name="Tìm trên CafeF").click()
            page.get_by_label("Mã CK hoặc tên công ty").fill("VNM")
            page.get_by_label("Năm tài chính").fill("2025")
            page.get_by_role("button", name="Tìm báo cáo").click()
            rows = page.locator(".report-card .cand-row")
            rows.first.wait_for(timeout=15_000)
            assert rows.count() == 2
            assert page.locator(".report-card img").count() == 0  # an untrusted title stays text
            assert page.evaluate("window.__xss") is None
            assert '<img src="x"' in page.locator(".report-card").inner_text()

            # A reload shows the same card from history, without searching again.
            page.reload()
            page.locator(".history-title", has_text="Tìm báo cáo VNM").click()
            rows.first.wait_for(timeout=10_000)

            # Choose, confirm (cloud parsing is allowed without a checkbox), import, ask.
            annual = rows.filter(has_text="Báo cáo năm 2025")
            annual.get_by_role("button", name="Chọn báo cáo này").click()
            assert annual.locator(".confirm-panel input[type=checkbox]").count() == 0
            annual.get_by_role("button", name="Xác nhận tải và xử lý").click()
            _wait_for_import(page, stack, timeout=20_000)
            page.locator("textarea:not([disabled])").wait_for(timeout=10_000)
            _ask(page, "Doanh thu năm 2025?")
            _answer(page, "Doanh thu năm 2025?").wait_for(timeout=15_000)

            # In a conversation with a report, a question taken as "find a report"
            # can be answered on the open report instead.
            _ask(page, "Tìm BCTC VNM năm 2025")
            override = page.get_by_role("button", name="Trả lời trên báo cáo đang mở").last
            override.wait_for(timeout=15_000)
            override.click()
            _answer(page, "Tìm BCTC VNM năm 2025").wait_for(timeout=15_000)
            assert stack.asked[-1] == "Tìm BCTC VNM năm 2025"
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_profit_choice_and_confirmation_resume_after_reload(stack, width):
    stack.deps.context_enabled = True
    seed = "Doanh thu năm 2025 là bao nhiêu?"
    initial = "lợi nhuận của công ty là bao nhiêu"
    direct = "lợi nhuận sau thuế của công ty là bao nhiêu?"
    confirmed = "lợi nhuận sau thuế của công ty trong năm 2025 là bao nhiêu?"
    mode = {"confirm": False}

    def resolve(question, history):
        if question == initial:
            return {"status": "needs_clarification", "metric": {}, "period": {},
                    "clarification_question": "Bạn muốn hỏi lợi nhuận nào (sau thuế, trước thuế, gộp)?"}
        if question == "sau thuế":
            assert history[-1].effective_question == initial
            assert "lợi nhuận nào" in history[-1].clarification_question
            if mode["confirm"]:
                return {"status": "needs_clarification", "metric": {}, "period": {},
                        "clarification_question": "Bạn muốn hỏi lợi nhuận sau thuế của công ty trong năm 2025?"}
            return {"status": "resolved", "question": direct,
                    "metric": {"kept": [{"source_run_id": history[-1].run_id, "text": "lợi nhuận"}],
                               "changed": ["sau thuế"]}, "period": {}}
        if question == "đúng":
            assert history[-1].effective_question == "sau thuế"
            assert "lợi nhuận sau thuế" in history[-1].clarification_question
            return {"status": "resolved", "question": confirmed,
                    "metric": {"kept": [{"source_run_id": history[-1].run_id, "text": "lợi nhuận sau thuế"}]},
                    "period": {"kept": [{"source_run_id": history[-1].run_id, "text": "năm 2025"}]}}
        return {"status": "standalone", "question": question, "metric": {}, "period": {}}

    stack.deps.clarify = resolve
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _sign_up(page, stack, f"profit{width}@example.com")
            _choose_report(page, stack.shared.dataset_id)
            _ask(page, seed)
            _answer(page, seed).wait_for(timeout=15_000)
            _ask(page, initial)
            page.locator(".context-card", has_text="Bạn muốn hỏi lợi nhuận nào").wait_for(timeout=15_000)
            page.reload()
            page.locator(".context-card", has_text="Bạn muốn hỏi lợi nhuận nào").wait_for(timeout=15_000)
            _ask(page, "sau thuế")
            _answer(page, direct).wait_for(timeout=15_000)
            assert stack.asked == [seed, direct]

            # Older conversations may already be waiting for confirmation.
            mode["confirm"] = True
            _ask(page, initial)
            sync_api.expect(page.locator(".context-card", has_text="Bạn muốn hỏi lợi nhuận nào")).to_have_count(2, timeout=15_000)
            _ask(page, "sau thuế")
            page.locator(".context-card", has_text="trong năm 2025").wait_for(timeout=15_000)
            page.reload()
            page.locator(".context-card", has_text="trong năm 2025").wait_for(timeout=15_000)
            _ask(page, "đúng")
            _answer(page, confirmed).wait_for(timeout=15_000)
            assert stack.asked == [seed, direct, confirmed]
            assert page.locator(".context-card", has_text="chỉ tiêu, kỳ hoặc công ty nào").count() == 0
            assert not errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_followup_context_clarification_and_failure_survive_reload(stack, width):
    first = "Vốn chủ sở hữu của công ty là gì"
    effective = "Vốn chủ sở hữu của công ty vào đầu năm là bao nhiêu?"
    stack.deps.context_enabled = True

    def resolve(question, history):
        if question == "vào đầu năm thì sao":
            return {"status": "resolved", "question": effective,
                    "metric": {"kept": [{"source_run_id": history[-1].run_id, "text": "Vốn chủ sở hữu"}]},
                    "period": {"changed": ["đầu năm"]}}
        if question == "Chỉ tiêu đó thì sao?":
            return {"status": "needs_clarification", "metric": {}, "period": {},
                    "clarification_question": '<img src="x" onerror="window.__context_xss = 1"> Bạn muốn hỏi chỉ tiêu nào?'}
        return question  # an invalid old-style reply: must not run QA

    stack.deps.clarify = resolve
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _sign_up(page, stack, f"context{width}@example.com")
            _choose_report(page, stack.shared.dataset_id)
            _ask(page, first)
            _answer(page, first).wait_for(timeout=15_000)
            _ask(page, "vào đầu năm thì sao")
            page.locator(".msg-caption", has_text=effective).wait_for(timeout=15_000)
            assert stack.asked == [first, effective]

            _ask(page, "Chỉ tiêu đó thì sao?")
            page.locator(".context-card", has_text="Bạn muốn hỏi chỉ tiêu nào?").wait_for(timeout=15_000)
            assert page.locator(".context-card img").count() == 0
            assert page.evaluate("window.__context_xss") is None
            assert stack.asked == [first, effective]

            _ask(page, "Lợi nhuận năm 2025 là bao nhiêu?")
            page.locator(".context-card", has_text="Bước xử lý ngữ cảnh gặp lỗi").wait_for(timeout=15_000)
            page.reload()
            if page.get_by_role("button", name="Mở thanh bên").is_visible():
                page.get_by_role("button", name="Mở thanh bên").click()
            page.locator(".history-title", has_text=first).click()
            page.locator(".msg-caption", has_text=effective).wait_for(timeout=10_000)
            assert page.locator(".context-card", has_text="Bạn muốn hỏi chỉ tiêu nào?").count() == 1
            assert page.locator(".context-card", has_text="Bước xử lý ngữ cảnh gặp lỗi").count() == 1
            assert page.locator(".context-card img, .context-card script").count() == 0
            assert stack.asked == [first, effective] and not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_answer_source_opens_original_pdf_page_in_chat(stack, width, tmp_path):
    from test_report_sources import make_source_pdf, seed_source

    stack.worker_open.clear()
    pdf = tmp_path / "source.pdf"
    make_source_pdf(pdf)
    record = registry.save_dataset(stack.shared.model_copy(update={"source_pdf_path": str(pdf)}))
    source = {"id": "profit-fixture", "dataset_id": record.dataset_id, "generation": "gen-1",
              "label": "Lợi nhuận sau thuế", "value": "123.456.789", "period": "Năm 2025",
              "unit": "VND", "section": "Kết quả kinh doanh", "page": 2}
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _sign_up(page, stack, f"source{width}@example.com")
            owner = page.request.get(f"{stack.origin}/api/me").json()["user"]["id"]
            session_id, message = seed_source(stack.db, owner, record, source)
            page.goto(f"{stack.origin}/chat?session={session_id}")
            chip = page.get_by_role("button", name="Đối chiếu Lợi nhuận sau thuế: 123.456.789, trang PDF 2", exact=True)
            chip.wait_for(timeout=10_000)
            chip.click()
            dialog = page.get_by_role("dialog", name="Đối chiếu báo cáo")
            image = dialog.get_by_alt_text("Trang 2 của báo cáo gốc", exact=True)
            sync_api.expect(image).to_be_visible(timeout=10_000)
            assert image.evaluate("img => img.naturalWidth > 0")
            assert dialog.get_by_role("link", name="Mở PDF gốc").get_attribute("href").endswith("#page=2")
            page.screenshot(path=str(tmp_path / f"report-source-{width}.png"), animations="disabled")
            dialog.get_by_role("button", name="Phóng to", exact=True).click()
            assert dialog.locator(".source-page-scroll").evaluate("el => el.scrollWidth > el.clientWidth")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            dialog.get_by_role("button", name="Trang trước", exact=True).click()
            sync_api.expect(dialog.get_by_alt_text("Trang 1 của báo cáo gốc", exact=True)).to_be_visible()
            page.keyboard.press("Escape")
            sync_api.expect(dialog).to_have_count(0)
            page.reload()
            chip.wait_for(timeout=10_000)
            chip.click()
            sync_api.expect(page.get_by_alt_text("Trang 2 của báo cáo gốc", exact=True)).to_be_visible()
            assert not errors
        finally:
            browser.close()
