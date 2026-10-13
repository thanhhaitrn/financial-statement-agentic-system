"""CafeF report discovery for the web app: one service for the form and chat.

Security and consistency boundaries:

* the owner comes from the cookie (form) or the run (chat), never from the
  request body; the request model forbids unknown fields;
* at most one discovery per owner runs at a time, across the API process and
  the chat worker (a cross-process file lock); a competing request gets 429;
* no database transaction is open while CafeF is being called;
* every outcome — candidates, a request for more information, a provider
  error — is stored as a typed message, so a reload shows the same card and
  never searches again. A provider failure is never shown as "no reports".

Searching only reads metadata. Nothing is downloaded or parsed until the user
confirms one candidate through ``POST /api/report-imports``.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Literal, Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentfinx.acquisition.service import ReportDiscoveryUnavailable
from agentfinx.datasets.locks import ProcessSingleton
from agentfinx.core.schemas.acquisition import ReportQuery
from agentfinx.api import db as webdb
from agentfinx.api.messages import message_out

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
MAX_CHOICES = 8
RESULT_KINDS = ("report_candidates", "report_search_clarification", "report_search_error")

PROVIDER_ERROR_TEXT = {
    "provider_unavailable": "Không kết nối được CafeF (hết thời gian chờ hoặc bị chặn truy cập).",
    "provider_rejected": "CafeF từ chối yêu cầu tìm kiếm.",
    "provider_malformed": "CafeF trả về dữ liệu không đọc được.",
}
NOT_NO_RESULTS = (
    " Điều này không có nghĩa là doanh nghiệp không có báo cáo; bạn có thể thử lại sau "
    "hoặc tải PDF lên."
)
CLARIFICATION_SUMMARY = {
    "ambiguous": "Có nhiều doanh nghiệp khớp; hãy chọn một.",
    "not_found": "Không tìm thấy doanh nghiệp này trong danh mục CafeF; hãy nhập mã CK.",
    "catalog_unavailable": "Danh mục mã CK chưa sẵn sàng; hãy nhập mã CK.",
    "needs_input": "Hãy cho biết mã CK hoặc tên doanh nghiệp cần tìm.",
    "confirm_ticker": "Hãy xác nhận mã CK trước khi tìm.",
    "ambiguous_period": "Kỳ báo cáo chưa rõ; hãy chọn lại kỳ trong form tìm kiếm.",
}
BUSY_TEXT = "Một yêu cầu tìm báo cáo khác đang chạy; hãy thử lại sau ít phút."
DATASET_REQUIRED_TEXT = (
    "Cuộc trò chuyện này chưa có báo cáo nên chưa trả lời được câu hỏi tài chính. "
    "Hãy chọn một báo cáo, tìm trên CafeF hoặc tải PDF lên."
)


class DiscoverySearch(BaseModel):
    """What to search for. ``ticker`` is set when the user picked a symbol
    from a clarification card; otherwise ``subject`` is resolved through the
    symbol catalog, never guessed."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=120)
    ticker: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9]{2,5}$")
    fiscal_year: Optional[int] = Field(default=None, ge=1990, le=2100)
    period: Literal["any", "annual", "quarter", "half_year"] = "any"
    fiscal_quarter: Optional[int] = Field(default=None, ge=1, le=4)
    fiscal_half: Optional[int] = Field(default=None, ge=1, le=2)
    scope: Literal["consolidated", "separate", "unknown"] = "unknown"
    audit_status: Literal["audited", "reviewed", "unaudited", "unknown"] = "unknown"

    @model_validator(mode="after")
    def check_fields(self):
        self.subject = " ".join(self.subject.split())
        if not self.subject:
            raise ValueError("subject must not be blank")
        if self.ticker:
            self.ticker = self.ticker.upper()
        if self.fiscal_quarter is not None and self.period != "quarter":
            raise ValueError("fiscal_quarter requires period=quarter")
        if self.fiscal_half is not None and self.period != "half_year":
            raise ValueError("fiscal_half requires period=half_year")
        return self

    def summary(self) -> str:
        parts = [self.ticker or self.subject]
        if self.fiscal_year:
            parts.append(f"năm {self.fiscal_year}")
        if self.period == "annual":
            parts.append("báo cáo năm")
        elif self.period == "quarter":
            parts.append(f"quý {self.fiscal_quarter}" if self.fiscal_quarter else "báo cáo quý")
        elif self.period == "half_year":
            parts.append({1: "6 tháng đầu", 2: "6 tháng cuối"}.get(self.fiscal_half, "bán niên"))
        if self.scope != "unknown":
            parts.append({"consolidated": "hợp nhất", "separate": "riêng"}[self.scope])
        return "Tìm báo cáo: " + " · ".join(parts)


class DiscoveryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)
    search: DiscoverySearch

    def fingerprint(self) -> str:
        body = {"session_id": self.session_id or "", "search": self.search.model_dump()}
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


class DiscoveryBusy(RuntimeError):
    """Another discovery is already running for this owner."""


@contextmanager
def owner_discovery_lock(owner_id: str) -> Iterator[None]:
    lock = ProcessSingleton("discovery-" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16])
    if not lock.try_acquire():
        raise DiscoveryBusy("a report discovery is already running for this account")
    try:
        yield
    finally:
        lock.release()


def candidate_out(candidate: Any) -> dict:
    """A candidate as the browser may see it: no owner, no session internals."""

    return {
        "candidate_id": candidate.candidate_id,
        "title": candidate.title,
        "ticker": candidate.ticker,
        "company": candidate.company,
        "exchange": candidate.exchange,
        "period_type": candidate.period_type,
        "fiscal_year": candidate.fiscal_year,
        "fiscal_quarter": candidate.fiscal_quarter,
        "fiscal_half": candidate.fiscal_half,
        "period_label": candidate.period_label,
        "period_verified": candidate.period_verified,
        "verification_note": candidate.verification_note,
        "scope": candidate.scope,
        "audit_status": candidate.audit_status,
        "source_url": candidate.source_url,
        "source_host": (urlparse(candidate.source_url).hostname or "").lower(),
        "expires_at": candidate.expires_at,
    }


def _symbol_out(symbol: Any) -> dict:
    return {"ticker": symbol.ticker, "company": symbol.company, "exchange": symbol.exchange}


def run_discovery(
    service: Any,
    search: DiscoverySearch,
    *,
    owner_id: str,
    session_id: str,
    suggested_ticker: Optional[str] = None,
) -> dict:
    """Resolve the subject and search CafeF; returns the message payload.

    Call under ``owner_discovery_lock`` with no database transaction open.
    ``suggested_ticker`` is a model's guess (chat): it is only ever offered
    back as a choice to confirm, never searched on its own authority."""

    base = {"search_request": search.model_dump(), "source_session_id": session_id}
    ticker, company, exchange = search.ticker or "", "", ""
    if not ticker:
        resolution = service.resolve_subject(search.subject)
        if resolution.status in {"resolved", "not_in_catalog"}:
            symbol = resolution.matches[0]
            ticker, company, exchange = symbol.ticker, symbol.company, symbol.exchange
        else:
            reason = resolution.status
            choices = list(resolution.matches[:MAX_CHOICES])
            if not choices and suggested_ticker:
                from agentfinx.acquisition.symbols import StockSymbol, looks_like_ticker

                if looks_like_ticker(suggested_ticker):
                    catalog = service.symbol_catalog()
                    known = catalog.get(suggested_ticker)
                    if known is not None or not catalog.usable():
                        reason = "confirm_ticker"
                        choices = [known or StockSymbol(suggested_ticker.upper(), "", "")]
            return {
                **base,
                "kind": "report_search_clarification",
                "clarification": {
                    "reason": reason,
                    "subject": search.subject,
                    "choices": [_symbol_out(item) for item in choices],
                    "more": len(resolution.matches) > MAX_CHOICES,
                },
                "catalog_status": resolution.catalog_status,
            }
    query = ReportQuery(
        owner_id=owner_id,
        session_id=session_id,
        ticker=ticker,
        company=company,
        exchange=exchange,
        fiscal_year=search.fiscal_year,
        period=search.period,
        fiscal_quarter=search.fiscal_quarter,
        fiscal_half=search.fiscal_half,
        scope="" if search.scope == "unknown" else search.scope,
        audit_status="" if search.audit_status == "unknown" else search.audit_status,
    )
    searched = {"subject": search.subject, "ticker": ticker, "company": company, "exchange": exchange,
                "fiscal_year": search.fiscal_year, "period": search.period,
                "fiscal_quarter": search.fiscal_quarter, "fiscal_half": search.fiscal_half}
    try:
        outcome = service.discover(query)
    except ReportDiscoveryUnavailable as exc:
        return {
            **base,
            "kind": "report_search_error",
            "search": searched,
            "error": {
                "code": exc.code,
                "message": PROVIDER_ERROR_TEXT.get(exc.code, PROVIDER_ERROR_TEXT["provider_unavailable"])
                + NOT_NO_RESULTS,
            },
        }
    return {
        **base,
        "kind": "report_candidates",
        "search": {**searched, "company": outcome.query.company, "exchange": outcome.query.exchange},
        "status": outcome.status,
        "candidates": [candidate_out(item) for item in outcome.candidates],
        "total_matched": outcome.total_matched,
        "truncated": outcome.truncated,
        "warnings": list(outcome.warnings),
        "catalog_status": outcome.catalog_status,
    }


def search_from_planner(report_search: dict) -> tuple[Optional[DiscoverySearch], Optional[str]]:
    """A form-equivalent search from the planner's ``report_search``, or the
    reason to ask the user first (missing subject, contradictory period)."""

    fields = dict(report_search or {})
    subject = " ".join(str(fields.get("subject") or "").split())
    if not subject:
        return None, "needs_input"
    period = fields.get("period") or "any"
    if fields.get("fiscal_quarter") is not None and period == "any":
        period = "quarter"
    if fields.get("fiscal_half") is not None and period == "any":
        period = "half_year"
    try:
        return DiscoverySearch(
            subject=subject,
            fiscal_year=fields.get("fiscal_year"),
            period=period,
            fiscal_quarter=fields.get("fiscal_quarter"),
            fiscal_half=fields.get("fiscal_half"),
            scope=fields.get("scope") or "unknown",
            audit_status=fields.get("audit_status") or "unknown",
        ), None
    except ValueError:
        return None, "ambiguous_period"


def clarification_payload(report_search: dict, reason: str, *, session_id: str) -> dict:
    fields = dict(report_search or {})
    subject = " ".join(str(fields.get("subject") or "").split())
    return {
        "kind": "report_search_clarification",
        "search_request": {"subject": subject} if subject else {},
        "source_session_id": session_id,
        "clarification": {"reason": reason, "subject": subject, "choices": [], "more": False},
    }


def busy_payload(search: DiscoverySearch, *, session_id: str) -> dict:
    return {
        "kind": "report_search_error",
        "search_request": search.model_dump(),
        "source_session_id": session_id,
        "error": {"code": "discovery_in_progress", "message": BUSY_TEXT},
    }


def dataset_required_payload(*, session_id: str) -> dict:
    return {"kind": "dataset_required", "source_session_id": session_id, "message": DATASET_REQUIRED_TEXT}


def result_summary(payload: dict) -> str:
    """Plain-text fallback for a result message (history search, old clients)."""

    kind = payload.get("kind")
    ticker = (payload.get("search") or {}).get("ticker") or (payload.get("search_request") or {}).get("subject", "")
    if kind == "report_candidates":
        count = len(payload.get("candidates") or [])
        return f"Tìm thấy {count} báo cáo cho {ticker}." if count else f"Không có báo cáo nào khớp cho {ticker}."
    if kind == "report_search_clarification":
        return CLARIFICATION_SUMMARY.get((payload.get("clarification") or {}).get("reason"), "Cần thêm thông tin để tìm báo cáo.")
    if kind == "report_search_error":
        return (payload.get("error") or {}).get("message", "Chưa tìm được báo cáo trên CafeF.")
    if kind == "dataset_required":
        return DATASET_REQUIRED_TEXT
    return ""


def store_result(
    conn,
    *,
    session_id: str,
    payload: dict,
    run_id: Optional[str] = None,
) -> str:
    return webdb.add_message(
        conn,
        session_id=session_id,
        role="assistant",
        content=result_summary(payload),
        run_id=run_id,
        kind=payload["kind"],
        payload=json.dumps(payload, ensure_ascii=False),
    )


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def register_discovery_routes(
    app: FastAPI,
    *,
    database: webdb.WebDB,
    current_user: Callable[..., Any],
    get_service: Callable[[], Any],
    report_discovery: bool,
) -> None:
    def replay(conn, row) -> dict:
        return {
            "session_id": row["session_id"],
            "request_message": message_out(webdb.get_message(conn, row["request_message_id"])),
            "result_message": message_out(webdb.get_message(conn, row["result_message_id"])),
        }

    @app.post("/api/report-discoveries")
    def discover_reports(body: DiscoveryBody, user=Depends(current_user)):
        if not report_discovery:
            raise _error(404, "report_discovery_disabled", "Tính năng tìm báo cáo đang tắt.")
        owner_id = user["id"]
        key = body.idempotency_key.strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            raise _error(422, "invalid_idempotency_key", "Khoá gửi yêu cầu không hợp lệ.")
        fingerprint = body.fingerprint()
        with database.read() as conn:
            prior = webdb.get_discovery_request(conn, owner_id, key)
            if prior is not None:
                if prior["request_fingerprint"] != fingerprint:
                    raise _error(409, "idempotency_conflict", "Yêu cầu này đã được gửi với nội dung khác.")
                return {**replay(conn, prior), "replayed": True}
            if body.session_id is not None:
                session = webdb.get_owned_session(conn, body.session_id, owner_id)
                if session is None:
                    raise _error(404, "session_not_found", "Không tìm thấy cuộc trò chuyện.")
                if session["read_only"]:
                    raise _error(409, "session_read_only",
                                 "Cuộc trò chuyện này chỉ còn để xem lại; hãy tìm trong một cuộc trò chuyện mới.")
        service = get_service()
        try:
            with owner_discovery_lock(owner_id):
                session_id = body.session_id
                if session_id is None:
                    # Searching from the start screen: the conversation holds
                    # the search card (and later the chosen report).
                    with database.transaction() as conn:
                        session_id = webdb.create_session(
                            conn, owner_id=owner_id, title=f"Tìm báo cáo {body.search.subject}"[:60]
                        )["id"]
                payload = run_discovery(service, body.search, owner_id=owner_id, session_id=session_id)
                with database.transaction() as conn:
                    request_id = webdb.add_message(
                        conn,
                        session_id=session_id,
                        role="user",
                        content=body.search.summary(),
                        kind="report_search_request",
                        payload=json.dumps(body.search.model_dump(), ensure_ascii=False),
                    )
                    result_id = store_result(conn, session_id=session_id, payload=payload)
                    webdb.insert_discovery_request(
                        conn,
                        owner_id=owner_id,
                        session_id=session_id,
                        idempotency_key=key,
                        request_fingerprint=fingerprint,
                        request_message_id=request_id,
                        result_message_id=result_id,
                    )
                    webdb.touch_session(conn, session_id)
                    row = webdb.get_discovery_request(conn, owner_id, key)
                    return {**replay(conn, row), "replayed": False}
        except DiscoveryBusy:
            raise _error(429, "discovery_in_progress",
                         "Một yêu cầu tìm báo cáo khác đang chạy; vui lòng đợi rồi thử lại.") from None
