"""Build the SQLite knowledge base from a registered dataset document."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

import json
from pathlib import Path

from ingestion.kb_builder import build_fact_rows
from ingestion.markdown_loader import load_markdown
from ingestion.note_parser import build_note_rows, infer_company, infer_fiscal_year
from ingestion.table_parser import attach_context
from kb.sqlite_repo import insert_financial_facts, init_db, sqlite_has_facts
from schemas.datasets import DatasetRecord


def _write_raw_tables(dataset: DatasetRecord, tables_with_context: list[dict]) -> None:
    raw_path = Path(dataset.raw_tables_path)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    with raw_path.open("w", encoding="utf-8") as handle:
        json.dump(tables_with_context, handle, ensure_ascii=False, indent=2)


def build_knowledge_base(dataset: DatasetRecord, *, reset: bool = False):
    print("\n=== BUILDING KNOWLEDGE BASE ===")

    conn = init_db(dataset.sqlite_db_path, reset=reset)

    if not reset and sqlite_has_facts(conn):
        print("SQLite already has facts → skipping KB build")
        return conn, 0

    md_text = load_markdown(dataset.file_path)
    company = dataset.company or infer_company(md_text)
    fiscal_year = dataset.fiscal_year or infer_fiscal_year(md_text)
    tables_with_context = attach_context(md_text)
    _write_raw_tables(dataset, tables_with_context)

    rows = build_fact_rows(
        tables_with_context,
        company=company,
        source=dataset.file_path,
        fiscal_year=fiscal_year,
    )
    rows.extend(
        build_note_rows(
            md_text,
            company=company,
            source=dataset.file_path,
            fiscal_year=fiscal_year,
        )
    )

    insert_financial_facts(conn, rows)

    print(f"Inserted {len(rows)} facts into SQLite")
    return conn, len(rows)
