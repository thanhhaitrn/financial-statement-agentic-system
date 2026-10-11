"""Browser smoke test of the web UI against mocked workers.

Runs in CI (``pip install -e '.[web,e2e]'`` then ``python -m playwright install
chromium``) and is skipped where Playwright is not installed. It drives the real
frontend, API, cookie auth and Origin checks; the pipeline, the parser and the
index builder are fakes, so nothing leaves the machine. The full end-to-end run
against real models and Qdrant stays a manual, local check.
"""

import json
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")
pytest.importorskip("multipart")
uvicorn = pytest.importorskip("uvicorn")
sync_api = pytest.importorskip("playwright.sync_api")

from acquisition.cafef import CafeFReportDiscoveryProvider  # noqa: E402
from acquisition.download import PdfArtifactValidator  # noqa: E402
from acquisition.service import ReportAcquisitionService  # noqa: E402
from acquisition.store import AcquisitionStore  # noqa: E402
from config.runtime_policy import AcquisitionPolicy  # noqa: E402
from dataset_catalog import registry  # noqa: E402
from schemas.acquisition import ConvertedDocument, ConvertedPage  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.chat_runner import ChatDependencies  # noqa: E402
from webapp.discovery import run_discovery  # noqa: E402
from webapp.imports import build_import_handler, make_session_attacher  # noqa: E402
from webapp.settings import WebSettings  # noqa: E402
from webapp.worker import run_chat_once  # noqa: E402

PDF = b"%PDF-1.7\nfixture"
MARKDOWN = "# BCTC\n\nNội dung báo cáo tài chính đầy đủ để ingest."
# Markdown the UI must render, plus markup DOMPurify must strip.
ANSWER = (
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
            markdown=MARKDOWN,
            pages=[ConvertedPage(page_number=1, markdown=MARKDOWN)],
            provider="local_text_layer",
            provider_version="test",
            tier="local_text",
            source_sha256=artifact.source_sha256,
            confirmed_credits="0",
        )


def _build_dataset(job, _artifact, _converted_path):
    record = registry.build_dataset_record(
        file_path="data/document.md",
        company=job.metadata["company"],
        fiscal_year=job.metadata.get("fiscal_year"),
        owner_id=job.owner_id,
        dataset_id=f"imp-{job.job_id[:12]}",
    )
    registry.save_dataset(record.model_copy(update={"status": "ready"}))
    return record.dataset_id


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def live(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(registry, "DATASETS_DIR", tmp_path / "dataset_store")
    monkeypatch.setattr(registry, "REGISTRY_PATH", registry.DATASETS_DIR / "registry.json")
    monkeypatch.setattr(registry, "MANIFESTS_DIR", registry.DATASETS_DIR / "manifests")
    monkeypatch.setattr(registry, "RAW_TABLES_DIR", registry.DATASETS_DIR / "raw_tables")
    monkeypatch.setattr(registry, "SQLITE_DIR", registry.DATASETS_DIR / "sqlite")
    shared = registry.save_dataset(
        registry.build_dataset_record(
            file_path="data/document.md", company="Công ty Mẫu", fiscal_year=2025
        ).model_copy(update={"status": "ready"})
    )

    port = _free_port()
    origin = f"http://127.0.0.1:{port}"
    settings = WebSettings(
        secret_key="smoke-secret-" + "z" * 40,
        db_path=tmp_path / "web.sqlite3", registration_mode="open",
        port=port,
        allowed_origins=frozenset({origin}),
        shared_dataset_ids=frozenset({shared.dataset_id}),
        heartbeat_interval_seconds=60,
        report_discovery=True,
    )
    catalog = tmp_path / "symbols.json"
    catalog.write_text(json.dumps({"retrieved_at": datetime.now(timezone.utc).isoformat(), "symbols": [
        {"ticker": "VNM", "company": "Công ty Cổ phần Sữa Việt Nam", "exchange": "HOSE"}]}), encoding="utf-8")
    validator = PdfArtifactValidator(
        max_file_bytes=1024 * 1024, max_pdf_pages=200, page_counter=lambda _path: 1
    )
    generations: dict[str, str] = {}

    def inspect(record):
        return generations.setdefault(record.dataset_id, "gen-1")

    service = ReportAcquisitionService(
        store=AcquisitionStore(tmp_path / "acquisition" / "acquisition.db"),
        discovery_provider=CafeFReportDiscoveryProvider(
            fetch_json=lambda _url: {"Success": True, "Data": DISCOVERY_ROWS}
        ),
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
    app = create_app(
        settings,
        generation_inspector=inspect,
        question_admitter=lambda _owner_id: None,
        import_service=service,
    )
    database = app.state.db
    service.dataset_attacher = make_session_attacher(database, generation_inspector=inspect)
    asked = []
    deps = ChatDependencies(
        shared_dataset_ids=settings.shared_dataset_ids,
        activate=lambda dataset: SimpleNamespace(generation=inspect(dataset), collection=object()),
        execute=lambda _ds, _col, question, **_kwargs: asked.append(question) or {},
        collect_errors=lambda _state: [],
        format_answer=lambda _state: ANSWER,
        discovery_enabled=True,
        plan_task=_plan_for,
        discover=lambda search, **kwargs: run_discovery(service, search, **kwargs),
    )
    import_step = build_import_handler(database, service=service)
    stop = threading.Event()

    def work():
        while not stop.is_set():
            chat = run_chat_once(database, "smoke-chat", deps)
            imported = import_step(database, "smoke-import")
            if not (chat or imported):
                stop.wait(0.05)

    worker = threading.Thread(target=work, daemon=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server_thread = threading.Thread(target=server.run, daemon=True)
    worker.start()
    server_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "web server did not start"
    try:
        yield SimpleNamespace(origin=origin, shared=shared, service=service, worker=worker,
                              deps=deps, asked=asked, database=database)
    finally:
        server.should_exit = True
        stop.set()
        server_thread.join(timeout=5)
        worker.join(timeout=5)


def _wait_for_import(page, live, text, *, timeout, browser_errors):
    """Wait for the import card, or fail with what the page and store show."""

    try:
        page.locator(".import-card .import-processing", has_text=text).wait_for(timeout=timeout)
    except sync_api.TimeoutError:
        cards = page.locator(".import-card")
        jobs = [
            (job.status, job.error_code, job.billing_status, job.session_attached,
             job.session_attachment_error)
            for job in live.service.store._all_jobs()
        ]
        pytest.fail(
            f"import never showed {text!r}; url={page.url}; "
            f"upload error={page.inner_text('#uploadError')!r}; "
            f"card={cards.first.inner_text() if cards.count() else None!r}; "
            f"welcome hidden={page.is_hidden('#welcomeState')}; "
            f"conversation={page.inner_text('#conversation')[:300]!r}; jobs={jobs}; "
            f"browser errors={browser_errors}; worker alive={live.worker.is_alive()}"
        )


def test_register_ask_and_upload_in_a_real_browser(live):
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        browser_errors: list[str] = []
        page.on("pageerror", lambda exc: browser_errors.append(f"pageerror: {exc}"))
        page.on("console", lambda msg: browser_errors.append(f"{msg.type}: {msg.text}")
                if msg.type in {"error", "warning"} else None)
        try:
            page.goto(f"{live.origin}/register")
            if page.is_hidden("#registerPanel"):
                page.click("#showRegister")
            page.fill("#registerName", "Người Thử")
            page.fill("#registerEmail", "smoke@example.com")
            page.fill("#registerPassword", "correct horse battery")
            page.fill("#registerConfirm", "correct horse battery")
            page.click("#registerSubmit")
            page.wait_for_url(f"{live.origin}/chat")
            assert page.evaluate("document.cookie") == ""  # the session cookie is HttpOnly

            page.select_option("#datasetSelect", live.shared.dataset_id)
            page.fill("#composerInput", "Tổng tài sản cuối kỳ?")
            page.click("#sendBtn")
            page.locator(".msg-ai .markdown strong", has_text="Tổng tài sản").wait_for(timeout=15_000)
            assert page.locator(".markdown img, .markdown script").count() == 0
            assert page.evaluate("window.__xss") is None

            page.click("#newChatBtn")
            page.click("#uploadOpenBtn")
            page.set_input_files(
                "#uploadFile",
                files=[{"name": "bctc.pdf", "mimeType": "application/pdf", "buffer": PDF}],
            )
            page.fill("#uploadCompany", "Công ty Tải Lên")
            page.fill("#uploadYear", "2025")
            page.click("#uploadSubmitBtn")
            _wait_for_import(page, live, "Sẵn sàng", timeout=15_000, browser_errors=browser_errors)
            page.locator(".import-card .import-billing", has_text="Đã xác nhận").wait_for(timeout=5_000)
            page.locator("#composerInput:not([disabled])").wait_for(timeout=10_000)

            page.reload()
            page.locator(".history-item", has_text="Công ty Tải Lên").wait_for(timeout=10_000)
        finally:
            browser.close()


def _register(page, live, email):
    page.goto(f"{live.origin}/register")
    if page.is_hidden("#registerPanel"):
        page.click("#showRegister")
    page.fill("#registerName", "Người Tìm")
    page.fill("#registerEmail", email)
    page.fill("#registerPassword", "correct horse battery")
    page.fill("#registerConfirm", "correct horse battery")
    page.click("#registerSubmit")
    page.wait_for_url(f"{live.origin}/chat")


def test_find_confirm_import_and_ask_in_a_real_browser(live):
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        try:
            _register(page, live, "finder@example.com")

            # Search from the start screen: metadata only, as a stored card.
            page.click("#discoveryOpenBtn")
            page.fill("#discoverySubject", "VNM")
            page.fill("#discoveryYear", "2025")
            page.click("#discoverySubmitBtn")
            rows = page.locator(".report-card .candidate-row")
            rows.first.wait_for(timeout=15_000)
            assert rows.count() == 2
            assert page.locator(".report-card img").count() == 0  # untrusted title stays text
            assert page.evaluate("window.__xss") is None
            assert '<img src="x"' in page.inner_text(".report-card")

            # A reload shows the same card from history, without searching again.
            page.reload()
            page.locator(".history-item", has_text="Tìm báo cáo VNM").click()
            rows.first.wait_for(timeout=10_000)

            # Choose, confirm (consent off by default), import, then ask.
            annual = rows.filter(has_text="Báo cáo năm 2025")
            annual.locator(".candidate-choose").click()
            consent = annual.locator(".confirm-panel input[type=checkbox]")
            assert not consent.is_checked()
            annual.get_by_role("button", name="Xác nhận tải và xử lý").click()
            page.locator(".import-card .import-processing", has_text="Sẵn sàng").wait_for(timeout=20_000)
            page.locator("#composerInput:not([disabled])").wait_for(timeout=10_000)
            page.fill("#composerInput", "Doanh thu năm 2025?")
            page.click("#sendBtn")
            page.locator(".msg-ai .markdown strong", has_text="Tổng tài sản").wait_for(timeout=15_000)

            # In a conversation with a report, a question taken as "find a report"
            # can be answered on the open report instead.
            page.fill("#composerInput", "Tìm BCTC VNM năm 2025")
            page.click("#sendBtn")
            override = page.get_by_role("button", name="Trả lời trên báo cáo đang mở").last
            override.wait_for(timeout=15_000)
            answers_before = page.locator(".msg-ai .markdown strong", has_text="Tổng tài sản").count()
            override.click()
            page.wait_for_function(
                "n => document.querySelectorAll('.msg-ai .markdown strong').length > n",
                arg=answers_before, timeout=15_000,
            )
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1280, 390])
def test_followup_context_clarification_and_failure_survive_reload(live, width):
    first = "Vốn chủ sở hữu của công ty là gì"
    effective = "Vốn chủ sở hữu của công ty vào đầu năm là bao nhiêu?"
    live.deps.context_enabled = True

    def resolve(question, history):
        if question == "vào đầu năm thì sao":
            return {"status": "resolved", "question": effective,
                    "metric": {"kept": [{"source_run_id": history[-1].run_id, "text": "Vốn chủ sở hữu"}]},
                    "period": {"changed": ["đầu năm"]}}
        if question == "Chỉ tiêu đó thì sao?":
            return {"status": "needs_clarification", "metric": {}, "period": {},
                    "clarification_question": '<img src="x" onerror="window.__context_xss = 1"> Bạn muốn hỏi chỉ tiêu nào?'}
        return question  # invalid old-style text; must not run QA

    live.deps.clarify = resolve
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": width, "height": 900})
        errors = []
        page.on("pageerror", lambda exc: errors.append(str(exc)))
        try:
            _register(page, live, "context@example.com")
            page.select_option("#datasetSelect", live.shared.dataset_id)
            page.fill("#composerInput", first)
            page.click("#sendBtn")
            page.locator(".msg-ai .markdown strong", has_text="Tổng tài sản").wait_for(timeout=15_000)
            page.fill("#composerInput", "vào đầu năm thì sao")
            page.click("#sendBtn")
            page.locator(".msg-caption", has_text=effective).wait_for(timeout=15_000)
            assert live.asked == [first, effective]

            page.fill("#composerInput", "Chỉ tiêu đó thì sao?")
            page.click("#sendBtn")
            page.locator(".context-card", has_text="Bạn muốn hỏi chỉ tiêu nào?").wait_for(timeout=15_000)
            assert page.locator(".context-card img").count() == 0
            assert page.evaluate("window.__context_xss") is None
            assert live.asked == [first, effective]

            page.fill("#composerInput", "Lợi nhuận năm 2025 là bao nhiêu?")
            page.click("#sendBtn")
            page.locator(".context-card", has_text="Không làm rõ được ngữ cảnh").wait_for(timeout=15_000)
            page.reload()
            if page.locator("#mobileSidebarBtn").is_visible():
                page.click("#mobileSidebarBtn")
            page.locator(".history-item", has_text=first).click()
            page.locator(".msg-caption", has_text=effective).wait_for(timeout=10_000)
            assert page.locator(".context-card", has_text="Bạn muốn hỏi chỉ tiêu nào?").count() == 1
            assert page.locator(".context-card", has_text="Không làm rõ được ngữ cảnh").count() == 1
            assert page.locator(".context-card img, .context-card script").count() == 0
            assert live.asked == [first, effective] and not errors
        finally:
            browser.close()
