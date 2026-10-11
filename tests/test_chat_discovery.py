"""Report discovery in chat: the web planner classifies first, only QA needs a
dataset, the QA path plans once, and the user can override a misclassification."""

import hashlib
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")

from fastapi.testclient import TestClient  # noqa: E402

import agents.planner_runner as planner_runner  # noqa: E402
from acquisition.cafef import CafeFReportDiscoveryProvider  # noqa: E402
from acquisition.service import ReportAcquisitionService  # noqa: E402
from acquisition.store import AcquisitionStore  # noqa: E402
from agents.profiles import AGENT_PROFILES  # noqa: E402
from config.runtime_policy import AcquisitionPolicy  # noqa: E402
from dataset_catalog import registry  # noqa: E402
from dataset_catalog.locks import ProcessSingleton  # noqa: E402
from schemas.agent_outputs import PlannerEvidencePlan  # noqa: E402
from schemas.report_search import WebPlannerPlan  # noqa: E402
from webapp import db as webdb  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.chat_runner import ChatDependencies  # noqa: E402
from webapp.discovery import run_discovery  # noqa: E402
from webapp.settings import WebSettings  # noqa: E402
from webapp.worker import run_chat_once  # noqa: E402

ORIGIN = "http://127.0.0.1:8000"
ROWS = [{"Name": "Báo cáo tài chính hợp nhất năm 2025 (đã kiểm toán)",
         "Link": "https://cafef1.mediacdn.vn/a.pdf", "Year": 2025, "Quarter": 5, "Time": "CN/2025"}]


def _configure_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(registry, "DATASETS_DIR", tmp_path / "dataset_store")
    monkeypatch.setattr(registry, "REGISTRY_PATH", registry.DATASETS_DIR / "registry.json")
    monkeypatch.setattr(registry, "MANIFESTS_DIR", registry.DATASETS_DIR / "manifests")
    monkeypatch.setattr(registry, "RAW_TABLES_DIR", registry.DATASETS_DIR / "raw_tables")
    monkeypatch.setattr(registry, "SQLITE_DIR", registry.DATASETS_DIR / "sqlite")


class Harness:
    def __init__(self, tmp_path, *, report_discovery=True):
        self.shared = registry.save_dataset(registry.build_dataset_record(
            file_path="data/document.md", company="Công ty Mẫu", fiscal_year=2025,
        ).model_copy(update={"status": "ready"}))
        catalog = tmp_path / "symbols.json"
        catalog.write_text(json.dumps({"retrieved_at": datetime.now(timezone.utc).isoformat(), "symbols": [
            {"ticker": "VNM", "company": "Công ty Cổ phần Sữa Việt Nam", "exchange": "HOSE"}]}),
            encoding="utf-8")
        self.provider_calls = []

        def fetch(url):
            self.provider_calls.append(url)
            return {"Success": True, "Data": ROWS}

        self.service = ReportAcquisitionService(
            store=AcquisitionStore(tmp_path / "acquisition.db"),
            discovery_provider=CafeFReportDiscoveryProvider(fetch_json=fetch),
            converter=SimpleNamespace(converter_identity="fake"), validator=SimpleNamespace(),
            downloader=SimpleNamespace(), policy=AcquisitionPolicy(),
            artifact_dir=tmp_path / "artifacts", converted_dir=tmp_path / "converted",
            symbol_catalog_path=catalog,
        )
        self.settings = WebSettings(
            secret_key="chat-discovery-" + "c" * 40, db_path=tmp_path / "web.sqlite3",
            registration_mode="open",
            allowed_origins=frozenset({ORIGIN}), shared_dataset_ids=frozenset({self.shared.dataset_id}),
            report_discovery=report_discovery,
        )
        self.admitted, self.usage = [], []
        self.app = create_app(
            self.settings, generation_inspector=lambda _r: "gen-1",
            question_admitter=self.admitted.append, import_service=self.service,
            usage_recorder=lambda owner, kind: self.usage.append(kind),
        )
        self.db = self.app.state.db
        self.client = TestClient(self.app, base_url=ORIGIN)
        self.owner = self.post("/api/register", {"name": "A", "email": "a@example.com",
                                                 "password": "correct horse battery"}).json()["user"]["id"]
        self.plans = []
        self.executed = []
        self.activated = []

    def post(self, path, payload):
        return self.client.post(path, json=payload, headers={"Origin": ORIGIN})

    def session(self, *, with_report):
        body = {"dataset_id": self.shared.dataset_id} if with_report else {}
        return self.post("/api/sessions", body).json()["id"]

    def ask(self, session_id, message, key="chat-key-00000001"):
        response = self.post("/api/chat", {"session_id": session_id, "message": message,
                                           "idempotency_key": key})
        assert response.status_code == 202, response.text
        return response.json()["run_id"]

    def deps(self, plan=None, *, planner_must_not_run=False, discovery_enabled=True):
        def plan_task(state):
            if planner_must_not_run:
                raise AssertionError("the web planner must not run here")
            self.plans.append(state["user_query"])
            return dict(plan)

        def execute(dataset, collection, question, **kwargs):
            self.executed.append(kwargs)
            return {}

        def activate(dataset):
            self.activated.append(dataset.dataset_id)
            return SimpleNamespace(generation="gen-1", collection=object())

        return ChatDependencies(
            shared_dataset_ids=self.settings.shared_dataset_ids,
            activate=activate, execute=execute,
            collect_errors=lambda _s: [], format_answer=lambda _s: "Trả lời trên báo cáo",
            discovery_enabled=discovery_enabled, plan_task=plan_task,
            discover=lambda search, **kwargs: run_discovery(self.service, search, **kwargs),
        )

    def run(self, run_id):
        return self.client.get(f"/api/chat-runs/{run_id}").json()


DISCOVER_VNM = {"task_kind": "report_discovery", "planner_plan": {}, "trace": [],
                "report_search": {"subject": "VNM", "fiscal_year": 2025, "period": "annual"}}
QA_PLAN = {"task_kind": "report_qa", "planner_plan": {"difficulty_level": "easy"},
           "trace": [{"event": "planner:done"}], "last_agent": "agent_planner"}


@pytest.fixture
def harness(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    return lambda **kwargs: Harness(tmp_path, **kwargs)


# ------------------------------------------------------------------ planner

def _fake_invoke(plan, calls):
    def invoke(_template, payload, *, structured_schema=None, plain_payload_factory=None, include_raw=True):
        calls.append({"schema": structured_schema, "instruction": payload["system_instruction"]})
        return {"parsed": plan, "raw": None, "mode": "structured", "parsing_error": None}
    return invoke


def test_the_qa_planner_prompt_and_schema_are_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(planner_runner, "invoke_prompt", _fake_invoke(PlannerEvidencePlan(), calls))
    planner_runner.run_planner({"user_query": "Doanh thu?", "trace": []})
    assert calls[0]["schema"] is PlannerEvidencePlan
    assert calls[0]["instruction"] == AGENT_PROFILES["agent_planner"]["system_instruction"]
    assert "task_kind" not in calls[0]["instruction"]
    assert "task_kind" not in PlannerEvidencePlan.model_json_schema()["properties"]


def test_a_discovery_plan_fills_in_nothing_from_the_open_dataset(monkeypatch, harness):
    h = harness()
    calls = []
    plan = WebPlannerPlan(task_kind="report_discovery",
                          report_search={"subject": "Vinamilk", "ticker_guess": "VNM", "period": "năm"})
    monkeypatch.setattr(planner_runner, "invoke_prompt", _fake_invoke(plan, calls))
    updates = planner_runner.run_web_planner({
        "user_query": "Tìm báo cáo của công ty Vinamilk năm trước",
        "dataset_id": h.shared.dataset_id, "trace": [],
    })
    assert calls[0]["schema"] is WebPlannerPlan and "task_kind" in calls[0]["instruction"]
    assert updates["task_kind"] == "report_discovery" and updates["planner_plan"] == {}
    assert updates["report_search"]["subject"] == "Vinamilk"
    assert updates["report_search"]["period"] == "annual" and updates["report_search"]["fiscal_year"] is None


def test_a_qa_plan_from_the_web_planner_equals_the_qa_planners(monkeypatch):
    base = PlannerEvidencePlan(difficulty_level="medium", company="", time_hint="",
                               analysis_axes=[{"axis": "agent_profitability", "objective": "Tính ROE"}])
    state = {"user_query": "Tính ROE năm 2025", "trace": []}
    monkeypatch.setattr(planner_runner, "invoke_prompt", _fake_invoke(base, []))
    qa = planner_runner.run_planner(dict(state, trace=[]))
    monkeypatch.setattr(planner_runner, "invoke_prompt",
                        _fake_invoke(WebPlannerPlan(**base.model_dump(), task_kind="report_qa"), []))
    web = planner_runner.run_web_planner(dict(state, trace=[]))
    assert web["planner_plan"] == qa["planner_plan"] and web["task_kind"] == "report_qa"


def test_the_graph_can_start_at_the_router():
    from graph.workflow import build_graph

    assert build_graph(entry="agent_router") is not None
    with pytest.raises(ValueError):
        build_graph(entry="agent_synth")


# --------------------------------------------------------------------- chat

def test_with_the_flag_off_chat_still_needs_a_report_and_skips_the_planner(harness):
    h = harness(report_discovery=False)
    no_report = h.session(with_report=False)
    refused = h.post("/api/chat", {"session_id": no_report, "message": "Tìm BCTC VNM",
                                   "idempotency_key": "chat-key-00000009"})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "session_without_dataset"

    h.ask(h.session(with_report=True), "Doanh thu?")
    run_chat_once(h.db, "w", h.deps(planner_must_not_run=True, discovery_enabled=False))
    assert h.executed == [{"owner_id": h.owner, "admission_quota": h.executed[0]["admission_quota"]}]


def test_finding_a_report_needs_no_dataset_and_touches_no_index(harness):
    h = harness()
    session_id = h.session(with_report=False)
    run_id = h.ask(session_id, "Tìm BCTC VNM năm 2025")

    run_chat_once(h.db, "w", h.deps(DISCOVER_VNM))

    run = h.run(run_id)
    assert run["status"] == "succeeded" and run["result_kind"] == "report_candidates"
    assert run["task_kind"] == "report_discovery"
    payload = run["answer"]["payload"]
    assert payload["origin"] == "chat" and payload["run_id"] == run_id
    assert payload["candidates"][0]["period_type"] == "annual"
    assert h.executed == [] and h.activated == [] and len(h.provider_calls) == 1
    history = h.client.get(f"/api/sessions/{session_id}/messages").json()
    assert [m["kind"] for m in history] == ["text", "report_candidates"]


def test_a_financial_question_without_a_report_gets_a_choose_card(harness):
    h = harness()
    run_id = h.ask(h.session(with_report=False), "Doanh thu năm 2025 là bao nhiêu?")
    run_chat_once(h.db, "w", h.deps(QA_PLAN))
    run = h.run(run_id)
    assert run["result_kind"] == "dataset_required" and run["answer"]["kind"] == "dataset_required"
    assert h.executed == [] and h.provider_calls == []


def test_a_qa_question_is_planned_once_and_starts_at_the_router(harness):
    h = harness()
    run_id = h.ask(h.session(with_report=True), "Doanh thu năm 2025?")
    run_chat_once(h.db, "w", h.deps(QA_PLAN))
    assert h.run(run_id)["result_kind"] == "answer" and h.run(run_id)["task_kind"] == "report_qa"
    assert len(h.plans) == 1
    assert h.executed[0]["planner_updates"] == QA_PLAN


def test_a_search_from_a_report_session_can_be_answered_on_that_report(harness):
    h = harness()
    session_id = h.session(with_report=True)
    original = h.ask(session_id, "Báo cáo VNM 2025 lợi nhuận bao nhiêu?")
    run_chat_once(h.db, "w", h.deps(DISCOVER_VNM))
    assert h.run(original)["result_kind"] == "report_candidates"
    [session] = [s for s in h.client.get("/api/sessions").json() if s["id"] == session_id]
    assert session["dataset_id"] == h.shared.dataset_id  # searching never swaps the report

    body = {"idempotency_key": "override-000001"}
    override = h.post(f"/api/chat-runs/{original}/answer-with-report", body)
    assert override.status_code == 202, override.text
    override_id = override.json()["run_id"]
    assert override.json()["override_of_run_id"] == original
    assert h.post(f"/api/chat-runs/{original}/answer-with-report", body).json()["run_id"] == override_id
    assert h.admitted == [h.owner] and h.usage == ["qa_override"]  # no second question quota

    run_chat_once(h.db, "w", h.deps(planner_must_not_run=True))
    assert h.run(override_id)["result_kind"] == "answer"
    assert "planner_updates" not in h.executed[0]  # the plain QA path plans for itself
    with h.db.read() as conn:
        question = webdb.get_message(conn, webdb.get_run(conn, original)["question_message_id"])
    assert question["run_id"] == original
    assert h.post(f"/api/chat-runs/{override_id}/answer-with-report",
                  {"idempotency_key": "override-000002"}).status_code == 409


def test_a_model_guessed_ticker_is_confirmed_before_searching(harness):
    h = harness()
    run_id = h.ask(h.session(with_report=False), "Tìm báo cáo Vinamilk")
    plan = {**DISCOVER_VNM, "report_search": {"subject": "Vinamilk", "ticker_guess": "VNM"}}
    run_chat_once(h.db, "w", h.deps(plan))
    clarification = h.run(run_id)["answer"]["payload"]["clarification"]
    assert clarification["reason"] == "confirm_ticker"
    assert [c["ticker"] for c in clarification["choices"]] == ["VNM"] and h.provider_calls == []


def test_a_contradictory_period_is_asked_about_first(harness):
    h = harness()
    run_id = h.ask(h.session(with_report=False), "Tìm BCTC năm quý 2 VNM")
    plan = {**DISCOVER_VNM, "report_search": {"subject": "VNM", "period": "annual", "fiscal_quarter": 2}}
    run_chat_once(h.db, "w", h.deps(plan))
    assert h.run(run_id)["answer"]["payload"]["clarification"]["reason"] == "ambiguous_period"
    assert h.provider_calls == []


def test_chat_and_form_share_the_owner_discovery_lock(harness):
    h = harness()
    run_id = h.ask(h.session(with_report=False), "Tìm BCTC VNM năm 2025")
    held = ProcessSingleton("discovery-" + hashlib.sha256(h.owner.encode()).hexdigest()[:16])
    assert held.try_acquire()
    try:
        run_chat_once(h.db, "w", h.deps(DISCOVER_VNM))
    finally:
        held.release()
    answer = h.run(run_id)["answer"]
    assert answer["kind"] == "report_search_error"
    assert answer["payload"]["error"]["code"] == "discovery_in_progress" and h.provider_calls == []
