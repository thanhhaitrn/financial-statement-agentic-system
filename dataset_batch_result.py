"""Generate answer/retrieved-context reports for RAGAs evaluation."""

from __future__ import annotations

import argparse
import csv
import importlib.util
import io
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from evaluation.contracts import (
    REPORT_SCHEMA_VERSION,
    atomic_write_text,
    git_revision,
    provider_limit_reason,
    sha256_file,
    stable_json_fingerprint,
)
from common import dedupe_keep_order as _dedupe_keep_order, prediction_key, _record_id
from evaluation.run_identity import (
    RUN_IDENTITY_VERSION,
    build_run_identity,
    build_runtime_fingerprints,
    build_selection_contract,
    dataset_identity_payload,
    _embedding_identity_payload,
    _model_identity_payload,
    _safe_endpoint_identity,
    _source_files_identity,
)


DEFAULT_DATASET_ID = "apec"
DEFAULT_SEED_FILE = "dau_tu_APEC_ragas_seed.json"
DEFAULT_OUTPUT = "ragas_runs/apec_predictions.json"
DEFAULT_SMOKE_LIMIT = 10
CONTEXT_SEPARATOR = "\n---\n"
METRIC_NAMES = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
)
SESSION_LIMIT_MARKERS = (
    "session usage limit",
    "reached your session usage limit",
    "status code: 429",
    "responseerror",
)

MODEL_STAGE_EVENTS = {
    "planner:done",
    "planner:error",
    "router:done",
    "router:error",
    "analysis:done",
    "analysis:error",
    "synth:done",
    "synth:error",
}

class SeedValidationError(ValueError):
    """Raised when a seed or report file is not usable."""


class SessionLimitError(RuntimeError):
    """Raised when the active LLM session hits its usage limit."""

    def __init__(
        self,
        message: str,
        *,
        predictions: list[dict] | None = None,
        scores: list[dict] | None = None,
        dataset_meta: dict | None = None,
    ):
        super().__init__(message)
        self.predictions = list(predictions or [])
        self.scores = list(scores or [])
        self.dataset_meta = dict(dataset_meta or {})


def is_session_limit_error(value: Any) -> bool:
    text = str(value or "").lower()
    if not text:
        return False
    return bool(provider_limit_reason(text))


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _normalize_contexts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        str(item or "").strip()
        for item in value
        if str(item or "").strip()
    ]


def load_seed_records(path: str | Path) -> list[dict]:
    seed_path = Path(path)
    if not seed_path.exists():
        raise SeedValidationError(f"Seed file not found: {seed_path}")

    try:
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise SeedValidationError(f"Invalid JSON in seed file: {seed_path} ({exc})") from exc

    if not isinstance(payload, list):
        raise SeedValidationError("Seed file must contain a JSON list.")

    records = []
    errors = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            errors.append(f"record {index}: must be an object")
            continue

        question = str(item.get("question", "") or "").strip()
        ground_truth = str(item.get("ground_truth", "") or "").strip()
        contexts = _normalize_contexts(item.get("contexts"))

        missing = []
        if not question:
            missing.append("question")
        if not ground_truth:
            missing.append("ground_truth")
        if not contexts:
            missing.append("contexts")
        if missing:
            errors.append(f"record {_record_id(item, index)}: missing {', '.join(missing)}")
            continue

        records.append(
            {
                "id": _record_id(item, index),
                "source_chunk_id": item.get("source_chunk_id"),
                "question_id_in_chunk": item.get("question_id_in_chunk"),
                "question": question,
                "ground_truth": ground_truth,
                "seed_contexts": contexts,
            }
        )

    if errors:
        preview = "; ".join(errors[:8])
        suffix = f"; ... {len(errors) - 8} more" if len(errors) > 8 else ""
        raise SeedValidationError(f"Invalid seed records: {preview}{suffix}")

    if not records:
        raise SeedValidationError("Seed file has no usable records.")

    return records


def select_records(records: list[dict], *, limit: int | None = DEFAULT_SMOKE_LIMIT, full: bool = False, offset: int = 0) -> list[dict]:
    records = list(records or [])
    if offset < 0:
        raise ValueError("--offset must be non-negative.")
    if offset:
        records = records[offset:]
    if full:
        return records
    if limit is None:
        limit = DEFAULT_SMOKE_LIMIT
    if limit <= 0:
        raise ValueError("--limit must be positive unless --full is used.")
    return records[:limit]


def prediction_complete(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    answer = str(item.get("answer", "") or "")
    errors = item.get("errors", []) or []
    return bool(answer.strip()) and not errors and not (
        is_session_limit_error(answer)
        or any(is_session_limit_error(error) for error in errors)
    )


def predictions_by_key(predictions: list[dict], *, completed_only: bool = False) -> dict[str, dict]:
    output = {}
    for item in predictions or []:
        if not isinstance(item, dict):
            continue
        if completed_only and not prediction_complete(item):
            continue
        key = prediction_key(item)
        if key:
            output[key] = item
    return output


def merge_predictions_for_records(
    records: list[dict],
    *,
    existing_predictions: list[dict] | None = None,
    new_predictions: list[dict] | None = None,
) -> list[dict]:
    by_key = predictions_by_key(existing_predictions or [])
    by_key.update(predictions_by_key(new_predictions or []))
    merged = []
    for record in records or []:
        item = by_key.get(prediction_key(record))
        if item:
            merged.append(item)
    return merged


def _fact_context_text(fact: dict) -> str:
    if not isinstance(fact, dict):
        return ""

    # Keep only fields that help judge relevance / ground the answer. Dropped:
    # content_type/status are constant noise; evidence_text duplicates Item +
    # Value. Entity/unit/value type/reference/source remain because the
    # deterministic factual contract uses them to disambiguate otherwise
    # identical figures.
    labels = (
        ("company", "Entity"),
        ("table", "Table"),
        ("subheading", "Subheading"),
        ("item_name", "Item"),
        ("time_hint", "Period"),
        ("value", "Value"),
        ("unit", "Unit"),
        ("value_type", "Value type"),
        ("note_ref", "Note ref"),
        ("note_number", "Note number"),
        ("note_title", "Note title"),
        ("reference", "Reference"),
        ("source", "Source"),
    )
    parts = []
    for key, label in labels:
        value = _clean_text(fact.get(key))
        if value:
            parts.append(f"{label}: {value}")
    return "\n".join(parts).strip()


def _facts_from_payload(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    facts = payload.get("facts", [])
    if not isinstance(facts, list):
        return []
    return [fact for fact in facts if isinstance(fact, dict)]


def extract_retrieved_contexts(final_state: dict) -> list[str]:
    contexts = []
    ragas_facts_by_table = final_state.get("ragas_facts_by_table", {}) if isinstance(final_state, dict) else {}
    for payload in (ragas_facts_by_table or {}).values():
        for fact in _facts_from_payload(payload):
            text = _fact_context_text(fact)
            if text:
                contexts.append(text)

    if contexts:
        return _dedupe_keep_order(contexts)

    evidence_pack = final_state.get("evidence_pack", {}) if isinstance(final_state, dict) else {}
    facts_by_table = evidence_pack.get("facts_by_table", {}) if isinstance(evidence_pack, dict) else {}

    for payload in (facts_by_table or {}).values():
        for fact in _facts_from_payload(payload):
            text = _fact_context_text(fact)
            if text:
                contexts.append(text)

    if not contexts:
        worker_results = final_state.get("worker_results", {}) if isinstance(final_state, dict) else {}
        for payload in (worker_results or {}).values():
            for fact in _facts_from_payload(payload):
                text = _fact_context_text(fact)
                if text:
                    contexts.append(text)

    return _dedupe_keep_order(contexts)


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


def _prediction_from_error(record: dict, exc: Exception) -> dict:
    return {
        "id": record.get("id"),
        "source_chunk_id": record.get("source_chunk_id"),
        "question_id_in_chunk": record.get("question_id_in_chunk"),
        "question": record.get("question", ""),
        "answer": "",
        "ground_truth": record.get("ground_truth", ""),
        "retrieved_contexts": [],
        "seed_contexts": record.get("seed_contexts", []),
        "errors": [f"runtime_error ({type(exc).__name__}): {exc}"],
        "runtime": None,
        "tokens": 0,
        "synth_status": "error",
    }


def _collect_pipeline_errors(final_state: dict) -> list[str]:
    from test import collect_pipeline_errors

    return collect_pipeline_errors(final_state)


def _extract_answer(final_state: dict) -> str:
    from output_formatter import format_final_answer

    synth_decision = final_state.get("synth_decision", {}) or {}
    answer = str(synth_decision.get("answer", "") or "").strip()
    return answer or format_final_answer(final_state)


def _extract_run_summary(final_state: dict) -> dict:
    from test import extract_run_summary

    return extract_run_summary(final_state)


def _merged_interval_duration_ms(intervals: list[tuple[datetime, datetime]]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals, key=lambda item: item[0])
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return int(round(sum((end - start).total_seconds() * 1000 for start, end in merged)))


def build_prediction_latency_breakdown(
    final_state: dict,
    *,
    prediction_e2e_ms: int | None,
) -> dict[str, Any]:
    """Split prediction wall time into model-stage and retrieval/local time.

    Model stages may execute concurrently, so their timestamped wall intervals
    are unioned rather than summed.  The remainder of end-to-end time is the
    retrieval/local-processing bucket.  This is an application-stage breakdown,
    not provider-reported token-generation time.
    """

    total_ms = int(prediction_e2e_ms) if prediction_e2e_ms is not None else None
    intervals: list[tuple[datetime, datetime]] = []
    untimestamped_ms = 0
    for entry in (final_state or {}).get("trace", []) or []:
        if not isinstance(entry, dict) or entry.get("event") not in MODEL_STAGE_EVENTS:
            continue
        duration = entry.get("duration_ms")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            continue
        try:
            ended_at = datetime.fromisoformat(str(entry.get("timestamp", "") or ""))
        except ValueError:
            untimestamped_ms += int(round(duration))
            continue
        intervals.append((ended_at - timedelta(milliseconds=float(duration)), ended_at))

    model_ms = _merged_interval_duration_ms(intervals) + untimestamped_ms
    if total_ms is not None:
        model_ms = min(max(0, model_ms), max(0, total_ms))
        retrieval_local_ms = max(0, total_ms - model_ms)
    else:
        retrieval_local_ms = None

    return {
        "retrieval_local_ms": retrieval_local_ms,
        "model_generation_ms": model_ms if intervals or untimestamped_ms else None,
        "prediction_e2e_ms": total_ms,
        "measurement": "trace_model_stage_wall_union",
        "provider_generation_only": False,
    }


def prepare_dataset_runtime(dataset_id: str):
    """Build/validate a dataset once and expose its reproducible generations."""

    from dataset_catalog.registry import describe_dataset, get_dataset
    from kb.sqlite_repo import read_kb_manifest
    from test import ensure_built

    dataset = get_dataset(dataset_id)
    if dataset is None:
        raise SystemExit(f"Dataset not found: {dataset_id}")

    dataset, conn, collection = ensure_built(dataset)
    kb_manifest = read_kb_manifest(conn)
    index_generation = str(
        getattr(collection, "generation", "")
        or getattr(collection, "build_fingerprint", "")
        or ""
    ).strip()
    kb_generation = stable_json_fingerprint(kb_manifest) if kb_manifest else ""
    dataset_generation = stable_json_fingerprint(
        {
            "dataset_id": dataset.dataset_id,
            "ingestion_version": dataset.ingestion_version,
            "source_sha256": kb_manifest.get("source_sha256", ""),
            "parser_version": kb_manifest.get("parser_version", ""),
            "schema_version": kb_manifest.get("schema_version", ""),
            "facts_sha256": kb_manifest.get("facts_sha256", ""),
        }
    )
    dataset_meta = {
        "dataset_id": dataset.dataset_id,
        "description": describe_dataset(dataset),
        "company": dataset.company,
        "ticker": dataset.ticker,
        "fiscal_year": dataset.fiscal_year,
        "fiscal_quarter": dataset.fiscal_quarter,
        "scope": dataset.scope,
        "audit_status": dataset.audit_status,
        "file_path": dataset.file_path,
        "ingestion_version": dataset.ingestion_version,
        "vector_collection_name": dataset.vector_collection_name,
        "status": dataset.status,
        "facts_count": dataset.facts_count,
        "vector_docs_count": dataset.vector_docs_count,
        "source_sha256": kb_manifest.get("source_sha256", ""),
        "facts_sha256": kb_manifest.get("facts_sha256", ""),
        "parser_version": kb_manifest.get("parser_version", ""),
        "kb_schema_version": kb_manifest.get("schema_version", ""),
        "dataset_generation": dataset_generation,
        "kb_generation": kb_generation,
        "index_generation": index_generation,
        "collection_generation": index_generation,
    }
    return dataset, conn, collection, dataset_meta


def run_predictions(
    *,
    dataset_id: str,
    records: list[dict],
    debug_trace: bool = False,
    on_checkpoint=None,
    prepared_runtime=None,
) -> tuple[dict, list[dict]]:
    from test import execute_query

    runtime = prepared_runtime or prepare_dataset_runtime(dataset_id)
    dataset, _conn, collection, dataset_meta = runtime
    if str(dataset.dataset_id) != str(dataset_id):
        raise ValueError("Prepared runtime dataset does not match dataset_id")

    predictions = []
    for index, record in enumerate(records, start=1):
        question = str(record.get("question", "") or "").strip()
        print(f"[{index}/{len(records)}] {question}")
        sys.stdout.flush()

        try:
            final_state = execute_query(
                dataset,
                collection,
                question,
                debug_trace=debug_trace,
            )
            run_summary = _extract_run_summary(final_state)
            errors = _collect_pipeline_errors(final_state)
            if any(is_session_limit_error(error) for error in errors):
                raise SessionLimitError(
                    "LLM session usage limit reached while running workflow.",
                    predictions=predictions,
                    dataset_meta=dataset_meta,
                )
            contexts = extract_retrieved_contexts(final_state)
            if not contexts:
                errors = _dedupe_keep_order([*errors, "no_retrieved_contexts"])

            synth_decision = final_state.get("synth_decision", {}) or {}
            prediction_runtime = _runtime_from_summary(run_summary)
            prediction = {
                "id": record.get("id"),
                "source_chunk_id": record.get("source_chunk_id"),
                "question_id_in_chunk": record.get("question_id_in_chunk"),
                "question": question,
                "answer": _extract_answer(final_state),
                "ground_truth": record.get("ground_truth", ""),
                "retrieved_contexts": contexts,
                "seed_contexts": record.get("seed_contexts", []),
                "errors": errors,
                "runtime": prediction_runtime,
                "latency_breakdown": build_prediction_latency_breakdown(
                    final_state,
                    prediction_e2e_ms=prediction_runtime,
                ),
                "tokens": _total_tokens_from_summary(run_summary),
                "synth_status": str(synth_decision.get("status", "") or "").strip(),
            }
            predictions.append(prediction)
        except SessionLimitError:
            if on_checkpoint is not None:
                on_checkpoint(dataset_meta, list(predictions))
            raise
        except Exception as exc:
            if is_session_limit_error(exc):
                if on_checkpoint is not None:
                    on_checkpoint(dataset_meta, list(predictions))
                raise SessionLimitError(
                    f"LLM session usage limit reached while running workflow: {exc}",
                    predictions=predictions,
                    dataset_meta=dataset_meta,
                ) from exc
            prediction = _prediction_from_error(record, exc)
            predictions.append(prediction)

        if on_checkpoint is not None:
            on_checkpoint(dataset_meta, list(predictions))

    return dataset_meta, predictions


def build_ragas_rows(predictions: list[dict]) -> list[dict]:
    rows = []
    for item in predictions or []:
        rows.append(
            {
                "question": str(item.get("question", "") or "").strip(),
                "answer": str(item.get("answer", "") or "").strip(),
                "contexts": [
                    str(context or "").strip()
                    for context in (item.get("retrieved_contexts", []) or [])
                    if str(context or "").strip()
                ],
                "ground_truth": str(item.get("ground_truth", "") or "").strip(),
            }
        )
    return rows


def _jsonify(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonify(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonify(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonify(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if hasattr(value, "item"):
        try:
            return _jsonify(value.item())
        except Exception:
            pass
    return value


def _score_metric_values(scores: list[dict]) -> dict[str, list[float]]:
    values = {name: [] for name in METRIC_NAMES}
    for row in scores or []:
        if not isinstance(row, dict):
            continue
        for name in METRIC_NAMES:
            value = row.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                if not math.isnan(float(value)) and not math.isinf(float(value)):
                    values[name].append(float(value))
    return values


def build_summary(predictions: list[dict], scores: list[dict]) -> dict:
    metric_values = _score_metric_values(scores)
    metric_means = {
        name: (sum(values) / len(values) if values else None)
        for name, values in metric_values.items()
    }
    successful_predictions = [
        item
        for item in predictions or []
        if not item.get("errors")
    ]
    breakdown_fields = (
        "retrieval_local_ms",
        "model_generation_ms",
        "prediction_e2e_ms",
    )
    breakdown_values = {
        field: [
            float(item.get("latency_breakdown", {}).get(field))
            for item in predictions or []
            if isinstance(item.get("latency_breakdown"), dict)
            and isinstance(item["latency_breakdown"].get(field), (int, float))
            and not isinstance(item["latency_breakdown"].get(field), bool)
        ]
        for field in breakdown_fields
    }
    return {
        "records_n": len(predictions or []),
        "successful_predictions_n": len(successful_predictions),
        "errored_predictions_n": len(predictions or []) - len(successful_predictions),
        "evaluated_scores_n": len(scores or []),
        "metric_means": metric_means,
        "prediction_latency_breakdown": {
            "measurement": "trace_model_stage_wall_union",
            "samples_n": min((len(values) for values in breakdown_values.values()), default=0),
            **{
                f"mean_{field}": (
                    round(sum(values) / len(values), 3) if values else None
                )
                for field, values in breakdown_values.items()
            },
        },
    }


def load_report(path: str | Path) -> dict:
    report_path = Path(path)
    if not report_path.exists():
        return {}
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except JSONDecodeError as exc:
        raise SeedValidationError(f"Invalid report JSON: {report_path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise SeedValidationError(f"Invalid report JSON: {report_path} must contain an object.")
    return payload


def predictions_from_report(report: dict) -> list[dict]:
    predictions = report.get("predictions", []) if isinstance(report, dict) else []
    return [item for item in (predictions or []) if isinstance(item, dict)]


def dataset_meta_from_report(report: dict) -> dict:
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    dataset = metadata.get("dataset", {}) if isinstance(metadata, dict) else {}
    return dict(dataset) if isinstance(dataset, dict) else {}


def validate_resume_report(
    report: dict,
    *,
    seed_file: str | Path,
    dataset_id: str,
    selected_records: list[dict] | None = None,
    full: bool | None = None,
    limit: int | None = None,
    offset: int | None = None,
    current_dataset_meta: dict | None = None,
    debug_trace: bool = False,
    skip_eval: bool = True,
) -> None:
    if not report:
        return
    metadata = report.get("metadata", {}) or {}
    if not isinstance(metadata, dict):
        raise SeedValidationError("Resume report metadata must be an object.")

    stored_identity = metadata.get("run_identity")
    if not isinstance(stored_identity, dict) or not stored_identity.get(
        "run_fingerprint"
    ):
        raise SeedValidationError(
            "Resume report has no complete run_identity; start a new output instead."
        )

    unsigned_stored_identity = dict(stored_identity)
    stored_fingerprint = str(
        unsigned_stored_identity.pop("run_fingerprint", "") or ""
    ).strip()
    if stable_json_fingerprint(unsigned_stored_identity) != stored_fingerprint:
        raise SeedValidationError("Resume report run_identity failed integrity validation.")
    if str(metadata.get("run_fingerprint", "") or "").strip() != stored_fingerprint:
        raise SeedValidationError("Resume report run fingerprint fields disagree.")

    stored_selection = stored_identity.get("selection", {}) or {}
    if selected_records is None:
        selected_records = predictions_from_report(report)
    if full is None:
        full = bool(stored_selection.get("full", metadata.get("selection") == "full"))
    if limit is None and not full:
        limit = stored_selection.get("limit", metadata.get("limit"))
    if offset is None:
        offset = int(stored_selection.get("offset", metadata.get("offset", 0)) or 0)

    dataset_meta = dict(current_dataset_meta or dataset_meta_from_report(report))
    dataset_meta.setdefault("dataset_id", dataset_id)
    expected_identity = build_run_identity(
        seed_file=seed_file,
        dataset_meta=dataset_meta,
        selected_records=selected_records,
        full=bool(full),
        limit=limit,
        offset=offset,
        debug_trace=debug_trace,
        skip_eval=skip_eval,
    )
    if stored_fingerprint == expected_identity["run_fingerprint"]:
        return

    mismatches = []
    if stored_identity.get("seed_sha256") != expected_identity.get("seed_sha256"):
        mismatches.append("seed")
    if stored_identity.get("selection") != expected_identity.get("selection"):
        mismatches.append("selection")
    if stored_identity.get("dataset") != expected_identity.get("dataset"):
        mismatches.append("dataset/index generation")
    stored_fingerprints = stored_identity.get("fingerprints", {}) or {}
    expected_fingerprints = expected_identity.get("fingerprints", {}) or {}
    for name in ("query", "embedding", "prompt", "model", "config"):
        if stored_fingerprints.get(name) != expected_fingerprints.get(name):
            mismatches.append(name)
    details = ", ".join(mismatches or ["run identity"])
    raise SeedValidationError(f"Resume report mismatch: {details}.")


def _retrieval_embedding_model() -> str:
    """The embedding model retrieval actually uses (single source of truth).

    Mirrors vectorstore.qdrant_store so the report never mislabels the run; falls
    back to the same env/default if that module cannot be imported.
    """
    try:
        from vectorstore.qdrant_store import EMBEDDING_MODEL

        return EMBEDDING_MODEL
    except Exception:
        return os.getenv("OLLAMA_EMBEDDING_MODEL", "bge-m3")


def build_report(
    *,
    seed_file: str,
    dataset_meta: dict,
    predictions: list[dict],
    scores: list[dict],
    full: bool,
    limit: int | None,
    skip_eval: bool,
    run_complete: bool = True,
    eval_error: str = "",
    offset: int = 0,
    selected_records: list[dict] | None = None,
    debug_trace: bool = False,
    source_report: dict | None = None,
    resume_repaired: bool = False,
) -> dict:
    seed_path = Path(seed_file)
    seed_checksum = sha256_file(seed_path) if seed_path.is_file() else ""
    provider_limit_errors = [
        str(error)
        for prediction in predictions or []
        if isinstance(prediction, dict)
        for error in list(prediction.get("errors", []) or []) + [prediction.get("answer", "")]
        if provider_limit_reason(error)
    ]
    source_metadata = (
        source_report.get("metadata", {})
        if isinstance(source_report, dict)
        else {}
    )
    if not isinstance(source_metadata, dict):
        source_metadata = {}
    source_latency_valid = source_metadata.get("latency_valid")
    source_latency_reasons = _dedupe_keep_order(
        source_metadata.get("latency_invalid_reasons", []) or []
    )
    if source_latency_valid is False and not source_latency_reasons:
        source_latency_reasons = ["source_report_latency_invalid"]
    # Resumed timings include samples from the source report, so any prior
    # provider/quota contamination remains attached to the whole latency run.
    latency_invalid_reasons = _dedupe_keep_order(
        [*source_latency_reasons, *provider_limit_errors]
    )
    model = os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud")
    embedding_model = _retrieval_embedding_model()
    identity_records = selected_records if selected_records is not None else predictions
    run_identity = build_run_identity(
        seed_file=seed_file,
        dataset_meta=dataset_meta,
        selected_records=identity_records,
        full=full,
        limit=limit,
        offset=offset,
        debug_trace=debug_trace,
        skip_eval=skip_eval,
    )
    selection_contract = run_identity["selection"]
    selection_complete = True
    if selected_records is not None:
        completed = predictions_by_key(predictions, completed_only=True)
        selection_complete = bool(selected_records) and all(
            prediction_key(record) in completed for record in selected_records
        )

    source_run_complete = source_metadata.get("run_complete")
    preserve_source_incomplete = (
        bool(source_report)
        and source_run_complete is False
        and not resume_repaired
    )
    effective_eval_error = str(eval_error or "").strip()
    if not effective_eval_error and preserve_source_incomplete:
        effective_eval_error = str(source_metadata.get("eval_error", "") or "").strip()
    latency_valid = not latency_invalid_reasons
    effective_run_complete = (
        bool(run_complete)
        and selection_complete
        and not preserve_source_incomplete
        and latency_valid
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "metadata": {
            "generated_at": _utc_now(),
            "dataset": dataset_meta,
            "seed_file": str(seed_file),
            "seed_sha256": seed_checksum,
            "git_revision": git_revision(),
            "dataset_fingerprint": stable_json_fingerprint(dataset_meta),
            "run_fingerprint": run_identity["run_fingerprint"],
            "run_identity": run_identity,
            "fingerprints": dict(run_identity["fingerprints"]),
            "context_source": "retrieval",
            "selection": "full" if full else "smoke",
            "limit": None if full else limit,
            "offset": int(offset),
            "selection_contract": selection_contract,
            "selected_query_ids": selection_contract["selected_query_ids"],
            "skip_eval": bool(skip_eval),
            "run_complete": effective_run_complete,
            "run_status": "complete" if effective_run_complete else "incomplete",
            "eval_error": effective_eval_error,
            "latency_valid": latency_valid,
            "latency_invalid_reasons": latency_invalid_reasons,
            "latency_scope": "prediction_only_excludes_ragas_judge",
            "ragas_installed": importlib.util.find_spec("ragas") is not None,
            "llm_model": model,
            "embedding_model": embedding_model,
            "resume_source_status": {
                "present": bool(source_report),
                "run_complete": source_run_complete,
                "latency_valid": source_latency_valid,
                "latency_invalid_reasons": source_latency_reasons,
                "eval_error": str(source_metadata.get("eval_error", "") or "").strip(),
                "repaired": bool(resume_repaired),
            },
        },
        "predictions": _jsonify(predictions),
        "scores": _jsonify(scores),
        "summary": build_summary(predictions, scores),
    }


def write_json_report(report: dict, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return atomic_write_text(
        path,
        json.dumps(_jsonify(report), ensure_ascii=False, indent=2),
    )


def _scores_by_id(scores: list[dict]) -> dict[str, dict]:
    return {
        str(row.get("id", "") or ""): row
        for row in scores or []
        if isinstance(row, dict) and str(row.get("id", "") or "").strip()
    }


def write_csv_report(report: dict, output_path: str | Path) -> Path:
    json_path = Path(output_path)
    csv_path = json_path.with_suffix(".csv")
    scores_by_id = _scores_by_id(report.get("scores", []) or [])
    fieldnames = [
        "id",
        "question",
        "answer",
        "ground_truth",
        "retrieved_contexts_n",
        "retrieved_contexts",
        "seed_contexts_n",
        "errors",
        "runtime",
        "retrieval_local_ms",
        "model_generation_ms",
        "tokens",
        *METRIC_NAMES,
    ]

    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for item in report.get("predictions", []) or []:
        score = scores_by_id.get(str(item.get("id", "") or ""), {})
        writer.writerow(
            {
                "id": item.get("id", ""),
                "question": item.get("question", ""),
                "answer": item.get("answer", ""),
                "ground_truth": item.get("ground_truth", ""),
                "retrieved_contexts_n": len(item.get("retrieved_contexts", []) or []),
                "retrieved_contexts": CONTEXT_SEPARATOR.join(item.get("retrieved_contexts", []) or []),
                "seed_contexts_n": len(item.get("seed_contexts", []) or []),
                "errors": "; ".join(item.get("errors", []) or []),
                "runtime": item.get("runtime"),
                "retrieval_local_ms": (item.get("latency_breakdown", {}) or {}).get(
                    "retrieval_local_ms"
                ),
                "model_generation_ms": (item.get("latency_breakdown", {}) or {}).get(
                    "model_generation_ms"
                ),
                "tokens": item.get("tokens", 0),
                **{name: score.get(name) for name in METRIC_NAMES},
            }
        )
    return atomic_write_text(csv_path, handle.getvalue())


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Run the financial QA workflow and save answers/retrieved contexts for RAGAs."
    )
    parser.add_argument("--dataset-id", default=DEFAULT_DATASET_ID, help="Registered dataset id to run.")
    parser.add_argument("--seed-file", default=DEFAULT_SEED_FILE, help="RAGAs seed JSON file.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="Output JSON path.")
    parser.add_argument("--limit", type=int, default=DEFAULT_SMOKE_LIMIT, help="Smoke run size. Ignored with --full.")
    parser.add_argument("--offset", type=int, default=0, help="Skip the first N seed records (e.g. --offset 10 --limit 20 runs records 11-30).")
    parser.add_argument("--full", action="store_true", help="Run all seed records.")
    parser.add_argument("--debug-trace", action="store_true", help="Enable graph debug trace.")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse predictions already present in --output and run only missing seed records.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        seed_records = load_seed_records(args.seed_file)
        selected_records = select_records(
            seed_records,
            limit=args.limit,
            full=args.full,
            offset=args.offset,
        )
    except (SeedValidationError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    existing_report = load_report(args.output) if args.resume and Path(args.output).exists() else {}
    prepared_runtime = None
    if existing_report:
        try:
            prepared_runtime = prepare_dataset_runtime(args.dataset_id)
            validate_resume_report(
                existing_report,
                seed_file=args.seed_file,
                dataset_id=args.dataset_id,
                selected_records=selected_records,
                full=args.full,
                limit=args.limit,
                offset=args.offset,
                current_dataset_meta=prepared_runtime[3],
                debug_trace=args.debug_trace,
                skip_eval=True,
            )
        except SeedValidationError as exc:
            raise SystemExit(str(exc)) from exc
    existing_predictions = predictions_from_report(existing_report)
    completed_keys = set(predictions_by_key(existing_predictions, completed_only=True))
    pending_records = [
        record
        for record in selected_records
        if prediction_key(record) not in completed_keys
    ]

    def write_checkpoint(dataset_meta: dict, current_predictions: list[dict]) -> None:
        merged_predictions = merge_predictions_for_records(
            selected_records,
            existing_predictions=existing_predictions,
            new_predictions=current_predictions,
        )
        report = build_report(
            seed_file=args.seed_file,
            dataset_meta=dataset_meta,
            predictions=merged_predictions,
            scores=[],
            full=args.full,
            limit=args.limit,
            skip_eval=True,
            run_complete=False,
            offset=args.offset,
            selected_records=selected_records,
            debug_trace=args.debug_trace,
            source_report=existing_report,
        )
        write_json_report(report, args.output)
        write_csv_report(report, args.output)

    stop_error = ""
    exit_code = 0
    if pending_records:
        try:
            dataset_meta, new_predictions = run_predictions(
                dataset_id=args.dataset_id,
                records=pending_records,
                debug_trace=args.debug_trace,
                on_checkpoint=write_checkpoint,
                prepared_runtime=prepared_runtime,
            )
        except SessionLimitError as exc:
            stop_error = f"session_limit ({type(exc).__name__}): {exc}"
            print(stop_error, file=sys.stderr)
            dataset_meta = exc.dataset_meta or dataset_meta_from_report(existing_report)
            new_predictions = exc.predictions
            exit_code = 1
    else:
        dataset_meta = (
            prepared_runtime[3]
            if prepared_runtime is not None
            else dataset_meta_from_report(existing_report)
        )
        new_predictions = []
        print("No pending records. Reusing existing predictions from output.")

    predictions = merge_predictions_for_records(
        selected_records,
        existing_predictions=existing_predictions,
        new_predictions=new_predictions,
    )
    selected_keys = {prediction_key(record) for record in selected_records}
    completed_keys = set(predictions_by_key(predictions, completed_only=True))
    resume_repaired = bool(pending_records) and selected_keys.issubset(completed_keys)
    report = build_report(
        seed_file=args.seed_file,
        dataset_meta=dataset_meta,
        predictions=predictions,
        scores=[],
        full=args.full,
        limit=args.limit,
        skip_eval=True,
        run_complete=not bool(stop_error),
        eval_error=stop_error,
        offset=args.offset,
        selected_records=selected_records,
        debug_trace=args.debug_trace,
        source_report=existing_report,
        resume_repaired=resume_repaired and not bool(stop_error),
    )
    json_path = write_json_report(report, args.output)
    csv_path = write_csv_report(report, json_path)

    print(f"Wrote JSON report: {json_path}")
    print(f"Wrote CSV report: {csv_path}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
