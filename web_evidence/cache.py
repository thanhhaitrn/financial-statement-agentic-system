"""Small SQLite cache for structured web evidence.

Holds public news only, shared by every user: every row holds source fields
only. The asking user's question (``query``) and any caller-supplied company
name are blanked on write and scrubbed from older files, so one user's
request can never come back in another user's results. Per-question analysis
never lands here. ``published_ts`` (UTC epoch) lets a date window be applied before
the row limit, and ``web_coverage`` records which span of a ticker's news
history the source has actually been read for.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from schemas.web_evidence import WebEvidence
from web_evidence.dates import parse_published


@dataclass(frozen=True)
class Coverage:
    """News for ``ticker`` published in [covered_from, covered_until] has been
    listed from the source; ``covered_until`` is when that listing ran."""

    ticker: str
    covered_from: float
    covered_until: float


# PRAGMA user_version of a cache file whose rows hold source fields only.
_SCRUBBED_VERSION = 1


def _published_ts(item: WebEvidence) -> Optional[float]:
    moment = parse_published(item.published_at)
    return moment.timestamp() if moment else None


def shared_copy(item: WebEvidence) -> WebEvidence:
    """The form of an article that may be shared: no request data."""

    return item.model_copy(update={"query": "", "company": ""})


class WebEvidenceCache:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS web_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    intent TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    company TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    publisher TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    query TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS web_evidence_source_hash
                    ON web_evidence(source_url, content_hash);
                CREATE INDEX IF NOT EXISTS web_evidence_ticker_retrieved
                    ON web_evidence(ticker, retrieved_at DESC);
                CREATE TABLE IF NOT EXISTS web_coverage (
                    ticker TEXT PRIMARY KEY,
                    covered_from REAL NOT NULL,
                    covered_until REAL NOT NULL
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(web_evidence)")}
            if "published_ts" not in columns:
                # Older cache files: add the column and backfill it once.
                connection.execute("ALTER TABLE web_evidence ADD COLUMN published_ts REAL")
                rows = connection.execute(
                    "SELECT evidence_id, published_at FROM web_evidence"
                ).fetchall()
                connection.executemany(
                    "UPDATE web_evidence SET published_ts = ? WHERE evidence_id = ?",
                    [
                        (moment.timestamp(), row["evidence_id"])
                        for row in rows
                        if (moment := parse_published(row["published_at"])) is not None
                    ],
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS web_evidence_ticker_published "
                "ON web_evidence(ticker, published_ts DESC)"
            )
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) < _SCRUBBED_VERSION:
                # Files written before rows were request-free: drop the stored
                # questions and company names from both columns and payloads.
                rows = connection.execute("SELECT evidence_id, payload_json FROM web_evidence").fetchall()
                updates = []
                for row in rows:
                    try:
                        payload = json.loads(row["payload_json"])
                    except (TypeError, ValueError):
                        payload = {}
                    payload["query"] = ""
                    payload["company"] = ""
                    updates.append((json.dumps(payload, ensure_ascii=False, sort_keys=True), row["evidence_id"]))
                connection.executemany(
                    "UPDATE web_evidence SET query = '', company = '', payload_json = ? WHERE evidence_id = ?",
                    updates,
                )
                connection.execute(f"PRAGMA user_version = {_SCRUBBED_VERSION}")

    def put_many(self, items: list[WebEvidence]) -> None:
        if not items:
            return
        items = [shared_copy(item) for item in items]
        rows = []
        for item in items:
            payload = item.model_dump(mode="json")
            rows.append(
                (
                    item.evidence_id,
                    item.intent,
                    item.ticker,
                    item.company,
                    item.title,
                    item.content,
                    item.source_url,
                    item.publisher,
                    item.published_at,
                    item.retrieved_at,
                    item.query,
                    item.content_hash,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    _published_ts(item),
                )
            )
        with self._connect() as connection:
            # Keep the latest revision, not multiple copies of a changed URL.
            connection.executemany(
                "DELETE FROM web_evidence WHERE source_url = ? AND content_hash != ?",
                [(item.source_url, item.content_hash) for item in items],
            )
            connection.executemany(
                """
                INSERT INTO web_evidence (
                    evidence_id, intent, ticker, company, title, content,
                    source_url, publisher, published_at, retrieved_at, query,
                    content_hash, payload_json, published_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    published_at=excluded.published_at,
                    retrieved_at=excluded.retrieved_at,
                    query=excluded.query,
                    payload_json=excluded.payload_json,
                    published_ts=excluded.published_ts
                """,
                rows,
            )

    def get(
        self,
        *,
        ticker: str,
        limit: int,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
    ) -> list[WebEvidence]:
        ticker_key = str(ticker or "").strip().upper()
        if not ticker_key:
            return []
        if since is not None or until is not None:
            # Window first, then limit: an article without a known date is
            # never inside a window.
            lower = since.timestamp() if since else float("-inf")
            upper = until.timestamp() if until else float("inf")
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT payload_json FROM web_evidence
                    WHERE ticker = ? AND intent = 'company_news'
                      AND published_ts IS NOT NULL AND published_ts >= ? AND published_ts <= ?
                    ORDER BY published_ts DESC, retrieved_at DESC
                    LIMIT ?
                    """,
                    (ticker_key, lower, upper, int(limit)),
                ).fetchall()
            return [WebEvidence.model_validate(json.loads(row["payload_json"])) for row in rows]
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM web_evidence
                WHERE ticker = ? AND intent = 'company_news'
                ORDER BY
                    CASE WHEN published_at = '' THEN 1 ELSE 0 END,
                    published_at DESC,
                    retrieved_at DESC
                LIMIT ?
                """,
                (ticker_key, int(limit)),
            ).fetchall()
        return [WebEvidence.model_validate(json.loads(row["payload_json"])) for row in rows]

    def put_listing(self, items: list[WebEvidence]) -> int:
        """Store news-list entries (``listing_only``) for URLs not held yet:
        a list entry never replaces an article whose text is stored."""

        new = []
        with self._connect() as connection:
            for item in items:
                if connection.execute("SELECT 1 FROM web_evidence WHERE source_url = ?",
                                      (item.source_url,)).fetchone() is None:
                    new.append(item)
        self.put_many(new)
        return len(new)

    def count_window(self, *, ticker: str, since: datetime, until: datetime) -> dict[str, int]:
        """How many of the window's articles are listed, by stored state."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM web_evidence WHERE ticker = ? AND intent = 'company_news' "
                "AND published_ts IS NOT NULL AND published_ts >= ? AND published_ts <= ?",
                (str(ticker or "").strip().upper(), since.timestamp(), until.timestamp()),
            ).fetchall()
        counts = {"full": 0, "summary_only": 0, "listing_only": 0}
        for row in rows:
            status = str(json.loads(row["payload_json"]).get("content_status") or "full")
            counts[status] = counts.get(status, 0) + 1
        counts["listed"] = len(rows)
        return counts

    def coverage(self, ticker: str) -> Optional[Coverage]:
        key = str(ticker or "").strip().upper()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT covered_from, covered_until FROM web_coverage WHERE ticker = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return Coverage(key, float(row["covered_from"]), float(row["covered_until"]))

    def record_coverage(self, ticker: str, covered_from: datetime, covered_until: datetime) -> Coverage:
        """Merge a newly listed span into the ticker's coverage. A listing
        always runs back from "now", so it joins the previous span when it
        reaches it; a gap means the older span can no longer be trusted."""

        key = str(ticker or "").strip().upper()
        start, end = covered_from.timestamp(), covered_until.timestamp()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT covered_from, covered_until FROM web_coverage WHERE ticker = ?", (key,)
            ).fetchone()
            if row is not None and start <= float(row["covered_until"]):
                start = min(start, float(row["covered_from"]))
            connection.execute(
                "INSERT INTO web_coverage (ticker, covered_from, covered_until) VALUES (?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET covered_from = excluded.covered_from, "
                "covered_until = excluded.covered_until",
                (key, start, end),
            )
        return Coverage(key, start, end)

    @staticmethod
    def age_seconds(items: list[WebEvidence], *, now: datetime | None = None) -> float | None:
        if not items:
            return None
        current = now or datetime.now(timezone.utc)
        timestamps = []
        for item in items:
            try:
                value = datetime.fromisoformat(item.retrieved_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            timestamps.append(value)
        if not timestamps:
            return None
        return max((current - max(timestamps)).total_seconds(), 0.0)
