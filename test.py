from tools.tool_runner import set_collection
from config.settings import CHROMA_COLLECTION, DB_PATH, DATA_FILE
from ingestion.pipeline import build_knowledge_base
from vectorstore.chroma_store import create_collection
from kb.sqlite_repo import init_db, sqlite_has_facts
from vectorstore.index_builder import build_vector_store
from graph.workflow import agentic_graph
import uuid
from output_formatter import format_final_answer

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


def main():
    conn, collection = ensure_built()
    set_collection(collection)

    user_input = input("Enter query: ").strip()

    initial_state = {
        "user_query": user_input,
        "w_last_agent_response": "",
        "w_last_agent": "",
        "tool_observations": [],
        "w_num_steps": 0,
        "plan": {},
        "worker_results": {},
        "web_summary": "",
        "expected_workers": [],
        "done_workers": [],
        "seen_tool_calls": [],
        "followup_rounds": 0,
        "run_id": str(uuid.uuid4())[:8],
        "trace": []
    }

    final_state = agentic_graph.invoke(initial_state)

    print("\n=== FINAL ANSWER ===")
    print(format_final_answer(final_state))

    print("\n=== TRACE ===")
    for e in final_state.get("trace", []):
        print(e)

if __name__ == "__main__":
    main()
