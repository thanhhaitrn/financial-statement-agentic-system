"""Focused source-link checks: factual provenance, access control, original PDF pages."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from agentfinx.api import db as webdb
from agentfinx.api.app import create_app
from agentfinx.api.chat_runner import _succeed
from agentfinx.api.messages import message_out
from agentfinx.api.report_sources import build_answer_sources
from agentfinx.api.settings import WebSettings
from agentfinx.datasets import registry
from agentfinx.datasets.kb.sqlite_repo import init_db, insert_financial_facts

ORIGIN = "http://testserver"


def make_source_pdf(path):
    writer = PdfWriter()
    for label in ("Page one: cover", "Page two: Profit 123.456.789 VND"):
        page = writer.add_blank_page(width=600, height=800)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"):
            DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 14 Tf 40 760 Td ({label}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(path)


def seed_source(database, owner, record, source):
    with database.transaction() as conn:
        session = webdb.create_session(conn, owner_id=owner, title="Report", dataset_id=record.dataset_id,
                                       dataset_generation="gen-1")
        question = webdb.add_message(conn, session_id=session["id"], role="user", content="Lợi nhuận?")
        run = webdb.create_run(conn, session_id=session["id"], owner_id=owner, idempotency_key="sources",
            question="Lợi nhuận?", question_message_id=question, dataset_id=record.dataset_id,
            dataset_generation="gen-1")
        webdb.claim_next_run(conn, "source-test")
    _succeed(database, run, "Lợi nhuận là **123.456.789 VND**.", report_sources=[source])
    with database.read() as conn:
        message = webdb.list_messages(conn, session["id"])[-1]
        return session["id"], message_out(message)


@pytest.fixture
def source_app(tmp_path, monkeypatch):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    generation = {"value": "gen-1"}
    app = create_app(WebSettings(secret_key="test-source-" + "s" * 40, db_path=tmp_path / "web.sqlite",
        registration_mode="open", allowed_origins=frozenset({ORIGIN})),
        generation_inspector=lambda _: generation["value"])
    client = TestClient(app)
    user = client.post("/api/register", json={"name": "Người dùng", "email": "sources@example.com",
        "password": "correct horse battery"}, headers={"Origin": ORIGIN}).json()["user"]
    pdf = tmp_path / "original.pdf"
    make_source_pdf(pdf)
    record = registry.save_dataset(registry.build_dataset_record(file_path=str(tmp_path / "source.md"),
        source_pdf_path=str(pdf), company="Công ty Mẫu", owner_id=user["id"], dataset_id="source-report",
        fiscal_year=2025).model_copy(update={"status": "ready"}))
    conn = init_db(record.sqlite_db_path)
    insert_financial_facts(conn, [{"company": "Công ty Mẫu", "fiscal_year": "2025", "heading": "Kết quả kinh doanh",
        "item_name": "Lợi nhuận", "row_label": "Lợi nhuận", "column_label": "Năm 2025", "period_label": "2025",
        "value": "123.456.789", "raw_value": "123.456.789", "normalized_value": "123456789",
        "value_kind": "amount", "parsed_value": "123456789", "unit": "VND", "period_role": "current",
        "section_path": "Kết quả kinh doanh", "block_id": "table-1", "source_page": "2", "source": record.file_path}])
    fact_id = conn.execute("SELECT fact_id FROM financial_facts").fetchone()[0]
    conn.close()
    state = {"evidence_ledger": {"entries": [{"selected_facts": [
        {"fact_id": fact_id, "value": "FAKE_VALUE", "source_page": "999"}, {"fact_id": "made-up"}]}]}}
    sources = build_answer_sources(state, record, "gen-1", "Lợi nhuận 123.456.789 VND")
    session, message = seed_source(app.state.db, user["id"], record, sources[0])
    return SimpleNamespace(client=client, app=app, generation=generation, source=sources[0], message=message,
                           record=record, session=session)


def test_answer_source_uses_stored_value_page_and_survives_reload(source_app):
    s = source_app
    assert s.source["value"] == "123.456.789" and s.source["page"] == 2
    base = f"/api/messages/{s.message['id']}/sources/{s.source['id']}"
    assert s.message["report_sources"] == [s.source]
    assert "original.pdf" not in json.dumps(s.message)
    info = s.client.get(base).json()
    assert info["has_pdf"] and info["page_count"] == 2 and info["page"] == 2
    image = s.client.get(base + "/page?page=2")
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    assert image.headers["cache-control"] == "private, no-store"
    assert s.client.get(base + "/pdf").content.startswith(b"%PDF-")
    assert s.client.get(base + "/page?page=3").status_code == 404
    assert s.client.get(base + "/page?page=0").status_code == 422
    with s.app.state.db.read() as conn:
        assert message_out(webdb.get_message(conn, s.message["id"]))["report_sources"] == [s.source]


def test_sources_enforce_owner_session_and_generation(source_app):
    s = source_app
    base = f"/api/messages/{s.message['id']}/sources/{s.source['id']}"
    stranger = TestClient(s.app)
    assert stranger.get(base).status_code == 401
    stranger.post("/api/register", json={"name": "Other", "email": "other@example.com",
        "password": "correct horse battery"}, headers={"Origin": ORIGIN})
    for suffix in ("", "/page?page=2", "/pdf"):
        assert stranger.get(base + suffix).status_code == 404
    s.generation["value"] = "gen-2"
    assert s.client.get(base).status_code == 409
    assert s.client.get(base + "/pdf").status_code == 409
