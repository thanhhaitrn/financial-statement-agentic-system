from config.settings import VECTOR_BATCH_SIZE
from vectorstore.chroma_store import add_in_batches, create_collection, delete_collection
from vectorstore.text_builder import build_documents_and_metadata
import pandas as pd

def build_vector_store(conn, collection_name: str, *, reset: bool = False):
    print("\n=== BUILDING VECTOR STORE ===")

    df = pd.read_sql("""
        SELECT
            company,
            heading,
            item_name,
            value,
            raw_value,
            normalized_value,
            source
        FROM financial_facts
    """, conn)

    documents, metadatas, ids = build_documents_and_metadata(df)

    if reset:
        try:
            delete_collection(collection_name)
        except Exception:
            pass

    collection = create_collection(collection_name)

    add_in_batches(
        collection,
        documents,
        metadatas,
        ids,
        batch_size=VECTOR_BATCH_SIZE
    )
    print(f"Added {len(documents)} documents to vector store")
    return collection, len(documents)
