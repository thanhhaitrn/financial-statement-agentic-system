from config.settings import BATCH_SIZE, CHROMA_COLLECTION
from vectorstore.chroma_store import add_in_batches, create_collection
from vectorstore.text_builder import build_documents_and_metadata
import pandas as pd

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