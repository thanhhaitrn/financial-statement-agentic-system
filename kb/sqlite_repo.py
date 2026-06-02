"""SQLite schema and insert helpers for normalized financial facts."""
# Code note: KB modules own SQLite schema compatibility and fact persistence helpers.

import sqlite3


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

def init_db(db_path: str, reset: bool = False):
    conn  = sqlite3.connect(db_path)
    cur = conn.cursor()

    if reset:
        cur.execute("DROP TABLE IF EXISTS financial_facts")

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
        source TEXT
        )
        """)

    existing_columns = _financial_fact_column_names(conn)
    for column_name, column_type in _FINANCIAL_FACT_COLUMNS.items():
        if column_name in existing_columns:
            continue
        cur.execute(
            f"ALTER TABLE financial_facts ADD COLUMN {column_name} {column_type}"
        )
    conn.commit()
    return conn


def _normalize_fact_row(row):
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
        )

    values = tuple(row)
    if len(values) == 8:
        company, heading, item_code, item_name, value, raw_value, normalized_value, source = values
        return (
            company,
            "",
            heading,
            item_code,
            "",
            "",
            item_name,
            value,
            raw_value,
            normalized_value,
            source,
        )
    if len(values) == 9:
        company, fiscal_year, heading, item_code, item_name, value, raw_value, normalized_value, source = values
        return (
            company,
            fiscal_year,
            heading,
            item_code,
            "",
            "",
            item_name,
            value,
            raw_value,
            normalized_value,
            source,
        )
    if len(values) == 10:
        company, fiscal_year, heading, item_code, subheading, item_name, value, raw_value, normalized_value, source = values
        return (
            company,
            fiscal_year,
            heading,
            item_code,
            "",
            subheading,
            item_name,
            value,
            raw_value,
            normalized_value,
            source,
        )
    if len(values) == 11:
        return values
    raise ValueError(f"financial_facts row must have 8, 9, 10, or 11 values, got {len(values)}")


def insert_financial_facts(conn, rows):
    if not rows:
        return

    normalized_rows = [_normalize_fact_row(row) for row in rows]
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
            source
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, normalized_rows)

    conn.commit()

def sqlite_has_facts(conn) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM financial_facts")
    return cur.fetchone()[0] > 0


def sqlite_count_facts(conn) -> int:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM financial_facts")
    return int(cur.fetchone()[0] or 0)
