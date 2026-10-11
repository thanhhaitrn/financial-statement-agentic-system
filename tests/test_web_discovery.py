"""Report discovery in the web app: form search, stored cards, owner lock."""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")

from fastapi.testclient import TestClient  # noqa: E402

from acquisition.cafef import CafeFReportDiscoveryProvider  # noqa: E402
from acquisition.service import ReportAcquisitionService  # noqa: E402
from acquisition.store import AcquisitionStore  # noqa: E402
from config.runtime_policy import AcquisitionPolicy  # noqa: E402
from dataset_catalog import registry  # noqa: E402
from dataset_catalog.locks import ProcessSingleton  # noqa: E402
from webapp import db as webdb  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.settings import WebSettings  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"
SYMBOLS = [
    {"ticker": "VNM", "company": "Công ty Cổ phần Sữa Việt Nam", "exchange": "HOSE"},
    {"ticker": "HPG", "company": "Công ty cổ phần Tập đoàn Hòa Phát", "exchange": "HOSE"},
    {"ticker": "HPX", "company": "Công ty Cổ phần Nông nghiệp Hòa Phát", "exchange": "UPCOM"},
]
ROWS = [
    {"Name": "Báo cáo tài chính hợp nhất năm 2025 (đã kiểm toán)",
     "Link": "https://cafef1.mediacdn.vn/a.pdf", "Year": 2025, "Quarter": 5, "Time": "CN/2025"},
    {"Name": "Báo cáo tài chính hợp nhất quý 2 năm 2025 <img src=x onerror=alert(1)>",
     "Link": "https://cafef1.mediacdn.vn/b.pdf", "Year": 2025, "Quarter": 2, "Time": "Q2/2025"},
    {"Name": "BCTC hợp nhất 2025 bổ sung", "Link": "https://cafef1.mediacdn.vn/c.pdf"},
]


class Harness:
    def __init__(self, tmp_path, *, report_discovery=True, payload=None, catalog=True):
        self.calls = []
        catalog_path = tmp_path / "symbols.json"
        if catalog:
            catalog_path.write_text(json.dumps({
                "retrieved_at": datetime.now(timezone.utc).isoformat(), "symbols": SYMBOLS,
            }), encoding="utf-8")

        def fetch(url):
            self.calls.append(url)
            return payload if payload is not None else {"Success": True, "Data": ROWS}

        self.service = ReportAcquisitionService(
            store=AcquisitionStore(tmp_path / "acquisition.db"),
            discovery_provider=CafeFReportDiscoveryProvider(fetch_json=fetch),
            converter=SimpleNamespace(converter_identity="fake"),
            validator=SimpleNamespace(),
            downloader=SimpleNamespace(),
            policy=AcquisitionPolicy(monthly_parse_credit_budget=1_000_000),
            artifact_dir=tmp_path / "artifacts",
            converted_dir=tmp_path / "converted",
            symbol_catalog_path=catalog_path,
        )
        settings = WebSettings(secret_key="discovery-secret-" + "d" * 40, db_path=tmp_path / "web.sqlite3",
                               registration_mode="open", allowed_origins=frozenset({ORIGIN}),
                               report_discovery=report_discovery)
        self.app = create_app(settings, generation_inspector=lambda _r: "gen-1",
                              question_admitter=lambda _o: None, import_service=self.service)
        self.db = self.app.state.db
        self.client = TestClient(self.app, base_url=ORIGIN)
        response = self.post("/api/register", {"name": "A", "email": "a@example.com",
                                               "password": "correct horse battery"})
        self.owner = response.json()["user"]["id"]

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers={"Origin": ORIGIN})

    def search(self, key="search-00000001", session_id=None, **search):
        body = {"idempotency_key": key, "search": search}
        if session_id:
            body["session_id"] = session_id
        return self.post("/api/report-discoveries", body)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    return lambda **kwargs: Harness(tmp_path, **kwargs)


def test_discovery_is_off_unless_enabled(harness):
    h = harness(report_discovery=False)
    assert h.client.get("/api/me").json()["features"]["report_discovery"] is False
    response = h.search(subject="VNM")
    assert response.status_code == 404 and h.calls == []


def test_a_form_search_from_the_start_screen_stores_a_reloadable_card(harness):
    h = harness()
    response = h.search(subject="VNM", fiscal_year=2025, period="annual")
    assert response.status_code == 200, response.text
    body = response.json()
    session_id = body["session_id"]
    assert body["request_message"]["kind"] == "report_search_request"
    result = body["result_message"]
    assert result["kind"] == "report_candidates"
    payload = result["payload"]
    assert payload["search"]["company"] == "Công ty Cổ phần Sữa Việt Nam"
    assert payload["search"]["exchange"] == "HOSE"
    assert [c["title"] for c in payload["candidates"]][0] == ROWS[0]["Name"]
    assert "owner_id" not in json.dumps(body) and "Type=1" in h.calls[0]

    history = h.client.get(f"/api/sessions/{session_id}/messages").json()
    assert [m["kind"] for m in history] == ["report_search_request", "report_candidates"]
    assert history[1]["payload"] == payload

    replay = h.search(subject="VNM", fiscal_year=2025, period="annual")
    assert replay.json()["replayed"] is True and replay.json()["result_message"]["id"] == result["id"]
    assert len(h.calls) == 1  # a replay never searches again
    changed = h.search(subject="VNM", fiscal_year=2024, period="annual")
    assert changed.status_code == 409

    # The candidate is bound to this session and can be confirmed from it.
    candidate = h.service.get_candidate(payload["candidates"][0]["candidate_id"],
                                        owner_id=h.owner, session_id=session_id)
    assert candidate.title == ROWS[0]["Name"]


def test_untrusted_titles_and_unverified_periods_are_carried_as_data(harness):
    h = harness()
    payload = h.search(subject="VNM").json()["result_message"]["payload"]
    by_title = {c["title"]: c for c in payload["candidates"]}
    assert "<img src=x onerror=alert(1)>" in next(t for t in by_title if "quý 2" in t)
    unverified = by_title["BCTC hợp nhất 2025 bổ sung"]
    assert unverified["period_verified"] is False
    assert unverified["verification_note"] == "missing_structured_period"
    assert unverified["fiscal_year"] is None


def test_an_ambiguous_name_asks_the_user_to_choose(harness):
    h = harness()
    card = h.search(subject="Hòa Phát").json()["result_message"]
    assert card["kind"] == "report_search_clarification" and h.calls == []
    assert [c["ticker"] for c in card["payload"]["clarification"]["choices"]] == ["HPG", "HPX"]

    chosen = h.search(key="search-00000002", subject="Hòa Phát", ticker="HPG").json()
    assert chosen["result_message"]["kind"] == "report_candidates"
    assert chosen["result_message"]["payload"]["search"]["ticker"] == "HPG"


def test_without_a_catalog_a_name_asks_for_the_ticker(harness):
    h = harness(catalog=False)
    card = h.search(subject="Sữa Việt Nam").json()["result_message"]
    assert card["payload"]["clarification"]["reason"] == "catalog_unavailable" and h.calls == []
    result = h.search(key="search-00000003", subject="VNM").json()["result_message"]["payload"]
    assert result["search"]["exchange"] == "" and "catalog_unavailable" in result["warnings"]


def test_a_provider_failure_is_an_error_card_not_no_results(harness):
    failing = harness(payload={"Success": False, "Data": []})
    card = failing.search(subject="VNM").json()["result_message"]
    assert card["kind"] == "report_search_error"
    assert card["payload"]["error"]["code"] == "provider_rejected"
    assert "không có nghĩa là doanh nghiệp không có báo cáo" in card["payload"]["error"]["message"]


def test_a_valid_empty_answer_is_no_results(harness):
    h = harness(payload={"Success": True, "Data": []})
    payload = h.search(subject="VNM").json()["result_message"]["payload"]
    assert payload["status"] == "no_results" and payload["candidates"] == []


def test_one_discovery_per_owner_at_a_time(harness):
    h = harness()
    held = ProcessSingleton("discovery-" + hashlib.sha256(h.owner.encode()).hexdigest()[:16])
    assert held.try_acquire()
    try:
        response = h.search(subject="VNM")
    finally:
        held.release()
    assert response.status_code == 429 and response.json()["detail"]["code"] == "discovery_in_progress"
    assert h.calls == []


def test_a_read_only_session_cannot_search(harness):
    h = harness()
    session_id = h.post("/api/sessions", {}).json()["id"]
    with h.db.transaction() as conn:
        webdb.mark_session_read_only(conn, session_id, "dataset_changed")
    assert h.search(subject="VNM", session_id=session_id).status_code == 409


def test_period_numbers_must_match_the_period_type(harness):
    h = harness()
    assert h.search(subject="VNM", period="annual", fiscal_quarter=2).status_code == 422
    assert h.search(key="search-00000004", subject="VNM", owner_id="x").status_code == 422
