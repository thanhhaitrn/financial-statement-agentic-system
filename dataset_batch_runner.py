"""CLI helper for running multiple dataset queries and saving their outputs."""
# Code note: Batch runner code executes the same workflow across many datasets and records aggregate results.

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


def _normalize_reference(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _normalize_query_record(item) -> dict:
    if isinstance(item, dict):
        query = str(
            item.get("query")
            or item.get("question")
            or item.get("prompt")
            or ""
        ).strip()
        reference = _normalize_reference(
            item.get("reference")
            if "reference" in item
            else item.get("references")
        )
    else:
        query = str(item or "").strip()
        reference = ""

    if not query:
        return {}
    return {
        "query": query,
        "reference": reference,
    }


def _dedupe_query_records(records: list) -> list[dict]:
    seen = set()
    output = []

    for item in records or []:
        record = _normalize_query_record(item)
        query = str(record.get("query", "") or "").strip()
        if not query or query in seen:
            continue
        output.append(record)
        seen.add(query)

    return output


def _query_texts(records: list) -> list[str]:
    return [
        str(record.get("query", "") or "").strip()
        for record in _dedupe_query_records(records)
        if str(record.get("query", "") or "").strip()
    ]


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run one or more queries across all registered datasets and save the outputs to JSON."
    )
    parser.add_argument(
        "--dataset-id",
        action="append",
        default=[],
        help="Dataset id to run. Repeat this flag to limit the batch to specific datasets.",
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
        "--overwrite-output",
        action="store_true",
        help="Overwrite the output JSON instead of preserving older batch reports in history.",
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


def load_query_records_from_file(path_str: str) -> list[dict]:
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
            return _dedupe_query_records(payload)
        if isinstance(payload, dict) and isinstance(payload.get("queries"), list):
            return _dedupe_query_records(payload.get("queries"))
        raise SystemExit("Invalid queries JSON. Use either a JSON list or an object with a 'queries' list.")

    return _dedupe_query_records(raw_text.splitlines())


def load_queries_from_file(path_str: str) -> list[str]:
    return _query_texts(load_query_records_from_file(path_str))


def resolve_query_records(cli_queries: list[str], queries_file: str = "") -> list[dict]:
    records = [
        {
            "query": str(query or "").strip(),
            "reference": "",
        }
        for query in (cli_queries or [])
        if str(query or "").strip()
    ]
    if queries_file:
        records.extend(load_query_records_from_file(queries_file))

    resolved = _dedupe_query_records(records)
    if not resolved:
        raise SystemExit("Missing queries. Provide at least one --query or use --queries-file.")

    return resolved


def resolve_queries(cli_queries: list[str], queries_file: str = "") -> list[str]:
    return _query_texts(resolve_query_records(cli_queries, queries_file))


def resolve_dataset_ids(dataset_ids: list[str] | None = None) -> list[str]:
    return _dedupe_keep_order(dataset_ids or [])


def resolve_datasets(datasets: list, dataset_ids: list[str] | None = None) -> list:
    selected_ids = resolve_dataset_ids(dataset_ids)
    if not selected_ids:
        return list(datasets or [])

    index = {
        str(getattr(dataset, "dataset_id", "") or "").strip(): dataset
        for dataset in (datasets or [])
        if str(getattr(dataset, "dataset_id", "") or "").strip()
    }
    missing = [dataset_id for dataset_id in selected_ids if dataset_id not in index]
    if missing:
        raise SystemExit(f"Unknown dataset id(s): {', '.join(missing)}")

    return [index[dataset_id] for dataset_id in selected_ids]


def load_existing_output(path: Path) -> dict | None:
    if not path.exists():
        return None

    raw_text = path.read_text(encoding="utf-8").strip()
    if not raw_text:
        return None

    try:
        payload = json.loads(raw_text)
    except JSONDecodeError as exc:
        raise SystemExit(f"Invalid existing output JSON: {path} ({exc})") from exc

    if not isinstance(payload, dict):
        raise SystemExit(f"Invalid existing output JSON: {path} must contain a JSON object.")

    return payload


def _runtime_from_summary(run_summary: dict) -> int | None:
    if not isinstance(run_summary, dict):
        return None
    if run_summary.get("duration_ms") is None:
        return None
    return int(run_summary.get("duration_ms") or 0)


def _total_tokens_from_summary(run_summary: dict) -> int:
    if not isinstance(run_summary, dict):
        return 0
    return int(run_summary.get("total_tokens") or 0)


def serialize_run_result(
    final_state: dict,
    query: str,
    *,
    reference: str = "",
    include_trace: bool = False,
) -> dict:
    synth_decision = final_state.get("synth_decision", {}) or {}
    formatted_answer = format_final_answer(final_state)
    answer = str(synth_decision.get("answer", "") or "").strip()
    run_summary = extract_run_summary(final_state)
    runtime = _runtime_from_summary(run_summary)
    result = {
        "query": query,
        "references": str(reference or "").strip(),
        "final_answer": answer or formatted_answer,
        "formatted_answer": formatted_answer,
        "synth_status": str(synth_decision.get("status", "") or "").strip(),
        "answer": answer,
        "missing": synth_decision.get("missing", []) or [],
        "errors": collect_pipeline_errors(final_state),
        "runtime": runtime,
        "total_tokens": _total_tokens_from_summary(run_summary),
        "run_summary": run_summary,
    }

    if include_trace:
        result["trace"] = final_state.get("trace", []) or []

    return result


def _runtime_error_result(query: str, exc: Exception, *, reference: str = "") -> dict:
    return {
        "query": query,
        "references": str(reference or "").strip(),
        "final_answer": "",
        "formatted_answer": "",
        "synth_status": "error",
        "answer": "",
        "missing": [],
        "errors": [f"runtime_error ({type(exc).__name__}): {exc}"],
        "runtime": None,
        "total_tokens": 0,
        "run_summary": {
            "event": "run:done",
            "trace_events_n": 0,
        },
    }


def run_dataset_queries(dataset, queries: list, *, debug_trace: bool = False, include_trace: bool = False) -> dict:
    query_records = _dedupe_query_records(queries)
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

    for query_record in query_records:
        query = str(query_record.get("query", "") or "").strip()
        reference = str(query_record.get("reference", "") or "").strip()
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
                    reference=reference,
                    include_trace=include_trace,
                )
            )
        except Exception as exc:
            dataset_result["runs"].append(_runtime_error_result(query, exc, reference=reference))

    return dataset_result


def build_report(
    datasets: list,
    queries: list,
    *,
    debug_trace: bool = False,
    include_trace: bool = False,
    selected_dataset_ids: list[str] | None = None,
) -> dict:
    query_records = _dedupe_query_records(queries)
    results = []

    for idx, dataset in enumerate(datasets, start=1):
        print(f"[{idx}/{len(datasets)}] {dataset.dataset_id}")
        sys.stdout.flush()
        results.append(
            run_dataset_queries(
                dataset,
                query_records,
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
        "queries": _query_texts(query_records),
        "query_records": query_records,
        "selected_dataset_ids": resolve_dataset_ids(selected_dataset_ids),
        "debug_trace": bool(debug_trace),
        "include_trace": bool(include_trace),
        "total_runs": total_runs,
        "runs_with_errors": runs_with_errors,
        "datasets_with_setup_error": datasets_with_setup_error,
        "results": results,
    }


def _dataset_report_without_runs(dataset_result: dict) -> dict:
    cleaned = {}
    for key, value in (dataset_result or {}).items():
        if key == "runs":
            continue
        cleaned[key] = value
    return cleaned


def build_query_reports(report: dict) -> list[dict]:
    query_records = _dedupe_query_records(
        report.get("query_records", []) or report.get("queries", []) or []
    )
    results = report.get("results", []) or []
    query_reports = []

    for query_record in query_records:
        query = str(query_record.get("query", "") or "").strip()
        reference = str(query_record.get("reference", "") or "").strip()
        query_results = []
        runs_with_errors = 0
        datasets_with_setup_error = 0
        total_runs = 0

        for dataset_result in results:
            if not isinstance(dataset_result, dict):
                continue

            item = _dataset_report_without_runs(dataset_result)
            if item.get("setup_error"):
                datasets_with_setup_error += 1
                query_results.append(item)
                continue

            run = None
            for candidate in (dataset_result.get("runs", []) or []):
                if not isinstance(candidate, dict):
                    continue
                if str(candidate.get("query", "") or "").strip() == query:
                    run = dict(candidate)
                    if reference and not run.get("references"):
                        run["references"] = reference
                    break

            if run is None:
                continue

            item["run"] = run
            query_results.append(item)
            total_runs += 1
            if run.get("errors"):
                runs_with_errors += 1

        query_reports.append(
            {
                "query": query,
                "references": reference,
                "generated_at": report.get("generated_at", ""),
                "datasets_n": len(query_results),
                "debug_trace": bool(report.get("debug_trace", False)),
                "include_trace": bool(report.get("include_trace", False)),
                "total_runs": total_runs,
                "runs_with_errors": runs_with_errors,
                "datasets_with_setup_error": datasets_with_setup_error,
                "results": query_results,
            }
        )

    return query_reports


def _normalize_query_report(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}

    query = str(item.get("query", "") or "").strip()
    if not query:
        return {}

    cleaned = dict(item)
    cleaned["query"] = query
    reference = _normalize_reference(
        cleaned.get("reference")
        if "reference" in cleaned
        else cleaned.get("references")
    )
    cleaned.pop("reference", None)
    cleaned["references"] = reference
    return cleaned


def normalize_existing_query_reports(existing_output: dict | None) -> list[dict]:
    if not existing_output:
        return []

    if isinstance(existing_output.get("query_reports"), list):
        reports = []
        for item in (existing_output.get("query_reports", []) or []):
            normalized = _normalize_query_report(item)
            if normalized:
                reports.append(normalized)
        return reports

    if "queries" in existing_output and "results" in existing_output:
        reports = []
        for item in (existing_output.get("history", []) or []):
            if not isinstance(item, dict):
                continue
            reports.extend(build_query_reports(item))
        reports.extend(build_query_reports(existing_output))

        merged = []
        index_by_query = {}
        for item in reports:
            normalized = _normalize_query_report(item)
            if not normalized:
                continue
            query = normalized["query"]
            if query in index_by_query:
                merged[index_by_query[query]] = normalized
            else:
                index_by_query[query] = len(merged)
                merged.append(normalized)
        return merged

    return []


def _primary_run_for_query_report(query_report: dict) -> tuple[dict, dict]:
    for result in (query_report.get("results", []) or []):
        if not isinstance(result, dict):
            continue
        run = result.get("run", {})
        if isinstance(run, dict):
            return result, run
    return {}, {}


def _query_output_record(query_report: dict) -> dict:
    _dataset_result, run = _primary_run_for_query_report(query_report)
    run_summary = run.get("run_summary", {}) if isinstance(run, dict) else {}
    reference = _normalize_reference(
        query_report.get("references")
        or query_report.get("reference")
        or run.get("references", "")
        or run.get("reference", "")
    )
    runtime = run.get("runtime")
    if runtime is None:
        runtime = _runtime_from_summary(run_summary)
    total_tokens = run.get("total_tokens")
    if total_tokens is None:
        total_tokens = _total_tokens_from_summary(run_summary)

    return {
        "query": str(query_report.get("query", "") or "").strip(),
        "final_answer": run.get("final_answer", "") or run.get("answer", ""),
        "references": reference,
        "runtime": runtime,
        "total_tokens": int(total_tokens or 0),
    }


def build_output_queries(query_reports: list[dict]) -> list[dict]:
    return [
        record
        for record in (_query_output_record(item) for item in (query_reports or []))
        if str(record.get("query", "") or "").strip()
    ]


def _merge_query_results(existing_results: list, latest_results: list) -> list[dict]:
    merged = [dict(item) for item in (existing_results or []) if isinstance(item, dict)]
    index_by_dataset_id = {
        str(item.get("dataset_id", "") or "").strip(): idx
        for idx, item in enumerate(merged)
        if str(item.get("dataset_id", "") or "").strip()
    }

    for item in (latest_results or []):
        if not isinstance(item, dict):
            continue
        dataset_id = str(item.get("dataset_id", "") or "").strip()
        normalized = dict(item)
        if dataset_id and dataset_id in index_by_dataset_id:
            merged[index_by_dataset_id[dataset_id]] = normalized
        else:
            if dataset_id:
                index_by_dataset_id[dataset_id] = len(merged)
            merged.append(normalized)

    return merged


def _query_report_stats(results: list[dict]) -> tuple[int, int, int, int]:
    datasets_n = len(results or [])
    total_runs = sum(1 for item in (results or []) if isinstance(item, dict) and isinstance(item.get("run"), dict))
    runs_with_errors = sum(
        1
        for item in (results or [])
        if isinstance(item, dict) and ((item.get("run", {}) or {}).get("errors"))
    )
    datasets_with_setup_error = sum(
        1 for item in (results or []) if isinstance(item, dict) and item.get("setup_error")
    )
    return datasets_n, total_runs, runs_with_errors, datasets_with_setup_error


def _merge_query_report(existing_item: dict, latest_item: dict, *, partial_dataset_update: bool = False) -> dict:
    if not partial_dataset_update:
        return latest_item

    merged_results = _merge_query_results(
        existing_item.get("results", []) or [],
        latest_item.get("results", []) or [],
    )
    datasets_n, total_runs, runs_with_errors, datasets_with_setup_error = _query_report_stats(merged_results)

    merged = dict(latest_item)
    merged["results"] = merged_results
    merged["datasets_n"] = datasets_n
    merged["total_runs"] = total_runs
    merged["runs_with_errors"] = runs_with_errors
    merged["datasets_with_setup_error"] = datasets_with_setup_error
    return merged


def build_output_document(report: dict, *, existing_output: dict | None = None, overwrite: bool = False) -> dict:
    new_query_reports = build_query_reports(report)
    partial_dataset_update = bool(resolve_dataset_ids(report.get("selected_dataset_ids", [])))
    if overwrite:
        merged_reports = list(new_query_reports)
    else:
        merged_reports = normalize_existing_query_reports(existing_output)
        index_by_query = {
            str(item.get("query", "") or "").strip(): idx
            for idx, item in enumerate(merged_reports)
            if str(item.get("query", "") or "").strip()
        }

        for item in new_query_reports:
            query = str(item.get("query", "") or "").strip()
            if query in index_by_query:
                merged_reports[index_by_query[query]] = _merge_query_report(
                    merged_reports[index_by_query[query]],
                    item,
                    partial_dataset_update=partial_dataset_update,
                )
            else:
                index_by_query[query] = len(merged_reports)
                merged_reports.append(item)

    queries = build_output_queries(merged_reports)

    return {
        "updated_at": report.get("generated_at", ""),
        "queries_n": len(queries),
        "queries": queries,
        "query_reports": merged_reports,
    }


def main():
    args = parse_args()
    queries = resolve_query_records(args.query, args.queries_file)
    dataset_ids = resolve_dataset_ids(args.dataset_id)
    datasets = resolve_datasets(load_registry(), dataset_ids)
    if not datasets:
        raise SystemExit("No datasets registered. Use test.py to register a dataset first.")

    report = build_report(
        datasets,
        queries,
        debug_trace=args.debug_trace,
        include_trace=args.include_trace,
        selected_dataset_ids=dataset_ids,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing_output = None if args.overwrite_output else load_existing_output(output_path)
    output_document = build_output_document(
        report,
        existing_output=existing_output,
        overwrite=args.overwrite_output,
    )
    output_path.write_text(
        json.dumps(output_document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Saved batch results to {output_path.resolve()}")


if __name__ == "__main__":
    main()
