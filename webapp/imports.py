"""PDF upload and report import for the local web app, and the import worker.

Security boundaries:

* the owner always comes from the session cookie; a client can never send an
  owner, a server path, usage figures, a provider job ID or report metadata
  for a CafeF candidate — the request models forbid unknown fields;
* uploads are private: staged under an owner-hashed directory, imported into
  the owner's dataset namespace and cached per owner;
* cloud parsing needs explicit consent, stored on the job (default: no);
* responses never contain server paths or raw provider/exception text.

The API only validates, reserves and records a job (through the confirmation
hand-off in ``webapp.handoff``). Downloading, parsing and index building run in
``agentfinx worker --queue import``.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from acquisition.download import PdfValidationError
from acquisition.quota import QuotaExceeded
from acquisition.service import IdempotencyConflict
from acquisition.store import CandidateAccessError
from webapp import db as webdb
from webapp.handoff import HandoffError, HandoffRequest, confirm_import, recover_handoffs

logger = logging.getLogger("agentfinx.web.imports")

_HEX_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_PDF_MIME_TYPES = frozenset({"application/pdf", "application/octet-stream", ""})
UPLOAD_SESSION_TITLE = "Báo cáo tải lên"
HANDOFF_CHECK_SECONDS = 30
CATALOG_CHECK_SECONDS = 600

GENERIC_IMPORT_ERROR = "Không xử lý được báo cáo này."
IMPORT_ERROR_TEXT = {
    "empty_file": "File tải lên rỗng.",
    "file_too_large": "PDF vượt quá giới hạn dung lượng.",
    "invalid_pdf_magic": "File tải lên không phải PDF.",
    "invalid_pdf": "File PDF bị lỗi cấu trúc trang.",
    "empty_pdf": "PDF không có trang nào.",
    "too_many_pages": "PDF vượt quá giới hạn số trang.",
    "invalid_content_type": "Nguồn không trả về file PDF.",
    "redirect_not_allowed": "Nguồn báo cáo chuyển hướng tới địa chỉ không được phép.",
    "source_not_allowed": "Nguồn báo cáo không được phép.",
    "cloud_consent_required": (
        "Không đọc được lớp văn bản của PDF này. Hãy thử lại và đồng ý gửi PDF tới "
        "dịch vụ trích xuất đám mây (LlamaParse)."
    ),
    "conversion_quality_failed": (
        "Nội dung trích xuất không đạt kiểm tra chất lượng nên báo cáo không được đưa vào."
    ),
    "remote_id_lost": "Quá trình trích xuất bị gián đoạn; chi phí đang được đối soát.",
    "provider_lifecycle_missing": "Dịch vụ trích xuất chưa được cấu hình đúng.",
    "session_unavailable": "Cuộc trò chuyện đích không còn; báo cáo không được xử lý.",
    "handoff_interrupted": "Yêu cầu tải báo cáo bị gián đoạn trước khi bắt đầu; không phát sinh chi phí.",
}
QUOTA_TEXT = {
    "import_billing_pending": (
        "Báo cáo trước vẫn đang chờ xác nhận chi phí xử lý. Bạn vẫn hỏi được trên báo cáo "
        "đó; lần tải lên tiếp theo sẽ mở khi chi phí được xác nhận."
    ),
    "owner_concurrency": "Bạn đang có một báo cáo khác đang được xử lý.",
    "trial_import_limit": "Đã dùng hết lượt tải báo cáo của thời gian dùng thử.",
    "trial_expired": "Thời gian dùng thử đã kết thúc.",
    "too_many_pages": "PDF vượt quá giới hạn số trang.",
    "global_provider_lock": "Ngân sách xử lý tài liệu đã hết; vui lòng thử lại sau.",
    "global_trial_lock": "Ngân sách xử lý tài liệu tạm thời đã hết; vui lòng thử lại sau.",
}
CANDIDATE_ERROR_TEXT = {
    "candidate_expired": "Kết quả tìm kiếm này đã hết hạn; hãy tìm lại.",
    "candidate_not_found": "Không tìm thấy báo cáo đã chọn; hãy tìm lại.",
    "candidate_owner_mismatch": "Không tìm thấy báo cáo đã chọn; hãy tìm lại.",
}
IDEMPOTENCY_TEXT = {
    "request_mismatch": "Yêu cầu này đã được gửi với lựa chọn khác.",
    "legacy_identity": (
        "Mã yêu cầu này thuộc một lần tải cũ không đủ thông tin đối chiếu; "
        "hãy xem lại báo cáo đó thay vì gửi lại."
    ),
}


def dataset_label(record: Any) -> str:
    if record is None:
        return ""
    period = str(record.fiscal_year or "")
    if record.fiscal_quarter:
        period = f"Q{record.fiscal_quarter}/{period}" if period else f"Q{record.fiscal_quarter}"
    ticker = f" ({record.ticker})" if record.ticker else ""
    return f"{record.company}{ticker}" + (f" · {period}" if period else "")


def owner_namespace(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]


def default_upload_dir() -> Path:
    # Under dataset_store/, which is git-ignored runtime storage.
    from dataset_catalog import registry

    return Path(registry.DATASETS_DIR) / "acquisition" / "web-uploads"


def default_catalog_path() -> Path:
    from dataset_catalog import registry

    return Path(registry.DATASETS_DIR) / "web" / "cafef_symbols.json"


class ImportMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str = Field(min_length=1, max_length=160)
    ticker: str = Field(default="", max_length=16, pattern=r"^[A-Za-z0-9.]*$")
    fiscal_year: Optional[int] = Field(default=None, ge=1990, le=2100)
    fiscal_quarter: Optional[int] = Field(default=None, ge=1, le=4)
    scope: Literal["consolidated", "separate", "unknown"] = "unknown"
    audit_status: Literal["audited", "reviewed", "unaudited", "unknown"] = "unknown"

    def as_job_metadata(self) -> dict:
        return {
            **self.model_dump(),
            "company": " ".join(self.company.split()),
            "ticker": self.ticker.strip().upper(),
            "report_type": "financial_statement",
        }


class ImportPayload(BaseModel):
    """Exactly one source: an ``upload_id`` with its metadata, or a
    ``confirmed_candidate_id`` (whose metadata comes from the server)."""

    model_config = ConfigDict(extra="forbid")

    session_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    upload_id: Optional[str] = Field(default=None, min_length=32, max_length=32)
    confirmed_candidate_id: Optional[str] = Field(default=None, min_length=32, max_length=32)
    idempotency_key: str = Field(min_length=8, max_length=128)
    allow_cloud_parse: bool = False
    metadata: Optional[ImportMetadata] = None


def import_job_out(job: Any) -> dict:
    """What the browser may see of an import job: no paths, no raw errors."""

    failed = job.status == "failed"
    known_error = failed and job.error_code in IMPORT_ERROR_TEXT
    return {
        "job_id": job.job_id,
        "session_id": job.session_id,
        "origin": job.origin,
        "status": job.status,
        "billing_status": job.billing_status,
        "billing_pending": job.billing_pending,
        "billing_blocking": job.billing_pending and job.billing_blocking,
        "estimated_credits": job.estimated_credits,
        "confirmed_credits": job.confirmed_credits,
        "page_count": job.page_count,
        "allow_cloud_parse": job.allow_cloud_parse,
        "dataset_id": job.dataset_id if job.status == "ready" else "",
        "session_attached": job.session_attached,
        "error_code": (job.error_code if known_error else "import_failed") if failed else "",
        "error_message": (
            IMPORT_ERROR_TEXT.get(job.error_code, GENERIC_IMPORT_ERROR) if failed else ""
        ),
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _candidate_error(exc: CandidateAccessError) -> HTTPException:
    code = str(exc)
    status = 410 if code == "candidate_expired" else 404
    public = "candidate_expired" if code == "candidate_expired" else "candidate_not_found"
    return _error(status, public, CANDIDATE_ERROR_TEXT.get(code, CANDIDATE_ERROR_TEXT["candidate_not_found"]))


def build_web_import_service(*, dataset_attacher: Optional[Callable[[str, str, str], None]] = None):
    from acquisition import build_default_acquisition_service
    from config.runtime_policy import RuntimePolicy

    # The converter's own default is "no consent": every job carries its own.
    service = build_default_acquisition_service(RuntimePolicy.from_env(), allow_cloud_parse=False)
    service.dataset_attacher = dataset_attacher
    return service


def make_session_attacher(
    database: webdb.WebDB,
    *,
    generation_inspector: Optional[Callable[[Any], str]] = None,
) -> Callable[[str, str, str], None]:
    """Attach a finished import's dataset to the session it was imported into."""

    from dataset_catalog.access import get_owned_dataset
    from dataset_catalog.activation import inspect_dataset_generation

    inspect = generation_inspector or inspect_dataset_generation

    def attach(owner_id: str, session_id: str, dataset_id: str) -> None:
        record = get_owned_dataset(dataset_id, owner_id)
        if record is None:
            raise LookupError("imported dataset is not owned by the session owner")
        generation = inspect(record)  # read-only; no DB transaction held here
        with database.transaction() as conn:
            attached = webdb.attach_session_dataset(
                conn,
                session_id=session_id,
                owner_id=owner_id,
                dataset_id=record.dataset_id,
                dataset_generation=generation,
                title=dataset_label(record) or None,
            )
        if not attached:
            raise LookupError("session is gone or already has a dataset")

    return attach


def build_import_handler(
    database: webdb.WebDB,
    *,
    service: Any = None,
    generation_inspector: Optional[Callable[[Any], str]] = None,
    catalog_path: Optional[Path] = None,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[webdb.WebDB, str], bool]:
    """Return the import queue's work step, after recovering interrupted jobs.

    Each step also finishes or undoes stale confirmation hand-offs and, when
    ``catalog_path`` is given (report discovery on), refreshes a stale symbol
    catalog — both throttled."""

    if service is None:
        service = build_web_import_service(
            dataset_attacher=make_session_attacher(
                database, generation_inspector=generation_inspector
            )
        )
    for job in service.recover_interrupted_jobs():
        logger.warning("recovered interrupted import %s -> %s", job.job_id, job.status)
    due = {"handoffs": 0.0, "catalog": 0.0}

    def housekeeping() -> None:
        now = clock()
        if now >= due["handoffs"]:
            due["handoffs"] = now + HANDOFF_CHECK_SECONDS
            try:
                recover_handoffs(database, service)
            except Exception:
                logger.exception("import hand-off recovery failed")
        if catalog_path is not None and now >= due["catalog"]:
            due["catalog"] = now + CATALOG_CHECK_SECONDS
            from web_evidence.symbols import refresh_catalog_if_due

            result = refresh_catalog_if_due(catalog_path)
            if result:
                logger.info("symbol catalog refresh: %s", result)

    def handle(_database: webdb.WebDB, _worker_id: str) -> bool:
        housekeeping()
        job = service.store.next_queued_job()
        if job is None:
            return False
        try:
            result = service.run_job(job.job_id)
        except Exception:
            logger.exception("import %s could not be run", job.job_id)
            return False
        logger.info(
            "import %s finished: %s (billing %s)", job.job_id, result.status, result.billing_status
        )
        return True

    return handle


def register_import_routes(
    app: FastAPI,
    *,
    database: webdb.WebDB,
    current_user: Callable[..., Any],
    get_service: Callable[[], Any],
    upload_dir: Path,
    report_discovery: bool = False,
) -> None:
    uploads_root = Path(upload_dir)

    @app.post("/api/report-uploads", status_code=201)
    async def upload_report(file: UploadFile = File(...), user=Depends(current_user)):
        max_bytes = int(get_service().policy.max_file_bytes)
        mime = str(file.content_type or "").split(";", 1)[0].strip().lower()
        name = str(file.filename or "")
        if mime not in _PDF_MIME_TYPES or (name and not name.lower().endswith(".pdf")):
            await file.close()
            raise _error(422, "invalid_content_type", "Chỉ nhận file PDF.")
        upload_id = uuid.uuid4().hex
        directory = uploads_root / owner_namespace(user["id"])
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{upload_id}.", suffix=".tmp", dir=str(directory)
        )
        temporary_path = Path(temporary_name)
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                while True:
                    chunk = await file.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > max_bytes:
                        raise _error(
                            413,
                            "file_too_large",
                            f"PDF vượt quá giới hạn {max_bytes // (1024 * 1024)} MB.",
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            with temporary_path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise _error(422, "invalid_pdf_magic", IMPORT_ERROR_TEXT["invalid_pdf_magic"])
            os.replace(temporary_path, directory / f"{upload_id}.pdf")
            return {"upload_id": upload_id, "size_bytes": size}
        finally:
            temporary_path.unlink(missing_ok=True)
            await file.close()

    @app.post("/api/report-imports", status_code=202)
    def create_import(payload: ImportPayload, user=Depends(current_user)):
        owner_id = user["id"]
        key = payload.idempotency_key.strip()
        if not _IDEMPOTENCY_RE.fullmatch(key):
            raise _error(422, "invalid_idempotency_key", "Khoá gửi yêu cầu không hợp lệ.")
        if (payload.upload_id is None) == (payload.confirmed_candidate_id is None):
            raise _error(422, "one_source_required", "Hãy chọn đúng một nguồn báo cáo.")
        service = get_service()
        with database.read() as conn:
            prior = webdb.get_handoff(conn, owner_id, key)
        upload_path: Optional[Path] = None

        if payload.confirmed_candidate_id is not None:
            if not report_discovery:
                raise _error(404, "report_discovery_disabled", "Tính năng tìm báo cáo đang tắt.")
            if payload.metadata is not None:
                raise _error(422, "candidate_metadata_not_allowed",
                             "Thông tin báo cáo lấy từ kết quả tìm kiếm, không gửi kèm.")
            if not payload.session_id:
                raise _error(422, "session_required", "Thiếu cuộc trò chuyện chứa kết quả tìm kiếm.")
            candidate_id = payload.confirmed_candidate_id
            if not _HEX_ID_RE.fullmatch(candidate_id):
                raise _error(404, "candidate_not_found", CANDIDATE_ERROR_TEXT["candidate_not_found"])
            title = "Báo cáo CafeF"
            if prior is None:
                # Checked up front so an expired or foreign candidate never
                # provisions a session. A replay skips it: an accepted request
                # returns its job even after the candidate expired.
                try:
                    candidate = service.get_candidate(
                        candidate_id, owner_id=owner_id, session_id=payload.session_id
                    )
                except CandidateAccessError as exc:
                    raise _candidate_error(exc) from None
                title = f"{candidate.ticker} · {candidate.title}"[:80]
            request = HandoffRequest(
                owner_id=owner_id,
                idempotency_key=key,
                source_kind="candidate",
                source_id=candidate_id,
                source_session_id=payload.session_id,
                allow_cloud_parse=payload.allow_cloud_parse,
                new_session_title=title,
            )

            def create_job(target_session_id: str):
                return service.create_candidate_import(
                    owner_id=owner_id,
                    session_id=target_session_id,
                    candidate_id=candidate_id,
                    idempotency_key=key,
                    allow_cloud_parse=payload.allow_cloud_parse,
                    source_session_id=payload.session_id,
                    hold=True,
                )
        else:
            upload_id = payload.upload_id
            if payload.metadata is None:
                raise _error(422, "metadata_required", "Hãy nhập thông tin báo cáo.")
            if not _HEX_ID_RE.fullmatch(upload_id):
                raise _error(404, "upload_not_found", "Không tìm thấy file đã tải lên; vui lòng tải lại.")
            metadata = payload.metadata.as_job_metadata()
            upload_path = uploads_root / owner_namespace(owner_id) / f"{upload_id}.pdf"
            if prior is None and not upload_path.is_file():
                raise _error(404, "upload_not_found", "Không tìm thấy file đã tải lên; vui lòng tải lại.")
            request = HandoffRequest(
                owner_id=owner_id,
                idempotency_key=key,
                source_kind="upload",
                source_id=upload_id,
                source_session_id=payload.session_id,
                allow_cloud_parse=payload.allow_cloud_parse,
                metadata=metadata,
                new_session_title=UPLOAD_SESSION_TITLE,
            )

            def create_job(target_session_id: str):
                return service.create_upload_import(
                    owner_id=owner_id,
                    session_id=target_session_id,
                    upload_path=upload_path,
                    idempotency_key=key,
                    metadata=metadata,
                    allow_cloud_parse=payload.allow_cloud_parse,
                    source_id=f"web-upload:{upload_id}",
                    source_session_id=payload.session_id,
                    hold=True,
                )

        try:
            result = confirm_import(database, service, request, create_job)
        except HandoffError as exc:
            raise _error(exc.status, exc.code, exc.message) from None
        except QuotaExceeded as exc:
            raise _error(429, exc.code, QUOTA_TEXT.get(exc.code, "Đã vượt giới hạn tải báo cáo.")) from None
        except PdfValidationError as exc:
            raise _error(422, exc.code, IMPORT_ERROR_TEXT.get(exc.code, GENERIC_IMPORT_ERROR)) from None
        except CandidateAccessError as exc:
            raise _candidate_error(exc) from None
        except IdempotencyConflict as exc:
            raise _error(409, "idempotency_conflict", IDEMPOTENCY_TEXT.get(exc.code, IDEMPOTENCY_TEXT["request_mismatch"])) from None
        if upload_path is not None:
            # The job works from its own content-addressed staged copy.
            upload_path.unlink(missing_ok=True)
        return {
            **import_job_out(result.job),
            "target_session_id": result.target_session_id,
            "created_session": result.created_session,
            "replayed": result.replayed,
        }

    @app.get("/api/report-imports/{job_id}")
    def get_import(job_id: str, user=Depends(current_user)):
        job = None
        if _HEX_ID_RE.fullmatch(job_id):
            job = get_service().store.get_job(job_id, owner_id=user["id"])
        if job is None:
            raise _error(404, "import_not_found", "Không tìm thấy yêu cầu tải báo cáo.")
        return import_job_out(job)
