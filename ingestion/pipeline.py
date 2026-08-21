"""Build the SQLite knowledge base from a registered dataset document."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from ingestion.kb_builder import build_fact_rows
from ingestion.frontmatter_parser import build_frontmatter_rows
from ingestion.note_parser import build_note_rows, infer_company, infer_fiscal_year
from ingestion.table_parser import attach_context
from kb.sqlite_repo import (
    SQLITE_SCHEMA_VERSION,
    facts_sha256,
    init_db,
    insert_financial_facts,
    kb_manifest_matches,
    normalize_financial_fact_rows,
    open_db_readonly,
    validate_kb_database,
    write_kb_manifest,
)
from schemas.datasets import DatasetRecord


PARSER_CONTRACT_VERSION = "agentfinx-parser-v5"


def _temporary_sibling(path: Path, *, suffix: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=suffix,
        dir=str(path.parent),
    )
    os.close(descriptor)
    return Path(temp_name)


def _stage_raw_tables(raw_path: Path, tables_with_context: list[dict]) -> Path:
    staged_path = _temporary_sibling(raw_path, suffix=".tmp")
    completed = False
    try:
        with staged_path.open("w", encoding="utf-8") as handle:
            json.dump(tables_with_context, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # Prove the staged artifact is valid JSON before it can replace the
        # previous parser output.
        with staged_path.open("r", encoding="utf-8") as handle:
            decoded = json.load(handle)
        if not isinstance(decoded, list):
            raise ValueError("raw tables artifact must contain a JSON list")
        completed = True
        return staged_path
    finally:
        if not completed:
            staged_path.unlink(missing_ok=True)


def _write_raw_tables(dataset: DatasetRecord, tables_with_context: list[dict]) -> None:
    raw_path = Path(dataset.raw_tables_path)
    staged_path = _stage_raw_tables(raw_path, tables_with_context)
    os.replace(staged_path, raw_path)


def _read_source(dataset: DatasetRecord) -> tuple[Path, bytes, str]:
    source_path = Path(dataset.file_path).resolve(strict=True)
    source_bytes = source_path.read_bytes()
    md_text = source_bytes.decode("utf-8")
    return source_path, source_bytes, md_text


def _manifest_identity(dataset: DatasetRecord, source_path: Path, source_bytes: bytes) -> dict:
    ingestion_version = str(dataset.ingestion_version or "v1").strip() or "v1"
    return {
        "source_path": str(source_path),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "parser_version": f"{PARSER_CONTRACT_VERSION}:{ingestion_version}",
        "schema_version": SQLITE_SCHEMA_VERSION,
    }


def _existing_kb_matches(db_path: Path, expected_manifest: dict) -> bool:
    if not db_path.is_file():
        return False
    try:
        conn = open_db_readonly(str(db_path))
        try:
            return kb_manifest_matches(conn, expected_manifest)
        finally:
            conn.close()
    except sqlite3.Error:
        # A missing/legacy/corrupt database is rebuilt in staging. It is never
        # modified or deleted before the replacement passes validation.
        return False


def _parse_fact_rows(dataset: DatasetRecord, md_text: str) -> tuple[list[dict], list[tuple]]:
    company = dataset.company or infer_company(md_text)
    fiscal_year = dataset.fiscal_year or infer_fiscal_year(md_text)
    tables_with_context = attach_context(md_text)
    if not isinstance(tables_with_context, list):
        raise ValueError("table parser must return a list")

    rows = build_fact_rows(
        tables_with_context,
        company=company,
        source=dataset.file_path,
        fiscal_year=fiscal_year,
    )
    rows.extend(
        build_frontmatter_rows(
            md_text,
            company=company,
            source=dataset.file_path,
            fiscal_year=fiscal_year,
        )
    )
    rows.extend(
        build_note_rows(
            md_text,
            company=company,
            source=dataset.file_path,
            fiscal_year=fiscal_year,
        )
    )
    normalized_rows = normalize_financial_fact_rows(rows)
    if not normalized_rows:
        raise ValueError("ingestion produced zero validated financial facts")
    return tables_with_context, normalized_rows


def build_knowledge_base(dataset: DatasetRecord, *, reset: bool = False):
    print("\n=== BUILDING KNOWLEDGE BASE ===")

    db_path = Path(dataset.sqlite_db_path)
    raw_path = Path(dataset.raw_tables_path)
    source_path, source_bytes, md_text = _read_source(dataset)
    manifest_identity = _manifest_identity(dataset, source_path, source_bytes)

    if not reset and _existing_kb_matches(db_path, manifest_identity):
        print("SQLite manifest matches source and parser → skipping KB build")
        return init_db(str(db_path)), 0

    # Parse and validate everything before creating or replacing a persistent
    # artifact. A parser exception therefore leaves the current KB untouched.
    tables_with_context, rows = _parse_fact_rows(dataset, md_text)
    manifest = {
        **manifest_identity,
        "facts_count": len(rows),
        "facts_sha256": facts_sha256(rows),
    }

    staged_raw_path = _stage_raw_tables(raw_path, tables_with_context)
    staged_db_path = _temporary_sibling(db_path, suffix=".sqlite.tmp")
    staged_conn = None
    try:
        staged_conn = init_db(str(staged_db_path), reset=True)
        insert_financial_facts(staged_conn, rows)
        write_kb_manifest(staged_conn, manifest)
        validate_kb_database(staged_conn, manifest)
        staged_conn.close()
        staged_conn = None

        with staged_db_path.open("rb") as handle:
            os.fsync(handle.fileno())

        # SQLite is the authoritative artifact and is replaced last. If either
        # staging or the raw-table swap fails, the current queryable KB remains.
        os.replace(staged_raw_path, raw_path)
        os.replace(staged_db_path, db_path)
    finally:
        if staged_conn is not None:
            staged_conn.close()
        staged_db_path.unlink(missing_ok=True)
        staged_raw_path.unlink(missing_ok=True)

    conn = init_db(str(db_path))
    print(f"Inserted {len(rows)} facts into SQLite")
    return conn, len(rows)
