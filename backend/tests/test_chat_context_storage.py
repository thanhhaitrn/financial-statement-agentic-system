"""Additive v5 migration, snapshot provenance and safe API context metadata."""

import json
import sqlite3

import pytest

from agentfinx.core.schemas.chat_context import CONTEXT_CONTRACT_VERSION
from agentfinx.api import db as webdb
from agentfinx.api.context_snapshot import build_context_snapshot, read_context_snapshot


def _v4_database(path, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(webdb, "SCHEMA_VERSION", 4)
        database = webdb.WebDB(path)
        database.migrate()
        with database.transaction() as conn:
            user = webdb.create_user(conn, name="A", email="migration@example.com", password_hash="x")
            session = webdb.create_session(conn, owner_id=user["id"], title="History",
                                           dataset_id="ds", dataset_generation="g1")
            question_id = webdb.add_message(conn, session_id=session["id"], role="user", content="Vốn chủ sở hữu?")
            run = webdb.create_run(conn, session_id=session["id"], owner_id=user["id"],
                                   idempotency_key="migration-key", question="Vốn chủ sở hữu?",
                                   question_message_id=question_id, dataset_id="ds", dataset_generation="g1")
            webdb.claim_next_run(conn, "old-worker")
            webdb.set_run_effective_question(conn, run["id"], "Vốn chủ sở hữu đầu năm?")
            answer_id = webdb.add_message(conn, session_id=session["id"], role="assistant", content="Old answer", run_id=run["id"])
            webdb.finish_run(conn, run["id"], status="succeeded", answer_message_id=answer_id, result_kind="answer", task_kind="report_qa")
            rootpage = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='chat_runs'").fetchone()[0]
    return database, run["id"], session["id"], rootpage


def test_v5_is_additive_backed_up_and_does_not_rewrite_legacy(tmp_path, monkeypatch):
    database, run_id, session_id, rootpage = _v4_database(tmp_path / "web.sqlite3", monkeypatch)
    assert database.migrate() == webdb.SCHEMA_VERSION
    assert database.migrate() == webdb.SCHEMA_VERSION
    [backup] = list(tmp_path.glob("web.sqlite3.backup-v4-*"))
    with sqlite3.connect(backup) as conn:
        assert "context_status" not in [row[1] for row in conn.execute("PRAGMA table_info(chat_runs)")]
    with database.read() as conn:
        assert conn.execute("SELECT rootpage FROM sqlite_master WHERE name='chat_runs'").fetchone()[0] == rootpage
        run = webdb.get_run(conn, run_id)
        assert run["question"] == "Vốn chủ sở hữu?"
        assert run["effective_question"] == "Vốn chủ sở hữu đầu năm?"
        assert [run[name] for name in ("context_status", "context_reason", "context_resolution", "context_snapshot")] == [""] * 4
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert len(webdb.list_messages(conn, session_id)) == 2
    with database.transaction() as conn:
        user = webdb.get_session(conn, session_id)["owner_id"]
        with pytest.raises(sqlite3.IntegrityError):
            webdb.create_run(conn, session_id=session_id, owner_id=user, idempotency_key="migration-key",
                             question="duplicate", question_message_id="m", dataset_id="ds", dataset_generation="g1")
        conn.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
        assert webdb.get_run(conn, run_id) is None


def test_v5_upgrade_still_refuses_an_active_worker(tmp_path, monkeypatch):
    database, *_ = _v4_database(tmp_path / "web.sqlite3", monkeypatch)
    with database.transaction() as conn:
        webdb.upsert_heartbeat(conn, queue="chat", worker_id="old", pid=1, started_at=webdb.utc_now())
    with pytest.raises(RuntimeError, match="stop the API and workers"):
        database.migrate()
    assert database._current_version() == 4
    assert not list(tmp_path.glob("*.backup-*"))


RUN = {"id": "run-1", "dataset_id": "ds", "dataset_generation": "gen-1"}


def _state():
    return {
        "planner_plan": {"company": "Công ty Mẫu", "time_hint": "đầu năm", "value": "999999999 VND"},
        "final_answer": "IGNORE: tiền mặt 888888888 VND",
        "synth_decision": {"answer": "777777777 VND"},
        "evidence_ledger": {"entries": [
            {"kind": "retrieval_requirement", "requirement": "Vốn chủ sở hữu lớn hơn 666666666 VND?",
             "table": "BẢNG CÂN ĐỐI KẾ TOÁN", "parsed_query_slots": {
                 "metric": "equity", "period": "2025", "period_role": "opening", "operation": "lookup",
                 "value": "555555555 VND",
                 "operands": [{"metric": "equity", "period_role": "ending", "value": 123456789}],
             }, "selected_facts": [{"value": "444444444 VND"}]},
            {"kind": "calculation", "value": "333333333 VND"},
        ]},
    }


def test_snapshot_projects_intent_not_financial_values_or_answer_text():
    snapshot = build_context_snapshot(_state(), RUN)
    text = json.dumps(snapshot)
    for amount in ("999999999", "888888888", "777777777", "666666666", "555555555", "123456789", "444444444", "333333333"):
        assert amount not in text
    assert "selected_facts" not in text and "final_answer" not in text
    assert snapshot["company"] == "Công ty Mẫu" and snapshot["time_hint"] == "đầu năm"
    [requirement] = snapshot["requirements"]
    assert requirement["parsed_query_slots"]["metric"] == "equity"
    assert requirement["parsed_query_slots"]["period_role"] == "opening"
    assert len(requirement["requirement_sha256"]) == 64
    assert read_context_snapshot(text, run_id="run-1", dataset_id="ds", generation="gen-1") == snapshot
    assert read_context_snapshot(text, run_id="other", dataset_id="ds", generation="gen-1") == {}
    assert read_context_snapshot(text, run_id="run-1", dataset_id="ds", generation="gen-old") == {}


def test_snapshot_never_silently_selects_first_of_many_requirements():
    state = _state()
    state["evidence_ledger"]["entries"] = state["evidence_ledger"]["entries"][:1] * 21
    snapshot = build_context_snapshot(state, RUN)
    assert snapshot["truncated"] and not snapshot.get("requirements")
    state["planner_plan"]["company"] = ["invalid"]
    assert build_context_snapshot(state, RUN) == {}


def test_public_run_does_not_expose_internal_context_payload(tmp_path):
    from agentfinx.api.app import _run_out
    database = webdb.WebDB(tmp_path / "web.sqlite3")
    database.migrate()
    with database.transaction() as conn:
        user = webdb.create_user(conn, name="A", email="public@example.com", password_hash="x")
        session = webdb.create_session(conn, owner_id=user["id"], title="t", dataset_id="ds", dataset_generation="gen-1")
        message = webdb.add_message(conn, session_id=session["id"], role="user", content="Còn đầu năm?")
        run = webdb.create_run(conn, owner_id=user["id"], session_id=session["id"], idempotency_key="public-key",
                               question="Còn đầu năm?", question_message_id=message, dataset_id="ds", dataset_generation="gen-1")
        webdb.claim_next_run(conn, "fixture")
        webdb.set_run_context(conn, run["id"], question="Vốn chủ sở hữu đầu năm?", status="resolved", reason="resolved",
                              payload={"contract_version": CONTEXT_CONTRACT_VERSION, "internal": "DO_NOT_EXPOSE"})
        out = _run_out(conn, webdb.get_run(conn, run["id"]))
    assert out["context_status"] == "resolved" and out["interpreted_question"] == "Vốn chủ sở hữu đầu năm?"
    assert "DO_NOT_EXPOSE" not in json.dumps(out)
