"""SQLite schema and insert helpers for normalized financial facts."""
# Code note: KB modules own SQLite schema compatibility and fact persistence helpers.

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any


SQLITE_SCHEMA_VERSION = 6
_KB_MANIFEST_TABLE = "kb_manifest"

TYPED_FACT_METADATA_FIELDS = (
    "row_label",
    "column_label",
    "value_kind",
    "parsed_value",
    "period_label",
    "period_role",
    "aggregation_level",
    "section_path",
    "block_id",
    "source_page",
    "metric_label",
    "entity_label",
    "scope_label",
    "counterparty",
    "transaction_type",
    "movement_type",
    "geography",
    "policy_topic",
    "section_key",
)
_ALLOWED_VALUE_KINDS = {
    "amount",
    "count",
    "percent",
    "multiple",
    "date",
    "identifier",
    "entity",
    "text",
}
_ALLOWED_PERIOD_ROLES = {"", "current", "previous"}
_ALLOWED_AGGREGATION_LEVELS = {"component", "total"}


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
    # Canonical typed-cell metadata shared by SQLite and vector payloads.
    "row_label": "TEXT",
    "column_label": "TEXT",
    "value_kind": "TEXT",
    "parsed_value": "TEXT",
    "period_label": "TEXT",
    "period_role": "TEXT",
    "aggregation_level": "TEXT",
    "section_path": "TEXT",
    "block_id": "TEXT",
    "source_page": "TEXT",
    # Semantic axes consumed by exact slot retrieval. Empty means the parser
    # could not bind that axis confidently; raw row/column labels stay intact.
    "metric_label": "TEXT",
    "entity_label": "TEXT",
    "scope_label": "TEXT",
    "counterparty": "TEXT",
    "transaction_type": "TEXT",
    "movement_type": "TEXT",
    "geography": "TEXT",
    "policy_topic": "TEXT",
    # Canonical primary-statement section identity (for example
    # ``tong_tai_san`` or ``no_phai_tra``). Empty outside recognized sections.
    "section_key": "TEXT",
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
            metadata_sha256 TEXT NOT NULL DEFAULT '',
            parser_version TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            facts_count INTEGER NOT NULL,
            facts_sha256 TEXT NOT NULL
        )
    """)
    manifest_columns = {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_KB_MANIFEST_TABLE})")
    }
    if "metadata_sha256" not in manifest_columns:
        conn.execute(
            f"ALTER TABLE {_KB_MANIFEST_TABLE} "
            "ADD COLUMN metadata_sha256 TEXT NOT NULL DEFAULT ''"
        )
    for field in ("converter_identity", "converter_provider_version"):
        if field not in manifest_columns:
            conn.execute(f"ALTER TABLE {_KB_MANIFEST_TABLE} ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")

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
        row_label TEXT,
        column_label TEXT,
        value_kind TEXT,
        parsed_value TEXT,
        period_label TEXT,
        period_role TEXT,
        aggregation_level TEXT,
        section_path TEXT,
        block_id TEXT,
        source_page TEXT,
        metric_label TEXT,
        entity_label TEXT,
        scope_label TEXT,
        counterparty TEXT,
        transaction_type TEXT,
        movement_type TEXT,
        geography TEXT,
        policy_topic TEXT,
        section_key TEXT,
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
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_semantic_slots "
        "ON financial_facts "
        "(heading, metric_label, entity_label, scope_label, period_role, value_type)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_semantic_dimensions "
        "ON financial_facts "
        "(transaction_type, movement_type, policy_topic, geography, counterparty)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_section_key "
        "ON financial_facts (heading, section_key, period_role)"
    )
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_financial_facts_block_id "
        "ON financial_facts (block_id)"
    )
    conn.commit()
    return conn


def open_db_readonly(db_path: str):
    """Open an existing SQLite database without running migrations."""

    resolved = Path(db_path).resolve(strict=True)
    return sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)


def _canonical_aggregation(value) -> str:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return "component"
    return normalized


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
            row.get("row_label", ""),
            row.get("column_label", ""),
            row.get("value_kind", ""),
            row.get("parsed_value", ""),
            row.get("period_label", ""),
            row.get("period_role", ""),
            _canonical_aggregation(row.get("aggregation_level", "")),
            row.get("section_path", ""),
            row.get("block_id", ""),
            row.get("source_page", ""),
            row.get("metric_label", ""),
            row.get("entity_label", ""),
            row.get("scope_label", ""),
            row.get("counterparty", ""),
            row.get("transaction_type", ""),
            row.get("movement_type", ""),
            row.get("geography", ""),
            row.get("policy_topic", ""),
            row.get("section_key", ""),
        )

    values = tuple(row)
    typed_padding = (
        "",  # row_label
        "",  # column_label
        "",  # value_kind
        "",  # parsed_value
        "",  # period_label
        "",  # period_role
        "component",
        "",  # section_path
        "",  # block_id
        "",  # source_page
        "",  # metric_label
        "",  # entity_label
        "",  # scope_label
        "",  # counterparty
        "",  # transaction_type
        "",  # movement_type
        "",  # geography
        "",  # policy_topic
        "",  # section_key
    )
    if len(values) == 8:
        company, heading, item_code, item_name, value, raw_value, normalized_value, source = values
        return (
            company, "", heading, item_code, "", "", item_name,
            value, raw_value, normalized_value, source, "", "", "", *typed_padding,
        )
    if len(values) == 9:
        company, fiscal_year, heading, item_code, item_name, value, raw_value, normalized_value, source = values
        return (
            company, fiscal_year, heading, item_code, "", "", item_name,
            value, raw_value, normalized_value, source, "", "", "", *typed_padding,
        )
    if len(values) == 10:
        company, fiscal_year, heading, item_code, subheading, item_name, value, raw_value, normalized_value, source = values
        return (
            company, fiscal_year, heading, item_code, "", subheading, item_name,
            value, raw_value, normalized_value, source, "", "", "", *typed_padding,
        )
    if len(values) == 11:
        return (*values, "", "", "", *typed_padding)
    if len(values) == 14:
        return (*values, *typed_padding)
    if len(values) == 24:
        normalized = [*values, "", "", "", "", "", "", "", "", ""]
        normalized[20] = _canonical_aggregation(normalized[20])
        return tuple(normalized)
    if len(values) == 27:
        normalized = [*values, "", "", "", "", "", ""]
        normalized[20] = _canonical_aggregation(normalized[20])
        return tuple(normalized)
    if len(values) == 32:
        normalized = [*values, ""]
        normalized[20] = _canonical_aggregation(normalized[20])
        return tuple(normalized)
    if len(values) == 33:
        normalized = list(values)
        normalized[20] = _canonical_aggregation(normalized[20])
        return tuple(normalized)
    raise ValueError(
        "financial_facts row must have 8, 9, 10, 11, 14, 24, 27, 32, or 33 values, "
        f"got {len(values)}"
    )


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
        # Legacy rows are padded with aggregation_level="component" for query
        # compatibility; that default alone does not turn them into canonical
        # typed facts.
        typed_values = [
            str(row[position] or "").strip()
            for position in (*range(14, 20), *range(21, 33))
        ]
        if not any(typed_values):
            continue
        required_typed = {
            "row_label": row[14],
            "column_label": row[15],
            "value_kind": row[16],
            "parsed_value": row[17],
            "aggregation_level": row[20],
            "section_path": row[21],
            "block_id": row[22],
        }
        missing_typed = sorted(
            name
            for name, value in required_typed.items()
            if not str(value or "").strip()
        )
        if missing_typed:
            raise ValueError(
                f"invalid typed metadata at index {index}: "
                f"missing {', '.join(missing_typed)}"
            )
        value_kind = str(row[16] or "").strip().lower()
        if value_kind not in _ALLOWED_VALUE_KINDS:
            raise ValueError(
                f"invalid typed metadata at index {index}: "
                f"unsupported value_kind={value_kind!r}"
            )
        period_role = str(row[19] or "").strip().lower()
        if period_role not in _ALLOWED_PERIOD_ROLES:
            raise ValueError(
                f"invalid typed metadata at index {index}: "
                f"unsupported period_role={period_role!r}"
            )
        aggregation = str(row[20] or "").strip().lower()
        if aggregation not in _ALLOWED_AGGREGATION_LEVELS:
            raise ValueError(
                f"invalid typed metadata at index {index}: "
                f"unsupported aggregation_level={aggregation!r}"
            )
        source_page = str(row[23] or "").strip()
        if source_page and not source_page.isdigit():
            raise ValueError(
                f"invalid typed metadata at index {index}: "
                f"source_page must be a positive integer, got {source_page!r}"
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
               value_type, unit, row_label, column_label, value_kind,
               parsed_value, period_label, period_role, aggregation_level,
               section_path, block_id, source_page, metric_label, entity_label,
               scope_label, counterparty, transaction_type, movement_type,
               geography, policy_topic, section_key
        FROM financial_facts
        ORDER BY rowid
    """).fetchall()
    return facts_sha256(rows)


def sqlite_has_valid_typed_metadata(conn) -> bool:
    """Re-run the canonical row contract against persisted SQLite values."""

    if not sqlite_has_fact_columns(conn):
        return False
    rows = conn.execute("""
        SELECT company, fiscal_year, heading, item_code, note_ref, subheading,
               item_name, value, raw_value, normalized_value, source, period,
               value_type, unit, row_label, column_label, value_kind,
               parsed_value, period_label, period_role, aggregation_level,
               section_path, block_id, source_page, metric_label, entity_label,
               scope_label, counterparty, transaction_type, movement_type,
               geography, policy_topic, section_key
        FROM financial_facts
        ORDER BY rowid
    """).fetchall()
    try:
        normalize_financial_fact_rows(rows)
    except ValueError:
        return False
    return True


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
            row_label,
            column_label,
            value_kind,
            parsed_value,
            period_label,
            period_role,
            aggregation_level,
            section_path,
            block_id,
            source_page,
            metric_label,
            entity_label,
            scope_label,
            counterparty,
            transaction_type,
            movement_type,
            geography,
            policy_topic,
            section_key,
            fact_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            metadata_sha256,
            parser_version,
            schema_version,
            facts_count,
            facts_sha256,
            converter_identity,
            converter_provider_version
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(manifest_id) DO UPDATE SET
            source_path = excluded.source_path,
            source_sha256 = excluded.source_sha256,
            metadata_sha256 = excluded.metadata_sha256,
            parser_version = excluded.parser_version,
            schema_version = excluded.schema_version,
            facts_count = excluded.facts_count,
            facts_sha256 = excluded.facts_sha256,
            converter_identity = excluded.converter_identity,
            converter_provider_version = excluded.converter_provider_version
        """,
        (
            str(manifest["source_path"]),
            str(manifest["source_sha256"]),
            str(manifest.get("metadata_sha256", "")),
            str(manifest["parser_version"]),
            int(manifest["schema_version"]),
            int(manifest["facts_count"]),
            str(manifest["facts_sha256"]),
            str(manifest.get("converter_identity", "")),
            str(manifest.get("converter_provider_version", "")),
        ),
    )
    conn.commit()


def read_kb_manifest(conn) -> dict[str, Any]:
    try:
        row = conn.execute(
            f"""
            SELECT source_path, source_sha256, metadata_sha256, parser_version, schema_version,
                   facts_count, facts_sha256
            FROM {_KB_MANIFEST_TABLE}
            WHERE manifest_id = 1
            """
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if row is None:
        return {}
    result = {
        "source_path": str(row[0]),
        "source_sha256": str(row[1]),
        "metadata_sha256": str(row[2]),
        "parser_version": str(row[3]),
        "schema_version": int(row[4]),
        "facts_count": int(row[5]),
        "facts_sha256": str(row[6]),
    }
    try:
        converter = conn.execute(
            f"SELECT converter_identity, converter_provider_version FROM {_KB_MANIFEST_TABLE} WHERE manifest_id = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        converter = None  # Pre-converter Markdown-only databases remain valid.
    if converter and converter[0]:
        result.update(converter_identity=str(converter[0]), converter_provider_version=str(converter[1]))
    return result


def kb_manifest_matches(conn, expected: dict[str, Any]) -> bool:
    actual = read_kb_manifest(conn)
    for field in (
        "source_path",
        "source_sha256",
        "metadata_sha256",
        "parser_version",
        "schema_version",
        "converter_identity",
        "converter_provider_version",
    ):
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
    if (
        not sqlite_has_populated_fact_values(conn)
        or not sqlite_has_stable_fact_ids(conn)
        or not sqlite_has_valid_typed_metadata(conn)
    ):
        raise ValueError(
            "SQLite facts contain empty values, invalid typed metadata, "
            "or unstable ids"
        )
    if sqlite_count_facts(conn) != int(expected_manifest.get("facts_count", -1)):
        raise ValueError("SQLite fact count does not match the ingestion manifest")
    if sqlite_facts_sha256(conn) != str(expected_manifest.get("facts_sha256", "")):
        raise ValueError("SQLite fact fingerprint does not match the ingestion manifest")
    if read_kb_manifest(conn) != expected_manifest:
        raise ValueError("SQLite ingestion manifest does not match the staged build")


# Note-schedule title like "5. Phải thu về cho vay ngắn hạn" / "17a. Phải trả
# ngắn hạn khác", optionally with an em-dash section suffix ("— Nguyên giá").
_NOTE_TITLE_RE = None

_SLOT_LEXICON_FOOTNOTE_RE = re.compile(
    r"\s*\((?:[ivxlcdm]+|[a-zđ]|\d+)\)\s*$",
    flags=re.IGNORECASE,
)
_SLOT_LEXICON_ENUMERATOR_RE = re.compile(
    r"^\s*(?:\([a-zđ]\)|\d{1,3}[a-zđ]?[.)])\s+",
    flags=re.IGNORECASE,
)
_SLOT_LEXICON_RANGE_RE = re.compile(
    r"^\d+(?:[.,]\d+)?\s*(?:-|–|—|đến|to)\s*"
    r"\d+(?:[.,]\d+)?(?:\s*(?:năm|tháng|ngày|%))?$",
    flags=re.IGNORECASE,
)
_SLOT_LEXICON_DATE_RE = re.compile(
    r"^(?:(?:19|20)\d{2}|\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2})$"
)
_SLOT_LEXICON_GENERIC_METRICS = {
    "chi tieu",
    "cong",
    "dien giai",
    "don vi tinh",
    "gia tri",
    "khoan muc",
    "ma so",
    "nam nay",
    "nam truoc",
    "noi dung",
    "so du",
    "thuyet minh",
    "tong",
    "tong cong",
}
_SLOT_LEXICON_GENERIC_ENTITIES = {
    "ban tai san co dinh",
    "chi phi khac",
    "co tuc",
    "co tuc duoc chia",
    "cong",
    "gop von",
    "ho tro ban hang",
    "hoan tra",
    "khac",
    "loi nhuan duoc chia",
    "mua hang hoa",
    "mua tai san co dinh",
    "nam nay",
    "nam truoc",
    "phan bo trong nam",
    "thu nhap khac",
    "tong",
    "tong cong",
    "vay them",
}


def _slot_lexicon_normalized_key(value: Any) -> str:
    text = str(value or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _clean_slot_lexicon_phrase(value: Any, *, axis: str) -> str:
    """Return one bounded canonical phrase, or empty for parser/noise artifacts."""

    phrase = " ".join(str(value or "").replace("\xa0", " ").split())
    phrase = phrase.strip(" \t\r\n|*_`#;:,")
    phrase = _SLOT_LEXICON_ENUMERATOR_RE.sub("", phrase)
    phrase = _SLOT_LEXICON_FOOTNOTE_RE.sub("", phrase).strip(" -–—")
    key = _slot_lexicon_normalized_key(phrase)
    tokens = key.split()

    if (
        not key
        or len(key) < 3
        or len(tokens) > 24
        or len(phrase) > 180
        or not any(char.isalpha() for char in phrase)
        or "|" in phrase
        or _SLOT_LEXICON_RANGE_RE.fullmatch(phrase)
        or _SLOT_LEXICON_DATE_RE.fullmatch(phrase)
        or key in {"trang", "page", "vnd", "vnd vnd", "empty", "null", "none"}
        or key.endswith("vnd")
        or re.fullmatch(r"\d+(?:\s+\d+)*", key)
        or re.match(r"^\(\s*\d+\s*=", phrase)
    ):
        return ""

    generic = (
        _SLOT_LEXICON_GENERIC_METRICS
        if axis == "metric"
        else _SLOT_LEXICON_GENERIC_ENTITIES
    )
    if key in generic:
        return ""
    return phrase.lower()


def derive_query_slot_lexicon(conn) -> dict[str, tuple[str, ...]]:
    """Derive dataset-scoped metric/entity phrases from canonical typed facts.

    The lexicon is intentionally narrower than raw row text: only typed
    ``metric_label``, ``entity_label``, and ``counterparty`` axes participate.
    This avoids teaching query parsing page headers, values, or row blobs while
    still covering report-specific metrics, asset classes, and legal names.
    """

    required_columns = {"metric_label", "entity_label", "counterparty"}
    if not sqlite_has_fact_columns(conn, required_columns):
        return {"metric": (), "entity": ()}

    by_axis: dict[str, dict[str, str]] = {"metric": {}, "entity": {}}
    column_axes = (
        ("metric_label", "metric"),
        ("entity_label", "entity"),
        ("counterparty", "entity"),
    )
    for column, axis in column_axes:
        rows = conn.execute(
            f"""
            SELECT DISTINCT {column}
            FROM financial_facts
            WHERE TRIM(COALESCE({column}, '')) != ''
            ORDER BY {column}
            """
        ).fetchall()
        for (raw_value,) in rows:
            phrase = _clean_slot_lexicon_phrase(raw_value, axis=axis)
            normalized = _slot_lexicon_normalized_key(phrase)
            if not normalized:
                continue
            previous = by_axis[axis].get(normalized)
            if previous is None or (phrase.casefold(), phrase) < (
                previous.casefold(),
                previous,
            ):
                by_axis[axis][normalized] = phrase

    return {
        axis: tuple(
            phrase
            for _normalized, phrase in sorted(
                phrases.items(),
                key=lambda item: (
                    -len(item[0].split()),
                    -len(item[0]),
                    item[0],
                    item[1],
                ),
            )
        )
        for axis, phrases in by_axis.items()
    }


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
