"""CLI entry point for building datasets and running the financial workflow."""
# Code note: CLI entry-point code wires dataset setup, vector indexing, and graph execution together.

import argparse
import sys
import time
import uuid
from pathlib import Path

from config.settings import DEFAULT_DATASET, DEFAULT_DATA_FILE
from datasets.registry import (
    build_dataset_record,
    delete_dataset as delete_dataset_record,
    describe_dataset,
    find_datasets,
    get_dataset,
    load_registry,
    save_dataset,
)
from ingestion.pipeline import build_knowledge_base
from kb.sqlite_repo import (
    init_db,
    sqlite_count_facts,
    sqlite_has_fact_columns,
    sqlite_has_facts,
    sqlite_has_populated_fact_values,
)
from output_formatter import format_final_answer
from tools.tool_runner import set_collection
from vectorstore.qdrant_store import create_collection
from vectorstore.index_builder import build_vector_store


def _dedupe_keep_order(items):
    seen = set()
    output = []

    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)

    return output


def parse_args():
    parser = argparse.ArgumentParser(description="Run the agentic financial QA pipeline.")
    parser.add_argument("--list-datasets", action="store_true", help="List registered datasets and exit.")
    parser.add_argument("--delete-dataset", action="store_true", help="Delete a registered dataset and exit.")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts for destructive actions like deleting a dataset.",
    )
    parser.add_argument("--dataset-id", default="", help="Select an existing dataset by id.")
    parser.add_argument(
        "--select-dataset",
        action="store_true",
        help="Prompt to choose a dataset from the matched registered datasets.",
    )
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


def _choose_dataset_interactively(matches, *, header="Select one dataset:"):
    print(header)
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
    prompt_dataset_selection = sys.stdin.isatty() and (
        args.select_dataset or (not selection_requested and not args.query.strip())
    )

    if args.file_path:
        company = str(args.company or "").strip()
        dataset_id = str(args.dataset_id or "").strip()
        if not dataset_id and not company:
            dataset_id = Path(args.file_path).stem
        dataset = build_dataset_record(
            file_path=args.file_path,
            company=company,
            dataset_id=dataset_id,
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
    if prompt_dataset_selection and matches:
        return _choose_dataset_interactively(
            matches,
            header="Available datasets. Select one:",
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


def resolve_dataset_for_delete(args):
    if args.file_path:
        raise SystemExit("Cannot use --file-path with --delete-dataset. Select an existing dataset instead.")

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
    if not selection_requested:
        raise SystemExit(
            "Deleting a dataset requires --dataset-id or filters like --company/--fiscal-year."
        )

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
            return _choose_dataset_interactively(
                matches,
                header="Multiple datasets matched. Select one to delete:",
            )
        raise SystemExit("Multiple datasets matched. Re-run with --dataset-id.")

    raise SystemExit("No dataset matched the provided filters.")


def ensure_built(dataset):
    required_fact_columns = {
        "company",
        "fiscal_year",
        "heading",
        "item_code",
        "subheading",
        "item_name",
        "value",
        "raw_value",
        "normalized_value",
        "source",
    }
    conn = init_db(dataset.sqlite_db_path)
    schema_outdated = not sqlite_has_fact_columns(conn, required_fact_columns)
    values_outdated = not sqlite_has_populated_fact_values(conn)

    rebuild_required = (
        str(dataset.ingestion_version or "").strip()
        != str(DEFAULT_DATASET["ingestion_version"] or "").strip()
    ) or schema_outdated or values_outdated

    facts_count = sqlite_count_facts(conn)

    if rebuild_required:
        reasons = []
        if str(dataset.ingestion_version or "").strip() != str(DEFAULT_DATASET["ingestion_version"] or "").strip():
            reasons.append(
                "Dataset ingestion_version is outdated "
                f"({dataset.ingestion_version or 'unknown'} -> {DEFAULT_DATASET['ingestion_version']})."
            )
        if schema_outdated:
            reasons.append("financial_facts schema is missing required columns.")
        if values_outdated:
            reasons.append("financial_facts is missing populated raw_value/normalized_value data.")
        print(" ".join(reasons) + " Rebuilding derived artifacts.")
        conn, n_rows = build_knowledge_base(dataset, reset=True)
        facts_count = n_rows
        dataset = save_dataset(
            dataset.model_copy(
                update={
                    "facts_count": facts_count,
                    "vector_docs_count": 0,
                    "status": "kb_ready" if facts_count > 0 else "registered",
                    "ingestion_version": DEFAULT_DATASET["ingestion_version"],
                }
            )
        )
    elif not sqlite_has_facts(conn):
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
    if rebuild_required or vector_docs_count == 0:
        collection, n_docs = build_vector_store(
            conn,
            dataset.vector_collection_name,
            reset=rebuild_required,
        )
        vector_docs_count = n_docs
        dataset = save_dataset(
            dataset.model_copy(
                update={
                    "facts_count": facts_count,
                    "vector_docs_count": vector_docs_count,
                    "status": "ready",
                    "ingestion_version": DEFAULT_DATASET["ingestion_version"],
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
                    "ingestion_version": DEFAULT_DATASET["ingestion_version"],
                }
            )
        )

    return dataset, conn, collection


def _resolve_query(args) -> str:
    query = args.query.strip()
    if query:
        return query

    if not sys.stdin.isatty():
        raise SystemExit("Missing query. Re-run with --query when stdin is not interactive.")

    try:
        query = input("Enter query: ").strip()
    except EOFError as exc:
        raise SystemExit("Missing query.") from exc

    if not query:
        raise SystemExit("Missing query.")

    return query


def _collect_pipeline_errors(final_state: dict) -> list[str]:
    errors = []
    synth_decision = final_state.get("synth_decision", {}) or {}
    synth_status = str(synth_decision.get("status", "") or "").strip().lower()
    synth_answer = str(synth_decision.get("answer", "") or "").strip()

    if synth_status == "error":
        message = "synth:error"
        if synth_answer:
            message = f"{message}: {synth_answer}"
        errors.append(message)

    for entry in final_state.get("trace", []):
        if not isinstance(entry, dict):
            continue

        event = str(entry.get("event", "") or "").strip()
        if not event:
            continue

        if not (event.endswith(":error") or event == "tool:error_runtime"):
            continue

        message = event
        error_type = str(entry.get("error_type", "") or "").strip()
        error = str(entry.get("error", "") or "").strip()
        if error_type:
            message = f"{message} ({error_type})"
        if error:
            message = f"{message}: {error}"
        errors.append(message)

    return _dedupe_keep_order(errors)


def _print_trace_entry(entry: dict) -> None:
    rendered = dict(entry or {})
    if "context_preview" in rendered and "context" not in rendered:
        rendered["context"] = rendered.pop("context_preview")
    rendered.pop("context_len", None)
    rendered.pop("context_length", None)
    print(rendered)
    sys.stdout.flush()


def _run_graph(agentic_graph, initial_state: dict, *, on_trace_entry=None) -> dict:
    final_state = dict(initial_state)
    printed_trace_count = 0

    for state in agentic_graph.stream(initial_state, stream_mode="values"):
        if not isinstance(state, dict):
            continue

        final_state = state
        trace_entries = final_state.get("trace", []) or []
        new_entries = trace_entries[printed_trace_count:]

        for entry in new_entries:
            if on_trace_entry is not None:
                on_trace_entry(entry)

        printed_trace_count = len(trace_entries)

    return final_state


def _run_graph_with_live_trace(agentic_graph, initial_state: dict) -> dict:
    return _run_graph(
        agentic_graph,
        initial_state,
        on_trace_entry=_print_trace_entry,
    )


def _build_run_trace_entry(final_state: dict, *, duration_ms: int) -> dict:
    summary = {
        "event": "run:done",
        "duration_ms": int(duration_ms),
        "trace_events_n": len(final_state.get("trace", []) or []),
    }

    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    token_event_count = 0

    for entry in final_state.get("trace", []) or []:
        if not isinstance(entry, dict):
            continue

        has_tokens = False
        if entry.get("input_tokens") is not None:
            input_tokens += int(entry.get("input_tokens") or 0)
            has_tokens = True
        if entry.get("output_tokens") is not None:
            output_tokens += int(entry.get("output_tokens") or 0)
            has_tokens = True
        if entry.get("total_tokens") is not None:
            total_tokens += int(entry.get("total_tokens") or 0)
            has_tokens = True

        if has_tokens:
            token_event_count += 1

    if token_event_count:
        summary["llm_steps_n"] = token_event_count
        summary["input_tokens"] = input_tokens
        summary["output_tokens"] = output_tokens
        summary["total_tokens"] = total_tokens

    return summary


def extract_run_summary(final_state: dict) -> dict:
    for entry in reversed(final_state.get("trace", []) or []):
        if isinstance(entry, dict) and str(entry.get("event", "") or "").strip() == "run:done":
            return dict(entry)
    return {}


def collect_pipeline_errors(final_state: dict) -> list[str]:
    return _collect_pipeline_errors(final_state)


def _build_initial_state(dataset, query: str, *, debug_trace: bool = False) -> dict:
    return {
        "user_query": query,
        "dataset_id": dataset.dataset_id,
        "debug_trace": debug_trace,
        "tool_observations": [],
        "planner_plan": {},
        "worker_plan": {},
        "evidence_pack": {},
        "evidence_cache": {},
        "worker_results": {},
        "web_summary": "",
        "expected_workers": [],
        "done_workers": {},
        "followup_rounds": 0,
        "run_id": str(uuid.uuid4())[:8],
        "trace": [],
    }


def execute_query(dataset, collection, query: str, *, debug_trace: bool = False, on_trace_entry=None) -> dict:
    from graph.workflow import agentic_graph

    set_collection(collection)
    initial_state = _build_initial_state(dataset, query, debug_trace=debug_trace)
    started_at = time.perf_counter()
    final_state = _run_graph(
        agentic_graph,
        initial_state,
        on_trace_entry=on_trace_entry,
    )
    run_log = _build_run_trace_entry(
        final_state,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
    )
    final_state = dict(final_state)
    final_state["trace"] = [*(final_state.get("trace", []) or []), run_log]

    return final_state


def run_query(dataset, collection, query: str, *, debug_trace: bool = False):
    print(f"\n=== DATASET ===\n{describe_dataset(dataset)}")
    print("\n=== TRACE ===")
    sys.stdout.flush()
    final_state = execute_query(
        dataset,
        collection,
        query,
        debug_trace=debug_trace,
        on_trace_entry=_print_trace_entry,
    )
    run_log = extract_run_summary(final_state)
    print(run_log)
    sys.stdout.flush()
    print("\n=== FINAL ANSWER ===")
    print(format_final_answer(final_state))

    return final_state


def _confirm_delete_dataset(dataset, *, skip_confirmation: bool = False):
    if skip_confirmation:
        return

    if not sys.stdin.isatty():
        raise SystemExit("Deleting a dataset in non-interactive mode requires --yes.")

    print("\n=== DELETE DATASET ===")
    print(describe_dataset(dataset))
    print(
        "This will remove the dataset from the registry and delete its manifest, "
        "SQLite DB, raw tables, and vector collection."
    )

    if input("Type DELETE to confirm: ").strip() != "DELETE":
        raise SystemExit("Delete cancelled.")


def delete_dataset_cli(args) -> int:
    dataset = resolve_dataset_for_delete(args)
    _confirm_delete_dataset(
        dataset,
        skip_confirmation=args.yes,
    )
    deleted = delete_dataset_record(
        dataset.dataset_id,
    )
    if deleted is None:
        raise SystemExit(f"Dataset not found: {dataset.dataset_id}")

    print("Deleted dataset:")
    print(describe_dataset(deleted))
    print("Purged derived artifacts. The source document file was left untouched.")

    return 0


def main():
    args = parse_args()

    if args.list_datasets:
        raise SystemExit(list_datasets())
    if args.delete_dataset:
        raise SystemExit(delete_dataset_cli(args))

    dataset = resolve_dataset(args)
    dataset, _conn, collection = ensure_built(dataset)

    query = _resolve_query(args)
    final_state = run_query(dataset, collection, query, debug_trace=args.debug_trace)
    errors = _collect_pipeline_errors(final_state)

    if errors:
        sys.stdout.flush()
        print("\n=== ERROR SUMMARY ===", file=sys.stderr)
        for item in errors:
            print(f"- {item}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
