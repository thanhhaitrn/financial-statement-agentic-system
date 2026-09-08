"""Small SQLite cache for structured web evidence."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from schemas.web_evidence import WebEvidence


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
                """
            )

    def put_many(self, items: list[WebEvidence]) -> None:
        if not items:
            return
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
                    content_hash, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    published_at=excluded.published_at,
                    retrieved_at=excluded.retrieved_at,
                    query=excluded.query,
                    payload_json=excluded.payload_json
                """,
                rows,
            )

    def get(self, *, ticker: str, limit: int) -> list[WebEvidence]:
        ticker_key = str(ticker or "").strip().upper()
        if not ticker_key:
            return []
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
