"""SQLite persistence for owner-scoped report candidates, import jobs and the
parse-attempt ledger.

Billing is tracked per remote parse attempt, independently of a job's
processing status: a job can be ``ready`` while its provider usage is still
unconfirmed. An attempt row is written *before* a remote job is created and
the remote job ID is stored as soon as the provider returns it, so a crash can
leave an unresolved submission but never an untraced one. Usage is recorded
idempotently per remote job ID — reconciling twice never charges twice.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Optional

from agentfinx.core.schemas.acquisition import (TERMINAL_REMOTE_STATUSES, ImportJob,
                                 ReportCandidate, utc_now_iso)

TERMINAL_JOB_STATUSES = {"ready", "failed"}
# PRAGMA user_version of the acquisition database.
SCHEMA_VERSION = 2
_UNRESOLVED_ATTEMPT_STATES = ("submitting", "remote_id_lost")


class CandidateAccessError(ValueError):
    pass


@dataclass(frozen=True)
class BillingSummary:
    """Billing state of one job derived from its parse attempts."""

    status: str  # "none" | "pending" | "confirmed"
    confirmed_credits: Optional[Decimal]
    attempts: int
    remote_terminal: bool
    remote_id_lost: bool
    # This job's own remote parse is unsettled (as opposed to waiting only on
    # another job that paid for a shared public parse).
    payer_pending: bool = False


def _decimal_or_none(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def _legacy_billing_fields(payload: dict) -> dict:
    """Jobs written before the ledger: keep processing state, the pending flag
    and accounting time; never promote an estimate to a confirmed charge."""

    if payload.get("billing_pending"):
        return {"billing_status": "pending"}
    conversion = (payload.get("metadata") or {}).get("conversion") or {}
    if conversion.get("cache_hit") or conversion.get("provider") == "local_text_layer":
        return {"billing_status": "confirmed", "confirmed_credits": "0"}
    if conversion or int(payload.get("billed_credits") or 0) > 0:
        return {"billing_status": "estimated_legacy"}
    return {"billing_status": "not_submitted"}


def _legacy_identity_fields(payload: dict) -> dict:
    """Jobs written before request identities: keep the source when it can be
    read back, but leave the fingerprint empty so a replay is refused rather
    than matched against a request it may not have been."""

    fields: dict = {"billing_blocking": bool(payload.get("billing_pending"))}
    if payload.get("origin") == "cafef_discovery" and payload.get("candidate_id"):
        fields.update(source_kind="candidate", source_id=payload["candidate_id"])
    elif payload.get("origin") == "upload" and payload.get("source_sha256"):
        fields.update(source_kind="upload", source_id=payload["source_sha256"])
    return fields


def _safe_lock_name(value: str) -> str:
    name = "".join(ch for ch in str(value) if ch.isalnum() or ch in "-_")
    if not name:
        raise ValueError("job id must not be empty")
    return name


class AcquisitionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._transaction_connection = ContextVar("acquisition_transaction", default=None)
        self._initialize()

    @contextmanager
    def _connect(self):
        active = self._transaction_connection.get()
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        """Serialize admission and reservation across processes sharing this DB."""
        if self._transaction_connection.get() is not None:
            yield
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            token = self._transaction_connection.set(connection)
            try:
                yield
            finally:
                self._transaction_connection.reset(token)

    @contextmanager
    def job_lock(self, job_id: str) -> Iterator[None]:
        """Serialize work on one import job: the worker's run and an admin reconcile."""

        from agentfinx.datasets.locks import file_lock

        with file_lock(self.path.parent / "locks" / f"import-job-{_safe_lock_name(job_id)}.lock"):
            yield

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS report_candidates (
                    candidate_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS report_candidates_owner
                    ON report_candidates(owner_id, session_id, expires_at);

                CREATE TABLE IF NOT EXISTS import_jobs (
                    job_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(owner_id, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS import_jobs_owner_status
                    ON import_jobs(owner_id, status, created_at);

                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_owner_kind_time
                    ON usage_events(owner_id, kind, created_at);

                CREATE TABLE IF NOT EXISTS account_trials (
                    owner_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS parse_slots (
                    job_id TEXT PRIMARY KEY,
                    acquired_at TEXT NOT NULL
                );

                -- One row per remote parse submission (payer) or per reuse of
                -- another job's remote parse (consumer). ``credits`` is the
                -- provider-reported usage; NULL means not yet known.
                CREATE TABLE IF NOT EXISTS parse_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    source_sha256 TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    page_selection TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('payer', 'consumer')),
                    payer_attempt_id TEXT,
                    state TEXT NOT NULL CHECK (
                        state IN ('submitting', 'submitted', 'completed', 'rejected', 'remote_id_lost')
                    ),
                    remote_job_id TEXT,
                    remote_status TEXT NOT NULL DEFAULT '',
                    credits TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS parse_attempts_job ON parse_attempts(job_id);
                CREATE INDEX IF NOT EXISTS parse_attempts_remote ON parse_attempts(remote_job_id);
                """
            )
        self._migrate()

    def _migrate(self) -> None:
        """v0 → v1: jobs written before the attempt ledger get an explicit
        billing status. v1 → v2: request identity fields are backfilled where
        the stored job allows it. Processing status, the pending flag and
        ``updated_at`` (the accounting time) are left exactly as they were."""

        with self.transaction(), self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version >= SCHEMA_VERSION:
                return
            rows = connection.execute("SELECT job_id, payload_json FROM import_jobs").fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                changed = False
                if version < 1 and "billing_status" not in payload:
                    payload.update(_legacy_billing_fields(payload))
                    changed = True
                if version < 2 and not payload.get("request_fingerprint"):
                    payload.update(_legacy_identity_fields(payload))
                    changed = True
                if not changed:
                    continue
                connection.execute(
                    "UPDATE import_jobs SET payload_json = ? WHERE job_id = ?",
                    (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["job_id"]),
                )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # ------------------------------------------------------------------ trials

    def ensure_trial(self, owner_id: str, *, started_at: str) -> str:
        """Create the immutable trial clock on first admission and return it."""

        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO account_trials(owner_id, started_at) VALUES (?, ?)",
                (owner_id, started_at),
            )
            row = connection.execute(
                "SELECT started_at FROM account_trials WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("failed to initialize account trial")
        return str(row["started_at"])

    # -------------------------------------------------------------- candidates

    def save_candidates(self, candidates: list[ReportCandidate]) -> None:
        rows = [
            (
                item.candidate_id,
                item.owner_id,
                item.session_id,
                item.expires_at,
                json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
            )
            for item in candidates
        ]
        if not rows:
            return
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO report_candidates
                    (candidate_id, owner_id, session_id, expires_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )

    def get_candidate(
        self,
        candidate_id: str,
        *,
        owner_id: str,
        session_id: str,
        now: datetime | None = None,
    ) -> ReportCandidate:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM report_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise CandidateAccessError("candidate_not_found")
        candidate = ReportCandidate.model_validate(json.loads(row["payload_json"]))
        if candidate.owner_id != owner_id or candidate.session_id != session_id:
            raise CandidateAccessError("candidate_owner_mismatch")
        current = now or datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(candidate.expires_at.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if current >= expires_at:
            raise CandidateAccessError("candidate_expired")
        return candidate

    # -------------------------------------------------------------------- jobs

    def create_job(self, job: ImportJob, *, idempotency_key: str) -> ImportJob:
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        payload = json.dumps(job.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        with self.transaction(), self._connect() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM import_jobs WHERE owner_id = ? AND idempotency_key = ?",
                (job.owner_id, key),
            ).fetchone()
            if existing is not None:
                return ImportJob.model_validate(json.loads(existing["payload_json"]))
            connection.execute(
                """
                INSERT INTO import_jobs (
                    job_id, owner_id, session_id, status, idempotency_key,
                    created_at, updated_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.owner_id,
                    job.session_id,
                    job.status,
                    key,
                    job.created_at,
                    job.updated_at,
                    payload,
                ),
            )
        return job

    def claim_job(self, job_id: str) -> ImportJob | None:
        """Only a queued, unheld job can be claimed. Duplicate dispatch is read-only."""
        with self.transaction():
            job = self.get_job(job_id)
            if job is None or job.status != "queued" or job.dispatch_hold:
                return None
            return self.update_job(job.model_copy(update={
                "status": "downloading" if job.origin == "cafef_discovery" else "parsing",
            }))

    @contextmanager
    def parse_slot(self, job_id: str, *, limit: int, hold_on_error=None):
        # No timed expiry: a crashed worker must not allow an in-flight remote
        # parse to be billed twice. Recovery must reconcile the provider job.
        # A job that already holds its slot (resumed after a crash) re-enters it
        # instead of counting against the limit a second time.
        # ``hold_on_error(exc)`` decides whether a failure leaves a remote parse
        # possibly running (keep the slot); default: the error's billing_pending.
        with self.transaction(), self._connect() as connection:
            held = connection.execute(
                "SELECT 1 FROM parse_slots WHERE job_id = ?", (job_id,)
            ).fetchone()
            if held is None:
                count = connection.execute("SELECT COUNT(*) FROM parse_slots").fetchone()[0]
                if count >= limit:
                    from agentfinx.acquisition.llamaparse import LlamaParseError
                    raise LlamaParseError("Global parse concurrency limit reached", retryable=True)
                connection.execute("INSERT INTO parse_slots VALUES (?, ?)", (job_id, utc_now_iso()))
        release = True
        try:
            yield
        except BaseException as exc:
            # Includes interrupts: a worker stopped mid-poll leaves its remote
            # parse running, so the slot stays held until reconciled.
            if hold_on_error is None:
                release = not bool(getattr(exc, "billing_pending", False))
            else:
                release = not hold_on_error(exc)
            raise
        finally:
            if release:
                self.release_parse_slot(job_id)

    def release_parse_slot(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM parse_slots WHERE job_id = ?", (job_id,))

    def holds_parse_slot(self, job_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM parse_slots WHERE job_id = ?", (job_id,)
            ).fetchone()
        return row is not None

    def get_job_by_idempotency(
        self,
        owner_id: str,
        idempotency_key: str,
    ) -> ImportJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM import_jobs WHERE owner_id = ? AND idempotency_key = ?",
                (owner_id, str(idempotency_key or "").strip()),
            ).fetchone()
        if row is None:
            return None
        return ImportJob.model_validate(json.loads(row["payload_json"]))

    def get_job(self, job_id: str, *, owner_id: str = "") -> ImportJob | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM import_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        job = ImportJob.model_validate(json.loads(row["payload_json"]))
        if owner_id and job.owner_id != owner_id:
            return None
        return job

    def update_job(self, job: ImportJob) -> ImportJob:
        updated = job.model_copy(update={"updated_at": utc_now_iso()})
        payload = json.dumps(updated.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE import_jobs
                SET status = ?, updated_at = ?, payload_json = ?
                WHERE job_id = ? AND owner_id = ?
                """,
                (updated.status, updated.updated_at, payload, updated.job_id, updated.owner_id),
            )
        if cursor.rowcount != 1:
            raise ValueError("import job not found or owner mismatch")
        return updated

    def _all_jobs(self, where: str = "", params: tuple = ()) -> list[ImportJob]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM import_jobs {where} ORDER BY created_at, rowid", params
            ).fetchall()
        return [ImportJob.model_validate(json.loads(row["payload_json"])) for row in rows]

    def jobs_in_statuses(self, statuses: set[str]) -> list[ImportJob]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        return self._all_jobs(f"WHERE status IN ({placeholders})", tuple(sorted(statuses)))

    def jobs_with_pending_billing(self) -> list[ImportJob]:
        return [job for job in self._all_jobs() if job.billing_pending]

    def next_queued_job(self) -> ImportJob | None:
        jobs = [job for job in self._all_jobs("WHERE status = 'queued'") if not job.dispatch_hold]
        return jobs[0] if jobs else None

    def held_jobs(self) -> list[ImportJob]:
        return [job for job in self._all_jobs("WHERE status = 'queued'") if job.dispatch_hold]

    def release_dispatch_hold(self, job_id: str) -> ImportJob | None:
        with self.transaction():
            job = self.get_job(job_id)
            if job is None or not job.dispatch_hold:
                return job
            return self.update_job(job.model_copy(update={"dispatch_hold": False}))

    def count_jobs(self, owner_id: str, *, statuses: set[str]) -> int:
        if not statuses:
            return 0
        placeholders = ",".join("?" for _ in statuses)
        params = [owner_id, *sorted(statuses)]
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM import_jobs WHERE owner_id = ? AND status IN ({placeholders})",
                params,
            ).fetchone()
        return int(row["n"] or 0)

    def count_blocking_jobs(self, owner_id: str, *, statuses: set[str]) -> int:
        """Distinct jobs still processing *or* awaiting their own billing, each
        counted once. A job waiting only on another job's payer does not block."""

        jobs = self._all_jobs("WHERE owner_id = ?", (owner_id,))
        return sum(
            1 for job in jobs
            if job.status in statuses or (job.billing_pending and job.billing_blocking)
        )

    def committed_credits(self, *, since: str = "") -> int:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM import_jobs").fetchall()
        total = 0
        for row in rows:
            job = ImportJob.model_validate(json.loads(row["payload_json"]))
            accounting_time = (
                job.updated_at if job.status in TERMINAL_JOB_STATUSES else job.created_at
            )
            if (since and accounting_time < since
                    and job.status in TERMINAL_JOB_STATUSES and not job.billing_pending):
                continue
            if job.billing_pending:
                total += max(job.reserved_credits, job.billed_credits)
            elif job.status in TERMINAL_JOB_STATUSES:
                total += job.billed_credits
            else:
                total += job.reserved_credits
        return total

    def count_unreconciled_jobs(self, owner_id: str) -> int:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM import_jobs WHERE owner_id = ?", (owner_id,)).fetchall()
        return sum(bool(json.loads(row["payload_json"]).get("billing_pending")) for row in rows)

    def count_all_jobs(self, *, statuses: set[str]) -> int:
        if not statuses:
            return 0
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS n FROM import_jobs WHERE status IN ({placeholders})",
                sorted(statuses),
            ).fetchone()
        return int(row["n"] or 0)

    # ---------------------------------------------------------------- usage

    def record_usage(self, owner_id: str, *, kind: str, amount: int = 1) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO usage_events(owner_id, kind, amount, created_at) VALUES (?, ?, ?, ?)",
                (owner_id, kind, int(amount), utc_now_iso()),
            )

    def usage_total(self, owner_id: str, *, kind: str, since: str = "") -> int:
        query = "SELECT COALESCE(SUM(amount), 0) AS n FROM usage_events WHERE owner_id = ? AND kind = ?"
        params: list[object] = [owner_id, kind]
        if since:
            query += " AND created_at >= ?"
            params.append(since)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return int(row["n"] or 0)

    # ----------------------------------------------------------- attempts

    def begin_attempt(
        self,
        job: ImportJob,
        *,
        tier: str,
        selection: str,
        role: str = "payer",
        payer_attempt_id: Optional[str] = None,
        state: str = "submitting",
        remote_job_id: Optional[str] = None,
        remote_status: str = "",
    ) -> str:
        attempt_id = uuid.uuid4().hex
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO parse_attempts (
                    attempt_id, job_id, owner_id, source_sha256, tier, page_selection,
                    role, payer_attempt_id, state, remote_job_id, remote_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id, job.job_id, job.owner_id, job.source_sha256, tier, selection,
                    role, payer_attempt_id, state, remote_job_id, str(remote_status or "").upper(),
                    now, now,
                ),
            )
        return attempt_id

    def update_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        remote_job_id: Optional[str] = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE parse_attempts SET state = ?, remote_job_id = COALESCE(?, remote_job_id), "
                "updated_at = ? WHERE attempt_id = ?",
                (state, remote_job_id, utc_now_iso(), attempt_id),
            )

    def attempts_for_job(self, job_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM parse_attempts WHERE job_id = ? ORDER BY created_at, rowid",
                (job_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_attempt(self, attempt_id: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM parse_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def attempt_for_selection(self, job_id: str, *, tier: str, selection: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM parse_attempts WHERE job_id = ? AND tier = ? AND page_selection = ? "
                "AND state != 'rejected' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (job_id, tier, selection),
            ).fetchone()
        return dict(row) if row is not None else None

    def payer_attempt_for_remote(self, remote_job_id: str) -> Optional[dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM parse_attempts WHERE remote_job_id = ? AND role = 'payer' "
                "ORDER BY created_at, rowid LIMIT 1",
                (remote_job_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def complete_remote(
        self,
        remote_job_id: str,
        *,
        remote_status: str,
        credits: Optional[Decimal],
    ) -> None:
        """Record provider status and usage for every attempt on a remote job.

        Credits are only ever filled while unknown (``COALESCE``), so repeated
        reconciliation or a racing worker cannot change or double a charge.
        """

        credit_text = None if credits is None else str(credits)
        status = str(remote_status or "").upper()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE parse_attempts
                   SET remote_status = ?,
                       credits = CASE WHEN role = 'payer' THEN COALESCE(credits, ?) ELSE credits END,
                       state = CASE WHEN state IN ('submitted', 'completed') AND ? THEN 'completed'
                                    ELSE state END,
                       updated_at = ?
                 WHERE remote_job_id = ?
                """,
                (status, credit_text, int(status in TERMINAL_REMOTE_STATUSES), utc_now_iso(), remote_job_id),
            )

    def mark_unresolved_submissions_lost(self, job_id: str) -> int:
        """A crash between writing an attempt and storing its remote ID leaves it
        ``submitting``; after a restart that submission's outcome is unknown."""

        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE parse_attempts SET state = 'remote_id_lost', updated_at = ? "
                "WHERE job_id = ? AND state = 'submitting'",
                (utc_now_iso(), job_id),
            )
        return cursor.rowcount

    def billing_summary(self, job_id: str) -> BillingSummary:
        attempts = [row for row in self.attempts_for_job(job_id) if row["state"] != "rejected"]
        if not attempts:
            return BillingSummary("none", None, 0, True, False)
        pending = lost = payer_pending = False
        terminal = True
        total = Decimal(0)
        for attempt in attempts:
            if attempt["role"] == "payer":
                if attempt["state"] in _UNRESOLVED_ATTEMPT_STATES:
                    pending = payer_pending = True
                    terminal = False
                    lost = lost or attempt["state"] == "remote_id_lost"
                    continue
                if str(attempt["remote_status"] or "").upper() not in TERMINAL_REMOTE_STATUSES:
                    terminal = False
                credits = _decimal_or_none(attempt["credits"])
                if credits is None:
                    pending = payer_pending = True
                else:
                    total += credits
            else:
                # A consumer reuses another job's remote parse: it pays nothing
                # itself but cannot settle before the payer's billing is known.
                # A remote job submitted before the ledger existed has no payer
                # row; its cost was accounted by that legacy job, so the
                # consumer settles once the remote job itself is terminal.
                payer = self.get_attempt(attempt["payer_attempt_id"]) if attempt["payer_attempt_id"] else None
                reference = payer if payer is not None else attempt
                if str(reference["remote_status"] or "").upper() not in TERMINAL_REMOTE_STATUSES:
                    terminal = False
                    pending = True
                elif payer is not None and _decimal_or_none(payer["credits"]) is None:
                    pending = True
        return BillingSummary(
            "pending" if pending else "confirmed",
            None if pending else total,
            len(attempts),
            terminal,
            lost,
            payer_pending,
        )


class ParseAttemptRecorder:
    """One job's view of the attempt ledger, handed to the converter."""

    def __init__(self, store: AcquisitionStore, job: ImportJob):
        self.store = store
        self.job = job

    def find_remote(self, *, tier: str, selection: str) -> Optional[str]:
        attempt = self.store.attempt_for_selection(self.job.job_id, tier=tier, selection=selection)
        if attempt and attempt["remote_job_id"]:
            return str(attempt["remote_job_id"])
        return None

    def unresolved_submission(self, *, tier: str, selection: str) -> bool:
        attempt = self.store.attempt_for_selection(self.job.job_id, tier=tier, selection=selection)
        return bool(attempt and attempt["state"] in _UNRESOLVED_ATTEMPT_STATES)

    def adopt(
        self,
        *,
        tier: str,
        selection: str,
        remote_job_id: str,
        remote_status: str = "",
    ) -> None:
        """Link to a remote parse another job (or an earlier run) submitted."""

        payer = self.store.payer_attempt_for_remote(remote_job_id)
        if payer is not None and payer["job_id"] == self.job.job_id:
            return
        self.store.begin_attempt(
            self.job,
            tier=tier,
            selection=selection,
            role="consumer",
            payer_attempt_id=payer["attempt_id"] if payer else None,
            state="completed" if remote_status else "submitted",
            remote_job_id=remote_job_id,
            remote_status=remote_status,
        )

    def begin(self, *, tier: str, selection: str) -> str:
        return self.store.begin_attempt(self.job, tier=tier, selection=selection)

    def submitted(self, attempt_id: str, remote_job_id: str) -> None:
        self.store.update_attempt(attempt_id, state="submitted", remote_job_id=remote_job_id)

    def rejected(self, attempt_id: str) -> None:
        self.store.update_attempt(attempt_id, state="rejected")

    def remote_id_lost(self, attempt_id: str) -> None:
        self.store.update_attempt(attempt_id, state="remote_id_lost")

    def completed(self, remote_job_id: str, *, remote_status: str, credits: Optional[Decimal]) -> None:
        self.store.complete_remote(remote_job_id, remote_status=remote_status, credits=credits)
