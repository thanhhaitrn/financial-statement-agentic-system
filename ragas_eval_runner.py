"""Evaluate an existing answer/retrieved-context report with RAGAs."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import importlib.util
import json
import math
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext
from numbers import Real
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from dataset_batch_result import (
    DEFAULT_SEED_FILE,
    METRIC_NAMES,
    SessionLimitError,
    build_ragas_rows,
    build_report,
    is_session_limit_error,
    load_report,
    prediction_key,
    predictions_from_report,
    write_csv_report,
    write_json_report,
)
from eval_retrieval_recall import (
    DEFAULT_FACTS_CONTRACT,
    FACTUAL_RECALL_THRESHOLD,
    evaluate_factual_recall,
    load_factual_contract_records,
    matched_official_gate_records,
)
from evaluation.contracts import REPORT_SCHEMA_VERSION


REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PREDICTIONS_FILE = "ragas_runs/apec_predictions.json"
DEFAULT_METRIC_SLEEP_SECONDS = 2.0
DEFAULT_SAMPLE_SLEEP_SECONDS = 1.0
RETIRED_METRIC_NAMES = ("answer_correctness",)
EVAL_REPORT_SCHEMA_VERSION = REPORT_SCHEMA_VERSION
_LATENCY_CONTAMINATION_RE = re.compile(
    r"(?i)(?:session[_\s-]*(?:usage\s*)?limit|usage[_\s-]*limit|quota|rate[_\s-]*limit|"
    r"too many requests|status\s*code\s*:?\s*429|http\s*429|retry-after|retrying.*(?:429|quota)|"
    r"backoff.*(?:429|quota|limit))"
)

# Labels emitted by dataset_batch_result._fact_context_text, in order.
_CONTEXT_LABELS = ("Table", "Subheading", "Item", "Period", "Value", "Note ref", "Note number", "Note title")
_CONTEXT_FIELD_RE = re.compile(
    r"(?P<label>" + "|".join(re.escape(label) for label in _CONTEXT_LABELS) + r")\s*:\s*",
)
_MONETARY_RE = re.compile(r"^\(?-?\d{1,3}(?:\.\d{3})+\)?$|^\(?-?\d{4,}\)?$")


def _parse_terse_context(text: str) -> dict[str, str]:
    """Parse a 'Label: value' fact string (newline- or space-separated) into a
    field dict. Returns {} when it doesn't look like the terse fact format."""
    matches = list(_CONTEXT_FIELD_RE.finditer(text or ""))
    if not matches:
        return {}
    fields = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        fields[match.group("label")] = text[start:end].strip()
    return fields


def _prose_context(text: str) -> str:
    """Render a terse Table/Item/Value fact as a natural-language sentence so the
    RAGAS judge can attribute prose ground-truth claims to it. Information is
    preserved verbatim; only phrasing changes. Falls back to the input when the
    text is not a recognizable fact string (e.g. web/prose facts)."""
    fields = _parse_terse_context(text)
    if "Value" not in fields:
        return text

    item = fields.get("Item", "")
    line_item, _, column = item.partition("|")
    line_item = line_item.strip()
    column = column.strip()

    location = fields.get("Table", "")
    subheading = fields.get("Subheading", "")
    if subheading:
        location = f"{location} — {subheading}" if location else subheading

    period_word = {"cuối": "cuối kỳ", "đầu": "đầu kỳ"}.get(fields.get("Period", "").strip(), "")
    # Prefer the column label already on the item; fall back to the period field.
    qualifier = column or period_word

    value = fields.get("Value", "")
    if _MONETARY_RE.match(value.replace(" ", "")):
        value = f"{value} VND"

    subject = line_item or item or location
    parts = []
    if location and subject != location:
        parts.append(f"Theo {location},")
    if qualifier:
        parts.append(f"{subject} ({qualifier}) là {value}.")
    else:
        parts.append(f"{subject} là {value}.")

    note_ref = fields.get("Note ref", "") or fields.get("Note number", "")
    if note_ref:
        parts.append(f"(Thuyết minh {note_ref})")
    return " ".join(parts).strip()


def build_ragas_evaluation_samples(predictions: list[dict]) -> list[dict]:
    # RAGAS_PROSE_CONTEXT renders terse facts as sentences for the judge. An A/B
    # re-score (2026-07-20, 8 ids) found no reliable gain: context_recall +0.00,
    # faithfulness +0.02 (below noise), and answer_relevancy drifted +0.12 on a
    # change that cannot affect it — i.e. minimax-m3 scoring noise (±0.1) swamps
    # the effect. Kept as an opt-in; default off. The real error source is the
    # judge itself (see id 212: answer == GT verbatim, value in context, yet
    # scored faithfulness=recall=0).
    prose = os.getenv("RAGAS_PROSE_CONTEXT", "0").strip() == "1"
    samples = []
    for item in build_ragas_rows(predictions):
        contexts = item["contexts"]
        if prose:
            contexts = [_prose_context(context) for context in contexts]
        samples.append(
            {
                "user_input": item["question"],
                "response": item["answer"],
                "retrieved_contexts": contexts,
                "reference": item["ground_truth"],
            }
        )
    return samples


from evaluation.ragas_evaluator import (
    _build_evaluator_embeddings,
    _build_evaluator_llm,
    _instantiate_metric,
    _load_metric_objects,
    _metric_from_module,
    _metric_name,
    _ragas_available,
    _set_metric_name,
)


def _score_has_metric(score: dict, metric_name: str) -> bool:
    if not isinstance(score, dict) or not metric_name or metric_name not in score:
        return False
    value = score.get(metric_name)
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, Real):
        return not math.isnan(float(value)) and not math.isinf(float(value))
    return True


def _normalize_score_row(score: dict) -> dict:
    row = dict(score or {}) if isinstance(score, dict) else {}
    for metric_name in RETIRED_METRIC_NAMES:
        row.pop(metric_name, None)
        row.pop(f"{metric_name}_error", None)
    return row


def _score_complete(score: dict) -> bool:
    if not isinstance(score, dict) or score.get("score_error"):
        return False
    return all(_score_has_metric(score, name) for name in METRIC_NAMES)


def _enrich_score_identity(score: dict, predictions: list[dict]) -> dict:
    """Migrate v1 score rows to the reference-aware sample identity.

    New score rows always carry ground_truth/reference.  An old compact row can
    be reused only when id+question identifies exactly one current prediction;
    ambiguous duplicate questions are deliberately re-scored.
    """
    row = _normalize_score_row(score)
    if row.get("ground_truth") or row.get("reference"):
        return row
    record_id = str(row.get("id", "") or "").strip()
    question = " ".join(str(row.get("question", "") or "").split()).strip()
    candidates = [
        prediction
        for prediction in predictions
        if str(prediction.get("id", "") or "").strip() == record_id
        and " ".join(str(prediction.get("question", "") or "").split()).strip() == question
    ]
    if len(candidates) == 1:
        reference = str(candidates[0].get("ground_truth", "") or "")
        row["ground_truth"] = reference
        row["reference"] = reference
    return row


def _scores_by_key(scores: list[dict], predictions: list[dict] | None = None) -> dict[str, dict]:
    output = {}
    for score in scores or []:
        row = _enrich_score_identity(score, predictions or [])
        key = prediction_key(row)
        if key:
            row["sample_fingerprint"] = key
            output[key] = row
    return output


def merge_scores_for_predictions(
    predictions: list[dict],
    *,
    existing_scores: list[dict] | None = None,
    new_scores: list[dict] | None = None,
) -> list[dict]:
    by_key = _scores_by_key(existing_scores or [], predictions)
    for score in new_scores or []:
        row = _enrich_score_identity(score, predictions)
        key = prediction_key(row)
        if key:
            row["sample_fingerprint"] = key
            by_key[key] = row
    merged = []
    for prediction in predictions or []:
        score = by_key.get(prediction_key(prediction))
        if score:
            merged.append(score)
    return merged


def _extract_ragas_scores(result: Any) -> list[dict]:
    scores = getattr(result, "scores", None)
    if scores is None and hasattr(result, "to_pandas"):
        scores = result.to_pandas().to_dict(orient="records")
    if scores is None:
        repr_dict = getattr(result, "_repr_dict", None)
        if callable(repr_dict):
            repr_dict = repr_dict()
        if isinstance(repr_dict, dict):
            scores = [repr_dict]
    if scores is None:
        return []
    return [score for score in list(scores) if isinstance(score, dict)]


def _extract_metric_value(result: Any, metric_name: str) -> Any:
    for score in _extract_ragas_scores(result):
        if metric_name in score:
            return score[metric_name]
    raise RuntimeError(f"RAGAs did not return a score for metric: {metric_name}")


def _is_event_loop_closed_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if "event loop is closed" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def _set_fresh_event_loop() -> None:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None

    if loop is not None and not loop.is_running() and not loop.is_closed():
        loop.close()

    asyncio.set_event_loop(asyncio.new_event_loop())


def _build_run_config():
    """RAGAs RunConfig tuned for a serialized cloud LLM endpoint.

    Ollama Cloud queues concurrent requests per account, so the default
    max_workers=16 just piles requests behind each other until they blow past
    RAGAs' default per-task timeout (180s) and surface as bare TimeoutErrors.
    Keep concurrency low and the timeout aligned with the LLM read timeout.
    """
    from llm.client import LLM_REQUEST_TIMEOUT_SECONDS
    from ragas.run_config import RunConfig

    return RunConfig(
        max_workers=int(os.getenv("RAGAS_MAX_WORKERS", "1")),
        timeout=int(os.getenv("RAGAS_TIMEOUT_SECONDS", str(int(LLM_REQUEST_TIMEOUT_SECONDS)))),
        max_retries=int(os.getenv("RAGAS_MAX_RETRIES", "3")),
        max_wait=int(os.getenv("RAGAS_MAX_WAIT_SECONDS", "120")),
    )


def _evaluate_one_metric(
    prediction: dict,
    *,
    metric: Any,
    batch_size: int | None = None,
    resources: dict[str, Any] | None = None,
) -> Any:
    from ragas import evaluate
    from ragas.dataset_schema import EvaluationDataset

    metric_name = _metric_name(metric)
    dataset = EvaluationDataset.from_list(build_ragas_evaluation_samples([prediction]))
    resources = resources if resources is not None else {}

    for attempt in range(2):
        if resources.get("llm") is None:
            resources["llm"] = _build_evaluator_llm()
        if resources.get("embeddings") is None:
            resources["embeddings"] = _build_evaluator_embeddings()

        try:
            result = evaluate(
                dataset,
                metrics=[metric],
                llm=resources["llm"],
                embeddings=resources["embeddings"],
                raise_exceptions=True,
                show_progress=False,
                batch_size=batch_size,
                run_config=_build_run_config(),
                allow_nest_asyncio=False,
            )
            return _extract_metric_value(result, metric_name)
        except Exception as exc:
            if attempt == 0 and _is_event_loop_closed_error(exc):
                _set_fresh_event_loop()
                resources.clear()
                _sleep(1.0)
                continue
            raise

    raise RuntimeError(f"RAGAs did not return a score for metric: {metric_name}")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _sleep(seconds: float) -> None:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds:
        time.sleep(seconds)


def evaluate_with_ragas(
    predictions: list[dict],
    *,
    batch_size: int | None = None,
    existing_scores: list[dict] | None = None,
    metric_sleep_seconds: float = DEFAULT_METRIC_SLEEP_SECONDS,
    sample_sleep_seconds: float = DEFAULT_SAMPLE_SLEEP_SECONDS,
    on_checkpoint=None,
) -> list[dict]:
    if not _ragas_available():
        raise RuntimeError(
            "RAGAs is not installed. Install it with: "
            "python -m pip install -e '.[eval]'"
        )

    existing_scores = list(existing_scores or [])
    existing_scores_by_key = _scores_by_key(existing_scores, predictions)
    new_scores_by_key: dict[str, dict] = {}
    evaluator_resources: dict[str, Any] = {}

    def merged_scores() -> list[dict]:
        return merge_scores_for_predictions(
            predictions,
            existing_scores=existing_scores,
            new_scores=list(new_scores_by_key.values()),
        )

    # The local registry lives under dataset_catalog, so RAGAS can import the
    # Hugging Face ``datasets`` package normally without sys.modules surgery.
    with nullcontext():
        metrics = _load_metric_objects()

        for index, prediction in enumerate(predictions, start=1):
            key = prediction_key(prediction)
            score = _normalize_score_row(existing_scores_by_key.get(key, {}))
            if _score_complete(score):
                continue

            print(f"[RAGAs {index}/{len(predictions)}] {prediction.get('question', '')}")
            sys.stdout.flush()

            reference = str(prediction.get("ground_truth", "") or "")
            score.update(
                {
                    "id": prediction.get("id"),
                    "question": prediction.get("question", ""),
                    "ground_truth": reference,
                    "reference": reference,
                    "sample_fingerprint": key,
                }
            )
            score.pop("score_error", None)

            for metric_index, metric in enumerate(metrics, start=1):
                metric_name = _metric_name(metric)
                if _score_has_metric(score, metric_name):
                    continue

                score.pop(f"{metric_name}_error", None)
                print(f"  [metric {metric_index}/{len(metrics)}] {metric_name}")
                sys.stdout.flush()

                try:
                    score[metric_name] = _evaluate_one_metric(
                        prediction,
                        metric=metric,
                        batch_size=batch_size,
                        resources=evaluator_resources,
                    )
                except Exception as exc:
                    score[f"{metric_name}_error"] = f"{type(exc).__name__}: {exc}"
                    new_scores_by_key[key] = _normalize_score_row(score)

                    if is_session_limit_error(exc):
                        current_scores = merged_scores()
                        error = f"session_limit ({type(exc).__name__}): {exc}"
                        if on_checkpoint is not None:
                            on_checkpoint(current_scores, error)
                        raise SessionLimitError(
                            f"LLM session usage limit reached while scoring RAGAs: {exc}",
                            scores=current_scores,
                        ) from exc

                new_scores_by_key[key] = _normalize_score_row(score)
                if on_checkpoint is not None:
                    metric_error = str(score.get(f"{metric_name}_error", "") or "")
                    on_checkpoint(
                        merged_scores(),
                        f"metric_error ({metric_name}): {metric_error}" if metric_error else "",
                    )

                if metric_index < len(metrics):
                    _sleep(metric_sleep_seconds)

            if index < len(predictions):
                _sleep(sample_sleep_seconds)

    return merged_scores()


def _report_seed_file(report: dict, fallback: str = DEFAULT_SEED_FILE) -> str:
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    return str(metadata.get("seed_file", "") or fallback)


def _report_dataset_meta(report: dict) -> dict:
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    dataset = metadata.get("dataset", {}) if isinstance(metadata, dict) else {}
    return dict(dataset) if isinstance(dataset, dict) else {}


def _report_selection(report: dict) -> tuple[bool, int | None]:
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    full = str(metadata.get("selection", "") or "").strip() == "full"
    limit = metadata.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = None
    return full, limit


def _source_prediction_status(report: dict) -> tuple[bool | None, str]:
    """Return completion state only when *report* is a prediction artifact.

    A previously interrupted RAGAS scoring report may legitimately become
    complete after resume, so only the upstream prediction checkpoint is
    sticky.  New prediction reports set ``skip_eval``; the empty-score check
    keeps compatibility with older prediction-only reports.
    """
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    if not isinstance(metadata, dict):
        return None, ""
    if "skip_eval" in metadata:
        is_prediction_report = metadata.get("skip_eval") is True
    else:
        # Legacy prediction-only reports predate the explicit marker.
        is_prediction_report = not bool(
            report.get("scores", []) if isinstance(report, dict) else []
        )
    complete = metadata.get("run_complete")
    if not is_prediction_report or not isinstance(complete, bool):
        return None, ""
    return complete, str(metadata.get("eval_error", "") or "").strip()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _file_fingerprint(path: str | Path) -> str | None:
    candidate = Path(path)
    try:
        return _sha256_bytes(candidate.read_bytes()) if candidate.is_file() else None
    except OSError:
        return None


def _git_revision() -> str:
    override = os.getenv("GIT_COMMIT", "").strip()
    if override:
        return override
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _resolve_seed_path(seed_file: str, *, input_path: Path | None = None) -> Path:
    candidate = Path(seed_file)
    if candidate.is_absolute():
        return candidate
    options = [REPO_ROOT / candidate]
    if input_path is not None:
        options.append(input_path.parent / candidate)
    return next((path for path in options if path.exists()), options[0])


def _resolve_facts_contract_path(contract_path: str | Path = DEFAULT_FACTS_CONTRACT) -> Path:
    candidate = Path(contract_path)
    return candidate if candidate.is_absolute() else REPO_ROOT / candidate


def _official_gate_predictions(
    predictions: list[dict],
    *,
    dataset_meta: dict | None = None,
    contract_path: str | Path = DEFAULT_FACTS_CONTRACT,
) -> list[dict]:
    """Return only reviewed contract identities present in this report."""
    metadata, contract_records = load_factual_contract_records(
        _resolve_facts_contract_path(contract_path)
    )
    report_dataset_id = str((dataset_meta or {}).get("dataset_id", "") or "").strip()
    contract_dataset_id = str(metadata.get("dataset_id", "") or "").strip()
    if report_dataset_id and contract_dataset_id and report_dataset_id != contract_dataset_id:
        return []
    return matched_official_gate_records(contract_records, predictions)


def _attach_explicit_seed_facts(
    predictions: list[dict],
    *,
    seed_path: Path,
) -> list[dict]:
    """Attach versioned expected_facts without changing legacy prediction keys."""
    try:
        payload = json.loads(seed_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [dict(prediction) for prediction in predictions]
    if not isinstance(payload, list):
        return [dict(prediction) for prediction in predictions]

    facts_by_key = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        facts = item.get("expected_facts", item.get("factual_facts"))
        if isinstance(facts, list):
            facts_by_key[prediction_key(item)] = facts

    output = []
    for prediction in predictions:
        item = dict(prediction)
        facts = facts_by_key.get(prediction_key(prediction))
        if facts is not None and "expected_facts" not in item:
            item["expected_facts"] = facts
        output.append(item)
    return output


def _prompt_fingerprint() -> str:
    candidates = (
        REPO_ROOT / "agents" / "prompts.py",
        REPO_ROOT / "agents" / "profiles.py",
        REPO_ROOT / "graph" / "evidence.py",
        REPO_ROOT / "tools" / "tools.py",
    )
    payload = {
        str(path.relative_to(REPO_ROOT)): fingerprint
        for path in candidates
        if (fingerprint := _file_fingerprint(path)) is not None
    }
    return _stable_fingerprint(payload)


def build_fingerprints(
    *,
    seed_path: Path,
    dataset_meta: dict,
    predictions: list[dict],
) -> dict[str, str | None]:
    safe_config = {
        name: os.getenv(name, "")
        for name in (
            "OLLAMA_MODEL",
            "OLLAMA_EMBEDDING_MODEL",
            "RAGAS_JUDGE",
            "RAGAS_JUDGE_MODEL",
            "RAGAS_MAX_WORKERS",
            "RAGAS_TIMEOUT_SECONDS",
            "RAGAS_MAX_RETRIES",
            "EVIDENCE_FACTS_LIMIT",
            "NOTE_FACTS_LIMIT",
        )
    }
    prediction_inputs = [
        {
            "id": item.get("id"),
            "question": item.get("question", ""),
            "ground_truth": item.get("ground_truth", ""),
            "retrieved_contexts": item.get("retrieved_contexts", []),
            "expected_facts": item.get("expected_facts", []),
        }
        for item in predictions
    ]
    index_contract = {
        key: dataset_meta.get(key)
        for key in (
            "dataset_id",
            "facts_count",
            "vector_docs_count",
            "index_fingerprint",
            "collection_name",
        )
    }
    base_url = os.getenv("OLLAMA_BASE_URL", "https://ollama.com")
    parsed_base_url = urlsplit(base_url)
    environment_contract = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "python": platform.python_version(),
        "ollama_endpoint": f"{parsed_base_url.scheme}://{parsed_base_url.hostname or ''}",
    }
    return {
        "seed_sha256": _file_fingerprint(seed_path),
        "facts_contract_sha256": _file_fingerprint(_resolve_facts_contract_path()),
        "predictions_sha256": _stable_fingerprint(prediction_inputs),
        "dataset_index_sha256": _stable_fingerprint(index_contract),
        "prompt_sha256": _prompt_fingerprint(),
        "config_sha256": _stable_fingerprint(safe_config),
        "environment_sha256": _stable_fingerprint(environment_contract),
        "git_revision": _git_revision(),
    }


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _latency_contamination_messages(
    predictions: list[dict],
    scores: list[dict],
    eval_error: str,
) -> list[str]:
    messages = [str(eval_error or "")]
    for prediction in predictions:
        messages.append(str(prediction.get("answer", "") or ""))
        messages.extend(str(error or "") for error in prediction.get("errors", []) or [])
    for score in scores:
        messages.extend(
            str(value or "")
            for key, value in score.items()
            if key == "score_error" or key.endswith("_error")
        )
    return [message for message in messages if _LATENCY_CONTAMINATION_RE.search(message)]


def build_latency_contract(
    *,
    predictions: list[dict],
    scores: list[dict],
    eval_error: str,
    judge_duration_ms: int,
    judge_metrics_n: int = 0,
    clean_environment_attested: bool = False,
) -> dict[str, Any]:
    sample_ids = [item.get("id") for item in predictions]
    matches_apec_cohort = sample_ids == list(range(181, 251))
    durations = [
        float(item["runtime"])
        for item in predictions
        if isinstance(item.get("runtime"), Real)
        and not isinstance(item.get("runtime"), bool)
        and float(item["runtime"]) >= 0
    ]
    tokens = [
        float(item.get("tokens", 0))
        for item in predictions
        if isinstance(item.get("tokens"), Real) and not isinstance(item.get("tokens"), bool)
    ]
    breakdowns = [
        item.get("latency_breakdown", {})
        for item in predictions
        if isinstance(item.get("latency_breakdown"), dict)
    ]

    def breakdown_values(field: str) -> list[float]:
        return [
            float(item[field])
            for item in breakdowns
            if isinstance(item.get(field), Real)
            and not isinstance(item.get(field), bool)
            and float(item[field]) >= 0
        ]

    retrieval_local = breakdown_values("retrieval_local_ms")
    model_generation = breakdown_values("model_generation_ms")
    invalid_reasons = []
    contamination = _latency_contamination_messages(predictions, scores, eval_error)
    if contamination:
        invalid_reasons.append("ollama_limit_or_quota_backoff_detected")
    if not clean_environment_attested:
        invalid_reasons.append("clean_environment_not_attested")
    if len(durations) != len(predictions):
        invalid_reasons.append("missing_prediction_latency")
    if any(item.get("errors") for item in predictions):
        invalid_reasons.append("prediction_errors_present")
    if not predictions:
        invalid_reasons.append("no_predictions")

    valid = not invalid_reasons
    return {
        "valid": valid,
        "baseline_eligible": (
            valid
            and matches_apec_cohort
            and len(retrieval_local) == len(predictions)
            and len(model_generation) == len(predictions)
        ),
        "benchmark_cohort": {
            "name": "apec_q181_250",
            "expected_samples_n": 70,
            "matches": matches_apec_cohort,
        },
        "invalid_reasons": invalid_reasons,
        "contamination_samples": contamination[:3],
        "warmup": "not_included_in_report_contract",
        "clean_environment_attested": bool(clean_environment_attested),
        "prediction": {
            "duration_source": "prediction.runtime_ms",
            "samples_n": len(durations),
            "p50_ms": round(_percentile(durations, 0.50), 3) if durations else None,
            "p95_ms": round(_percentile(durations, 0.95), 3) if durations else None,
            "mean_ms": round(sum(durations) / len(durations), 3) if durations else None,
            "mean_tokens": round(sum(tokens) / len(tokens), 3) if tokens else None,
            "breakdown": {
                "measurement": "trace_model_stage_wall_union",
                "samples_n": min(len(retrieval_local), len(model_generation)),
                "complete": (
                    len(retrieval_local) == len(predictions)
                    and len(model_generation) == len(predictions)
                ),
                "retrieval_local_mean_ms": (
                    round(sum(retrieval_local) / len(retrieval_local), 3)
                    if retrieval_local
                    else None
                ),
                "model_generation_mean_ms": (
                    round(sum(model_generation) / len(model_generation), 3)
                    if model_generation
                    else None
                ),
            },
        },
        "judge": {
            "duration_ms": int(max(0, judge_duration_ms)),
            "metrics_requested_n": int(max(0, judge_metrics_n)),
            "scope": "ragas_evaluation_wall_including_framework_overhead",
            "included_in_prediction_latency": False,
        },
    }


def _metric_failures(predictions: list[dict], scores: list[dict]) -> list[str]:
    scores_by_key = _scores_by_key(scores, predictions)
    failures = []
    for prediction in predictions:
        key = prediction_key(prediction)
        score = scores_by_key.get(key, {})
        if not score:
            failures.append(f"{key}:missing_score")
            continue
        for metric_name in METRIC_NAMES:
            if not _score_has_metric(score, metric_name):
                detail = score.get(f"{metric_name}_error", "missing")
                failures.append(f"{key}:{metric_name}:{detail}")
    return failures


def _pending_metric_count(predictions: list[dict], scores: list[dict]) -> int:
    scores_by_key = _scores_by_key(scores, predictions)
    return sum(
        not _score_has_metric(scores_by_key.get(prediction_key(prediction), {}), metric_name)
        for prediction in predictions
        for metric_name in METRIC_NAMES
    )


def apply_evaluation_contract(
    report: dict,
    *,
    seed_path: Path,
    predictions: list[dict],
    scores: list[dict],
    eval_error: str,
    judge_duration_ms: int,
    judge_metrics_n: int = 0,
    clean_environment_attested: bool | None = None,
    source_prediction_complete: bool | None = None,
    source_prediction_error: str = "",
) -> dict:
    output = dict(report)
    metadata = dict(output.get("metadata", {}) or {})
    summary = dict(output.get("summary", {}) or {})
    dataset_meta = metadata.get("dataset", {}) if isinstance(metadata.get("dataset"), dict) else {}
    latency_environment = metadata.get("latency_environment", {})
    if clean_environment_attested is None:
        clean_environment_attested = bool(
            isinstance(latency_environment, dict) and latency_environment.get("clean") is True
        )
    gate_predictions = _official_gate_predictions(
        predictions,
        dataset_meta=dataset_meta,
    )
    gate = evaluate_factual_recall(gate_predictions, threshold=FACTUAL_RECALL_THRESHOLD)
    source_incomplete = source_prediction_complete is False
    effective_eval_error = str(eval_error or "").strip()
    if source_incomplete and source_prediction_error:
        effective_eval_error = "; ".join(
            value
            for value in (effective_eval_error, str(source_prediction_error).strip())
            if value
        )
    latency = build_latency_contract(
        predictions=predictions,
        scores=scores,
        eval_error=effective_eval_error,
        judge_duration_ms=judge_duration_ms,
        judge_metrics_n=judge_metrics_n,
        clean_environment_attested=clean_environment_attested,
    )

    output["schema_version"] = EVAL_REPORT_SCHEMA_VERSION
    metadata.update(
        {
            "schema_version": EVAL_REPORT_SCHEMA_VERSION,
            "run_complete": (
                bool(metadata.get("run_complete", not effective_eval_error))
                and not bool(effective_eval_error)
                and not source_incomplete
            ),
            "run_status": "incomplete" if (effective_eval_error or source_incomplete) else (
                "quality_gate_failed" if gate["status"] == "fail" else "complete"
            ),
            "fingerprints": build_fingerprints(
                seed_path=seed_path,
                dataset_meta=dataset_meta,
                predictions=predictions,
            ),
            "quality_policy": {
                "hard_gate": "deterministic_factual_recall >= 0.95",
                "ragas": "diagnostic_only",
                "ragas_mean_delta_gate": None,
            },
            "latency": latency,
            # v1-compatible scalar fields mirror the structured v2 contract so
            # old readers cannot accidentally accept an unattested baseline.
            "latency_valid": latency["valid"],
            "latency_invalid_reasons": latency["invalid_reasons"],
            "latency_scope": "prediction_only_excludes_ragas_judge",
        }
    )
    if source_incomplete:
        metadata["source_prediction_complete"] = False
        metadata["eval_error"] = effective_eval_error or "source_prediction_incomplete"
    if clean_environment_attested:
        metadata["latency_environment"] = {
            "clean": True,
            "attestation": (
                latency_environment.get("attestation", "source_report")
                if isinstance(latency_environment, dict)
                else "source_report"
            ),
        }
    summary.update(
        {
            "quality_gate": {key: value for key, value in gate.items() if key != "rows"},
            "deterministic_factual_recall_rows": gate["rows"],
            "latency": latency,
        }
    )
    output["metadata"] = metadata
    output["summary"] = summary
    return output


def atomic_write_report_pair(report: dict, output_path: str | Path) -> tuple[Path, Path]:
    """Atomically replace JSON and CSV checkpoints in their destination directory."""
    json_path = Path(output_path)
    csv_path = json_path.with_suffix(".csv")
    json_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".{json_path.stem}-", dir=json_path.parent))
    temporary_json = temporary_dir / json_path.name
    temporary_csv = temporary_json.with_suffix(".csv")
    try:
        write_json_report(report, temporary_json)
        write_csv_report(report, temporary_json)
        # JSON is the authoritative checkpoint.  Publish the derived CSV first
        # so a visible new JSON never points at a stale companion artifact.
        os.replace(temporary_csv, csv_path)
        os.replace(temporary_json, json_path)
    finally:
        for candidate in (temporary_json, temporary_csv):
            candidate.unlink(missing_ok=True)
        try:
            temporary_dir.rmdir()
        except OSError:
            pass
    return json_path, csv_path


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Score an existing answer/retrieved-context report with RAGAs."
    )
    parser.add_argument(
        "--predictions-file",
        default=DEFAULT_PREDICTIONS_FILE,
        help="Input JSON report generated by dataset_batch_result.py.",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Output JSON path. Defaults to overwriting --predictions-file.",
    )
    parser.add_argument("--batch-size", type=int, default=None, help="Optional RAGAs evaluation batch size.")
    parser.add_argument(
        "--metric-sleep-seconds",
        type=float,
        default=_env_float("RAGAS_METRIC_SLEEP_SECONDS", DEFAULT_METRIC_SLEEP_SECONDS),
        help="Seconds to sleep between metric evaluations for the same prediction.",
    )
    parser.add_argument(
        "--sample-sleep-seconds",
        type=float,
        default=_env_float("RAGAS_SAMPLE_SLEEP_SECONDS", DEFAULT_SAMPLE_SLEEP_SECONDS),
        help="Seconds to sleep between predictions.",
    )
    parser.add_argument("--force", action="store_true", help="Re-score all predictions, ignoring existing scores.")
    parser.add_argument(
        "--attest-clean-latency-run",
        action="store_true",
        help=(
            "Attest that the input prediction run was produced without Ollama session/usage/quota "
            "limits or quota backoff. Reports are latency-invalid without this attestation."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.predictions_file)
    output_path = Path(args.output or args.predictions_file)

    report = load_report(input_path)
    source_prediction_complete, source_prediction_error = _source_prediction_status(report)
    if args.attest_clean_latency_run:
        report.setdefault("metadata", {})["latency_environment"] = {
            "clean": True,
            "attestation": "cli:--attest-clean-latency-run",
        }
    source_latency_environment = report.get("metadata", {}).get("latency_environment", {})
    clean_latency_attested = bool(
        isinstance(source_latency_environment, dict)
        and source_latency_environment.get("clean") is True
    )
    predictions = predictions_from_report(report)
    if not predictions:
        raise SystemExit(f"No predictions found in: {input_path}")

    seed_path = _resolve_seed_path(_report_seed_file(report), input_path=input_path)
    predictions = _attach_explicit_seed_facts(predictions, seed_path=seed_path)

    existing_output_report = load_report(output_path) if output_path.exists() and not args.force else {}
    existing_scores = [] if args.force else (existing_output_report.get("scores", []) or report.get("scores", []) or [])
    judge_metrics_n = _pending_metric_count(predictions, existing_scores)
    eval_error = ""
    exit_code = 0
    judge_started = time.monotonic()

    full, limit = _report_selection(report)

    def write_score_checkpoint(current_scores: list[dict], current_error: str = "") -> None:
        checkpoint_report = build_report(
            seed_file=_report_seed_file(report),
            dataset_meta=_report_dataset_meta(report),
            predictions=predictions,
            scores=current_scores,
            full=full,
            limit=limit,
            skip_eval=False,
            run_complete=False,
            eval_error=current_error,
        )
        checkpoint_report = apply_evaluation_contract(
            checkpoint_report,
            seed_path=seed_path,
            predictions=predictions,
            scores=current_scores,
            eval_error=current_error or "checkpoint_incomplete",
            judge_duration_ms=round((time.monotonic() - judge_started) * 1000),
            judge_metrics_n=judge_metrics_n,
            clean_environment_attested=clean_latency_attested,
            source_prediction_complete=source_prediction_complete,
            source_prediction_error=source_prediction_error,
        )
        atomic_write_report_pair(checkpoint_report, output_path)

    try:
        scores = evaluate_with_ragas(
            predictions,
            batch_size=args.batch_size,
            existing_scores=existing_scores,
            metric_sleep_seconds=args.metric_sleep_seconds,
            sample_sleep_seconds=args.sample_sleep_seconds,
            on_checkpoint=write_score_checkpoint,
        )
    except SessionLimitError as exc:
        eval_error = f"session_limit ({type(exc).__name__}): {exc}"
        print(eval_error, file=sys.stderr)
        scores = exc.scores
        exit_code = 1
    except Exception as exc:
        eval_error = f"ragas_eval_error ({type(exc).__name__}): {exc}"
        print(eval_error, file=sys.stderr)
        scores = merge_scores_for_predictions(predictions, existing_scores=existing_scores, new_scores=[])
        exit_code = 1

    metric_failures = _metric_failures(predictions, scores)
    if metric_failures and not eval_error:
        preview = "; ".join(metric_failures[:5])
        suffix = f"; ... {len(metric_failures) - 5} more" if len(metric_failures) > 5 else ""
        eval_error = f"ragas_metrics_incomplete: {preview}{suffix}"
        exit_code = 1

    judge_duration_ms = round((time.monotonic() - judge_started) * 1000)

    scored_report = build_report(
        seed_file=_report_seed_file(report),
        dataset_meta=_report_dataset_meta(report),
        predictions=predictions,
        scores=scores,
        full=full,
        limit=limit,
        skip_eval=False,
        run_complete=not bool(eval_error),
        eval_error=eval_error,
    )
    scored_report = apply_evaluation_contract(
        scored_report,
        seed_path=seed_path,
        predictions=predictions,
        scores=scores,
        eval_error=eval_error,
        judge_duration_ms=judge_duration_ms,
        judge_metrics_n=judge_metrics_n,
        clean_environment_attested=clean_latency_attested,
        source_prediction_complete=source_prediction_complete,
        source_prediction_error=source_prediction_error,
    )
    gate_status = scored_report["summary"]["quality_gate"]["status"]
    if gate_status == "fail":
        exit_code = 1
    json_path, csv_path = atomic_write_report_pair(scored_report, output_path)

    print(f"Wrote JSON report: {json_path}")
    print(f"Wrote CSV report: {csv_path}")
    print(json.dumps(scored_report["summary"], ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
