"""SQLite persistence for owner-scoped report candidates and import jobs."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from schemas.acquisition import ImportJob, ReportCandidate, utc_now_iso

TERMINAL_JOB_STATUSES = {"ready", "failed"}


class CandidateAccessError(ValueError):
    pass


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
                """
            )

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
        """Only a queued job can be claimed. Duplicate dispatch is read-only."""
        with self.transaction():
            job = self.get_job(job_id)
            if job is None or job.status != "queued":
                return None
            return self.update_job(job.model_copy(update={
                "status": "downloading" if job.origin == "cafef_discovery" else "parsing",
            }))

    @contextmanager
    def parse_slot(self, job_id: str, *, limit: int):
        # No timed expiry: a crashed worker must not allow an in-flight remote
        # parse to be billed twice. Recovery must reconcile the provider job.
        with self.transaction(), self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM parse_slots").fetchone()[0]
            if count >= limit:
                from acquisition.llamaparse import LlamaParseError
                raise LlamaParseError("Global parse concurrency limit reached", retryable=True)
            connection.execute("INSERT INTO parse_slots VALUES (?, ?)", (job_id, utc_now_iso()))
        release = True
        try:
            yield
        except Exception as exc:
            release = not bool(getattr(exc, "billing_pending", False))
            raise
        finally:
            if release:
                with self._connect() as connection:
                    connection.execute("DELETE FROM parse_slots WHERE job_id = ?", (job_id,))

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
