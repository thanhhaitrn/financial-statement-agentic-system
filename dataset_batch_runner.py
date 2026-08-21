"""CLI helper for running multiple dataset queries and saving their outputs."""
# Code note: Batch runner code executes the same workflow across many datasets and records aggregate results.

import argparse
import json
import sys
from datetime import datetime, timezone
from json import JSONDecodeError
from pathlib import Path

from dataset_catalog.registry import describe_dataset, load_registry
from dataset_batch_result import build_runtime_fingerprints, dataset_identity_payload
from evaluation.contracts import (
    REPORT_SCHEMA_VERSION,
    atomic_write_text,
    provider_limit_reason,
    stable_json_fingerprint,
)
from output_formatter import format_final_answer
from test import collect_pipeline_errors, ensure_built, execute_query, extract_run_summary
from common import dedupe_keep_order as _dedupe_keep_order

def _normalize_reference(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "\n".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _normalize_query_record(item) -> dict:
    if isinstance(item, dict):
        query_id = item.get("id") if "id" in item else item.get("query_id")
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
        query_id = None
        query = str(item or "").strip()
        reference = ""

    if not query:
        return {}
    record = {
        "query": query,
        "reference": reference,
    }
    if query_id not in (None, ""):
        record["id"] = query_id
    return record


def _query_record_key(item: dict) -> str:
    record = _normalize_query_record(item)
    if not record:
        return ""
    return stable_json_fingerprint(
        {
            "id": record.get("id"),
            "query": record.get("query", ""),
            "reference": record.get("reference", ""),
        }
    )


def _dedupe_query_records(records: list) -> list[dict]:
    seen = set()
    output = []

    for item in records or []:
        record = _normalize_query_record(item)
        query = str(record.get("query", "") or "").strip()
        key = _query_record_key(record)
        if not query or key in seen:
            continue
        output.append(record)
        seen.add(key)

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
    query_id=None,
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
    if query_id not in (None, ""):
        result["query_id"] = query_id

    if include_trace:
        result["trace"] = final_state.get("trace", []) or []

    return result


def _runtime_error_result(
    query: str,
    exc: Exception,
    *,
    query_id=None,
    reference: str = "",
) -> dict:
    result = {
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
    if query_id not in (None, ""):
        result["query_id"] = query_id
    return result


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
        dataset, conn, collection = ensure_built(dataset)
    except Exception as exc:
        dataset_result["setup_error"] = f"{type(exc).__name__}: {exc}"
        return dataset_result

    dataset_result["status"] = dataset.status
    dataset_result["facts_count"] = dataset.facts_count
    dataset_result["vector_docs_count"] = dataset.vector_docs_count
    try:
        from kb.sqlite_repo import read_kb_manifest

        kb_manifest = read_kb_manifest(conn)
    except Exception:
        kb_manifest = {}
    index_generation = str(
        getattr(collection, "generation", "")
        or getattr(collection, "build_fingerprint", "")
        or ""
    ).strip()
    dataset_result.update(
        {
            "ingestion_version": dataset.ingestion_version,
            "vector_collection_name": dataset.vector_collection_name,
            "source_sha256": kb_manifest.get("source_sha256", ""),
            "facts_sha256": kb_manifest.get("facts_sha256", ""),
            "parser_version": kb_manifest.get("parser_version", ""),
            "kb_schema_version": kb_manifest.get("schema_version", ""),
            "kb_generation": (
                stable_json_fingerprint(kb_manifest) if kb_manifest else ""
            ),
            "index_generation": index_generation,
        }
    )
    dataset_result["dataset_generation"] = stable_json_fingerprint(
        {
            "dataset_id": dataset.dataset_id,
            "ingestion_version": dataset.ingestion_version,
            "source_sha256": kb_manifest.get("source_sha256", ""),
            "parser_version": kb_manifest.get("parser_version", ""),
            "schema_version": kb_manifest.get("schema_version", ""),
            "facts_sha256": kb_manifest.get("facts_sha256", ""),
        }
    )
    dataset_result["dataset_identity"] = dataset_identity_payload(dataset_result)

    for query_record in query_records:
        query = str(query_record.get("query", "") or "").strip()
        reference = str(query_record.get("reference", "") or "").strip()
        query_id = query_record.get("id")
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
                    query_id=query_id,
                    reference=reference,
                    include_trace=include_trace,
                )
            )
        except Exception as exc:
            dataset_result["runs"].append(
                _runtime_error_result(
                    query,
                    exc,
                    query_id=query_id,
                    reference=reference,
                )
            )

    return dataset_result


def _batch_provider_limit_reasons(results: list[dict]) -> list[str]:
    reasons = []
    for dataset_result in results or []:
        if not isinstance(dataset_result, dict):
            continue
        values = [dataset_result.get("setup_error", "")]
        for run in dataset_result.get("runs", []) or []:
            if not isinstance(run, dict):
                continue
            values.extend(run.get("errors", []) or [])
            values.append(run.get("final_answer", ""))
        for value in values:
            if provider_limit_reason(value):
                text = str(value or "").strip()
                if text and text not in reasons:
                    reasons.append(text)
    return reasons


def build_batch_run_identity(
    *,
    query_records: list[dict],
    results: list[dict],
    selected_dataset_ids: list[str] | None,
    debug_trace: bool,
    include_trace: bool,
) -> dict:
    records = _dedupe_query_records(query_records)
    actual_dataset_ids = [
        str(item.get("dataset_id", "") or "").strip()
        for item in results or []
        if isinstance(item, dict) and str(item.get("dataset_id", "") or "").strip()
    ]
    selection = {
        "full": True,
        "offset": 0,
        "limit": None,
        "selected_count": len(records),
        "selected_query_ids": [record.get("id") for record in records],
        "selected_query_keys": [_query_record_key(record) for record in records],
        "selected_dataset_ids": actual_dataset_ids,
        "requested_dataset_ids": resolve_dataset_ids(selected_dataset_ids),
    }
    datasets = [
        dataset_identity_payload(
            item.get("dataset_identity", {}) or item
        )
        for item in results or []
        if isinstance(item, dict)
    ]
    runtime = build_runtime_fingerprints(
        debug_trace=debug_trace,
        skip_eval=True,
    )
    config_payload = {
        "runtime_config": runtime["config"],
        "include_trace": bool(include_trace),
    }
    identity = {
        "identity_version": 1,
        "selection": selection,
        "datasets": datasets,
        "fingerprints": {
            "selection": stable_json_fingerprint(selection),
            "query": stable_json_fingerprint(
                {
                    "selected_query_ids": selection["selected_query_ids"],
                    "selected_query_keys": selection["selected_query_keys"],
                }
            ),
            "dataset": stable_json_fingerprint(datasets),
            "index": stable_json_fingerprint(
                [
                    {
                        "dataset_id": item.get("dataset_id", ""),
                        "collection": item.get("vector_collection_name", ""),
                        "generation": item.get("index_generation", ""),
                    }
                    for item in datasets
                ]
            ),
            "embedding": runtime["embedding"],
            "prompt": runtime["prompt"],
            "model": runtime["model"],
            "config": stable_json_fingerprint(config_payload),
        },
    }
    identity["run_fingerprint"] = stable_json_fingerprint(identity)
    return identity


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
    provider_limit_reasons = _batch_provider_limit_reasons(results)
    run_identity = build_batch_run_identity(
        query_records=query_records,
        results=results,
        selected_dataset_ids=selected_dataset_ids,
        debug_trace=debug_trace,
        include_trace=include_trace,
    )
    expected_runs = len(results) * len(query_records)
    run_complete = (
        datasets_with_setup_error == 0
        and total_runs == expected_runs
        and runs_with_errors == 0
        and not provider_limit_reasons
    )

    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "datasets_n": len(results),
        "queries": _query_texts(query_records),
        "query_records": query_records,
        "selected_dataset_ids": resolve_dataset_ids(selected_dataset_ids),
        "debug_trace": bool(debug_trace),
        "include_trace": bool(include_trace),
        "selection_contract": run_identity["selection"],
        "run_identity": run_identity,
        "run_fingerprint": run_identity["run_fingerprint"],
        "fingerprints": run_identity["fingerprints"],
        "run_complete": run_complete,
        "run_status": "complete" if run_complete else "incomplete",
        "latency_valid": not provider_limit_reasons,
        "latency_invalid_reasons": provider_limit_reasons,
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
        query_id = query_record.get("id")
        query_key = _query_record_key(query_record)
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
                if _query_record_key(candidate) == query_key:
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

        provider_reasons = []
        for result in query_results:
            values = [result.get("setup_error", "")]
            run = result.get("run", {}) or {}
            values.extend(run.get("errors", []) or [])
            values.append(run.get("final_answer", ""))
            for value in values:
                if provider_limit_reason(value):
                    text = str(value or "").strip()
                    if text and text not in provider_reasons:
                        provider_reasons.append(text)
        query_complete = (
            len(query_results) == len(results)
            and datasets_with_setup_error == 0
            and total_runs == len(results)
            and runs_with_errors == 0
            and not provider_reasons
        )
        query_report = {
                "query": query,
                "references": reference,
                "generated_at": report.get("generated_at", ""),
                "datasets_n": len(query_results),
                "debug_trace": bool(report.get("debug_trace", False)),
                "include_trace": bool(report.get("include_trace", False)),
                "total_runs": total_runs,
                "runs_with_errors": runs_with_errors,
                "datasets_with_setup_error": datasets_with_setup_error,
                "run_fingerprint": report.get("run_fingerprint", ""),
                "fingerprints": dict(report.get("fingerprints", {}) or {}),
                "run_complete": query_complete,
                "run_status": "complete" if query_complete else "incomplete",
                "latency_valid": not provider_reasons,
                "latency_invalid_reasons": provider_reasons,
                "results": query_results,
            }
        if query_id not in (None, ""):
            query_report["query_id"] = query_id
        query_reports.append(query_report)

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


def _query_report_key(item: dict) -> str:
    if not isinstance(item, dict):
        return ""
    return _query_record_key(
        {
            "id": item.get("query_id") if "query_id" in item else item.get("id"),
            "query": item.get("query", ""),
            "reference": item.get("references", item.get("reference", "")),
        }
    )


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
            query_key = _query_report_key(normalized)
            if query_key in index_by_query:
                merged[index_by_query[query_key]] = normalized
            else:
                index_by_query[query_key] = len(merged)
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
    dataset_summaries = []
    for result in query_report.get("results", []) or []:
        if not isinstance(result, dict) or not isinstance(result.get("run"), dict):
            continue
        run = result["run"]
        summary = run.get("run_summary", {}) or {}
        runtime = run.get("runtime")
        if runtime is None:
            runtime = _runtime_from_summary(summary)
        tokens = run.get("total_tokens")
        if tokens is None:
            tokens = _total_tokens_from_summary(summary)
        dataset_summaries.append(
            {
                "dataset_id": str(result.get("dataset_id", "") or ""),
                "final_answer": run.get("final_answer", "") or run.get("answer", ""),
                "runtime": runtime,
                "total_tokens": int(tokens or 0),
                "errors": list(run.get("errors", []) or []),
            }
        )

    single = dataset_summaries[0] if len(dataset_summaries) == 1 else {}
    reference = _normalize_reference(
        query_report.get("references")
        or query_report.get("reference")
    )

    return {
        "query": str(query_report.get("query", "") or "").strip(),
        # Legacy scalar answer remains valid only when the query ran on one
        # dataset.  Multi-dataset output is explicit and never picks index zero.
        "final_answer": single.get("final_answer", ""),
        "references": reference,
        "runtime": sum(float(item.get("runtime") or 0) for item in dataset_summaries),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in dataset_summaries),
        "dataset_summaries": dataset_summaries,
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
    source_incomplete = existing_item.get("run_complete") is False
    merged["run_complete"] = bool(merged.get("run_complete", True)) and not source_incomplete
    merged["run_status"] = "complete" if merged["run_complete"] else "incomplete"
    source_latency_invalid = existing_item.get("latency_valid") is False
    merged["latency_valid"] = bool(merged.get("latency_valid", True)) and not source_latency_invalid
    merged["latency_invalid_reasons"] = _dedupe_keep_order(
        [
            *(existing_item.get("latency_invalid_reasons", []) or []),
            *(merged.get("latency_invalid_reasons", []) or []),
        ]
    )
    return merged


def build_output_document(report: dict, *, existing_output: dict | None = None, overwrite: bool = False) -> dict:
    new_query_reports = build_query_reports(report)
    partial_dataset_update = bool(resolve_dataset_ids(report.get("selected_dataset_ids", [])))
    if overwrite:
        merged_reports = list(new_query_reports)
    else:
        merged_reports = normalize_existing_query_reports(existing_output)
        index_by_query = {
            _query_report_key(item): idx
            for idx, item in enumerate(merged_reports)
            if _query_report_key(item)
        }

        for item in new_query_reports:
            query_key = _query_report_key(item)
            if query_key in index_by_query:
                merged_reports[index_by_query[query_key]] = _merge_query_report(
                    merged_reports[index_by_query[query_key]],
                    item,
                    partial_dataset_update=partial_dataset_update,
                )
            else:
                index_by_query[query_key] = len(merged_reports)
                merged_reports.append(item)

    queries = build_output_queries(merged_reports)

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "updated_at": report.get("generated_at", ""),
        "run_identity": dict(report.get("run_identity", {}) or {}),
        "run_fingerprint": str(report.get("run_fingerprint", "") or ""),
        "fingerprints": dict(report.get("fingerprints", {}) or {}),
        "run_complete": bool(report.get("run_complete", False)),
        "run_status": str(report.get("run_status", "incomplete") or "incomplete"),
        "latency_valid": bool(report.get("latency_valid", False)),
        "latency_invalid_reasons": list(report.get("latency_invalid_reasons", []) or []),
        "document_fingerprint": stable_json_fingerprint(merged_reports),
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
    atomic_write_text(
        output_path,
        json.dumps(output_document, ensure_ascii=False, indent=2),
    )

    print(f"Saved batch results to {output_path.resolve()}")


if __name__ == "__main__":
    main()
