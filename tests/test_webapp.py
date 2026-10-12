"""Local web app: auth, owner scoping, chat runs, worker, static frontend."""

from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")

from fastapi.testclient import TestClient  # noqa: E402

from acquisition.quota import QuotaExceeded  # noqa: E402
from dataset_catalog import registry  # noqa: E402
from dataset_catalog.activation import DatasetNotReady  # noqa: E402
from dataset_catalog.locks import ProcessSingleton  # noqa: E402
from webapp import db as webdb  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.chat_runner import SAFE_MESSAGES, ChatDependencies  # noqa: E402
from webapp.settings import WebConfigError, WebSettings, load_settings  # noqa: E402
from webapp.worker import (WorkerAlreadyRunning, recover_chat_queue,  # noqa: E402
                           run_chat_once, run_worker)

ORIGIN = "http://127.0.0.1:8000"
SECRET = "test-secret-" + "x" * 40
WEB = Path(__file__).resolve().parents[1] / "web"


def _configure_registry_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))


def _save_dataset(company, owner=""):
    return registry.save_dataset(
        registry.build_dataset_record(
            file_path="data/document.md",
            company=company,
            fiscal_year=2025,
            owner_id=owner,
        ).model_copy(update={"status": "ready"})
    )


class Web:
    """One app + client with controllable generations and quota."""

    def __init__(self, tmp_path, *, shared=frozenset()):
        self.settings = WebSettings(
            secret_key=SECRET,
            db_path=tmp_path / "web.sqlite3", registration_mode="open",
            allowed_origins=frozenset({ORIGIN}),
            shared_dataset_ids=frozenset(shared),
            worker_poll_seconds=0.01,
            heartbeat_interval_seconds=60,
        )
        self.generations: dict[str, str] = {}
        self.admitted: list[str] = []
        self.quota_error = None
        self.app = create_app(
            self.settings,
            generation_inspector=self._inspect,
            question_admitter=self._admit,
        )
        self.db = self.app.state.db

    def _inspect(self, record):
        generation = self.generations.get(record.dataset_id)
        if generation is None:
            raise DatasetNotReady("dataset_not_ready", "Báo cáo chưa được build xong.")
        return generation

    def _admit(self, owner_id):
        if self.quota_error is not None:
            raise self.quota_error
        self.admitted.append(owner_id)

    def client(self):
        return TestClient(self.app, base_url=ORIGIN)


def post(client, path, payload=None, *, origin=ORIGIN):
    headers = {"Origin": origin} if origin else {}
    return client.post(path, json=payload or {}, headers=headers)


def register(client, name, email, password="correct horse battery"):
    response = post(client, "/api/register", {"name": name, "email": email, "password": password})
    assert response.status_code == 201, response.text
    return response.json()["user"]


@pytest.fixture
def web(monkeypatch, tmp_path):
    _configure_registry_paths(monkeypatch, tmp_path)
    return Web(tmp_path)


# ---------------------------------------------------------------------------
# Settings and auth
# ---------------------------------------------------------------------------


def test_settings_have_no_default_secret_and_bind_locally():
    with pytest.raises(WebConfigError):
        load_settings({})
    with pytest.raises(WebConfigError):
        load_settings({"FINX_SECRET_KEY": "too-short"})
    with pytest.raises(WebConfigError):
        load_settings({"FINX_SECRET_KEY": SECRET, "FINX_SHARED_DATASET_IDS": "../bad"})

    settings = load_settings({"FINX_SECRET_KEY": SECRET, "FINX_WEB_DB": "/tmp/x.sqlite3"})
    assert settings.host == "127.0.0.1"
    assert settings.shared_dataset_ids == frozenset()
    # The browser's origin is the Next.js UI, not the API port.
    assert settings.allowed_origins == {"http://127.0.0.1:3000", "http://localhost:3000"}


def test_health_reports_actual_worker_context_and_api_mismatch(web):
    from schemas.chat_context import CONTEXT_CONTRACT_VERSION

    # A dependency override represents a worker started with a different env.
    deps = ChatDependencies(shared_dataset_ids=frozenset(), context_enabled=True)
    assert run_worker("chat", web.settings, max_iterations=1, deps=deps) == 0
    health = web.client().get("/api/health").json()
    assert health["config"]["chat_context"] is False
    assert health["workers"]["chat"]["config"]["chat_context"] is True
    assert health["context_configuration_in_sync"] is False
    with web.db.transaction() as conn:
        webdb.upsert_heartbeat(conn, queue="chat", worker_id="restarted", pid=1, started_at=webdb.utc_now(),
            runtime_config={"chat_context": False, "context_contract_version": CONTEXT_CONTRACT_VERSION,
                            "secret": "DO_NOT_EXPOSE"})
    health = web.client().get("/api/health").json()
    assert health["context_configuration_in_sync"] is True
    assert "DO_NOT_EXPOSE" not in str(health)


def test_register_sets_an_httponly_lax_cookie_and_never_returns_a_token(web):
    client = web.client()
    response = post(
        client, "/api/register", {"name": "Alice", "email": "Alice@Example.com", "password": "correct horse"}
    )

    assert response.status_code == 201
    assert "token" not in response.text
    cookie = response.headers["set-cookie"].lower()
    assert "finx_session=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert client.get("/api/me").json()["user"]["email"] == "alice@example.com"

    with web.db.read() as conn:
        stored = conn.execute("SELECT password_hash FROM users").fetchone()["password_hash"]
    assert stored.startswith("$argon2")


def test_login_logout_and_invalid_credentials(web):
    client = web.client()
    register(client, "Alice", "alice@example.com")
    assert post(client, "/api/logout").status_code == 200
    assert client.get("/api/me").status_code == 401

    wrong = post(client, "/api/login", {"email": "alice@example.com", "password": "nope-nope"})
    unknown = post(client, "/api/login", {"email": "ghost@example.com", "password": "nope-nope"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()  # no account enumeration via messages

    ok = post(client, "/api/login", {"email": "alice@example.com", "password": "correct horse battery"})
    assert ok.status_code == 200
    assert client.get("/api/me").status_code == 200


def test_tampered_or_foreign_tokens_are_rejected(web):
    import jwt as pyjwt

    client = web.client()
    user = register(client, "Alice", "alice@example.com")
    forged = pyjwt.encode({"sub": user["id"], "typ": "session", "iat": 1, "exp": 4102444800}, "other-secret", algorithm="HS256")
    client.cookies.set("finx_session", forged)
    assert client.get("/api/me").status_code == 401


def test_state_changing_requests_need_an_allowed_origin(web):
    client = web.client()
    payload = {"name": "A", "email": "a@example.com", "password": "correct horse"}

    missing = post(client, "/api/register", payload, origin=None)
    foreign = post(client, "/api/register", payload, origin="https://evil.example")
    assert missing.status_code == foreign.status_code == 403
    assert missing.json()["detail"]["code"] == "origin_rejected"

    via_referer = client.post("/api/register", json=payload, headers={"Referer": f"{ORIGIN}/login"})
    assert via_referer.status_code == 201


# ---------------------------------------------------------------------------
# Datasets and owner scoping
# ---------------------------------------------------------------------------


def test_dataset_list_is_owner_scoped_and_hides_internal_paths(monkeypatch, tmp_path):
    _configure_registry_paths(monkeypatch, tmp_path)
    boot = Web(tmp_path)
    alice_client, bob_client = boot.client(), boot.client()
    alice = register(alice_client, "Alice", "alice@example.com")
    bob = register(bob_client, "Bob", "bob@example.com")

    mine = _save_dataset("Cong ty Alice", owner=alice["id"])
    _save_dataset("Cong ty Bob", owner=bob["id"])
    sample = _save_dataset("Cong ty Mau")
    _save_dataset("Cong ty An")  # ownerless but not allowlisted

    web = Web(tmp_path, shared={sample.dataset_id})
    client = web.client()
    post(client, "/api/login", {"email": "alice@example.com", "password": "correct horse battery"})
    items = client.get("/api/datasets").json()

    assert {(item["id"], item["access"]) for item in items} == {
        (mine.dataset_id, "owned"),
        (sample.dataset_id, "shared"),
    }
    for item in items:
        assert set(item) == {
            "id", "label", "company", "ticker", "fiscal_year", "fiscal_quarter",
            "scope", "audit_status", "ready", "access",
        }
        assert "sqlite" not in str(item) and "financial_statement__" not in str(item)


def _alice_session(web):
    client = web.client()
    alice = register(client, "Alice", "alice@example.com")
    dataset = _save_dataset("Cong ty Alice", owner=alice["id"])
    web.generations[dataset.dataset_id] = "gen-1"
    response = post(client, "/api/sessions", {"dataset_id": dataset.dataset_id})
    assert response.status_code == 201, response.text
    return client, alice, dataset, response.json()


def test_sessions_messages_and_runs_are_invisible_to_other_users(web):
    client, _alice, _dataset, session = _alice_session(web)
    run = post(client, "/api/chat", {"session_id": session["id"], "message": "Tổng tài sản?", "idempotency_key": "key-00000001"}).json()

    intruder = web.client()
    register(intruder, "Bob", "bob@example.com")
    assert intruder.get(f"/api/sessions/{session['id']}/messages").status_code == 404
    assert intruder.get(f"/api/chat-runs/{run['run_id']}").status_code == 404
    assert intruder.delete(f"/api/sessions/{session['id']}", headers={"Origin": ORIGIN}).status_code == 404
    assert post(intruder, "/api/chat", {"session_id": session["id"], "message": "x", "idempotency_key": "key-00000002"}).status_code == 404
    assert post(intruder, "/api/sessions", {"dataset_id": _dataset.dataset_id}).status_code == 404
    assert intruder.get("/api/sessions").json() == []


def test_only_the_owner_can_rename_a_session(web):
    client, _alice, _dataset, session = _alice_session(web)
    path = f"/api/sessions/{session['id']}"

    renamed = client.patch(path, json={"title": "  Vốn   chủ sở hữu  "}, headers={"Origin": ORIGIN})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["title"] == "Vốn chủ sở hữu"
    assert client.get("/api/sessions").json()[0]["title"] == "Vốn chủ sở hữu"
    assert client.patch(path, json={"title": "   "}, headers={"Origin": ORIGIN}).status_code == 422
    assert client.patch(path, json={"title": "x"}).status_code == 403  # no Origin

    intruder = web.client()
    register(intruder, "Bob", "bob@example.com")
    assert intruder.patch(path, json={"title": "Của Bob"}, headers={"Origin": ORIGIN}).status_code == 404
    assert client.get("/api/sessions").json()[0]["title"] == "Vốn chủ sở hữu"


def test_session_requires_a_ready_dataset(web):
    client = web.client()
    alice = register(client, "Alice", "alice@example.com")
    dataset = _save_dataset("Cong ty Alice", owner=alice["id"])
    response = post(client, "/api/sessions", {"dataset_id": dataset.dataset_id})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "dataset_not_ready"


# ---------------------------------------------------------------------------
# Chat submission
# ---------------------------------------------------------------------------


def test_chat_is_idempotent_admits_quota_once_and_allows_one_active_run(web):
    client, alice, _dataset, session = _alice_session(web)
    body = {"session_id": session["id"], "message": "Tổng tài sản cuối kỳ?", "idempotency_key": "key-aaaaaaaa"}

    first = post(client, "/api/chat", body)
    again = post(client, "/api/chat", body)
    assert first.status_code == again.status_code == 202
    assert first.json()["status"] == "queued" and first.json()["replayed"] is False
    assert again.json()["run_id"] == first.json()["run_id"] and again.json()["replayed"] is True

    messages = client.get(f"/api/sessions/{session['id']}/messages").json()
    assert [m["role"] for m in messages] == ["user"]
    assert web.admitted == [alice["id"]]

    other = post(client, "/api/chat", {**body, "idempotency_key": "key-bbbbbbbb"})
    assert other.status_code == 409
    assert other.json()["detail"]["code"] == "run_in_progress"


def test_quota_rejection_creates_no_message_and_no_run(web):
    client, _alice, _dataset, session = _alice_session(web)
    web.quota_error = QuotaExceeded("trial_question_limit", "limit")

    response = post(client, "/api/chat", {"session_id": session["id"], "message": "Hỏi", "idempotency_key": "key-cccccccc"})

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "trial_question_limit"
    assert client.get(f"/api/sessions/{session['id']}/messages").json() == []


def test_rebuilt_dataset_makes_the_session_read_only(web):
    client, _alice, dataset, session = _alice_session(web)
    web.generations[dataset.dataset_id] = "gen-2"  # rebuilt after the session was pinned

    response = post(client, "/api/chat", {"session_id": session["id"], "message": "Hỏi", "idempotency_key": "key-dddddddd"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "dataset_changed"

    listed = next(s for s in client.get("/api/sessions").json() if s["id"] == session["id"])
    assert listed["read_only"] is True and listed["read_only_message"]

    blocked = post(client, "/api/chat", {"session_id": session["id"], "message": "Hỏi", "idempotency_key": "key-eeeeeeee"})
    assert blocked.json()["detail"]["code"] == "session_read_only"


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def _deps(web, *, generation="gen-1", errors=(), answer="**Tổng tài sản**: 45.952.496.972.636 VND", calls=None):
    calls = calls if calls is not None else []

    def execute(dataset, collection, question, *, owner_id, admission_quota):
        admission_quota.admit_question(owner_id)  # must be a no-op: already admitted by the API
        calls.append((dataset.dataset_id, question, owner_id))
        return {"final": True}

    return ChatDependencies(
        shared_dataset_ids=web.settings.shared_dataset_ids,
        activate=lambda dataset: SimpleNamespace(generation=generation, collection=object()),
        execute=execute,
        collect_errors=lambda _state: list(errors),
        format_answer=lambda _state: answer,
    )


def _submit(client, session, key="key-ffffffff", message="Tổng tài sản cuối kỳ?"):
    response = post(client, "/api/chat", {"session_id": session["id"], "message": message, "idempotency_key": key})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def test_worker_answers_a_queued_run_through_the_real_pipeline_contract(web):
    client, alice, dataset, session = _alice_session(web)
    run_id = _submit(client, session)
    calls = []

    assert run_chat_once(web.db, "worker-test", _deps(web, calls=calls)) is True

    run = client.get(f"/api/chat-runs/{run_id}").json()
    assert run["status"] == "succeeded"
    assert run["answer"]["content"] == "**Tổng tài sản**: 45.952.496.972.636 VND"
    assert calls == [(dataset.dataset_id, "Tổng tài sản cuối kỳ?", alice["id"])]
    assert web.admitted == [alice["id"]]  # the worker never admitted quota again
    roles = [m["role"] for m in client.get(f"/api/sessions/{session['id']}/messages").json()]
    assert roles == ["user", "assistant"]


def test_worker_hides_pipeline_errors_and_never_invents_an_answer(web):
    client, _alice, _dataset, session = _alice_session(web)
    run_id = _submit(client, session)
    leaked = "synth:error: you (someone) have reached your session usage limit (status code: 429)"

    run_chat_once(web.db, "worker-test", _deps(web, errors=[leaked], answer="Lỗi khi chạy synth: 429"))

    run = client.get(f"/api/chat-runs/{run_id}").json()
    assert run["status"] == "failed"
    assert run["error_message"] == SAFE_MESSAGES["pipeline_error"]
    # Random hex IDs can contain "429"; check only the user-visible fields.
    visible = str({key: value for key, value in run.items() if key not in {"run_id", "session_id"}})
    assert "429" not in visible and "usage limit" not in visible
    assert [m["role"] for m in client.get(f"/api/sessions/{session['id']}/messages").json()] == ["user"]


def test_worker_rechecks_the_generation_under_the_dataset_lock(web):
    client, _alice, _dataset, session = _alice_session(web)
    run_id = _submit(client, session)
    calls = []

    run_chat_once(web.db, "worker-test", _deps(web, generation="gen-9", calls=calls))

    run = client.get(f"/api/chat-runs/{run_id}").json()
    assert run["status"] == "failed" and run["error_code"] == "dataset_changed"
    assert calls == []  # the model was never asked against a rebuilt report
    listed = next(s for s in client.get("/api/sessions").json() if s["id"] == session["id"])
    assert listed["read_only"] is True


def test_restart_interrupts_running_runs_without_rerunning_them(web):
    client, _alice, _dataset, session = _alice_session(web)
    run_id = _submit(client, session)
    with web.db.transaction() as conn:
        assert webdb.claim_next_run(conn, "dead-worker") == run_id

    assert recover_chat_queue(web.db) == 1

    run = client.get(f"/api/chat-runs/{run_id}").json()
    assert run["status"] == "interrupted"
    assert run["error_message"] == SAFE_MESSAGES["worker_restarted"]
    # The session can take a new question again.
    assert post(client, "/api/chat", {"session_id": session["id"], "message": "Lại", "idempotency_key": "key-gggggggg"}).status_code == 202


def test_one_worker_per_queue_and_health_reports_it(web):
    client, _alice, _dataset, session = _alice_session(web)
    _submit(client, session)

    held = ProcessSingleton("worker-chat")
    assert held.try_acquire()
    try:
        with pytest.raises(WorkerAlreadyRunning):
            run_worker("chat", web.settings, max_iterations=1, deps=_deps(web))
    finally:
        held.release()

    assert run_worker("chat", web.settings, max_iterations=1, deps=_deps(web)) == 0
    health = client.get("/api/health").json()
    assert health["workers"]["chat"]["status"] == "alive"
    assert health["workers"]["import"]["status"] == "missing"
    assert "secret" not in str(health).lower()


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------


def test_the_api_serves_no_pages_or_files(web):
    client = web.client()
    for path in ("/", "/chat", "/login", "/chatbox.js", "/vendor/purify.min.js", "/app.py", "/.env",
                 "/registry.json", "/web.sqlite3"):
        assert client.get(path).status_code == 404, path


def test_the_ui_keeps_tokens_out_of_scripts_and_renders_no_raw_html():
    sources = {path.relative_to(WEB).as_posix(): path.read_text(encoding="utf-8")
               for folder in ("app", "components", "lib") for path in (WEB / folder).rglob("*.ts*")}
    assert sources
    for name, source in sources.items():
        assert "localStorage" not in source and "sessionStorage" not in source, name
        assert "Authorization" not in source, name
        assert "dangerouslySetInnerHTML" not in source, name
    markdown = sources["components/chat/Markdown.tsx"]
    assert "skipHtml" in markdown and '"img"' in markdown and "rehype-raw" not in (WEB / "package.json").read_text()
