import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("multipart")

from fastapi import Header, HTTPException

from schemas.acquisition import ImportJob
from src.agentfinx.web_api import create_app


class _JobStore:
    def __init__(self):
        self.jobs = {}

    def get_job(self, job_id, *, owner_id=""):
        job = self.jobs.get(job_id)
        if job is None or (owner_id and job.owner_id != owner_id):
            return None
        return job


class _Service:
    def __init__(self):
        self.policy = SimpleNamespace(max_file_bytes=1024)
        self.store = _JobStore()

    def discover(self, query):
        return []

    def create_upload_import(
        self,
        *,
        owner_id,
        session_id,
        upload_path,
        idempotency_key,
        metadata,
    ):
        assert Path(upload_path).read_bytes().startswith(b"%PDF-")
        assert idempotency_key == "request-1"
        job = ImportJob(
            job_id="job-1",
            owner_id=owner_id,
            session_id=session_id,
            origin="upload",
            upload_path=str(upload_path),
            metadata=metadata,
        )
        self.store.jobs[job.job_id] = job
        return job


def _owner_from_token(authorization: Annotated[str, Header()] = "") -> str:
    owners = {"Bearer token-a": "owner-a", "Bearer token-b": "owner-b"}
    owner = owners.get(authorization)
    if not owner:
        raise HTTPException(status_code=401, detail="invalid session")
    return owner


def test_report_import_api_uses_authenticated_owner_and_dispatches(tmp_path: Path):
    service = _Service()
    dispatched = []
    app = create_app(
        service,
        upload_dir=tmp_path / "uploads",
        owner_resolver=_owner_from_token,
        dispatch_job=dispatched.append,
        session_authorizer=lambda owner, session: (owner, session) == ("owner-a", "session-a"),
    )

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            uploaded = await client.post(
                "/report-uploads",
                headers={"Authorization": "Bearer token-a"},
                files={"file": ("report.pdf", b"%PDF-1.7\nfixture", "application/pdf")},
            )
            assert uploaded.status_code == 200
            upload_id = uploaded.json()["upload_id"]

            imported = await client.post(
                "/report-imports",
                headers={
                    "Authorization": "Bearer token-a",
                    "Idempotency-Key": "request-1",
                },
                json={
                    "session_id": "session-a",
                    "upload_id": upload_id,
                    "metadata": {"ticker": "VNM"},
                },
            )
            assert imported.status_code == 200
            assert imported.json()["owner_id"] == "owner-a"

            hidden = await client.get(
                "/report-imports/job-1",
                headers={"Authorization": "Bearer token-b"},
            )
            assert hidden.status_code == 404

    asyncio.run(exercise())
    assert dispatched == ["job-1"]


def test_report_import_api_requires_exactly_one_source(tmp_path: Path):
    app = create_app(
        _Service(),
        upload_dir=tmp_path / "uploads",
        owner_resolver=_owner_from_token,
        session_authorizer=lambda owner, session: (owner, session) == ("owner-a", "session-a"),
        dispatch_job=lambda _job_id: None,
    )

    async def exercise():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post(
                "/report-imports",
                headers={"Authorization": "Bearer token-a"},
                json={"session_id": "session-a"},
            )
            assert response.status_code == 422

    asyncio.run(exercise())


@pytest.mark.parametrize("authorized,dispatcher,status", [(None, True, 503), (False, True, 404), (True, False, 503)])
def test_api_fails_closed_without_session_access_or_dispatch(tmp_path, authorized, dispatcher, status):
    service = _Service()
    app = create_app(service, upload_dir=tmp_path, owner_resolver=_owner_from_token,
                     session_authorizer=None if authorized is None else lambda *_: authorized,
                     dispatch_job=(lambda _: None) if dispatcher else None)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/report-imports", headers={"Authorization": "Bearer token-a"},
                                         json={"session_id": "session-other", "upload_id": "a"})
            assert response.status_code == status
            assert not service.store.jobs
    asyncio.run(exercise())


def test_upload_rejects_html_disguised_as_pdf_without_publishing(tmp_path):
    app = create_app(_Service(), upload_dir=tmp_path, owner_resolver=_owner_from_token)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/report-uploads", headers={"Authorization": "Bearer token-a"},
                files={"file": ("fake.pdf", b"<html>not a PDF</html>", "application/pdf")})
            assert response.status_code == 422
            assert not list(tmp_path.rglob("*.pdf"))
            assert not list(tmp_path.rglob("*.tmp"))
    asyncio.run(exercise())
