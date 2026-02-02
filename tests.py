from config.settings import BATCH_SIZE, CHROMA_COLLECTION, COMPANY_NAME
from ingestion.kb_builder import build_fact_rows
from ingestion.markdown_loader import load_markdown
from ingestion.table_parser import attach_context
from vectorstore.chroma_store import CHROMA_PATH, add_in_batches, create_collection
from kb.sqlite_repo import init_db, insert_financial_facts
import pandas as pd

from vectorstore.text_builder import build_documents_and_metadata


DATA_FILE = "data/document.md"
DB_PATH = "financial_kb.db"  

def sqlite_has_facts(conn) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM financial_facts")
    return cur.fetchone()[0] > 0

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


def build_vector_store(conn):
    print("\n=== BUILDING VECTOR STORE ===")

    df = pd.read_sql("""
        SELECT company, heading, item_name, value, source
        FROM financial_facts
    """, conn)

    documents, metadatas, ids = build_documents_and_metadata(df)

    collection = create_collection(CHROMA_COLLECTION)

    add_in_batches(
        collection,
        documents,
        metadatas,
        ids,
        batch_size=BATCH_SIZE
    )
    print(f"Added {len(documents)} documents to vector store")
    return collection

def tests():
    print("\n=== TEST MODE (READ-ONLY) ===")

    conn = init_db(DB_PATH)
    collection = create_collection(CHROMA_COLLECTION)

    if collection.count() == 0:
        print("Vector DB empty")

        if sqlite_has_facts(conn):
            raise RuntimeError(
                "SQLite already has facts but vector DB is empty. "
                "This means state is inconsistent. "
                "Delete DBs and rebuild intentionally."
            )
        print("Building KB + vectors ONCE")
        conn = build_knowledge_base()
        build_vector_store(conn)
    
        print("Build complete. Restart script to query.")
        return 

    print("Vector DB already exists → skip build")
    print("Vector count:", collection.count())

    query = input("\nEnter query: ").strip()
    results = collection.query(
        query_texts= [query],
        n_results=3
    )

    print("\n=== SAMPLE QUERY RESULTS ===")
    for doc in results["documents"][0]:
        print("-", doc)

if __name__ == "__main__":
    tests()