from config.settings import COMPANY_NAME, DATA_FILE, DB_PATH
from ingestion.kb_builder import build_fact_rows
from ingestion.markdown_loader import load_markdown
from ingestion.table_parser import attach_context
from kb.sqlite_repo import insert_financial_facts, init_db, sqlite_has_facts

def build_knowledge_base():
    print("\n=== BUILDING KNOWLEDGE BASE ===")

    conn = init_db(DB_PATH)

    if sqlite_has_facts(conn):
        print("SQLite already has facts → skipping KB build")
        return conn

    md_text = load_markdown(DATA_FILE)
    tables_with_context = attach_context(md_text)

    rows = build_fact_rows(
        tables_with_context,
        company=COMPANY_NAME,
        source=DATA_FILE
    )

    insert_financial_facts(conn, rows)

    print(f"Inserted {len(rows)} facts into SQLite")
    return conn