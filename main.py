from config.settings import (
    DATA_FILE,
    COMPANY_NAME,
    CHROMA_COLLECTION,
    BATCH_SIZE,
)

# Ingestion
from ingestion.markdown_loader import load_markdown
from ingestion.table_parser import attach_context
from ingestion.kb_builder import build_fact_rows

# KB
from kb.sqlite_repo import init_db, insert_financial_facts

# Vector store
from vectorstore.chroma_store import create_collection, add_in_batches, delete_collection
from vectorstore.text_builder import build_documents_and_metadata

# Graph
from graph.workflow import agentic_graph

import pandas as pd


def build_knowledge_base():
    print("\n=== BUILDING KNOWLEDGE BASE ===")

    md_text = load_markdown(DATA_FILE)
    tables_with_context = attach_context(md_text)

    rows = build_fact_rows(
        tables_with_context,
        company=COMPANY_NAME,
        source=DATA_FILE
    )

    conn = init_db("financial_kb.db")
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


def run_agent():
    print("\n=== RUNNING AGENT ===")

    state = {
        "query": "Theo Bảng cân đối kế toán của Công ty Cổ phần Sông Đà, nợ dài hạn là bao nhiêu?",
        "last_agent_response": "",
        "last_agent": "",
        "tool_observations": [],
        "num_steps": 0,
    }

    result = agentic_graph.invoke(state)

    print("\n=== FINAL ANSWER ===")
    print(result["last_agent_response"])

def semantic_query_test(collection):
    print("\n=== SEMANTIC QUERY CHECK ===")

    query = "Nợ dài hạn của công ty là bao nhiêu?"
    results = collection.query(
        query_texts=[query],
        n_results=5
    )

    for score, doc in zip(
        results["distances"][0],
        results["documents"][0]
    ):
        print("\nScore:", score)
        print(doc)

def load_existing_collection():
    collection = create_collection(CHROMA_COLLECTION)
    return collection

def check_existing_collection(collection):
    print("\n=== EXISTING COLLECTION CHECK ===")
    count = collection.count()
    print("Vector count:", count)

    if count == 0:
        raise RuntimeError(
            "Collection exists but is EMPTY — did you forget to build it?"
        )
    
def main():
    conn = build_knowledge_base()
    vc = build_vector_store(conn)
    #collection = load_existing_collection()
    #check_existing_collection(collection)
    semantic_query_test(vc)

if __name__ == "__main__":
    main()