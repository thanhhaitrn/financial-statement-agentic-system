"""Build or rebuild the Qdrant vector index from SQLite financial facts."""
# Code note: Vectorstore modules turn normalized facts into searchable text and metadata for retrieval.

from config.settings import VECTOR_BATCH_SIZE
from vectorstore.qdrant_store import add_in_batches, create_collection, delete_collection
from vectorstore.text_builder import build_documents_and_metadata
from vectorstore.lexical_index import reset_lexical_index
import pandas as pd

def build_vector_store(conn, collection_name: str, *, reset: bool = False):
    print("\n=== BUILDING VECTOR STORE ===")

    df = pd.read_sql("""
        SELECT
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
            unit
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
    # Drop any cached lexical index so it rebuilds from the fresh corpus.
    reset_lexical_index(collection_name)
    print(f"Added {len(documents)} documents to vector store")
    return collection, len(documents)
