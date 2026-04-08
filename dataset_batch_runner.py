import argparse
import json
import sys
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path

from datasets.registry import describe_dataset, load_registry
from output_formatter import format_final_answer
from test import collect_pipeline_errors, ensure_built, execute_query, extract_run_summary


def _dedupe_keep_order(items):
    seen = set()
    output = []

    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)

    return output


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run one or more queries across all registered datasets and save the outputs to JSON."
    )
    parser.add_argument(
        "--query",
        action="append",
        default=[],
        help="Query to run. Repeat this flag to test multiple questions.",
    )
    parser.add_argument(
        "--queries-file",
        default="",
        help="Path to a .txt or .json file containing queries to run.",
    )
    parser.add_argument(
        "--output",
        default="batch_test_results.json",
        help="Path to the JSON file where results will be written.",
    )
    parser.add_argument(
        "--include-trace",
        action="store_true",
        help="Include the full graph trace for each run in the output JSON.",
    )
    parser.add_argument(
        "--debug-trace",
        action="store_true",
        help="Enable debug-only trace logs during batch runs.",
    )
    return parser.parse_args()


def load_queries_from_file(path_str: str) -> list[str]:
    path = Path(path_str)
    if not path.exists():
        raise SystemExit(f"Queries file not found: {path}")

    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return []

    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(raw_text)
        except JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON in queries file: {path} ({exc})") from exc
        if isinstance(payload, list):
            return _dedupe_keep_order(payload)
        if isinstance(payload, dict) and isinstance(payload.get("queries"), list):
            return _dedupe_keep_order(payload.get("queries"))
        raise SystemExit("Invalid queries JSON. Use either a JSON list or an object with a 'queries' list.")

    return _dedupe_keep_order(raw_text.splitlines())


def resolve_queries(cli_queries: list[str], queries_file: str = "") -> list[str]:
    queries = list(cli_queries or [])
    if queries_file:
        queries.extend(load_queries_from_file(queries_file))

    resolved = _dedupe_keep_order(queries)
    if not resolved:
        raise SystemExit("Missing queries. Provide at least one --query or use --queries-file.")

    return resolved


def serialize_run_result(final_state: dict, query: str, *, include_trace: bool = False) -> dict:
    synth_decision = final_state.get("synth_decision", {}) or {}
    result = {
        "query": query,
        "formatted_answer": format_final_answer(final_state),
        "synth_status": str(synth_decision.get("status", "") or "").strip(),
        "answer": str(synth_decision.get("answer", "") or "").strip(),
        "missing": synth_decision.get("missing", []) or [],
        "errors": collect_pipeline_errors(final_state),
        "run_summary": extract_run_summary(final_state),
    }

    if include_trace:
        result["trace"] = final_state.get("trace", []) or []

    return result


def _runtime_error_result(query: str, exc: Exception) -> dict:
    return {
        "query": query,
        "formatted_answer": "",
        "synth_status": "error",
        "answer": "",
        "missing": [],
        "errors": [f"runtime_error ({type(exc).__name__}): {exc}"],
        "run_summary": {
            "event": "run:done",
            "trace_events_n": 0,
        },
    }


def run_dataset_queries(dataset, queries: list[str], *, debug_trace: bool = False, include_trace: bool = False) -> dict:
    dataset_result = {
        "dataset_id": dataset.dataset_id,
        "description": describe_dataset(dataset),
        "company": dataset.company,
        "ticker": dataset.ticker,
        "fiscal_year": dataset.fiscal_year,
        "fiscal_quarter": dataset.fiscal_quarter,
        "scope": dataset.scope,
        "audit_status": dataset.audit_status,
        "file_path": dataset.file_path,
        "runs": [],
    }

    try:
        dataset, _conn, collection = ensure_built(dataset)
    except Exception as exc:
        dataset_result["setup_error"] = f"{type(exc).__name__}: {exc}"
        return dataset_result

    dataset_result["status"] = dataset.status
    dataset_result["facts_count"] = dataset.facts_count
    dataset_result["vector_docs_count"] = dataset.vector_docs_count

    for query in queries:
        try:
            final_state = execute_query(
                dataset,
                collection,
                query,
                debug_trace=debug_trace,
            )
            dataset_result["runs"].append(
                serialize_run_result(
                    final_state,
                    query,
                    include_trace=include_trace,
                )
            )
        except Exception as exc:
            dataset_result["runs"].append(_runtime_error_result(query, exc))

    return dataset_result


def build_report(datasets: list, queries: list[str], *, debug_trace: bool = False, include_trace: bool = False) -> dict:
    results = []

    for idx, dataset in enumerate(datasets, start=1):
        print(f"[{idx}/{len(datasets)}] {dataset.dataset_id}")
        sys.stdout.flush()
        results.append(
            run_dataset_queries(
                dataset,
                queries,
                debug_trace=debug_trace,
                include_trace=include_trace,
            )
        )

    total_runs = sum(len(item.get("runs", []) or []) for item in results)
    runs_with_errors = sum(
        1
        for item in results
        for run in (item.get("runs", []) or [])
        if run.get("errors")
    )
    datasets_with_setup_error = sum(1 for item in results if item.get("setup_error"))

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "datasets_n": len(results),
        "queries": queries,
        "debug_trace": bool(debug_trace),
        "include_trace": bool(include_trace),
        "total_runs": total_runs,
        "runs_with_errors": runs_with_errors,
        "datasets_with_setup_error": datasets_with_setup_error,
        "results": results,
    }


def main():
    args = parse_args()
    queries = resolve_queries(args.query, args.queries_file)
    datasets = load_registry()
    if not datasets:
        raise SystemExit("No datasets registered. Use test.py to register a dataset first.")

    report = build_report(
        datasets,
        queries,
        debug_trace=args.debug_trace,
        include_trace=args.include_trace,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Saved batch results to {output_path.resolve()}")


if __name__ == "__main__":
    main()
