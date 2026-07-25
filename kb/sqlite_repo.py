"""SQLite schema and insert helpers for normalized financial facts."""
# Code note: KB modules own SQLite schema compatibility and fact persistence helpers.

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SQLITE_SCHEMA_VERSION = 2
_KB_MANIFEST_TABLE = "kb_manifest"


_FINANCIAL_FACT_COLUMNS = {
    "company": "TEXT",
    "fiscal_year": "TEXT",
    "heading": "TEXT",
    "item_code": "TEXT",
    "note_ref": "TEXT",
    "subheading": "TEXT",
    "item_name": "TEXT",
    "value": "TEXT",
    "raw_value": "TEXT",
    "normalized_value": "TEXT",
    "source": "TEXT",
    # Slot disambiguators for value-lookup rows (period/value-type/unit). Added
    # via ALTER for existing DBs; empty for non-table facts.
    "period": "TEXT",
    "value_type": "TEXT",
    "unit": "TEXT",
    # Deterministic identity used as the Qdrant source id. Existing databases
    # receive the column through the same additive migration as slot fields.
    "fact_id": "TEXT",
}


def _financial_fact_column_names(conn) -> set[str]:
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(financial_facts)")
    return {str(row[1]).strip() for row in cur.fetchall()}


def sqlite_has_fact_columns(conn, required_columns=None) -> bool:
    required = set(required_columns or [])
    if not required:
        required = set(_FINANCIAL_FACT_COLUMNS.keys())
    existing = _financial_fact_column_names(conn)
    return required.issubset(existing)


def sqlite_has_populated_fact_values(conn) -> bool:
    if not sqlite_has_fact_columns(conn, {"raw_value", "normalized_value"}):
        return False

    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*)
        FROM financial_facts
        WHERE TRIM(COALESCE(raw_value, '')) = ''
           OR TRIM(COALESCE(normalized_value, '')) = ''
    """)
    return int(cur.fetchone()[0] or 0) == 0


def sqlite_has_stable_fact_ids(conn) -> bool:
    if not sqlite_has_fact_columns(conn, {"fact_id"}):
        return False
    cur = conn.cursor()
    cur.execute("""
        SELECT COUNT(*), COUNT(DISTINCT fact_id)
        FROM financial_facts
        WHERE TRIM(COALESCE(fact_id, '')) = ''
    """)
    empty_count, _empty_distinct = cur.fetchone()
    if int(empty_count or 0) != 0:
        return False
    total_count, distinct_count = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT fact_id) FROM financial_facts"
    ).fetchone()
    return int(total_count or 0) == int(distinct_count or 0)


def _create_manifest_table(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {_KB_MANIFEST_TABLE} (
            manifest_id INTEGER PRIMARY KEY CHECK (manifest_id = 1),
            source_path TEXT NOT NULL,
            source_sha256 TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            facts_count INTEGER NOT NULL,
            facts_sha256 TEXT NOT NULL
        )
    """)

def init_db(db_path: str, reset: bool = False):
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    if reset:
        cur.execute("DROP TABLE IF EXISTS financial_facts")
        cur.execute(f"DROP TABLE IF EXISTS {_KB_MANIFEST_TABLE}")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS financial_facts (
        company TEXT,
        fiscal_year TEXT,
        heading TEXT,
        item_code TEXT,
        note_ref TEXT,
        subheading TEXT,
        item_name TEXT,
        value TEXT,
        raw_value TEXT,
        normalized_value TEXT,
        source TEXT,
        period TEXT,
        value_type TEXT,
        unit TEXT,
        fact_id TEXT
        )
        """)

    existing_columns = _financial_fact_column_names(conn)
    for column_name, column_type in _FINANCIAL_FACT_COLUMNS.items():
        if column_name in existing_columns:
            continue
        cur.execute(
            f"ALTER TABLE financial_facts ADD COLUMN {column_name} {column_type}"
        )
    _create_manifest_table(conn)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_heading_item "
        "ON financial_facts (heading, item_name)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_note_ref "
        "ON financial_facts (note_ref)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_fact_id "
        "ON financial_facts (fact_id)"
    )
    conn.commit()
    return conn


def open_db_readonly(db_path: str):
    """Open an existing SQLite database without running migrations."""

    resolved = Path(db_path).resolve(strict=True)
    return sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)


def _normalize_fact_row(row):
    # Canonical column order, including the slot fields period/value_type/unit
    # (empty for non-table facts). Tuples from legacy builders lack the slot
    # fields and are padded; dict rows (table facts) carry them.
    if isinstance(row, dict):
        return (
            row.get("company", ""),
            row.get("fiscal_year", ""),
            row.get("heading", ""),
            row.get("item_code", ""),
            row.get("note_ref", ""),
            row.get("subheading", ""),
            row.get("item_name", ""),
            row.get("value", ""),
            row.get("raw_value", ""),
            row.get("normalized_value", ""),
            row.get("source", ""),
            row.get("period", ""),
            row.get("value_type", ""),
            row.get("unit", ""),
        )

    values = tuple(row)
    if len(values) == 8:
        company, heading, item_code, item_name, value, raw_value, normalized_value, source = values
        return (
            company, "", heading, item_code, "", "", item_name,
            value, raw_value, normalized_value, source, "", "", "",
        )
    if len(values) == 9:
        company, fiscal_year, heading, item_code, item_name, value, raw_value, normalized_value, source = values
        return (
            company, fiscal_year, heading, item_code, "", "", item_name,
            value, raw_value, normalized_value, source, "", "", "",
        )
    if len(values) == 10:
        company, fiscal_year, heading, item_code, subheading, item_name, value, raw_value, normalized_value, source = values
        return (
            company, fiscal_year, heading, item_code, "", subheading, item_name,
            value, raw_value, normalized_value, source, "", "", "",
        )
    if len(values) == 11:
        return (*values, "", "", "")
    if len(values) == 14:
        return values
    raise ValueError(f"financial_facts row must have 8, 9, 10, 11, or 14 values, got {len(values)}")


def normalize_financial_fact_rows(rows) -> list[tuple]:
    """Normalize and structurally validate parsed facts before persistence."""

    normalized_rows = [_normalize_fact_row(row) for row in (rows or [])]
    for index, row in enumerate(normalized_rows):
        heading = str(row[2] or "").strip()
        item_name = str(row[6] or "").strip()
        raw_value = str(row[8] or "").strip()
        normalized_value = str(row[9] or "").strip()
        source = str(row[10] or "").strip()
        if not heading or not item_name or not raw_value or not normalized_value or not source:
            raise ValueError(
                "invalid financial fact at index "
                f"{index}: heading, item_name, raw/normalized value and source are required"
            )
    return normalized_rows


def _fact_identity(row: tuple) -> str:
    payload = json.dumps(
        [str(value or "") for value in row],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def facts_sha256(rows) -> str:
    normalized_rows = normalize_financial_fact_rows(rows)
    digest = hashlib.sha256()
    for row in normalized_rows:
        digest.update(_fact_identity(row).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def sqlite_facts_sha256(conn) -> str:
    rows = conn.execute("""
        SELECT company, fiscal_year, heading, item_code, note_ref, subheading,
               item_name, value, raw_value, normalized_value, source, period,
               value_type, unit
        FROM financial_facts
        ORDER BY rowid
    """).fetchall()
    return facts_sha256(rows)


def insert_financial_facts(conn, rows):
    if not rows:
        return

    normalized_rows = normalize_financial_fact_rows(rows)
    existing_ids = {
        str(row[0])
        for row in conn.execute(
            "SELECT fact_id FROM financial_facts WHERE TRIM(COALESCE(fact_id, '')) != ''"
        ).fetchall()
    }
    identity_counts: dict[str, int] = {}
    rows_with_ids = []
    for row in normalized_rows:
        base_id = _fact_identity(row)
        occurrence = identity_counts.get(base_id, 0) + 1
        fact_id = base_id if occurrence == 1 else f"{base_id}:{occurrence}"
        while fact_id in existing_ids:
            occurrence += 1
            fact_id = f"{base_id}:{occurrence}"
        identity_counts[base_id] = occurrence
        existing_ids.add(fact_id)
        rows_with_ids.append((*row, fact_id))
    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO financial_facts (
            company,
            fiscal_year,
            heading,
            item_code,
            note_ref,
            subheading,
            item_name,
            value,
            raw_value,
            normalized_value,
            source,
            period,
            value_type,
            unit,
            fact_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, rows_with_ids)

    conn.commit()

def sqlite_has_facts(conn) -> bool:
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM financial_facts")
        return cur.fetchone()[0] > 0
    except sqlite3.OperationalError:
        return False


def sqlite_count_facts(conn) -> int:
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM financial_facts")
        return int(cur.fetchone()[0] or 0)
    except sqlite3.OperationalError:
        return 0


def write_kb_manifest(conn, manifest: dict[str, Any]) -> None:
    required = {
        "source_path",
        "source_sha256",
        "parser_version",
        "schema_version",
        "facts_count",
        "facts_sha256",
    }
    missing = sorted(required.difference(manifest))
    if missing:
        raise ValueError(f"KB manifest is missing fields: {', '.join(missing)}")
    _create_manifest_table(conn)
    conn.execute(
        f"""
        INSERT INTO {_KB_MANIFEST_TABLE} (
            manifest_id,
            source_path,
            source_sha256,
            parser_version,
            schema_version,
            facts_count,
            facts_sha256
        ) VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(manifest_id) DO UPDATE SET
            source_path = excluded.source_path,
            source_sha256 = excluded.source_sha256,
            parser_version = excluded.parser_version,
            schema_version = excluded.schema_version,
            facts_count = excluded.facts_count,
            facts_sha256 = excluded.facts_sha256
        """,
        (
            str(manifest["source_path"]),
            str(manifest["source_sha256"]),
            str(manifest["parser_version"]),
            int(manifest["schema_version"]),
            int(manifest["facts_count"]),
            str(manifest["facts_sha256"]),
        ),
    )
    conn.commit()


def read_kb_manifest(conn) -> dict[str, Any]:
    try:
        row = conn.execute(
            f"""
            SELECT source_path, source_sha256, parser_version, schema_version,
                   facts_count, facts_sha256
            FROM {_KB_MANIFEST_TABLE}
            WHERE manifest_id = 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}
    return {
        "source_path": str(row[0]),
        "source_sha256": str(row[1]),
        "parser_version": str(row[2]),
        "schema_version": int(row[3]),
        "facts_count": int(row[4]),
        "facts_sha256": str(row[5]),
    }


def kb_manifest_matches(conn, expected: dict[str, Any]) -> bool:
    actual = read_kb_manifest(conn)
    for field in ("source_path", "source_sha256", "parser_version", "schema_version"):
        if str(actual.get(field, "")) != str(expected.get(field, "")):
            return False
    return (
        sqlite_has_facts(conn)
        and sqlite_has_stable_fact_ids(conn)
        and sqlite_count_facts(conn) == int(actual.get("facts_count", -1))
        and sqlite_facts_sha256(conn) == str(actual.get("facts_sha256", ""))
    )


def validate_kb_database(conn, expected_manifest: dict[str, Any]) -> None:
    quick_check = conn.execute("PRAGMA quick_check").fetchone()
    if not quick_check or str(quick_check[0]).lower() != "ok":
        raise ValueError(f"SQLite integrity check failed: {quick_check}")
    if not sqlite_has_fact_columns(conn):
        raise ValueError("SQLite financial_facts schema is incomplete")
    if not sqlite_has_populated_fact_values(conn) or not sqlite_has_stable_fact_ids(conn):
        raise ValueError("SQLite facts contain empty values or unstable ids")
    if sqlite_count_facts(conn) != int(expected_manifest.get("facts_count", -1)):
        raise ValueError("SQLite fact count does not match the ingestion manifest")
    if sqlite_facts_sha256(conn) != str(expected_manifest.get("facts_sha256", "")):
        raise ValueError("SQLite fact fingerprint does not match the ingestion manifest")
    if read_kb_manifest(conn) != expected_manifest:
        raise ValueError("SQLite ingestion manifest does not match the staged build")


# Note-schedule title like "5. Phải thu về cho vay ngắn hạn" / "17a. Phải trả
# ngắn hạn khác", optionally with an em-dash section suffix ("— Nguyên giá").
_NOTE_TITLE_RE = None


def derive_keyword_augmentation(conn) -> dict:
    """Dataset-derived routing keywords: {table_heading: set(keyword)}.

    Two sources, both deterministic reads of the built KB:
    1. Note-schedule titles from numbered note subheadings -> NOTE keywords.
    2. Primary-statement lines that carry a note_ref -> keywords for their own
       table AND for NOTE (the schedule detailing them lives in the notes).

    Merged over the static ALLOWED_KEYWORDS by set_dynamic_keywords so the
    keyworder can route line items the hand-written vocabulary never listed
    (e.g. "trả trước cho người bán ngắn hạn", "phải thu về cho vay ngắn hạn").
    """
    import re

    global _NOTE_TITLE_RE
    if _NOTE_TITLE_RE is None:
        _NOTE_TITLE_RE = re.compile(r"^\s*\d{1,2}[a-zđ]?\.\s+(?P<title>.+?)\s*(?:—.*)?$")

    note_heading = "THUYẾT MINH BÁO CÁO TÀI CHÍNH"
    augmented: dict[str, set[str]] = {note_heading: set()}
    cur = conn.cursor()

    cur.execute(
        "SELECT DISTINCT subheading FROM financial_facts "
        "WHERE heading = ? AND subheading != ''",
        (note_heading,),
    )
    for (subheading,) in cur.fetchall():
        match = _NOTE_TITLE_RE.match(str(subheading or ""))
        if match:
            title = match.group("title").strip().lower()
            if len(title) >= 4:
                augmented[note_heading].add(title)

    cur.execute(
        "SELECT DISTINCT heading, item_name FROM financial_facts "
        "WHERE heading != ? AND note_ref IS NOT NULL AND note_ref != ''",
        (note_heading,),
    )
    for heading, item_name in cur.fetchall():
        stem = str(item_name or "").split("|", 1)[0].strip().lower()
        if len(stem) >= 4:
            augmented.setdefault(str(heading), set()).add(stem)
            # The note_ref means a note schedule details this line.
            augmented[note_heading].add(stem)

    return {table: kws for table, kws in augmented.items() if kws}
