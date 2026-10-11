"""Web PDF upload → import job → import worker → report attached to its session."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient  # noqa: E402

from acquisition.download import PdfArtifactValidator  # noqa: E402
from acquisition.llamaparse import LlamaParseError  # noqa: E402
from acquisition.service import ReportAcquisitionService  # noqa: E402
from acquisition.store import AcquisitionStore  # noqa: E402
from config.runtime_policy import AcquisitionPolicy  # noqa: E402
from dataset_catalog import registry  # noqa: E402
from schemas.acquisition import ConvertedDocument, ConvertedPage  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.imports import build_import_handler, make_session_attacher  # noqa: E402
from webapp.settings import WebSettings  # noqa: E402
from webapp.worker import run_worker  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"
SECRET = "test-secret-" + "y" * 40
PDF = b"%PDF-1.7\nfixture"
MARKDOWN = "# BCTC\n\nNội dung báo cáo tài chính đầy đủ để ingest."
STATIC = Path(__file__).resolve().parents[1] / "webapp" / "static"


def _configure_registry_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))


class _Converter:
    """Honours per-job consent like the real adapter; this PDF has no text layer."""

    converter_identity = "fake"
    supports_attempt_ledger = True

    def __init__(self, *, usage_reported=True):
        self.calls = []
        self.usage_reported = usage_reported

    def convert(self, artifact, *, private, consent=None, recorder=None):
        self.calls.append({"private": private, "consent": consent})
        if not consent:
            raise LlamaParseError("Cloud parsing requires consent: /srv/private/path",
                                  code="cloud_consent_required")
        return ConvertedDocument(
            markdown=MARKDOWN,
            pages=[ConvertedPage(page_number=1, markdown=MARKDOWN)],
            provider="llamaparse",
            provider_version="test",
            tier="cost_effective",
            source_sha256=artifact.source_sha256,
            billed_pages=1,
            billed_credits=3 if self.usage_reported else 0,
            estimated_credits=3,
            confirmed_credits="3" if self.usage_reported else None,
            credits_confirmed=self.usage_reported,
        )


def _build_dataset(job, _artifact, _converted_path):
    record = registry.build_dataset_record(
        file_path="data/document.md",
        company=job.metadata["company"],
        fiscal_year=job.metadata.get("fiscal_year"),
        ticker=job.metadata.get("ticker", ""),
        owner_id=job.owner_id,
        dataset_id=f"imp-{job.job_id[:12]}",
    )
    registry.save_dataset(record.model_copy(update={"status": "ready"}))
    return record.dataset_id


class WebImports:
    def __init__(self, tmp_path, *, converter=None):
        self.tmp_path = tmp_path
        self.settings = WebSettings(
            secret_key=SECRET,
            db_path=tmp_path / "web.sqlite3", registration_mode="open",
            allowed_origins=frozenset({ORIGIN}),
            worker_poll_seconds=0.01,
            heartbeat_interval_seconds=60,
        )
        self.generations: dict[str, str] = {}
        self.converter = converter or _Converter()
        self.service = ReportAcquisitionService(
            store=AcquisitionStore(tmp_path / "acquisition" / "acquisition.db"),
            discovery_provider=SimpleNamespace(),
            converter=self.converter,
            validator=PdfArtifactValidator(
                max_file_bytes=4096, max_pdf_pages=200, page_counter=lambda _path: 1
            ),
            downloader=SimpleNamespace(),
            policy=AcquisitionPolicy(
                max_file_bytes=4096,
                max_pdf_pages=200,
                monthly_parse_credit_budget=100_000,
                trial_successful_imports=10,
            ),
            artifact_dir=tmp_path / "artifacts",
            converted_dir=tmp_path / "converted",
            dataset_builder=_build_dataset,
        )
        self.app = create_app(
            self.settings,
            generation_inspector=self._inspect,
            question_admitter=lambda _owner_id: None,
            import_service=self.service,
        )
        self.db = self.app.state.db
        self.service.dataset_attacher = make_session_attacher(
            self.db, generation_inspector=self._inspect
        )

    def _inspect(self, record):
        return self.generations.setdefault(record.dataset_id, "gen-1")

    def client(self):
        return TestClient(self.app, base_url=ORIGIN)

    def work(self):
        return build_import_handler(self.db, service=self.service)(self.db, "import-test")

    def upload_files(self):
        root = self.tmp_path / "storage" / "acquisition" / "web-uploads"
        return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


@pytest.fixture
def make_web(monkeypatch, tmp_path):
    _configure_registry_paths(monkeypatch, tmp_path)
    return lambda **kwargs: WebImports(tmp_path, **kwargs)


def _post(client, path, payload=None, *, origin=ORIGIN):
    return client.post(path, json=payload if payload is not None else {},
                       headers={"Origin": origin} if origin else {})


def _register(client, email):
    response = _post(client, "/api/register",
                     {"name": "Người dùng", "email": email, "password": "correct horse battery"})
    assert response.status_code == 201, response.text
    return response.json()["user"]


def _upload(client, body=PDF, *, name="report.pdf", content_type="application/pdf", origin=ORIGIN):
    return client.post("/api/report-uploads", files={"file": (name, body, content_type)},
                       headers={"Origin": origin} if origin else {})


def _start_import(client, *, key="import-key-1", consent=True, **extra):
    upload = _upload(client)
    assert upload.status_code == 201, upload.text
    session = _post(client, "/api/sessions", {})
    assert session.status_code == 201, session.text
    payload = {
        "session_id": session.json()["id"],
        "upload_id": upload.json()["upload_id"],
        "idempotency_key": key,
        "allow_cloud_parse": consent,
        "metadata": {"company": "Công ty A", "ticker": "aaa", "fiscal_year": 2025},
        **extra,
    }
    return session.json(), payload, _post(client, "/api/report-imports", payload)


def _chat(client, session_id, key="chat-key-0001"):
    return _post(client, "/api/chat",
                 {"session_id": session_id, "message": "Doanh thu năm 2025?", "idempotency_key": key})


def test_upload_import_worker_attaches_the_report_and_chat_opens(make_web):
    web = make_web()
    client = web.client()
    _register(client, "alice@example.com")

    session, payload, response = _start_import(client)

    assert response.status_code == 202, response.text
    job = response.json()
    assert job["status"] == "queued" and job["allow_cloud_parse"] is True
    assert str(web.tmp_path) not in response.text and "path" not in json.dumps(job)
    assert web.upload_files() == []  # the job works from its own staged copy
    [listed] = client.get("/api/sessions").json()
    assert listed["import_job_id"] == job["job_id"] and listed["dataset_id"] is None
    assert _chat(client, session["id"]).json()["detail"]["code"] == "session_without_dataset"

    assert web.work() is True

    status = client.get(f"/api/report-imports/{job['job_id']}").json()
    assert status["status"] == "ready" and status["session_attached"] is True
    assert status["billing_status"] == "confirmed" and status["confirmed_credits"] == "3"
    [listed] = client.get("/api/sessions").json()
    assert listed["dataset_id"] == status["dataset_id"]
    assert listed["title"].startswith("Công ty A (AAA)")
    assert any(item["id"] == status["dataset_id"] and item["access"] == "owned"
               for item in client.get("/api/datasets").json())
    assert _chat(client, session["id"]).status_code == 202
    assert web.converter.calls == [{"private": True, "consent": True}]
    assert web.work() is False

    # Importing from a conversation that already has a report opens a new
    # conversation for it, created only at confirmation; the old one keeps its report.
    second_upload = _upload(client).json()["upload_id"]
    again = _post(client, "/api/report-imports",
                  {**payload, "upload_id": second_upload, "idempotency_key": "import-key-2"})
    assert again.status_code == 202, again.text
    assert again.json()["created_session"] is True
    assert again.json()["target_session_id"] != session["id"]
    sessions = {item["id"]: item for item in client.get("/api/sessions").json()}
    assert sessions[session["id"]]["dataset_id"] == status["dataset_id"]
    assert again.json()["target_session_id"] in sessions


def test_consent_defaults_to_false_and_errors_are_safe(make_web):
    web = make_web()
    client = web.client()
    _register(client, "alice@example.com")
    upload = _upload(client).json()
    session = _post(client, "/api/sessions", {}).json()

    response = _post(client, "/api/report-imports", {
        "session_id": session["id"], "upload_id": upload["upload_id"],
        "idempotency_key": "import-key-1", "metadata": {"company": "Công ty A"},
    })
    assert response.status_code == 202 and response.json()["allow_cloud_parse"] is False
    web.work()

    status = client.get(f"/api/report-imports/{response.json()['job_id']}").json()
    assert status["status"] == "failed" and status["error_code"] == "cloud_consent_required"
    assert "LlamaParse" in status["error_message"] and "/srv" not in json.dumps(status)
    assert status["billing_status"] == "not_submitted"
    assert web.converter.calls == [{"private": True, "consent": False}]


@pytest.mark.parametrize("extra", [
    {"owner_id": "someone-else"},
    {"upload_path": "/etc/passwd"},
    {"billed_credits": 0},
    {"remote_job_id": "job-x"},
])
def test_client_cannot_set_owner_paths_usage_or_remote_ids(make_web, extra):
    web = make_web()
    client = web.client()
    _register(client, "alice@example.com")

    _session, _payload, response = _start_import(client, **extra)

    assert response.status_code == 422
    assert web.service.store.next_queued_job() is None


def test_metadata_rejects_unknown_fields(make_web):
    client = make_web().client()
    _register(client, "alice@example.com")
    _session, payload, _response = _start_import(client, key="import-key-1", consent=False)
    payload["metadata"] = {"company": "A", "owner_id": "x"}
    payload["idempotency_key"] = "import-key-2"
    assert _post(client, "/api/report-imports", payload).status_code == 422


def test_uploads_are_checked_before_anything_is_kept(make_web):
    web = make_web()
    client = web.client()
    _register(client, "alice@example.com")

    assert _upload(client, b"<html>not a pdf</html>").json()["detail"]["code"] == "invalid_pdf_magic"
    assert _upload(client, b"%PDF-" + b"x" * 5000).status_code == 413
    assert _upload(client, PDF, name="report.html", content_type="text/html").status_code == 422
    assert _upload(client, PDF, origin=None).status_code == 403
    assert web.upload_files() == []


def test_imports_uploads_and_sessions_are_owner_scoped(make_web):
    web = make_web()
    alice_client, bob_client = web.client(), web.client()
    _register(alice_client, "alice@example.com")
    _register(bob_client, "bob@example.com")
    alice_session, payload, response = _start_import(alice_client)
    job_id = response.json()["job_id"]

    assert bob_client.get(f"/api/report-imports/{job_id}").status_code == 404
    bob_upload = _upload(bob_client).json()["upload_id"]
    bob_session = _post(bob_client, "/api/sessions", {}).json()
    into_alice = _post(bob_client, "/api/report-imports",
                       {**payload, "idempotency_key": "bob-key-0001", "upload_id": bob_upload})
    assert into_alice.status_code == 404
    alice_upload = _upload(alice_client).json()["upload_id"]
    steal = _post(bob_client, "/api/report-imports",
                  {**payload, "session_id": bob_session["id"], "upload_id": alice_upload,
                   "idempotency_key": "bob-key-0002"})
    assert steal.status_code == 404 and steal.json()["detail"]["code"] == "upload_not_found"
    assert alice_session["id"] != bob_session["id"]


def test_import_replay_is_idempotent_and_consent_is_part_of_it(make_web):
    web = make_web()
    client = web.client()
    _register(client, "alice@example.com")
    _session, payload, first = _start_import(client)

    replay = _post(client, "/api/report-imports", payload)
    assert replay.status_code == 202 and replay.json()["replayed"] is True
    assert replay.json()["job_id"] == first.json()["job_id"]
    conflict = _post(client, "/api/report-imports", {**payload, "allow_cloud_parse": False})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_ready_with_unconfirmed_usage_can_chat_but_blocks_the_next_upload(make_web):
    web = make_web(converter=_Converter(usage_reported=False))
    client = web.client()
    _register(client, "alice@example.com")
    session, _payload, response = _start_import(client)
    web.work()

    status = client.get(f"/api/report-imports/{response.json()['job_id']}").json()
    assert status["status"] == "ready" and status["session_attached"] is True
    assert status["billing_status"] == "pending" and status["confirmed_credits"] == ""
    assert _chat(client, session["id"]).status_code == 202

    _next_session, _next_payload, blocked = _start_import(client, key="import-key-2")
    assert blocked.status_code == 429
    assert blocked.json()["detail"]["code"] == "import_billing_pending"
    assert "chờ xác nhận chi phí" in blocked.json()["detail"]["message"]


def test_import_worker_recovers_interrupted_jobs_and_reports_health(make_web):
    web = make_web()
    client = web.client()
    _register(client, "alice@example.com")
    _session, _payload, response = _start_import(client)
    job = web.service.store.claim_job(response.json()["job_id"])  # worker died mid-parse
    assert job.status == "parsing"

    handler = build_import_handler(web.db, service=web.service)
    assert web.service.store.get_job(job.job_id).status == "queued"
    assert run_worker("import", web.settings, max_iterations=1, import_handler=handler) == 0

    assert web.service.store.get_job(job.job_id).status == "ready"
    assert client.get("/api/health").json()["workers"]["import"]["status"] == "alive"


def test_upload_form_is_pdf_only_with_consent_unticked():
    html = (STATIC / "chat.html").read_text(encoding="utf-8")
    script = (STATIC / "chatbox.js").read_text(encoding="utf-8")
    assert 'accept="application/pdf,.pdf"' in html
    assert '<input type="checkbox" id="uploadConsent" />' in html
    assert "allow_cloud_parse: uploadConsent.checked" in script
    assert "owner" not in script.split("// ---------- report upload ----------")[1].split("composerForm")[0]
