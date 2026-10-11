"""SQLite store for the local web app: users, chat sessions, messages, runs.

Separate from the dataset registry and the acquisition store. Callers pass the
owner explicitly; every lookup that returns user data is owner-scoped in SQL.
The schema is versioned and migrated forward on startup; a database written by
newer code is refused rather than silently downgraded.
"""
# Code note: Web modules expose the pipeline to browsers; comments call out security boundaries.

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

SCHEMA_VERSION = 7
# A heartbeat this recent means a worker may still be running old code.
MIGRATION_WORKER_GRACE_SECONDS = 45
ACTIVE_RUN_STATUSES = ("queued", "running")
TERMINAL_RUN_STATUSES = ("succeeded", "failed", "interrupted")

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE chat_sessions (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            dataset_id TEXT,
            dataset_generation TEXT,
            read_only INTEGER NOT NULL DEFAULT 0,
            read_only_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_sessions_owner ON chat_sessions(owner_id, updated_at)",
        """
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            run_id TEXT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_messages_session ON messages(session_id, created_at)",
        """
        CREATE TABLE chat_runs (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            question TEXT NOT NULL,
            question_message_id TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            dataset_generation TEXT NOT NULL,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')
            ),
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            answer_message_id TEXT,
            worker_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE (session_id, idempotency_key)
        )
        """,
        # One active run per session is a database invariant, not just a check
        # in request code that two concurrent requests could both pass.
        """
        CREATE UNIQUE INDEX idx_one_active_run_per_session
            ON chat_runs(session_id) WHERE status IN ('queued', 'running')
        """,
        "CREATE INDEX idx_runs_queue ON chat_runs(status, created_at)",
        """
        CREATE TABLE worker_heartbeats (
            queue TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            pid INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL
        )
        """,
    ),
    # A session created for a PDF upload starts without a dataset and records
    # its import job, so the page can resume status polling after a reload.
    2: ("ALTER TABLE chat_sessions ADD COLUMN import_job_id TEXT",),
    # The question the pipeline actually answered when a follow-up was
    # rewritten from earlier turns (FINX_CHAT_CONTEXT); '' when unchanged.
    3: ("ALTER TABLE chat_runs ADD COLUMN effective_question TEXT NOT NULL DEFAULT ''",),
    # Report discovery: a run may have no dataset (it searches for one), a
    # message may carry a typed payload (candidate cards), an import target
    # session can be provisioned before it is shown, and the cross-database
    # import hand-off keeps a durable journal. SQLite cannot drop NOT NULL, so
    # chat_runs is rebuilt; data, the per-session idempotency key, the
    # one-active-run index and the session foreign key are carried over.
    4: (
        "ALTER TABLE chat_sessions ADD COLUMN provisioning INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'text'",
        "ALTER TABLE messages ADD COLUMN payload TEXT NOT NULL DEFAULT ''",
        """
        CREATE TABLE chat_runs_v4 (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            question TEXT NOT NULL,
            question_message_id TEXT NOT NULL,
            dataset_id TEXT,
            dataset_generation TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')
            ),
            error_code TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            answer_message_id TEXT,
            worker_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            effective_question TEXT NOT NULL DEFAULT '',
            task_kind TEXT NOT NULL DEFAULT '',
            result_kind TEXT NOT NULL DEFAULT '',
            override_of_run_id TEXT,
            UNIQUE (session_id, idempotency_key),
            CHECK ((dataset_id IS NULL) = (dataset_generation IS NULL))
        )
        """,
        """
        INSERT INTO chat_runs_v4 (
            id, session_id, owner_id, idempotency_key, question, question_message_id,
            dataset_id, dataset_generation, status, error_code, error_message,
            answer_message_id, worker_id, created_at, started_at, finished_at, effective_question
        )
        SELECT id, session_id, owner_id, idempotency_key, question, question_message_id,
               dataset_id, dataset_generation, status, error_code, error_message,
               answer_message_id, worker_id, created_at, started_at, finished_at, effective_question
          FROM chat_runs
        """,
        "DROP TABLE chat_runs",
        "ALTER TABLE chat_runs_v4 RENAME TO chat_runs",
        """
        CREATE UNIQUE INDEX idx_one_active_run_per_session
            ON chat_runs(session_id) WHERE status IN ('queued', 'running')
        """,
        "CREATE INDEX idx_runs_queue ON chat_runs(status, created_at)",
        """
        CREATE TABLE import_handoffs (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            source_session_id TEXT,
            target_session_id TEXT NOT NULL,
            created_target INTEGER NOT NULL DEFAULT 0,
            job_id TEXT,
            state TEXT NOT NULL CHECK (state IN ('started', 'job_created', 'linked', 'aborted')),
            error_code TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (owner_id, idempotency_key)
        )
        """,
        "CREATE INDEX idx_handoffs_state ON import_handoffs(state, updated_at)",
        "CREATE INDEX idx_handoffs_job ON import_handoffs(job_id)",
        """
        CREATE TABLE discovery_requests (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            idempotency_key TEXT NOT NULL,
            request_fingerprint TEXT NOT NULL,
            request_message_id TEXT,
            result_message_id TEXT,
            created_at TEXT NOT NULL,
            UNIQUE (owner_id, idempotency_key)
        )
        """,
    ),
    5: (
        "ALTER TABLE chat_runs ADD COLUMN context_status TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE chat_runs ADD COLUMN context_reason TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE chat_runs ADD COLUMN context_resolution TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE chat_runs ADD COLUMN context_snapshot TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE worker_heartbeats ADD COLUMN runtime_config TEXT NOT NULL DEFAULT ''",
    ),
    # Server-side login sessions (logout revokes a stolen cookie too), a log of
    # login/sign-up attempts for rate limiting, and single-use invites.
    6: (
        """
        CREATE TABLE auth_sessions (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            revoked_at TEXT
        )
        """,
        "CREATE INDEX idx_auth_sessions_user ON auth_sessions(user_id)",
        "CREATE INDEX idx_auth_sessions_expiry ON auth_sessions(expires_at)",
        """
        CREATE TABLE auth_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,
            key_hash TEXT NOT NULL,
            attempted_at REAL NOT NULL
        )
        """,
        "CREATE INDEX idx_auth_attempts_key ON auth_attempts(scope, key_hash, attempted_at)",
        "CREATE INDEX idx_auth_attempts_time ON auth_attempts(attempted_at)",
        """
        CREATE TABLE invites (
            id TEXT PRIMARY KEY,
            code_hash TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            used_by TEXT REFERENCES users(id) ON DELETE SET NULL,
            revoked_at TEXT
        )
        """,
    ),
    # Company news that was not ready when a run answered: the chat worker
    # finishes it later and adds it to the conversation as its own message.
    7: (
        """
        CREATE TABLE news_updates (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE REFERENCES chat_runs(id) ON DELETE CASCADE,
            session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
            owner_id TEXT NOT NULL,
            context_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'expired', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            due_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            message_id TEXT,
            worker_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX idx_news_updates_due ON news_updates(status, due_at)",
        "CREATE INDEX idx_news_updates_session ON news_updates(session_id, status)",
    ),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


class WebDB:
    def __init__(self, path: Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """``BEGIN IMMEDIATE``: writers across processes serialize up front."""

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    def _current_version(self) -> int:
        if not self.path.exists():
            return 0
        conn = self.connect()
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            if not exists:
                return 0
            return int(conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0])
        finally:
            conn.close()

    def _prepare_upgrade(self, current: int) -> Optional[Path]:
        """Before upgrading an existing database: refuse while a worker looks
        alive, then take a consistent online backup next to the database."""

        conn = self.connect()
        try:
            rows = conn.execute("SELECT queue, heartbeat_at FROM worker_heartbeats").fetchall()
            now = datetime.now(timezone.utc)
            for row in rows:
                try:
                    age = (now - datetime.fromisoformat(row["heartbeat_at"])).total_seconds()
                except ValueError:
                    continue
                if age < MIGRATION_WORKER_GRACE_SECONDS:
                    raise RuntimeError(
                        f"the '{row['queue']}' worker reported {age:.0f}s ago; stop the API and "
                        f"workers before upgrading the web database (schema v{current} → "
                        f"v{SCHEMA_VERSION})"
                    )
            stamp = now.strftime("%Y%m%dT%H%M%SZ")
            backup_path = self.path.with_name(f"{self.path.name}.backup-v{current}-{stamp}")
            target = sqlite3.connect(backup_path)
            try:
                conn.backup(target)
            finally:
                target.close()
            return backup_path
        finally:
            conn.close()

    def migrate(self) -> int:
        current = self._current_version()
        if 0 < current < SCHEMA_VERSION:
            self._prepare_upgrade(current)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            current = int(
                conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]
            )
            if current > SCHEMA_VERSION:
                raise RuntimeError(
                    f"web database schema v{current} is newer than this code (v{SCHEMA_VERSION})"
                )
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[version]:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now()),
                )
            conn.execute("COMMIT")
            return SCHEMA_VERSION
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


def create_user(conn, *, name: str, email: str, password_hash: str) -> sqlite3.Row:
    user_id = new_id()
    conn.execute(
        "INSERT INTO users (id, name, email, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, email, password_hash, utc_now()),
    )
    return get_user(conn, user_id)


def get_user(conn, user_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def get_user_by_email(conn, email: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


# ---------------------------------------------------------------------------
# Login sessions, attempts and invites
# ---------------------------------------------------------------------------


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def create_auth_session(conn, *, user_id: str, expires_at: str) -> str:
    session_id = new_id()
    conn.execute(
        "INSERT INTO auth_sessions (id, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (session_id, user_id, utc_now(), expires_at),
    )
    return session_id


def get_session_user(conn, session_id: str, user_id: str) -> Optional[sqlite3.Row]:
    """The user of a login session that is neither revoked nor expired."""

    return conn.execute(
        "SELECT users.* FROM auth_sessions JOIN users ON users.id = auth_sessions.user_id "
        "WHERE auth_sessions.id = ? AND auth_sessions.user_id = ? "
        "AND auth_sessions.revoked_at IS NULL AND auth_sessions.expires_at > ?",
        (session_id, user_id, utc_now()),
    ).fetchone()


def revoke_auth_session(conn, session_id: str, user_id: str) -> bool:
    cursor = conn.execute(
        "UPDATE auth_sessions SET revoked_at = ? "
        "WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
        (utc_now(), session_id, user_id),
    )
    return cursor.rowcount == 1


def prune_auth_sessions(conn, *, before: str) -> None:
    conn.execute(
        "DELETE FROM auth_sessions WHERE expires_at < ? OR revoked_at < ?", (before, before)
    )


def count_attempts(conn, scope: str, key_hash: str, *, since: float) -> list[float]:
    rows = conn.execute(
        "SELECT attempted_at FROM auth_attempts WHERE scope = ? AND key_hash = ? "
        "AND attempted_at > ? ORDER BY attempted_at",
        (scope, key_hash, since),
    ).fetchall()
    return [float(row["attempted_at"]) for row in rows]


def add_attempt(conn, scope: str, key_hash: str, *, at: float) -> int:
    cursor = conn.execute(
        "INSERT INTO auth_attempts (scope, key_hash, attempted_at) VALUES (?, ?, ?)",
        (scope, key_hash, at),
    )
    return int(cursor.lastrowid)


def delete_attempts(conn, attempt_ids: list[int]) -> None:
    conn.executemany("DELETE FROM auth_attempts WHERE id = ?", [(item,) for item in attempt_ids])


def clear_attempts(conn, scope: str, key_hash: str) -> None:
    conn.execute("DELETE FROM auth_attempts WHERE scope = ? AND key_hash = ?", (scope, key_hash))


def prune_attempts(conn, *, before: float) -> None:
    conn.execute("DELETE FROM auth_attempts WHERE attempted_at < ?", (before,))


def create_invite(conn, *, code_hash: str, email: str, expires_at: str) -> sqlite3.Row:
    invite_id = new_id()
    conn.execute(
        "INSERT INTO invites (id, code_hash, email, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (invite_id, code_hash, email, utc_now(), expires_at),
    )
    return conn.execute("SELECT * FROM invites WHERE id = ?", (invite_id,)).fetchone()


def get_usable_invite(conn, code_hash: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM invites WHERE code_hash = ? AND used_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > ?",
        (code_hash, utc_now()),
    ).fetchone()


def use_invite(conn, invite_id: str, user_id: str) -> bool:
    cursor = conn.execute(
        "UPDATE invites SET used_at = ?, used_by = ? "
        "WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL",
        (utc_now(), user_id, invite_id),
    )
    return cursor.rowcount == 1


def revoke_invite(conn, invite_id: str) -> bool:
    cursor = conn.execute(
        "UPDATE invites SET revoked_at = ? WHERE id = ? AND used_at IS NULL AND revoked_at IS NULL",
        (utc_now(), invite_id),
    )
    return cursor.rowcount == 1


def create_news_update(conn, *, run_id: str, session_id: str, owner_id: str, context: dict,
                       due_at: str, expires_at: str) -> str:
    update_id = new_id()
    now = utc_now()
    conn.execute(
        "INSERT INTO news_updates (id, run_id, session_id, owner_id, context_json, status, due_at, expires_at, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (update_id, run_id, session_id, owner_id, json.dumps(context, ensure_ascii=False), due_at, expires_at,
         now, now),
    )
    return update_id


def claim_due_news_update(conn, worker_id: str, *, now: str) -> Optional[sqlite3.Row]:
    row = conn.execute(
        "SELECT * FROM news_updates WHERE status = 'pending' AND due_at <= ? ORDER BY due_at LIMIT 1", (now,)
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE news_updates SET status = 'running', attempts = attempts + 1, worker_id = ?, updated_at = ? "
        "WHERE id = ? AND status = 'pending'",
        (worker_id, now, row["id"]),
    )
    return conn.execute("SELECT * FROM news_updates WHERE id = ?", (row["id"],)).fetchone()


def reschedule_news_update(conn, update_id: str, *, due_at: str) -> None:
    conn.execute(
        "UPDATE news_updates SET status = 'pending', due_at = ?, worker_id = '', updated_at = ? "
        "WHERE id = ? AND status = 'running'",
        (due_at, utc_now(), update_id),
    )


def finish_news_update(conn, update_id: str, *, status: str, message_id: Optional[str]) -> None:
    conn.execute(
        "UPDATE news_updates SET status = ?, message_id = ?, updated_at = ? WHERE id = ?",
        (status, message_id, utc_now(), update_id),
    )


def get_news_update_for_run(conn, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM news_updates WHERE run_id = ?", (run_id,)).fetchone()


def recover_news_updates(conn) -> int:
    """A worker that stopped mid-update: run those again (no answer was sent)."""

    cursor = conn.execute(
        "UPDATE news_updates SET status = 'pending', worker_id = '', updated_at = ? WHERE status = 'running'",
        (utc_now(),),
    )
    return cursor.rowcount


def list_invites(conn) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, email, created_at, expires_at, used_at, revoked_at FROM invites "
        "ORDER BY created_at DESC"
    ).fetchall()


# ---------------------------------------------------------------------------
# Sessions and messages
# ---------------------------------------------------------------------------


def create_session(
    conn,
    *,
    owner_id: str,
    title: str,
    dataset_id: Optional[str] = None,
    dataset_generation: Optional[str] = None,
    provisioning: bool = False,
) -> sqlite3.Row:
    session_id = new_id()
    now = utc_now()
    conn.execute(
        "INSERT INTO chat_sessions (id, owner_id, title, dataset_id, dataset_generation, "
        "provisioning, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (session_id, owner_id, title, dataset_id, dataset_generation, int(provisioning), now, now),
    )
    return get_session(conn, session_id)


def get_session(conn, session_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,)).fetchone()


def get_owned_session(
    conn, session_id: str, owner_id: str, *, include_provisioning: bool = False
) -> Optional[sqlite3.Row]:
    # A session still being provisioned by an import hand-off is not the
    # user's yet: it is invisible to every user-facing lookup.
    query = "SELECT * FROM chat_sessions WHERE id = ? AND owner_id = ?"
    if not include_provisioning:
        query += " AND provisioning = 0"
    return conn.execute(query, (session_id, owner_id)).fetchone()


def list_sessions(conn, owner_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*,
               (SELECT r.id FROM chat_runs r
                 WHERE r.session_id = s.id AND r.status IN ('queued', 'running')
                 LIMIT 1) AS active_run_id,
               (SELECT n.run_id FROM news_updates n
                 WHERE n.session_id = s.id AND n.status IN ('pending', 'running')
                 ORDER BY n.created_at DESC LIMIT 1) AS news_update_run_id
          FROM chat_sessions s
         WHERE s.owner_id = ? AND s.provisioning = 0
         ORDER BY s.updated_at DESC, s.rowid DESC
        """,
        (owner_id,),
    ).fetchall()


def delete_owned_session(conn, session_id: str, owner_id: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM chat_sessions WHERE id = ? AND owner_id = ?",
        (session_id, owner_id),
    )
    return cursor.rowcount == 1


def rename_owned_session(conn, session_id: str, owner_id: str, title: str) -> bool:
    cursor = conn.execute(
        "UPDATE chat_sessions SET title = ?, updated_at = ? "
        "WHERE id = ? AND owner_id = ? AND provisioning = 0",
        (title, utc_now(), session_id, owner_id),
    )
    return cursor.rowcount == 1


def mark_session_read_only(conn, session_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE chat_sessions SET read_only = 1, read_only_reason = ?, updated_at = ? "
        "WHERE id = ? AND read_only = 0",
        (reason, utc_now(), session_id),
    )


def set_session_import(conn, session_id: str, owner_id: str, job_id: str) -> bool:
    """Link an import job to an owned, still dataset-less, writable session."""

    cursor = conn.execute(
        "UPDATE chat_sessions SET import_job_id = ?, updated_at = ? "
        "WHERE id = ? AND owner_id = ? AND dataset_id IS NULL AND read_only = 0",
        (job_id, utc_now(), session_id, owner_id),
    )
    return cursor.rowcount == 1


def finish_provisioning(conn, session_id: str) -> None:
    conn.execute(
        "UPDATE chat_sessions SET provisioning = 0, updated_at = ? WHERE id = ?",
        (utc_now(), session_id),
    )


def delete_provisioning_session(conn, session_id: str, owner_id: str) -> bool:
    cursor = conn.execute(
        "DELETE FROM chat_sessions WHERE id = ? AND owner_id = ? AND provisioning = 1",
        (session_id, owner_id),
    )
    return cursor.rowcount == 1


def attach_session_dataset(
    conn,
    *,
    session_id: str,
    owner_id: str,
    dataset_id: str,
    dataset_generation: str,
    title: Optional[str] = None,
) -> bool:
    """Pin a finished import's dataset to its session.

    Only a session that has no dataset yet can be attached, so an import can
    never swap the report underneath an existing conversation.
    """

    cursor = conn.execute(
        "UPDATE chat_sessions SET dataset_id = ?, dataset_generation = ?, "
        "title = COALESCE(?, title), updated_at = ? "
        "WHERE id = ? AND owner_id = ? AND dataset_id IS NULL",
        (dataset_id, dataset_generation, title, utc_now(), session_id, owner_id),
    )
    return cursor.rowcount == 1


def touch_session(conn, session_id: str, *, title: Optional[str] = None) -> None:
    if title is None:
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
            (utc_now(), session_id),
        )
    else:
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ?, title = ? WHERE id = ?",
            (utc_now(), title, session_id),
        )


def add_message(
    conn,
    *,
    session_id: str,
    role: str,
    content: str,
    run_id: Optional[str] = None,
    kind: str = "text",
    payload: str = "",
) -> str:
    """``kind`` other than ``text`` marks a structured message (e.g. report
    candidates) whose ``payload`` is server-built JSON; ``content`` stays a
    plain-text summary."""

    message_id = new_id()
    conn.execute(
        "INSERT INTO messages (id, session_id, role, content, run_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, session_id, role, content, run_id, kind, payload, utc_now()),
    )
    return message_id


def list_messages(conn, session_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at, rowid",
        (session_id,),
    ).fetchall()


def count_user_messages(conn, session_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role = 'user'",
            (session_id,),
        ).fetchone()[0]
    )


def get_message(conn, message_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()


# ---------------------------------------------------------------------------
# Chat runs
# ---------------------------------------------------------------------------


def find_run_by_key(conn, session_id: str, idempotency_key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chat_runs WHERE session_id = ? AND idempotency_key = ?",
        (session_id, idempotency_key),
    ).fetchone()


def get_active_run(conn, session_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chat_runs WHERE session_id = ? AND status IN ('queued', 'running')",
        (session_id,),
    ).fetchone()


def create_run(
    conn,
    *,
    session_id: str,
    owner_id: str,
    idempotency_key: str,
    question: str,
    question_message_id: str,
    dataset_id: Optional[str],
    dataset_generation: Optional[str],
    override_of_run_id: Optional[str] = None,
    link_question_message: bool = True,
) -> sqlite3.Row:
    run_id = new_id()
    conn.execute(
        "INSERT INTO chat_runs (id, session_id, owner_id, idempotency_key, question, "
        "question_message_id, dataset_id, dataset_generation, status, created_at, "
        "override_of_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
        (
            run_id,
            session_id,
            owner_id,
            idempotency_key,
            question,
            question_message_id,
            dataset_id,
            dataset_generation,
            utc_now(),
            override_of_run_id,
        ),
    )
    if link_question_message:
        conn.execute("UPDATE messages SET run_id = ? WHERE id = ?", (run_id, question_message_id))
    return get_run(conn, run_id)


def get_run(conn, run_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM chat_runs WHERE id = ?", (run_id,)).fetchone()


def get_owned_run(conn, run_id: str, owner_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chat_runs WHERE id = ? AND owner_id = ?",
        (run_id, owner_id),
    ).fetchone()


def claim_next_run(conn, worker_id: str) -> Optional[str]:
    """Move the oldest queued run to ``running``; call inside a transaction."""

    row = conn.execute(
        "SELECT id FROM chat_runs WHERE status = 'queued' ORDER BY created_at, rowid LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    cursor = conn.execute(
        "UPDATE chat_runs SET status = 'running', worker_id = ?, started_at = ? "
        "WHERE id = ? AND status = 'queued'",
        (worker_id, utc_now(), row["id"]),
    )
    return str(row["id"]) if cursor.rowcount == 1 else None


def set_run_effective_question(conn, run_id: str, question: str) -> None:
    conn.execute(
        "UPDATE chat_runs SET effective_question = ? WHERE id = ? AND status = 'running'",
        (question, run_id),
    )


def set_run_context(conn, run_id: str, *, question: str, status: str, reason: str, payload: dict) -> bool:
    cursor = conn.execute(
        "UPDATE chat_runs SET effective_question=?, context_status=?, context_reason=?, "
        "context_resolution=? WHERE id=? AND status='running'",
        (question, status, reason, json.dumps(payload, ensure_ascii=False), run_id),
    )
    return cursor.rowcount == 1


def list_context_runs(conn, run, *, limit: int = 10) -> list[sqlite3.Row]:
    """Completed QA ancestors only; rowid breaks same-second timestamp ties.

    Legacy successful text answers have empty task/result/context fields.
    A clarification can be a succeeded RUN without being a completed QA turn.
    """
    rows = conn.execute(
        "SELECT id, question, effective_question, context_resolution, context_snapshot "
        "FROM chat_runs WHERE owner_id=? AND session_id=? AND dataset_id IS ? "
        "AND dataset_generation IS ? AND status='succeeded' "
        "AND task_kind IN ('', 'report_qa') AND result_kind IN ('', 'answer') "
        "AND answer_message_id IS NOT NULL "
        "AND context_status NOT IN ('failed', 'needs_clarification') "
        "AND rowid < (SELECT rowid FROM chat_runs WHERE id=?) "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?",
        (run['owner_id'], run['session_id'], run['dataset_id'], run['dataset_generation'],
         run['id'], limit),
    ).fetchall()
    return list(reversed(rows))


def set_context_snapshot(conn, run_id: str, snapshot: dict) -> None:
    conn.execute(
        "UPDATE chat_runs SET context_snapshot=? WHERE id=? AND status='running'",
        (json.dumps(snapshot, ensure_ascii=False), run_id),
    )


def finish_run(
    conn,
    run_id: str,
    *,
    status: str,
    error_code: str = "",
    error_message: str = "",
    answer_message_id: Optional[str] = None,
    result_kind: str = "",
    task_kind: str = "",
) -> bool:
    if status not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"not a terminal run status: {status}")
    cursor = conn.execute(
        "UPDATE chat_runs SET status = ?, error_code = ?, error_message = ?, "
        "answer_message_id = ?, finished_at = ?, result_kind = ?, task_kind = ? "
        "WHERE id = ? AND status = 'running'",
        (status, error_code, error_message, answer_message_id, utc_now(),
         result_kind, task_kind, run_id),
    )
    return cursor.rowcount == 1


def interrupt_running_runs(conn, *, error_code: str, error_message: str) -> int:
    """Runs a dead worker left ``running`` become ``interrupted``, never re-run."""

    cursor = conn.execute(
        "UPDATE chat_runs SET status = 'interrupted', error_code = ?, error_message = ?, "
        "finished_at = ? WHERE status = 'running'",
        (error_code, error_message, utc_now()),
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Import hand-offs (see webapp.handoff)
# ---------------------------------------------------------------------------


def get_handoff(conn, owner_id: str, idempotency_key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM import_handoffs WHERE owner_id = ? AND idempotency_key = ?",
        (owner_id, idempotency_key),
    ).fetchone()


def get_handoff_by_id(conn, handoff_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM import_handoffs WHERE id = ?", (handoff_id,)).fetchone()


def get_handoff_by_job(conn, job_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM import_handoffs WHERE job_id = ?", (job_id,)).fetchone()


def insert_handoff(
    conn,
    *,
    owner_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    source_session_id: Optional[str],
    target_session_id: str,
    created_target: bool,
) -> sqlite3.Row:
    handoff_id = new_id()
    now = utc_now()
    conn.execute(
        "INSERT INTO import_handoffs (id, owner_id, idempotency_key, request_fingerprint, "
        "source_session_id, target_session_id, created_target, state, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'started', ?, ?)",
        (handoff_id, owner_id, idempotency_key, request_fingerprint, source_session_id,
         target_session_id, int(created_target), now, now),
    )
    return get_handoff_by_id(conn, handoff_id)


def update_handoff(
    conn, handoff_id: str, *, state: str, job_id: Optional[str] = None, error_code: str = ""
) -> None:
    conn.execute(
        "UPDATE import_handoffs SET state = ?, job_id = COALESCE(?, job_id), error_code = ?, "
        "updated_at = ? WHERE id = ?",
        (state, job_id, error_code, utc_now(), handoff_id),
    )


def delete_handoff(conn, handoff_id: str) -> None:
    conn.execute("DELETE FROM import_handoffs WHERE id = ?", (handoff_id,))


def get_discovery_request(conn, owner_id: str, idempotency_key: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM discovery_requests WHERE owner_id = ? AND idempotency_key = ?",
        (owner_id, idempotency_key),
    ).fetchone()


def insert_discovery_request(
    conn,
    *,
    owner_id: str,
    session_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    request_message_id: str,
    result_message_id: str,
) -> None:
    conn.execute(
        "INSERT INTO discovery_requests (id, owner_id, session_id, idempotency_key, "
        "request_fingerprint, request_message_id, result_message_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), owner_id, session_id, idempotency_key, request_fingerprint,
         request_message_id, result_message_id, utc_now()),
    )


def list_unfinished_handoffs(conn, *, updated_before: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM import_handoffs WHERE state IN ('started', 'job_created') "
        "AND updated_at <= ? ORDER BY updated_at",
        (updated_before,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Worker heartbeats
# ---------------------------------------------------------------------------


def upsert_heartbeat(conn, *, queue: str, worker_id: str, pid: int, started_at: str,
                     runtime_config: dict | None = None) -> None:
    if runtime_config is None:
        # Preserve the old call shape, including fixtures/old-schema upgrade
        # checks. Current workers always provide their explicit configuration.
        conn.execute(
            "INSERT INTO worker_heartbeats (queue, worker_id, pid, started_at, heartbeat_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(queue) DO UPDATE SET worker_id=excluded.worker_id, "
            "pid=excluded.pid, started_at=excluded.started_at, heartbeat_at=excluded.heartbeat_at",
            (queue, worker_id, pid, started_at, utc_now()),
        )
        return
    conn.execute(
        "INSERT INTO worker_heartbeats (queue, worker_id, pid, started_at, heartbeat_at, runtime_config) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(queue) DO UPDATE SET worker_id = excluded.worker_id, "
        "pid = excluded.pid, started_at = excluded.started_at, heartbeat_at = excluded.heartbeat_at, "
        "runtime_config = excluded.runtime_config",
        (queue, worker_id, pid, started_at, utc_now(), json.dumps(runtime_config or {})),
    )


def list_heartbeats(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM worker_heartbeats").fetchall()
