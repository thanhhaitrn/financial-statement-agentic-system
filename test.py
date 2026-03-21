import argparse
import sys
import uuid

from config.settings import DEFAULT_DATASET, DEFAULT_DATA_FILE
from datasets.registry import (
    build_dataset_record,
    describe_dataset,
    find_datasets,
    get_dataset,
    load_registry,
    save_dataset,
)
from ingestion.pipeline import build_knowledge_base
from kb.sqlite_repo import init_db, sqlite_count_facts, sqlite_has_facts
from output_formatter import format_final_answer
from tools.tool_runner import set_collection
from vectorstore.chroma_store import create_collection
from vectorstore.index_builder import build_vector_store


def parse_args():
    parser = argparse.ArgumentParser(description="Run the agentic financial QA pipeline.")
    parser.add_argument("--list-datasets", action="store_true", help="List registered datasets and exit.")
    parser.add_argument("--dataset-id", default="", help="Select an existing dataset by id.")
    parser.add_argument("--company", default="", help="Filter/select company or create a dataset with this company.")
    parser.add_argument("--ticker", default="", help="Filter/select ticker or create a dataset with this ticker.")
    parser.add_argument("--industry", default="", help="Industry metadata when creating a dataset.")
    parser.add_argument("--report-type", default="", help="Filter/select report type or set it when creating a dataset.")
    parser.add_argument("--fiscal-year", type=int, default=None, help="Filter/select fiscal year.")
    parser.add_argument("--fiscal-quarter", type=int, default=None, help="Filter/select fiscal quarter.")
    parser.add_argument("--scope", default="", help="Filter/select scope like consolidated or standalone.")
    parser.add_argument("--audit-status", default="", help="Filter/select audited vs unaudited.")
    parser.add_argument("--file-path", default="", help="Register/build a dataset from this document path.")
    parser.add_argument("--query", default="", help="Run a query non-interactively.")
    parser.add_argument("--debug-trace", action="store_true", help="Include debug-only trace logs.")
    return parser.parse_args()


def ensure_default_dataset():
    dataset = build_dataset_record(
        file_path=DEFAULT_DATA_FILE,
        company=DEFAULT_DATASET["company"],
        dataset_id=DEFAULT_DATASET["dataset_id"],
        ticker=DEFAULT_DATASET["ticker"],
        industry=DEFAULT_DATASET["industry"],
        report_type=DEFAULT_DATASET["report_type"],
        fiscal_year=DEFAULT_DATASET["fiscal_year"],
        fiscal_quarter=DEFAULT_DATASET["fiscal_quarter"],
        scope=DEFAULT_DATASET["scope"],
        audit_status=DEFAULT_DATASET["audit_status"],
        ingestion_version=DEFAULT_DATASET["ingestion_version"],
    )
    return save_dataset(dataset)


def list_datasets() -> int:
    records = load_registry()
    if not records:
        print("No datasets registered.")
        return 0

    print("Registered datasets:")
    for idx, record in enumerate(records, start=1):
        print(f"{idx}. {describe_dataset(record)}")
    return 0


def _choose_dataset_interactively(matches):
    print("Multiple datasets matched. Select one:")
    for idx, record in enumerate(matches, start=1):
        print(f"{idx}. {describe_dataset(record)}")

    while True:
        raw = input("Choose dataset number: ").strip()
        if not raw:
            continue
        if raw.isdigit():
            index = int(raw)
            if 1 <= index <= len(matches):
                return matches[index - 1]
        print("Invalid selection. Try again.")


def resolve_dataset(args):
    selection_requested = any(
        [
            args.dataset_id,
            args.company,
            args.ticker,
            args.report_type,
            args.fiscal_year is not None,
            args.fiscal_quarter is not None,
            args.scope,
            args.audit_status,
        ]
    )

    if args.file_path:
        company = args.company or DEFAULT_DATASET["company"]
        dataset = build_dataset_record(
            file_path=args.file_path,
            company=company,
            dataset_id=args.dataset_id,
            ticker=args.ticker,
            industry=args.industry,
            report_type=args.report_type or DEFAULT_DATASET["report_type"],
            fiscal_year=(
                args.fiscal_year
                if args.fiscal_year is not None
                else DEFAULT_DATASET["fiscal_year"]
            ),
            fiscal_quarter=(
                args.fiscal_quarter
                if args.fiscal_quarter is not None
                else DEFAULT_DATASET["fiscal_quarter"]
            ),
            scope=args.scope or DEFAULT_DATASET["scope"],
            audit_status=args.audit_status or DEFAULT_DATASET["audit_status"],
            ingestion_version=DEFAULT_DATASET["ingestion_version"],
        )
        return save_dataset(dataset)

    if args.dataset_id:
        dataset = get_dataset(args.dataset_id)
        if dataset is None:
            raise SystemExit(f"Dataset not found: {args.dataset_id}")
        return dataset

    matches = find_datasets(
        company=args.company,
        ticker=args.ticker,
        report_type=args.report_type,
        fiscal_year=args.fiscal_year,
        fiscal_quarter=args.fiscal_quarter,
        scope=args.scope,
        audit_status=args.audit_status,
    )
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        if sys.stdin.isatty():
            return _choose_dataset_interactively(matches)
        raise SystemExit("Multiple datasets matched. Re-run with --dataset-id.")

    if selection_requested:
        raise SystemExit(
            "No dataset matched the provided filters. "
            "Use --list-datasets to inspect registry or register a new one with --file-path."
        )

    return ensure_default_dataset()


def ensure_built(dataset):
    conn = init_db(dataset.sqlite_db_path)
    facts_count = sqlite_count_facts(conn)

    if not sqlite_has_facts(conn):
        conn, n_rows = build_knowledge_base(dataset)
        facts_count = n_rows
        dataset = save_dataset(
            dataset.model_copy(
                update={
                    "facts_count": facts_count,
                    "status": "kb_ready" if facts_count > 0 else "registered",
                }
            )
        )

    collection = create_collection(dataset.vector_collection_name)
    vector_docs_count = collection.count()
    if vector_docs_count == 0:
        collection, n_docs = build_vector_store(conn, dataset.vector_collection_name)
        vector_docs_count = n_docs
        dataset = save_dataset(
            dataset.model_copy(
                update={
                    "facts_count": facts_count,
                    "vector_docs_count": vector_docs_count,
                    "status": "ready",
                }
            )
        )
    else:
        dataset = save_dataset(
            dataset.model_copy(
                update={
                    "facts_count": facts_count,
                    "vector_docs_count": vector_docs_count,
                    "status": "ready" if facts_count > 0 else dataset.status,
                }
            )
        )

    return dataset, conn, collection


def run_query(dataset, collection, query: str, *, debug_trace: bool = False):
    from graph.workflow import agentic_graph

    set_collection(collection)

    initial_state = {
        "user_query": query,
        "dataset_id": dataset.dataset_id,
        "debug_trace": debug_trace,
        "tool_observations": [],
        "planner_plan": {},
        "worker_plan": {},
        "worker_results": {},
        "web_summary": "",
        "expected_workers": [],
        "done_workers": [],
        "followup_rounds": 0,
        "run_id": str(uuid.uuid4())[:8],
        "trace": [],
    }

    final_state = agentic_graph.invoke(initial_state)

    print(f"\n=== DATASET ===\n{describe_dataset(dataset)}")
    print("\n=== FINAL ANSWER ===")
    print(format_final_answer(final_state))

    print("\n=== TRACE ===")
    for entry in final_state.get("trace", []):
        print(entry)


def main():
    args = parse_args()

    if args.list_datasets:
        raise SystemExit(list_datasets())

    dataset = resolve_dataset(args)
    dataset, _conn, collection = ensure_built(dataset)

    query = args.query.strip()
    if not query:
        query = input("Enter query: ").strip()

    run_query(dataset, collection, query, debug_trace=args.debug_trace)


if __name__ == "__main__":
    main()
