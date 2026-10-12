"""Confirm an import across the web database and the acquisition database.

The two databases cannot share a transaction, so a confirmation is a small
durable state machine in ``import_handoffs``, keyed by (owner, idempotency key):

  started      the hand-off row exists; if the import needs a new conversation,
               that session exists with ``provisioning = 1`` (hidden from the
               history and from every user-facing lookup)
  job_created  the acquisition job exists with ``dispatch_hold`` set, so the
               import worker will not claim it
  linked       the session points at the job, provisioning is cleared, and the
               hold is released

Every step is idempotent. Replaying the same request, or recovery after a
crash, finishes the hand-off instead of creating a second job, session or
reservation. A hand-off whose job was never created is aborted and its
provisioning session removed. A session the user deleted is never recreated:
the held job is cancelled, which releases its reservation.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from agentfinx.api import db as webdb

logger = logging.getLogger("agentfinx.web.handoff")

# An API request finishes its hand-off in well under this; recovery only
# touches hand-offs older than it, so it never races a live request.
STALE_AFTER = timedelta(minutes=2)


class HandoffError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class HandoffRequest:
    owner_id: str
    idempotency_key: str
    source_kind: str  # "upload" | "candidate"
    source_id: str
    source_session_id: Optional[str]
    allow_cloud_parse: bool
    metadata: dict = field(default_factory=dict)
    new_session_title: str = "Báo cáo"

    @property
    def fingerprint(self) -> str:
        body = {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_session_id": self.source_session_id or "",
            "allow_cloud_parse": bool(self.allow_cloud_parse),
            "metadata": self.metadata or {},
        }
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class HandoffResult:
    job: Any
    target_session_id: str
    created_session: bool
    replayed: bool


def _has_competing_import(service: Any, session: Any) -> bool:
    job_id = session["import_job_id"] if "import_job_id" in session.keys() else None
    if not job_id:
        return False
    job = service.store.get_job(job_id)
    return job is not None and job.status != "failed"


def _begin(database: webdb.WebDB, service: Any, request: HandoffRequest) -> tuple[Any, bool]:
    with database.transaction() as conn:
        row = webdb.get_handoff(conn, request.owner_id, request.idempotency_key)
        if row is not None and row["state"] == "aborted":
            # Nothing was created last time (e.g. a quota refusal): start over,
            # even if the user changed a choice since.
            webdb.delete_handoff(conn, row["id"])
            row = None
        if row is not None:
            if row["request_fingerprint"] != request.fingerprint:
                raise HandoffError(
                    "idempotency_conflict", "Yêu cầu này đã được gửi với lựa chọn khác.", 409
                )
            return row, True
        source = None
        if request.source_session_id:
            source = webdb.get_owned_session(conn, request.source_session_id, request.owner_id)
            if source is None:
                raise HandoffError("session_not_found", "Không tìm thấy cuộc trò chuyện.", 404)
        reusable = (
            source is not None
            and not source["dataset_id"]
            and not source["read_only"]
            and not _has_competing_import(service, source)
        )
        if reusable:
            target_id, created = source["id"], False
        else:
            # The import gets its own conversation; the one the user is in
            # keeps its report. Created only now, at confirmation.
            target = webdb.create_session(
                conn, owner_id=request.owner_id, title=request.new_session_title, provisioning=True
            )
            target_id, created = target["id"], True
        row = webdb.insert_handoff(
            conn,
            owner_id=request.owner_id,
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint,
            source_session_id=request.source_session_id,
            target_session_id=target_id,
            created_target=created,
        )
        return row, False


def _abort(database: webdb.WebDB, handoff: Any, code: str) -> None:
    with database.transaction() as conn:
        webdb.update_handoff(conn, handoff["id"], state="aborted", error_code=str(code)[:64])
        if handoff["created_target"]:
            webdb.delete_provisioning_session(
                conn, handoff["target_session_id"], handoff["owner_id"]
            )


def _link_and_release(database: webdb.WebDB, service: Any, handoff: Any, job_id: str) -> None:
    with database.transaction() as conn:
        current = webdb.get_handoff_by_id(conn, handoff["id"])
        linked = current["state"] == "linked"
        if not linked:
            linked = webdb.set_session_import(
                conn, current["target_session_id"], current["owner_id"], job_id
            )
            if linked:
                webdb.finish_provisioning(conn, current["target_session_id"])
                webdb.update_handoff(conn, current["id"], state="linked", job_id=job_id)
    if not linked:
        # The session was deleted or got a report meanwhile: never recreate
        # it, and give the reservation back.
        service.cancel_held_job(job_id, reason="session_unavailable")
        _abort(database, handoff, "session_unavailable")
        raise HandoffError(
            "session_unavailable",
            "Cuộc trò chuyện không còn nhận báo cáo này; hãy thử lại từ một cuộc trò chuyện mới.",
            409,
        )
    service.release_dispatch_hold(job_id)


def confirm_import(
    database: webdb.WebDB,
    service: Any,
    request: HandoffRequest,
    create_job: Callable[[str], Any],
) -> HandoffResult:
    """Create (or replay) an import for ``request``. ``create_job(target)``
    must create a held acquisition job with the same idempotency key."""

    handoff, replayed = _begin(database, service, request)
    if handoff["state"] == "linked":
        service.release_dispatch_hold(handoff["job_id"])  # idempotent
        job_id = handoff["job_id"]
    else:
        job_id = handoff["job_id"]
        if handoff["state"] == "started":
            try:
                job = create_job(handoff["target_session_id"])
            except Exception as exc:
                _abort(database, handoff, getattr(exc, "code", type(exc).__name__))
                raise
            job_id = job.job_id
            with database.transaction() as conn:
                webdb.update_handoff(conn, handoff["id"], state="job_created", job_id=job_id)
        _link_and_release(database, service, handoff, job_id)
    return HandoffResult(
        job=service.store.get_job(job_id),
        target_session_id=handoff["target_session_id"],
        created_session=bool(handoff["created_target"]),
        replayed=replayed,
    )


def recover_handoffs(
    database: webdb.WebDB,
    service: Any,
    *,
    now: Optional[datetime] = None,
    stale_after: timedelta = STALE_AFTER,
) -> list[str]:
    """Finish or undo hand-offs a crash left behind (run by the import worker)."""

    current = now or datetime.now(timezone.utc)
    cutoff = (current - stale_after).replace(microsecond=0).isoformat()
    outcomes: list[str] = []
    with database.read() as conn:
        unfinished = webdb.list_unfinished_handoffs(conn, updated_before=cutoff)
    for handoff in unfinished:
        job_id = handoff["job_id"]
        if handoff["state"] == "started":
            job = service.store.get_job_by_idempotency(handoff["owner_id"], handoff["idempotency_key"])
            if job is None:
                _abort(database, handoff, "handoff_interrupted")
                outcomes.append(f"{handoff['id']}:aborted")
                continue
            job_id = job.job_id
            with database.transaction() as conn:
                webdb.update_handoff(conn, handoff["id"], state="job_created", job_id=job_id)
        try:
            _link_and_release(database, service, handoff, job_id)
            outcomes.append(f"{handoff['id']}:linked")
        except HandoffError:
            outcomes.append(f"{handoff['id']}:aborted")

    # A linked hand-off whose hold was never released, or a held job whose
    # hand-off is gone or aborted.
    for job in service.store.held_jobs():
        if job.created_at > cutoff:
            continue
        with database.read() as conn:
            handoff = webdb.get_handoff_by_job(conn, job.job_id)
        if handoff is not None and handoff["state"] == "linked":
            service.release_dispatch_hold(job.job_id)
            outcomes.append(f"{job.job_id}:released")
        elif handoff is None or handoff["state"] == "aborted":
            service.cancel_held_job(job.job_id, reason="handoff_interrupted")
            outcomes.append(f"{job.job_id}:cancelled")
    for outcome in outcomes:
        logger.warning("import hand-off recovery: %s", outcome)
    return outcomes
