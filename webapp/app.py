"""FastAPI app: same-origin static frontend plus a JSON API.

The API never runs the model, parses a document or builds an index; it only
records work for the workers and reports state. Security boundaries:

* sessions live in an ``HttpOnly``/``SameSite=Lax`` cookie, never in page JS;
* every state-changing ``/api`` request must come from an allowed ``Origin``
  (CSRF defence in addition to ``SameSite``); CORS stays closed;
* every session, message and run lookup is owner-scoped in SQL;
* datasets go through ``dataset_catalog.access`` — no internal paths or
  collection names are ever returned.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from dataset_catalog.access import (DatasetAccessError, get_accessible_dataset,
                                    list_accessible_datasets)
from dataset_catalog.activation import DatasetNotReady, inspect_dataset_generation
from webapp import auth
from webapp import db as webdb
from webapp import throttle
from webapp.chat_runner import SAFE_MESSAGES
from webapp.discovery import register_discovery_routes
from webapp.imports import (UPLOAD_SESSION_TITLE, dataset_label,
                            default_upload_dir, register_import_routes)
from webapp.messages import message_out
from webapp.settings import WebSettings

logger = logging.getLogger("agentfinx.web.api")

STATIC_DIR = Path(__file__).resolve().parent / "static"
_PAGES = {
    "/": "landing.html",
    "/landing.html": "landing.html",
    "/login": "auth.html",
    "/register": "auth.html",
    "/auth.html": "auth.html",
    "/chat": "chat.html",
    "/chat.html": "chat.html",
}
_ASSETS = frozenset(
    {"tokens.css", "auth.css", "chatstyle.css", "landing.css", "auth.js", "chatbox.js", "landing.js"}
)
_VENDOR = frozenset({"marked.min.js", "purify.min.js"})
_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

READ_ONLY_TEXT = {
    "dataset_changed": "Báo cáo đã được cập nhật; cuộc trò chuyện này chỉ còn để xem lại.",
    "dataset_unavailable": "Báo cáo không còn khả dụng; cuộc trò chuyện này chỉ còn để xem lại.",
    "artifact_missing": "Dữ liệu của báo cáo không còn; cuộc trò chuyện này chỉ còn để xem lại.",
    "kb_invalid": "Dữ liệu của báo cáo cần build lại; cuộc trò chuyện này chỉ còn để xem lại.",
    "index_missing": "Chỉ mục của báo cáo cần build lại; cuộc trò chuyện này chỉ còn để xem lại.",
}


class RegisterPayload(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=auth.MIN_PASSWORD_LENGTH, max_length=auth.MAX_PASSWORD_LENGTH)
    remember: bool = True
    invite_code: str = Field(default="", max_length=128)


class LoginPayload(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=auth.MAX_PASSWORD_LENGTH)
    remember: bool = False


class CreateSessionPayload(BaseModel):
    # Omitted for a PDF upload: the session exists before its import, and the
    # import worker attaches the report once it is ready.
    dataset_id: Optional[str] = Field(default=None, min_length=1, max_length=128)


class RenameSessionPayload(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ChatPayload(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=8, max_length=128)


class AnswerWithReportPayload(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)


def _error(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": detail})


def _throttled(exc: throttle.Throttled) -> HTTPException:
    minutes = max(1, -(-exc.retry_after // 60))
    return HTTPException(
        status_code=429,
        detail={"code": "too_many_attempts",
                "message": f"Bạn đã thử quá nhiều lần. Vui lòng thử lại sau khoảng {minutes} phút."},
        headers={"Retry-After": str(exc.retry_after)},
    )


# One message for "already registered" and every invite problem: sign-up
# must not tell a stranger which emails have accounts.
_REGISTRATION_FAILED = "Không thể tạo tài khoản với thông tin này. Nếu bạn đã có tài khoản, hãy đăng nhập."
_INVALID_INVITE = "Mã mời không hợp lệ, đã được dùng, đã hết hạn hoặc không dành cho email này."


def _referer_origin(referer: Optional[str]) -> Optional[str]:
    if not referer:
        return None
    parts = urlsplit(referer)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


_dataset_label = dataset_label


def _dataset_out(item: Any) -> dict:
    record = item.record
    return {
        "id": record.dataset_id,
        "label": _dataset_label(record),
        "company": record.company,
        "ticker": record.ticker,
        "fiscal_year": record.fiscal_year,
        "fiscal_quarter": record.fiscal_quarter,
        "scope": record.scope,
        "audit_status": record.audit_status,
        "ready": record.status == "ready",
        "access": item.access,
    }


def _user_out(row: Any) -> dict:
    return {"id": row["id"], "name": row["name"], "email": row["email"]}


def _session_out(row: Any, labels: dict[str, str]) -> dict:
    keys = row.keys()
    reason = str(row["read_only_reason"] or "")
    read_only = bool(row["read_only"])
    return {
        "id": row["id"],
        "title": row["title"],
        "dataset_id": row["dataset_id"],
        "dataset_label": labels.get(row["dataset_id"] or "", ""),
        "read_only": read_only,
        "read_only_reason": reason if read_only else "",
        "read_only_message": READ_ONLY_TEXT.get(reason, "") if read_only else "",
        "active_run_id": row["active_run_id"] if "active_run_id" in keys else None,
        "import_job_id": row["import_job_id"] if "import_job_id" in keys else None,
        # A run whose company news is still being finished: the page resumes it.
        "news_update_run_id": row["news_update_run_id"] if "news_update_run_id" in keys else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


_message_out = message_out


def _run_out(conn, run: Any) -> dict:
    answer = None
    if run["answer_message_id"]:
        message = webdb.get_message(conn, run["answer_message_id"])
        if message is not None:
            # A run's answer may be a card (candidates, clarification, "choose
            # a report"): the browser renders its payload as text.
            answer = message_out(message)
    keys = run.keys()
    news = webdb.get_news_update_for_run(conn, run["id"])
    news_update = None
    if news is not None:
        news_message = webdb.get_message(conn, news["message_id"]) if news["message_id"] else None
        news_update = {
            "status": "pending" if news["status"] in {"pending", "running"} else news["status"],
            "message": message_out(news_message) if news_message is not None else None,
        }
    return {
        "news_update": news_update,
        "task_kind": (run["task_kind"] if "task_kind" in keys else "") or "",
        "result_kind": (run["result_kind"] if "result_kind" in keys else "") or "",
        "override_of_run_id": run["override_of_run_id"] if "override_of_run_id" in keys else None,
        "run_id": run["id"],
        "session_id": run["session_id"],
        "status": run["status"],
        "error_code": run["error_code"] or "",
        "error_message": run["error_message"] or "",
        "answer": answer,
        "context_status": run["context_status"] if "context_status" in keys else "",
        "context_reason": run["context_reason"] if "context_reason" in keys else "",
        # Effective question is also recorded for unchanged/first turns.
        "interpreted_question": (
            run["effective_question"] if "effective_question" in run.keys() else ""
        ) or "",
    }


def _derive_title(question: str) -> str:
    title = " ".join(question.split())
    return (title[:60] + "…") if len(title) > 60 else (title or "Cuộc trò chuyện mới")


def _default_question_admitter() -> Callable[[str], None]:
    state: dict[str, Any] = {}

    def admit(owner_id: str) -> None:
        if "quota" not in state:
            from acquisition import build_default_acquisition_quota

            state["quota"] = build_default_acquisition_quota()
        state["quota"].admit_question(owner_id)

    return admit


def _default_usage_recorder() -> Callable[[str, str], None]:
    state: dict[str, Any] = {}

    def record(owner_id: str, kind: str) -> None:
        if "quota" not in state:
            from acquisition import build_default_acquisition_quota

            state["quota"] = build_default_acquisition_quota()
        state["quota"].store.record_usage(owner_id, kind=kind)

    return record


def create_app(
    settings: WebSettings,
    *,
    generation_inspector: Optional[Callable[[Any], str]] = None,
    question_admitter: Optional[Callable[[str], None]] = None,
    import_service: Optional[Any] = None,
    usage_recorder: Optional[Callable[[str, str], None]] = None,
) -> FastAPI:
    database = webdb.WebDB(settings.db_path)
    database.migrate()
    inspect_generation = generation_inspector or inspect_dataset_generation
    admit_question = question_admitter or _default_question_admitter()
    record_usage = usage_recorder or _default_usage_recorder()
    shared_ids = settings.shared_dataset_ids
    import_state: dict[str, Any] = {"service": import_service}

    def get_import_service() -> Any:
        # Built on first use; the API process only admits and records jobs.
        if import_state["service"] is None:
            from webapp.imports import build_web_import_service

            import_state["service"] = build_web_import_service()
        return import_state["service"]

    app = FastAPI(title="AgentFinX", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.db = database

    @app.middleware("http")
    async def security_guard(request: Request, call_next):
        if request.method in _STATE_CHANGING and request.url.path.startswith("/api/"):
            origin = request.headers.get("origin") or _referer_origin(request.headers.get("referer"))
            if origin is None or origin.rstrip("/") not in settings.allowed_origins:
                return JSONResponse(
                    {"detail": {"code": "origin_rejected", "message": "Nguồn gửi yêu cầu không hợp lệ."}},
                    status_code=403,
                )
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, exc: RequestValidationError):
        # Location and reason only: pydantic's default body echoes the rejected
        # value ("input"), which for a password would land in response logs.
        errors = [
            {"loc": list(error.get("loc", ())), "msg": str(error.get("msg", "")),
             "type": str(error.get("type", ""))}
            for error in exc.errors()
        ]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception):
        logger.exception("unhandled API error", exc_info=exc)
        return JSONResponse(
            {"detail": {"code": "internal_error", "message": "Lỗi máy chủ, vui lòng thử lại."}},
            status_code=500,
        )

    login_account = throttle.Limit("login_account", settings.login_account_limit,
                                    settings.login_window_seconds)
    login_ip = throttle.Limit("login_ip", settings.login_ip_limit, settings.login_window_seconds)
    register_ip = throttle.Limit("register_ip", settings.register_ip_limit,
                                 settings.register_window_seconds)

    def client_address(request: Request) -> str:
        return throttle.client_address(
            request.client.host if request.client else None,
            request.headers.get("x-forwarded-for"),
            settings.trusted_proxies,
        )

    def admit_attempt(checks: list[tuple[throttle.Limit, str]]) -> list[int]:
        with database.transaction() as conn:
            try:
                return throttle.admit(conn, checks, now=time.time())
            except throttle.Throttled as exc:
                raise _throttled(exc) from None

    def current_user(request: Request) -> Any:
        claims = auth.decode_token(request.cookies.get(auth.SESSION_COOKIE), settings.secret_key)
        if claims is None:
            raise _error(401, "unauthenticated", "Bạn chưa đăng nhập.")
        # The signature proves who issued the token; the session row proves it
        # was not logged out or otherwise revoked since.
        with database.read() as conn:
            user = webdb.get_session_user(conn, claims.session_id, claims.user_id)
        if user is None:
            raise _error(401, "unauthenticated", "Phiên đăng nhập không còn hợp lệ.")
        return user

    def start_session(conn, response: Response, user_id: str, remember: bool) -> None:
        """Inside a write transaction: record a login session, set its cookie."""

        ttl = settings.remember_ttl_seconds if remember else settings.session_ttl_seconds
        now = datetime.now(timezone.utc)
        webdb.prune_auth_sessions(conn, before=webdb.iso(now - timedelta(days=7)))
        session_id = webdb.create_auth_session(
            conn, user_id=user_id, expires_at=webdb.iso(now + timedelta(seconds=ttl))
        )
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_token(user_id, session_id, settings.secret_key, ttl,
                             now=int(now.timestamp())),
            max_age=ttl if remember else None,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
            path="/",
        )

    def dataset_labels(owner_id: str) -> dict[str, str]:
        return {
            item.record.dataset_id: _dataset_label(item.record)
            for item in list_accessible_datasets(owner_id, shared_ids)
        }

    # ------------------------------------------------------------------ auth

    @app.get("/api/auth-config")
    def auth_config():
        return {"registration": settings.registration_mode}

    @app.post("/api/register", status_code=201)
    def register(payload: RegisterPayload, request: Request, response: Response):
        if settings.registration_mode == "closed":
            raise _error(403, "registration_closed", "Hiện chưa mở đăng ký tài khoản mới.")
        # Every attempt counts, before any hashing: sign-up cannot be used to
        # burn CPU on Argon2 or to mint accounts in bulk.
        admit_attempt([(register_ip, client_address(request))])
        try:
            email = auth.normalize_email(payload.email)
        except ValueError:
            raise _error(422, "invalid_email", "Email không hợp lệ.") from None
        if not auth.is_deliverable_email(email):
            raise _error(422, "invalid_email", "Email không hợp lệ.")
        name = " ".join(payload.name.split())
        if not name:
            raise _error(422, "invalid_name", "Vui lòng nhập họ tên.")
        invite_hash = ""
        if settings.registration_mode == "invite":
            # The invite is checked before the email: without a valid invite
            # for that address, nothing reveals whether it has an account.
            invite_hash = auth.hash_invite_code(payload.invite_code)
            with database.read() as conn:
                invite = webdb.get_usable_invite(conn, invite_hash) if payload.invite_code.strip() else None
            if invite is None or invite["email"] != email:
                raise _error(403, "invalid_invite", _INVALID_INVITE)
        password_hash = auth.hash_password(payload.password)
        with database.transaction() as conn:
            invite = webdb.get_usable_invite(conn, invite_hash) if invite_hash else None
            if invite_hash and (invite is None or invite["email"] != email):
                raise _error(403, "invalid_invite", _INVALID_INVITE)
            if webdb.get_user_by_email(conn, email) is not None:
                raise _error(409, "registration_failed", _REGISTRATION_FAILED)
            user = webdb.create_user(conn, name=name, email=email, password_hash=password_hash)
            if invite is not None and not webdb.use_invite(conn, invite["id"], user["id"]):
                raise _error(403, "invalid_invite", _INVALID_INVITE)
            start_session(conn, response, user["id"], payload.remember)
        return {"user": _user_out(user)}

    @app.post("/api/login")
    def login(payload: LoginPayload, request: Request, response: Response):
        try:
            email = auth.normalize_email(payload.email)
        except ValueError:
            email = ""
        # Unknown and malformed emails are limited exactly like real accounts,
        # so the limit itself reveals nothing about which accounts exist.
        account = email or payload.email.strip().lower()
        attempts = admit_attempt([(login_account, account), (login_ip, client_address(request))])
        with database.read() as conn:
            user = webdb.get_user_by_email(conn, email) if email else None
        if not auth.verify_password(payload.password, user["password_hash"] if user else None):
            raise _error(401, "invalid_credentials", "Email hoặc mật khẩu không đúng.")
        with database.transaction() as conn:
            # Only failures count toward the limits.
            throttle.forget(conn, attempts)
            throttle.clear(conn, login_account, account)
            start_session(conn, response, user["id"], payload.remember)
        return {"user": _user_out(user)}

    @app.post("/api/logout")
    def logout(request: Request, response: Response):
        claims = auth.decode_token(request.cookies.get(auth.SESSION_COOKIE), settings.secret_key)
        if claims is not None:
            # Revoked server-side: a copy of this cookie stops working too.
            with database.transaction() as conn:
                webdb.revoke_auth_session(conn, claims.session_id, claims.user_id)
        response.delete_cookie(
            auth.SESSION_COOKIE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
        )
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return {
            "user": _user_out(user),
            "features": {
                "report_discovery": settings.report_discovery,
                "chat_context": settings.chat_context,
            },
        }

    # ---------------------------------------------------------------- health

    @app.get("/api/health")
    def health():
        from datetime import datetime, timezone

        from schemas.chat_context import CONTEXT_CONTRACT_VERSION
        from vectorstore.qdrant_store import ensure_multiprocess_qdrant_backend

        with database.read() as conn:
            beats = webdb.list_heartbeats(conn)
        now = datetime.now(timezone.utc)
        workers = {queue: {"status": "missing"} for queue in ("chat", "import")}
        for beat in beats:
            try:
                age = (now - datetime.fromisoformat(beat["heartbeat_at"])).total_seconds()
            except ValueError:
                continue
            workers[beat["queue"]] = {
                "status": "alive" if age <= settings.heartbeat_stale_seconds else "stale",
                "seconds_since_heartbeat": round(max(age, 0.0), 1),
            }
            try:
                runtime = json.loads(beat["runtime_config"] or "{}")
            except (TypeError, ValueError):
                runtime = {}
            if isinstance(runtime, dict):
                # Publish only feature booleans and the code contract, not env.
                workers[beat["queue"]]["config"] = {
                    name: value for name, value in runtime.items()
                    if (name in {"chat_context", "report_discovery", "web_analysis"} and isinstance(value, bool))
                    or (name == "context_contract_version" and isinstance(value, str) and len(value) < 80)
                }
        try:
            ensure_multiprocess_qdrant_backend()
            qdrant = "configured"
        except RuntimeError:
            qdrant = "invalid"
        # Configuration shape only: nothing here claims a provider is healthy.
        chat_worker = workers["chat"]
        worker_context = chat_worker.get("config", {}).get("chat_context")
        worker_contract = chat_worker.get("config", {}).get("context_contract_version")
        synchronized = None
        if chat_worker["status"] == "alive" and isinstance(worker_context, bool):
            synchronized = (worker_context == settings.chat_context
                            and worker_contract == CONTEXT_CONTRACT_VERSION)
        return {
            "api": "ok",
            "workers": workers,
            "config": {"qdrant_backend": qdrant, "shared_datasets": len(shared_ids),
                       "chat_context": settings.chat_context,
                       "context_contract_version": CONTEXT_CONTRACT_VERSION},
            "context_configuration_in_sync": synchronized,
        }

    # -------------------------------------------------------------- datasets

    @app.get("/api/datasets")
    def datasets(user=Depends(current_user)):
        return [_dataset_out(item) for item in list_accessible_datasets(user["id"], shared_ids)]

    # -------------------------------------------------------------- sessions

    @app.get("/api/sessions")
    def list_sessions(user=Depends(current_user)):
        labels = dataset_labels(user["id"])
        with database.read() as conn:
            rows = webdb.list_sessions(conn, user["id"])
        return [_session_out(row, labels) for row in rows]

    @app.post("/api/sessions", status_code=201)
    def create_session(payload: CreateSessionPayload, user=Depends(current_user)):
        if payload.dataset_id is None:
            # A conversation for a PDF upload: no dataset until its import is ready.
            with database.transaction() as conn:
                row = webdb.create_session(conn, owner_id=user["id"], title=UPLOAD_SESSION_TITLE)
            return _session_out(row, {})
        try:
            accessible = get_accessible_dataset(payload.dataset_id, user["id"], shared_ids)
        except DatasetAccessError:
            raise _error(404, "dataset_not_found", "Không tìm thấy báo cáo.") from None
        record = accessible.record
        try:
            generation = inspect_generation(record)
        except DatasetNotReady as exc:
            raise _error(409, exc.code, str(exc)) from None
        label = _dataset_label(record)
        with database.transaction() as conn:
            row = webdb.create_session(
                conn,
                owner_id=user["id"],
                title=label or "Cuộc trò chuyện mới",
                dataset_id=record.dataset_id,
                dataset_generation=generation,
            )
        return _session_out(row, {record.dataset_id: label})

    @app.get("/api/sessions/{session_id}/messages")
    def session_messages(session_id: str, user=Depends(current_user)):
        with database.read() as conn:
            if webdb.get_owned_session(conn, session_id, user["id"]) is None:
                raise _error(404, "session_not_found", "Không tìm thấy cuộc trò chuyện.")
            rows = webdb.list_messages(conn, session_id)
        return [_message_out(row) for row in rows]

    @app.patch("/api/sessions/{session_id}")
    def rename_session(session_id: str, payload: RenameSessionPayload, user=Depends(current_user)):
        title = " ".join(payload.title.split())
        if not title:
            raise _error(422, "invalid_title", "Tên cuộc trò chuyện không được để trống.")
        with database.transaction() as conn:
            if not webdb.rename_owned_session(conn, session_id, user["id"], title):
                raise _error(404, "session_not_found", "Không tìm thấy cuộc trò chuyện.")
            row = webdb.get_owned_session(conn, session_id, user["id"])
        return _session_out(row, dataset_labels(user["id"]))

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str, user=Depends(current_user)):
        # Deleting a session never deletes its dataset.
        with database.transaction() as conn:
            if not webdb.delete_owned_session(conn, session_id, user["id"]):
                raise _error(404, "session_not_found", "Không tìm thấy cuộc trò chuyện.")
        return {"deleted": True}

    # ------------------------------------------------------------------ chat

    def mark_read_only(session_id: str, reason: str) -> None:
        with database.transaction() as conn:
            webdb.mark_session_read_only(conn, session_id, reason)

    def check_session_dataset(session: Any, user: Any) -> None:
        # Re-check access and generation before accepting work: the dataset may
        # have been rebuilt or unshared since the session was created.
        try:
            accessible = get_accessible_dataset(session["dataset_id"], user["id"], shared_ids)
        except DatasetAccessError:
            mark_read_only(session["id"], "dataset_unavailable")
            raise _error(409, "dataset_unavailable", READ_ONLY_TEXT["dataset_unavailable"]) from None
        try:
            generation = inspect_generation(accessible.record)
        except DatasetNotReady as exc:
            if exc.code in {"artifact_missing", "kb_invalid", "index_missing"}:
                mark_read_only(session["id"], exc.code)
            raise _error(409, exc.code, str(exc)) from None
        if generation != session["dataset_generation"]:
            mark_read_only(session["id"], "dataset_changed")
            raise _error(409, "dataset_changed", READ_ONLY_TEXT["dataset_changed"])

    @app.post("/api/chat", status_code=202)
    def chat(payload: ChatPayload, user=Depends(current_user)):
        key = payload.idempotency_key.strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            raise _error(422, "invalid_idempotency_key", "Khoá gửi tin nhắn không hợp lệ.")
        question = payload.message.strip()
        if not question:
            raise _error(422, "empty_message", "Vui lòng nhập câu hỏi.")

        with database.read() as conn:
            session = webdb.get_owned_session(conn, payload.session_id, user["id"])
            if session is None:
                raise _error(404, "session_not_found", "Không tìm thấy cuộc trò chuyện.")
            replay = webdb.find_run_by_key(conn, session["id"], key)
            if replay is not None:
                return {**_run_out(conn, replay), "replayed": True}
        if session["read_only"]:
            raise _error(409, "session_read_only", READ_ONLY_TEXT.get(session["read_only_reason"], "Cuộc trò chuyện chỉ còn để xem lại."))
        if session["dataset_id"]:
            check_session_dataset(session, user)
        elif not settings.report_discovery:
            raise _error(409, "session_without_dataset", "Hãy chọn báo cáo trước khi hỏi.")
        # With discovery on, a session without a report still takes messages:
        # the worker decides whether it asks to find a report, and answers a
        # financial question only with a "choose a report" card.

        from acquisition.quota import QuotaExceeded

        # BEGIN IMMEDIATE serializes every chat submission, so a duplicate key
        # racing this request cannot also consume quota or insert a message.
        with database.transaction() as conn:
            replay = webdb.find_run_by_key(conn, session["id"], key)
            if replay is not None:
                return {**_run_out(conn, replay), "replayed": True}
            if webdb.get_active_run(conn, session["id"]) is not None:
                raise _error(409, "run_in_progress", "Câu hỏi trước vẫn đang được xử lý.")
            try:
                admit_question(user["id"])
            except QuotaExceeded as exc:
                raise _error(429, exc.code, "Đã hết lượt hỏi theo giới hạn hiện tại.") from None
            first_question = webdb.count_user_messages(conn, session["id"]) == 0
            message_id = webdb.add_message(
                conn, session_id=session["id"], role="user", content=question
            )
            run = webdb.create_run(
                conn,
                session_id=session["id"],
                owner_id=user["id"],
                idempotency_key=key,
                question=question,
                question_message_id=message_id,
                dataset_id=session["dataset_id"],
                dataset_generation=session["dataset_generation"],
            )
            webdb.touch_session(conn, session["id"], title=_derive_title(question) if first_question else None)
            return {**_run_out(conn, run), "replayed": False}

    @app.post("/api/chat-runs/{run_id}/answer-with-report", status_code=202)
    def answer_with_report(run_id: str, payload: AnswerWithReportPayload, user=Depends(current_user)):
        """The user's explicit override when a question was taken as a
        request to find a report: answer it on the report this conversation
        has. Linked to the original run; no second question quota."""

        if not settings.report_discovery:
            raise _error(404, "report_discovery_disabled", "Tính năng tìm báo cáo đang tắt.")
        key = payload.idempotency_key.strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            raise _error(422, "invalid_idempotency_key", "Khoá gửi yêu cầu không hợp lệ.")
        with database.read() as conn:
            original = webdb.get_owned_run(conn, run_id, user["id"])
            session = (
                webdb.get_owned_session(conn, original["session_id"], user["id"]) if original else None
            )
            if original is None or session is None:
                raise _error(404, "run_not_found", "Không tìm thấy yêu cầu.")
            replay = webdb.find_run_by_key(conn, session["id"], key)
            if replay is not None:
                return {**_run_out(conn, replay), "replayed": True}
        if original["task_kind"] != "report_discovery":
            raise _error(409, "not_a_discovery_run", "Câu hỏi này đã được trả lời trên báo cáo.")
        if session["read_only"]:
            raise _error(409, "session_read_only", READ_ONLY_TEXT.get(session["read_only_reason"], "Cuộc trò chuyện chỉ còn để xem lại."))
        if not session["dataset_id"]:
            raise _error(409, "session_without_dataset", "Cuộc trò chuyện này chưa có báo cáo.")
        check_session_dataset(session, user)
        with database.transaction() as conn:
            replay = webdb.find_run_by_key(conn, session["id"], key)
            if replay is not None:
                return {**_run_out(conn, replay), "replayed": True}
            if webdb.get_active_run(conn, session["id"]) is not None:
                raise _error(409, "run_in_progress", "Câu hỏi trước vẫn đang được xử lý.")
            run = webdb.create_run(
                conn,
                session_id=session["id"],
                owner_id=user["id"],
                idempotency_key=key,
                question=original["question"],
                question_message_id=original["question_message_id"],
                dataset_id=session["dataset_id"],
                dataset_generation=session["dataset_generation"],
                override_of_run_id=original["id"],
                link_question_message=False,
            )
            # Model usage is still recorded; the question quota is not charged twice.
            record_usage(user["id"], "qa_override")
            return {**_run_out(conn, run), "replayed": False}

    @app.get("/api/chat-runs/{run_id}")
    def chat_run(run_id: str, user=Depends(current_user)):
        with database.read() as conn:
            run = webdb.get_owned_run(conn, run_id, user["id"])
            if run is None:
                raise _error(404, "run_not_found", "Không tìm thấy yêu cầu.")
            return _run_out(conn, run)

    # --------------------------------------------------------------- imports

    register_import_routes(
        app,
        database=database,
        current_user=current_user,
        get_service=get_import_service,
        upload_dir=default_upload_dir(),
        report_discovery=settings.report_discovery,
    )
    register_discovery_routes(
        app,
        database=database,
        current_user=current_user,
        get_service=get_import_service,
        report_discovery=settings.report_discovery,
    )

    # ---------------------------------------------------------------- static

    def static_file(name: str) -> FileResponse:
        return FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-cache"})

    def page(name: str):
        def handler():
            return static_file(name)

        return handler

    for route, name in _PAGES.items():
        app.add_api_route(route, page(name), methods=["GET"], include_in_schema=False)

    @app.get("/vendor/{asset}", include_in_schema=False)
    def vendor_asset(asset: str):
        if asset not in _VENDOR:
            raise HTTPException(status_code=404)
        return static_file(f"vendor/{asset}")

    @app.get("/{asset}", include_in_schema=False)
    def asset_file(asset: str):
        if asset not in _ASSETS:
            raise HTTPException(status_code=404)
        return static_file(asset)

    return app


# Referenced by tests to keep the worker's user-facing texts and the API's in sync.
__all__ = ["create_app", "READ_ONLY_TEXT", "SAFE_MESSAGES"]
