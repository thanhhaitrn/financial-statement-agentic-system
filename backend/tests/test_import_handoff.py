"""Source-bound idempotency, the cross-database import hand-off, the web DB v4
migration, and billing that blocks an owner only on its own unsettled parse."""

import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")
pytest.importorskip("multipart")

from fastapi.testclient import TestClient  # noqa: E402

from agentfinx.acquisition.cafef import CafeFReportDiscoveryProvider  # noqa: E402
from agentfinx.acquisition.download import PdfArtifactValidator  # noqa: E402
from agentfinx.acquisition.service import IdempotencyConflict, ReportAcquisitionService  # noqa: E402
from agentfinx.acquisition.store import AcquisitionStore, ParseAttemptRecorder  # noqa: E402
from agentfinx.core.config.runtime_policy import AcquisitionPolicy  # noqa: E402
from agentfinx.datasets import registry  # noqa: E402
from agentfinx.core.schemas.acquisition import (ConvertedDocument, ConvertedPage,  # noqa: E402
                                 ImportJob, ReportQuery)
from agentfinx.api import db as webdb  # noqa: E402
from agentfinx.api.app import create_app  # noqa: E402
from agentfinx.api.handoff import HandoffRequest, _begin, recover_handoffs  # noqa: E402
from agentfinx.api.imports import make_session_attacher  # noqa: E402
from agentfinx.api.settings import WebSettings  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"
PDF = b"%PDF-1.7\nfixture"
MARKDOWN = "# BCTC\n\nNội dung báo cáo tài chính đầy đủ để ingest."
ROWS = [
    {"Name": "Báo cáo tài chính hợp nhất năm 2025 (đã kiểm toán)",
     "Link": "https://cafef1.mediacdn.vn/a.pdf", "Year": 2025, "Quarter": 5, "Time": "CN/2025"},
    {"Name": "Báo cáo tài chính hợp nhất quý 2 năm 2025",
     "Link": "https://cafef1.mediacdn.vn/b.pdf", "Year": 2025, "Quarter": 2, "Time": "Q2/2025"},
]


def later():
    return datetime.now(timezone.utc) + timedelta(minutes=5)


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
        file_path="data/document.md", company=job.metadata.get("company") or "Công ty",
        owner_id=job.owner_id, dataset_id=f"imp-{job.job_id[:12]}",
    )
    registry.save_dataset(record.model_copy(update={"status": "ready"}))
    return record.dataset_id


class Harness:
    def __init__(self, tmp_path, *, report_discovery=True):
        self.settings = WebSettings(
            secret_key="handoff-secret-" + "k" * 40,
            db_path=tmp_path / "web.sqlite3", registration_mode="open",
            allowed_origins=frozenset({ORIGIN}),
            report_discovery=report_discovery,
            heartbeat_interval_seconds=60,
        )
        self.service = ReportAcquisitionService(
            store=AcquisitionStore(tmp_path / "acquisition" / "acquisition.db"),
            discovery_provider=CafeFReportDiscoveryProvider(
                fetch_json=lambda _url: {"Success": True, "Data": ROWS}
            ),
            converter=_Converter(),
            validator=PdfArtifactValidator(
                max_file_bytes=4096, max_pdf_pages=200, page_counter=lambda _path: 1
            ),
            downloader=SimpleNamespace(),
            policy=AcquisitionPolicy(
                max_file_bytes=4096, monthly_parse_credit_budget=1_000_000,
                trial_successful_imports=10, per_owner_concurrency=5,
            ),
            artifact_dir=tmp_path / "artifacts",
            converted_dir=tmp_path / "converted",
            dataset_builder=_build_dataset,
        )
        self.app = create_app(
            self.settings,
            generation_inspector=lambda _record: "gen-1",
            question_admitter=lambda _owner_id: None,
            import_service=self.service,
        )
        self.db = self.app.state.db
        self.service.dataset_attacher = make_session_attacher(
            self.db, generation_inspector=lambda _record: "gen-1"
        )
        self.client = TestClient(self.app, base_url=ORIGIN)
        response = self.post("/api/register", {"name": "A", "email": "a@example.com",
                                               "password": "correct horse battery"})
        assert response.status_code == 201, response.text
        self.owner = response.json()["user"]["id"]

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers={"Origin": ORIGIN})

    def upload(self):
        response = self.client.post("/api/report-uploads", files={"file": ("r.pdf", PDF, "application/pdf")},
                                    headers={"Origin": ORIGIN})
        assert response.status_code == 201, response.text
        return response.json()["upload_id"]

    def new_session(self):
        return self.post("/api/sessions", {}).json()["id"]

    def session_with_dataset(self):
        with self.db.transaction() as conn:
            return webdb.create_session(conn, owner_id=self.owner, title="Có báo cáo",
                                        dataset_id="ds-existing", dataset_generation="gen-1")["id"]

    def candidates(self, session_id):
        return self.service.discover(
            ReportQuery(owner_id=self.owner, session_id=session_id, ticker="VNM")
        ).candidates

    def all_sessions(self):
        with self.db.read() as conn:
            return {row["id"]: dict(row) for row in conn.execute("SELECT * FROM chat_sessions")}

    def listed(self):
        return {item["id"] for item in self.client.get("/api/sessions").json()}

    def candidate_request(self, source, candidate, key):
        return HandoffRequest(owner_id=self.owner, idempotency_key=key, source_kind="candidate",
                              source_id=candidate.candidate_id, source_session_id=source,
                              allow_cloud_parse=False, new_session_title="t")

    def held_candidate_job(self, handoff, source, candidate, key):
        return self.service.create_candidate_import(
            owner_id=self.owner, session_id=handoff["target_session_id"],
            candidate_id=candidate.candidate_id, idempotency_key=key,
            source_session_id=source, hold=True,
        )


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    return lambda **kwargs: Harness(tmp_path, **kwargs)


# ----------------------------------------------------------- idempotency

def test_an_upload_key_is_bound_to_its_file_consent_and_metadata(harness):
    h = harness()
    body = {"session_id": h.new_session(), "upload_id": h.upload(),
            "idempotency_key": "key-00000001", "metadata": {"company": "A"}}
    first = h.post("/api/report-imports", body)
    assert first.status_code == 202, first.text

    replay = h.post("/api/report-imports", body)  # the uploaded file is gone by now
    assert replay.status_code == 202 and replay.json()["replayed"] is True
    assert replay.json()["job_id"] == first.json()["job_id"]
    for change in ({"upload_id": h.upload()}, {"allow_cloud_parse": True},
                   {"metadata": {"company": "B"}}):
        conflict = h.post("/api/report-imports", {**body, **change})
        assert conflict.status_code == 409, change
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_a_candidate_import_is_bound_to_its_source_session_and_key(harness):
    h = harness()
    source = h.new_session()
    first, second = h.candidates(source)
    body = {"session_id": source, "confirmed_candidate_id": first.candidate_id,
            "idempotency_key": "key-00000002"}
    assert h.post("/api/report-imports", body).status_code == 202
    assert h.post("/api/report-imports",
                  {**body, "confirmed_candidate_id": second.candidate_id}).status_code == 409
    moved = h.post("/api/report-imports", {"session_id": h.new_session(),
                                           "confirmed_candidate_id": second.candidate_id,
                                           "idempotency_key": "key-00000003"})
    assert moved.status_code == 404  # a candidate never moves to another session
    override = h.post("/api/report-imports", {**body, "idempotency_key": "key-00000004",
                                              "metadata": {"company": "Khác"}})
    assert override.status_code == 422


def test_an_expired_candidate_is_gone_but_an_accepted_replay_returns_its_job(harness):
    h = harness()
    source = h.new_session()
    candidate = h.candidates(source)[0]
    body = {"session_id": source, "confirmed_candidate_id": candidate.candidate_id,
            "idempotency_key": "key-00000005"}
    accepted = h.post("/api/report-imports", body).json()["job_id"]

    h.service._now = lambda: datetime.now(timezone.utc) + timedelta(hours=1)
    replay = h.post("/api/report-imports", body)
    assert replay.status_code == 202 and replay.json()["job_id"] == accepted
    expired = h.post("/api/report-imports", {**body, "idempotency_key": "key-00000006"})
    assert expired.status_code == 410 and expired.json()["detail"]["code"] == "candidate_expired"


def test_candidate_imports_need_the_feature_flag(harness):
    h = harness(report_discovery=False)
    source = h.new_session()
    candidate = h.candidates(source)[0]
    response = h.post("/api/report-imports", {"session_id": source, "idempotency_key": "key-00000007",
                                              "confirmed_candidate_id": candidate.candidate_id})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "report_discovery_disabled"


def test_replaying_a_job_recorded_before_identities_is_refused(harness):
    h = harness()
    h.service.store.create_job(
        ImportJob(job_id="legacy", owner_id=h.owner, session_id="s", origin="upload",
                  source_sha256="b" * 64),
        idempotency_key="legacy-key-01",
    )
    with pytest.raises(IdempotencyConflict) as error:
        h.service.create_upload_import(owner_id=h.owner, session_id="s", upload_path="unused.pdf",
                                       idempotency_key="legacy-key-01", source_id="web-upload:x")
    assert error.value.code == "legacy_identity"


def test_acquisition_v1_jobs_get_their_identity_backfilled(tmp_path):
    path = tmp_path / "acquisition.db"
    AcquisitionStore(path)
    payload = ImportJob(job_id="old", owner_id="o", session_id="s", origin="upload",
                        source_sha256="c" * 64, billing_pending=True, billing_status="pending",
                        created_at="2026-01-01T00:00:00+00:00",
                        updated_at="2026-01-02T00:00:00+00:00").model_dump(mode="json")
    for key in ("source_kind", "source_id", "source_session_id", "request_fingerprint",
                "dispatch_hold", "billing_blocking"):
        payload.pop(key)
    import json
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO import_jobs VALUES ('old', 'o', 's', 'failed', 'k', ?, ?, ?)",
                     (payload["created_at"], payload["updated_at"], json.dumps(payload)))
        conn.execute("PRAGMA user_version = 1")

    job = AcquisitionStore(path).get_job("old")

    assert (job.source_kind, job.source_id, job.request_fingerprint) == ("upload", "c" * 64, "")
    assert job.billing_blocking and job.updated_at == "2026-01-02T00:00:00+00:00"


# --------------------------------------------------------------- hand-off

def test_searching_from_a_report_session_creates_a_target_only_at_confirmation(harness):
    h = harness()
    source = h.session_with_dataset()
    before = set(h.all_sessions())
    candidate = h.candidates(source)[0]
    assert set(h.all_sessions()) == before  # searching creates nothing

    response = h.post("/api/report-imports", {"session_id": source, "idempotency_key": "key-00000008",
                                              "confirmed_candidate_id": candidate.candidate_id})
    assert response.status_code == 202, response.text
    target = response.json()["target_session_id"]
    assert target != source and response.json()["created_session"] is True
    assert target in h.listed() and h.all_sessions()[source]["dataset_id"] == "ds-existing"
    job = h.service.store.get_job(response.json()["job_id"])
    assert not job.dispatch_hold and (job.source_session_id, job.session_id) == (source, target)


def test_a_held_job_waits_for_its_session_link(harness):
    h = harness()
    source = h.session_with_dataset()
    candidate = h.candidates(source)[0]
    handoff, _ = _begin(h.db, h.service, h.candidate_request(source, candidate, "key-00000009"))
    job = h.held_candidate_job(handoff, source, candidate, "key-00000009")
    # The API "crashed" here: the web side never recorded the job.
    assert h.service.store.next_queued_job() is None and h.service.store.claim_job(job.job_id) is None
    assert handoff["target_session_id"] not in h.listed()
    assert recover_handoffs(h.db, h.service) == []  # too recent: never races a live request

    assert recover_handoffs(h.db, h.service, now=later()) == [f"{handoff['id']}:linked"]
    assert h.service.store.next_queued_job().job_id == job.job_id
    assert handoff["target_session_id"] in h.listed()


def test_a_crash_before_the_job_removes_the_provisioning_session(harness):
    h = harness()
    source = h.session_with_dataset()
    candidate = h.candidates(source)[0]
    handoff, _ = _begin(h.db, h.service, h.candidate_request(source, candidate, "key-00000010"))

    assert recover_handoffs(h.db, h.service, now=later()) == [f"{handoff['id']}:aborted"]
    assert handoff["target_session_id"] not in h.all_sessions()
    assert h.service.store._all_jobs() == [] and h.service.store.committed_credits() == 0


def test_a_crash_after_linking_releases_the_hold(harness):
    h = harness()
    source = h.session_with_dataset()
    candidate = h.candidates(source)[0]
    handoff, _ = _begin(h.db, h.service, h.candidate_request(source, candidate, "key-00000011"))
    job = h.held_candidate_job(handoff, source, candidate, "key-00000011")
    with h.db.transaction() as conn:
        webdb.set_session_import(conn, handoff["target_session_id"], h.owner, job.job_id)
        webdb.finish_provisioning(conn, handoff["target_session_id"])
        webdb.update_handoff(conn, handoff["id"], state="linked", job_id=job.job_id)

    assert recover_handoffs(h.db, h.service, now=later()) == [f"{job.job_id}:released"]
    assert h.service.store.next_queued_job().job_id == job.job_id


def test_a_replay_finishes_an_interrupted_handoff_without_duplicates(harness):
    h = harness()
    source = h.session_with_dataset()
    candidate = h.candidates(source)[0]
    handoff, _ = _begin(h.db, h.service, h.candidate_request(source, candidate, "key-00000012"))
    job = h.held_candidate_job(handoff, source, candidate, "key-00000012")
    sessions_before = set(h.all_sessions())

    replay = h.post("/api/report-imports", {"session_id": source, "idempotency_key": "key-00000012",
                                            "confirmed_candidate_id": candidate.candidate_id})

    assert replay.status_code == 202, replay.text
    assert replay.json()["job_id"] == job.job_id and replay.json()["replayed"] is True
    assert replay.json()["target_session_id"] == handoff["target_session_id"]
    assert set(h.all_sessions()) == sessions_before
    assert len(h.service.store._all_jobs()) == 1
    assert h.service.store.committed_credits() == job.reserved_credits


def test_a_deleted_session_is_never_recreated(harness):
    h = harness()
    source = h.new_session()  # no report: the import would go into this one
    candidate = h.candidates(source)[0]
    handoff, _ = _begin(h.db, h.service, h.candidate_request(source, candidate, "key-00000013"))
    assert handoff["target_session_id"] == source
    job = h.held_candidate_job(handoff, source, candidate, "key-00000013")
    assert h.client.delete(f"/api/sessions/{source}", headers={"Origin": ORIGIN}).status_code == 200

    assert recover_handoffs(h.db, h.service, now=later()) == [f"{handoff['id']}:aborted"]
    cancelled = h.service.store.get_job(job.job_id)
    assert cancelled.status == "failed" and cancelled.error_code == "session_unavailable"
    assert h.service.store.committed_credits() == 0 and source not in h.all_sessions()


def test_quota_refusal_leaves_no_provisioning_session(harness):
    h = harness()
    h.service.policy = AcquisitionPolicy(max_file_bytes=4096, monthly_parse_credit_budget=1,
                                         trial_successful_imports=10)
    h.service.quota.policy = h.service.policy
    source = h.session_with_dataset()
    candidate = h.candidates(source)[0]
    before = set(h.all_sessions())
    refused = h.post("/api/report-imports", {"session_id": source, "idempotency_key": "key-00000014",
                                             "confirmed_candidate_id": candidate.candidate_id})
    assert refused.status_code == 429
    assert set(h.all_sessions()) == before


# ------------------------------------------------------------------ billing

def test_waiting_on_another_owners_payer_does_not_block_imports(harness):
    h = harness()
    store = h.service.store
    payer = store.create_job(ImportJob(job_id="payer", owner_id="someone-else", session_id="s",
                                       origin="cafef_discovery", source_sha256="a" * 64,
                                       request_fingerprint="f1"), idempotency_key="p")
    reuser = store.create_job(ImportJob(job_id="reuser", owner_id=h.owner, session_id="s",
                                        origin="cafef_discovery", source_sha256="a" * 64,
                                        request_fingerprint="f2"), idempotency_key="r")
    attempt = store.begin_attempt(payer, tier="cost_effective", selection="all")
    store.update_attempt(attempt, state="submitted", remote_job_id="remote-x")
    ParseAttemptRecorder(store, reuser).adopt(tier="cost_effective", selection="all",
                                             remote_job_id="remote-x", remote_status="COMPLETED")

    reuser_fields = h.service._billing_fields(reuser)
    assert reuser_fields["billing_pending"] and not reuser_fields["billing_blocking"]
    assert h.service._billing_fields(payer)["billing_blocking"]
    store.update_job(reuser.model_copy(update={"status": "ready", **reuser_fields}))
    assert store.count_blocking_jobs(h.owner, statuses=set()) == 0
    # The reservation is still held until the payer's usage is known.
    assert store.committed_credits() >= reuser.reserved_credits


# ---------------------------------------------------------------- migration

def _v3_database(path, monkeypatch):
    monkeypatch.setattr(webdb, "SCHEMA_VERSION", 3)
    database = webdb.WebDB(path)
    database.migrate()
    stamp = "2026-01-01T00:00:00+00:00"
    with database.transaction() as conn:
        conn.execute("INSERT INTO users VALUES ('u1', 'A', 'a@example.com', 'h', ?)", (stamp,))
        conn.execute("INSERT INTO chat_sessions (id, owner_id, title, dataset_id, dataset_generation, "
                     "created_at, updated_at) VALUES ('s1', 'u1', 't', 'ds', 'gen-1', ?, ?)", (stamp, stamp))
        conn.execute("INSERT INTO messages (id, session_id, role, content, run_id, created_at) "
                     "VALUES ('m1', 's1', 'user', 'Doanh thu?', 'r1', ?)", (stamp,))
        conn.execute("INSERT INTO chat_runs (id, session_id, owner_id, idempotency_key, question, "
                     "question_message_id, dataset_id, dataset_generation, status, created_at) "
                     "VALUES ('r1', 's1', 'u1', 'key-1', 'Doanh thu?', 'm1', 'ds', 'gen-1', 'queued', ?)",
                     (stamp,))
    monkeypatch.setattr(webdb, "SCHEMA_VERSION", 4)
    return database


def test_a_v3_web_database_upgrades_with_a_backup_and_keeps_its_data(tmp_path, monkeypatch):
    database = _v3_database(tmp_path / "web.sqlite3", monkeypatch)

    database.migrate()
    database.migrate()  # idempotent: no second backup

    [backup] = list(tmp_path.glob("web.sqlite3.backup-v3-*"))
    with sqlite3.connect(backup) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_runs").fetchone()[0] == 1
    with database.read() as conn:
        run = webdb.get_run(conn, "r1")
        assert (run["dataset_id"], run["status"], run["task_kind"]) == ("ds", "queued", "")
        assert webdb.list_messages(conn, "s1")[0]["kind"] == "text"
    with database.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError):  # still one active run per session
            webdb.create_run(conn, session_id="s1", owner_id="u1", idempotency_key="key-2",
                             question="q", question_message_id="m1", dataset_id="ds",
                             dataset_generation="gen-1")
    with database.transaction() as conn:
        s2 = webdb.create_session(conn, owner_id="u1", title="tìm")["id"]
        webdb.create_run(conn, session_id=s2, owner_id="u1", idempotency_key="key-3",
                         question="Tìm báo cáo VNM", question_message_id="m1",
                         dataset_id=None, dataset_generation=None)
    with database.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError):  # dataset and generation go together
            conn.execute("INSERT INTO chat_runs (id, session_id, owner_id, idempotency_key, question, "
                         "question_message_id, dataset_id, status, created_at) VALUES "
                         "('r9', ?, 'u1', 'key-9', 'q', 'm1', 'ds', 'succeeded', 'x')", (s2,))
    with database.transaction() as conn:
        conn.execute("DELETE FROM chat_sessions WHERE id = 's1'")
    with database.read() as conn:
        assert webdb.get_run(conn, "r1") is None  # the session foreign key still cascades


def test_the_upgrade_refuses_while_a_worker_is_alive(tmp_path, monkeypatch):
    database = _v3_database(tmp_path / "web.sqlite3", monkeypatch)
    with database.transaction() as conn:
        webdb.upsert_heartbeat(conn, queue="chat", worker_id="w", pid=1,
                               started_at=webdb.utc_now())

    with pytest.raises(RuntimeError, match="stop the API and workers"):
        database.migrate()
    assert database._current_version() == 3
    assert list(tmp_path.glob("web.sqlite3.backup-*")) == []
