"""Optional FastAPI surface for report discovery and queued imports.

The app factory deliberately receives services and a FastAPI-compatible owner
dependency. The dependency must derive ownership from a trusted session or
token; this module never accepts a client-supplied owner ID as authority.
Import work is dispatched to a worker callback, so API processes never run
LlamaParse or build Qdrant collections inline.
"""

import hashlib
import os
import tempfile
import uuid
from pathlib import Path
from typing import Annotated, Callable

from pydantic import BaseModel, Field

try:
    from fastapi import (Depends, FastAPI, File, Header, HTTPException,
                         UploadFile)
except ImportError:  # pragma: no cover - exercised only without the optional extra
    Depends = FastAPI = File = Header = HTTPException = UploadFile = None

from acquisition.download import PdfValidationError
from acquisition.quota import QuotaExceeded
from acquisition.service import ReportDiscoveryUnavailable
from acquisition.store import CandidateAccessError
from schemas.acquisition import ReportQuery


class ReportDiscoveryRequest(BaseModel):
    session_id: str
    ticker: str
    company: str = ""
    exchange: str = ""
    fiscal_year: int | None = None
    fiscal_quarter: int | None = Field(default=None, ge=1, le=4)
    report_type: str = "financial_statement"
    scope: str = ""
    audit_status: str = ""


class ReportImportRequest(BaseModel):
    session_id: str
    confirmed_candidate_id: str = ""
    upload_id: str = ""
    metadata: dict = Field(default_factory=dict)


def _owner_namespace(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]


def create_app(
    acquisition_service,
    *,
    upload_dir: str | Path,
    owner_resolver: Callable[..., str],
    dispatch_job: Callable[[str], None] | None = None,
    session_authorizer: Callable[[str, str], bool] | None = None,
):
    if FastAPI is None:
        raise RuntimeError("Web API requires: pip install -e '.[web]'")

    app = FastAPI(title="AgentFinX acquisition API", version="0.2.0")
    uploads_root = Path(upload_dir)

    def require_session(owner_id: str, session_id: str):
        if session_authorizer is None:
            raise HTTPException(status_code=503, detail="session authorization is not configured")
        if not session_authorizer(owner_id, session_id):
            raise HTTPException(status_code=404, detail="session not found")

    def require_owner(owner_id: str) -> str:
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise HTTPException(status_code=401, detail="authenticated owner is required")
        return owner_id

    def stage_path(owner_id: str, upload_id: str) -> Path:
        if not upload_id or any(char not in "0123456789abcdef" for char in upload_id):
            raise HTTPException(status_code=404, detail="upload not found")
        path = uploads_root / _owner_namespace(owner_id) / f"{upload_id}.pdf"
        if not path.exists():
            raise HTTPException(status_code=404, detail="upload not found")
        return path

    @app.post("/report-uploads")
    async def upload_report(
        file: Annotated[UploadFile, File()],
        authenticated_owner: Annotated[str, Depends(owner_resolver)],
    ):
        owner_id = require_owner(authenticated_owner)
        mime = str(file.content_type or "").split(";", 1)[0].strip().lower()
        if mime not in {"application/pdf", "application/octet-stream"}:
            await file.close()
            raise HTTPException(status_code=422, detail="invalid_content_type")
        upload_id = uuid.uuid4().hex
        directory = uploads_root / _owner_namespace(owner_id)
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
                    if size > acquisition_service.policy.max_file_bytes:
                        raise HTTPException(status_code=413, detail="file too large")
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            with temporary_path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise HTTPException(status_code=422, detail="invalid_pdf_magic")
            final_path = directory / f"{upload_id}.pdf"
            os.replace(temporary_path, final_path)
            return {"upload_id": upload_id, "size_bytes": size}
        finally:
            temporary_path.unlink(missing_ok=True)
            await file.close()

    @app.post("/report-discoveries")
    def discover_reports(
        request: ReportDiscoveryRequest,
        authenticated_owner: Annotated[str, Depends(owner_resolver)],
    ):
        owner_id = require_owner(authenticated_owner)
        require_session(owner_id, request.session_id)
        try:
            candidates = acquisition_service.discover(
                ReportQuery(owner_id=owner_id, **request.model_dump())
            )
        except ReportDiscoveryUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"candidates": [item.model_dump(mode="json") for item in candidates]}

    @app.post("/report-imports")
    def create_import(
        request: ReportImportRequest,
        authenticated_owner: Annotated[str, Depends(owner_resolver)],
        idempotency_key: Annotated[
            str,
            Header(alias="Idempotency-Key"),
        ] = "",
    ):
        owner_id = require_owner(authenticated_owner)
        require_session(owner_id, request.session_id)
        if dispatch_job is None:
            raise HTTPException(status_code=503, detail="worker dispatch is not configured")
        if bool(request.confirmed_candidate_id) == bool(request.upload_id):
            raise HTTPException(
                status_code=422,
                detail="provide exactly one of confirmed_candidate_id or upload_id",
            )
        try:
            if request.confirmed_candidate_id:
                job = acquisition_service.create_candidate_import(
                    owner_id=owner_id,
                    session_id=request.session_id,
                    candidate_id=request.confirmed_candidate_id,
                    idempotency_key=idempotency_key,
                )
            else:
                job = acquisition_service.create_upload_import(
                    owner_id=owner_id,
                    session_id=request.session_id,
                    upload_path=stage_path(owner_id, request.upload_id),
                    idempotency_key=idempotency_key,
                    metadata=request.metadata,
                )
        except CandidateAccessError as exc:
            code = str(exc)
            status_code = 410 if code == "candidate_expired" else 404
            raise HTTPException(status_code=status_code, detail=code) from exc
        except QuotaExceeded as exc:
            raise HTTPException(status_code=429, detail=exc.code) from exc
        except PdfValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.code) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if dispatch_job is not None and job.status == "queued":
            dispatch_job(job.job_id)
        return job.model_dump(mode="json")

    @app.get("/report-imports/{job_id}")
    def get_import(
        job_id: str,
        authenticated_owner: Annotated[str, Depends(owner_resolver)],
    ):
        owner_id = require_owner(authenticated_owner)
        job = acquisition_service.store.get_job(job_id, owner_id=owner_id)
        if job is None:
            raise HTTPException(status_code=404, detail="import job not found")
        return job.model_dump(mode="json")

    return app
