from config.settings import CHROMA_COLLECTION, DB_PATH, DATA_FILE
from ingestion.pipeline import build_knowledge_base
from vectorstore.chroma_store import create_collection
from kb.sqlite_repo import init_db, sqlite_has_facts
from vectorstore.index_builder import build_vector_store


def ensure_built():
    conn = init_db(DB_PATH)
    collection = create_collection(CHROMA_COLLECTION)

    if collection.count() == 0:
        if sqlite_has_facts(conn):
            raise RuntimeError(
                "SQLite already has facts but vector DB is empty. "
                "Delete DBs and rebuild intentionally."
            )
        n_rows = build_knowledge_base(conn, DATA_FILE)
        collection, n_docs = build_vector_store(conn)
        print(f"Built SQLite facts: {n_rows} | Built vectors: {n_docs}")

    return conn, collection

def tests():
    print("\n=== TEST MODE (READ-ONLY) ===")
    conn, collection = ensure_built()

    print("Vector count:", collection.count())
    query = input("\nEnter query: ").strip()

    results = collection.query(query_texts=[query], n_results=10)
    print("\n=== SAMPLE QUERY RESULTS ===")
    for doc in results["documents"][0]:
        print("-", doc)

if __name__ == "__main__":
    tests()