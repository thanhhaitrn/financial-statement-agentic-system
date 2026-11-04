"""Answer evidence from stored facts, and owner-checked views of the original PDF."""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from agentfinx.api import db as webdb
from agentfinx.datasets.access import DatasetAccessError, get_accessible_dataset
from agentfinx.datasets.kb.sqlite_repo import open_db_readonly

logger = logging.getLogger(__name__)
_PDF_LOCK = threading.Lock()  # PDFium must not run concurrently across threads.
MAX_SOURCES = 24


def _error(status, message):
    return HTTPException(status, {"code": "report_source_unavailable", "message": message})


def _fact_ids(state: dict) -> list[str]:
    ids = []

    def add(facts):
        for fact in facts if isinstance(facts, list) else []:
            if isinstance(fact, dict) and fact.get("fact_id"):
                value = str(fact["fact_id"])
                if value not in ids and len(value) <= 160:
                    ids.append(value)

    ledger = state.get("evidence_ledger") or {}
    for entry in ledger.get("entries", []) if isinstance(ledger, dict) else []:
        if isinstance(entry, dict):
            add(entry.get("selected_facts"))
    # Injected pipelines and analysis agents may carry the same IDs in facts.
    for key in ("worker_results", "ragas_facts_by_table"):
        results = state.get(key) or {}
        for result in results.values() if isinstance(results, dict) else []:
            if isinstance(result, dict):
                add(result.get("facts"))
    return ids[:160]


def build_answer_sources(state: dict, dataset, generation: str, answer: str) -> list[dict]:
    """Never trust an LLM's value/page: re-read selected fact IDs from this KB."""
    ids = _fact_ids(state)
    if not ids:
        return []
    try:
        conn = open_db_readonly(dataset.sqlite_db_path)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM financial_facts WHERE fact_id IN ({','.join('?' for _ in ids)})", ids,
            ).fetchall()
        finally:
            conn.close()
        by_id = {row["fact_id"]: row for row in rows}
        sources = []
        for fact_id in ids:
            row = by_id.get(fact_id)
            if row is None:
                continue
            raw_page = str(row["source_page"] or "")
            sources.append({
                "id": fact_id,
                "dataset_id": dataset.dataset_id,
                "generation": generation,
                "label": str(row["row_label"] or row["item_name"] or ""),
                "value": str(row["value"] or ""),
                "period": str(row["column_label"] or row["period_label"] or ""),
                "unit": str(row["unit"] or ""),
                "section": str(row["section_path"] or row["heading"] or ""),
                "page": int(raw_page) if raw_page.isdigit() and int(raw_page) > 0 else None,
            })
        # Put numbers explicitly present in the answer before supporting operands.
        digits = re.sub(r"[.,\s]", "", answer)
        sources.sort(key=lambda source: not (
            len(re.sub(r"\D", "", source["value"])) >= 4
            and re.sub(r"[.,\s]", "", source["value"]) in digits
        ))
        return sources[:MAX_SOURCES]
    except (OSError, ValueError, sqlite3.Error):
        logger.warning("Could not read answer sources for dataset %s", dataset.dataset_id)
        return []  # Supplemental references must not prevent an answer.


def register_report_source_routes(app, *, database, current_user, shared_ids, inspect_generation):
    def locate(message_id, source_id, owner_id):
        with database.read() as conn:
            message = webdb.get_message(conn, message_id)
            session = webdb.get_owned_session(conn, message["session_id"], owner_id) if message else None
            run = webdb.get_owned_run(conn, message["run_id"], owner_id) if message and message["run_id"] else None
        if not message or not session or not run or message["role"] != "assistant":
            raise _error(404, "Không tìm thấy nguồn báo cáo.")
        try:
            payload = json.loads(message["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        source = next((item for item in payload.get("report_sources", [])
                       if item.get("id") == source_id and item.get("dataset_id") == run["dataset_id"]), None)
        if not source:
            raise _error(404, "Không tìm thấy nguồn báo cáo.")
        try:
            dataset = get_accessible_dataset(run["dataset_id"], owner_id, shared_ids).record
        except (DatasetAccessError, ValueError):
            raise _error(404, "Không tìm thấy nguồn báo cáo.") from None
        try:
            current = inspect_generation(dataset)
        except Exception:
            raise _error(409, "Phiên bản báo cáo này không còn khả dụng.") from None
        if current != source.get("generation") or current != run["dataset_generation"]:
            raise _error(409, "Báo cáo đã thay đổi từ lúc trả lời; hãy hỏi lại trên phiên bản mới.")
        return source, dataset

    def pdf_path(dataset):
        if not dataset.source_pdf_path:
            raise _error(404, "Báo cáo này chưa có bản PDF gốc.")
        path = Path(dataset.source_pdf_path)
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise _error(404, "Bản PDF gốc không còn khả dụng.")
        return path

    base = "/api/messages/{message_id}/sources/{source_id}"

    @app.get(base)
    def source_info(message_id: str, source_id: str, request: Request, user=Depends(current_user)):
        source, dataset = locate(message_id, source_id, user["id"])
        count = 0
        if dataset.source_pdf_path and Path(dataset.source_pdf_path).is_file():
            from pypdf import PdfReader

            try:
                count = len(PdfReader(str(pdf_path(dataset))).pages)
            except Exception:
                logger.warning("Cannot inspect source PDF for dataset %s", dataset.dataset_id)
        return {"source": source, "company": dataset.company, "page_count": count,
                "has_pdf": bool(count), "page": source["page"] if source["page"] and source["page"] <= count else 1}

    @app.get(base + "/page")
    def source_page(message_id: str, source_id: str, request: Request,
                    page: int = Query(ge=1, le=2000), user=Depends(current_user)):
        _, dataset = locate(message_id, source_id, user["id"])
        path = pdf_path(dataset)
        import pypdfium2 as pdfium

        try:
            with _PDF_LOCK, closing(pdfium.PdfDocument(str(path))) as pdf:
                if page > len(pdf):
                    raise _error(404, "Trang báo cáo không tồn tại.")
                with closing(pdf[page - 1]) as pdf_page:
                    width, height = pdf_page.get_size()
                    # Bound both dimensions even for unusual page aspect ratios.
                    scale = min(1800 / max(width, 1), 2400 / max(height, 1), 3)
                    with closing(pdf_page.render(scale=scale)) as bitmap:
                        out = io.BytesIO()
                        image = bitmap.to_pil()
                        try:
                            image.save(out, format="PNG")
                        finally:
                            image.close()
            return Response(out.getvalue(), media_type="image/png", headers={"Cache-Control": "private, no-store"})
        except HTTPException:
            raise
        except Exception:
            logger.exception("Cannot render source PDF for dataset %s", dataset.dataset_id)
            raise _error(422, "Chưa hiển thị được trang này. Bạn có thể mở PDF gốc.") from None

    @app.get(base + "/pdf")
    def source_pdf(message_id: str, source_id: str, request: Request, user=Depends(current_user)):
        _, dataset = locate(message_id, source_id, user["id"])
        return FileResponse(pdf_path(dataset), media_type="application/pdf", filename="bao-cao-goc.pdf",
                            content_disposition_type="inline", headers={"Cache-Control": "private, no-store"})
