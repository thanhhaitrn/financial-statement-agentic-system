"""Analyze evaluation reports without treating stochastic RAGAS means as gates.

RAGAS metrics are grouped by factual/analytical question and remain useful as
diagnostics.  The only quality gate is deterministic factual recall, evaluated
against normalized entity/metric/period/value/unit/reference facts at >= 0.95.

Usage:
  python analyze_batch_metrics.py ragas_runs/apec_q181_210_v3.json [more.json ...]
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
from pathlib import Path
from typing import Any

from eval_retrieval_recall import (
    DEFAULT_FACTS_CONTRACT,
    FACTUAL_RECALL_THRESHOLD,
    evaluate_factual_recall,
    gold_numbers,
    is_analytical,
    load_factual_contract_records,
    matched_official_gate_records,
)


METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_recall")
REPO_ROOT = Path(__file__).resolve().parent


def _official_gate_predictions(
    predictions: list[dict[str, Any]],
    report: dict[str, Any],
    *,
    facts_contract: str | Path,
) -> list[dict[str, Any]]:
    contract_path = Path(facts_contract)
    if not contract_path.is_absolute():
        contract_path = REPO_ROOT / contract_path
    contract_metadata, contract_records = load_factual_contract_records(contract_path)
    report_metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    dataset_meta = report_metadata.get("dataset", {}) if isinstance(report_metadata, dict) else {}
    report_dataset_id = str(
        (dataset_meta.get("dataset_id", "") if isinstance(dataset_meta, dict) else "")
        or (report_metadata.get("dataset_id", "") if isinstance(report_metadata, dict) else "")
        or ""
    ).strip()
    contract_dataset_id = str(contract_metadata.get("dataset_id", "") or "").strip()
    if report_dataset_id and contract_dataset_id and report_dataset_id != contract_dataset_id:
        return []
    return matched_official_gate_records(contract_records, predictions)


def _computable_from(target: str, source_nums: set[str]) -> bool:
    """Compatibility helper for reports created before structured facts.

    Derived values are diagnostic only.  The hard gate never excuses a missing
    expected fact merely because two unrelated retrieved values can form it.
    """
    try:
        target_value = int(target)
        values = [int(value) for value in source_nums]
    except (TypeError, ValueError):
        return False
    for index, first in enumerate(values):
        for second in values[index + 1:]:
            if target_value in {first + second, abs(first - second)}:
                return True
    return False


def factual_recall(ground_truth: str, contexts: list[str]) -> float | None:
    """Legacy API backed by the same strict deterministic matcher as the gate."""
    gate = evaluate_factual_recall(
        [
            {
                "id": "legacy",
                "question": "Tra cứu số liệu",
                "ground_truth": ground_truth,
                "retrieved_contexts": contexts,
            }
        ]
    )
    return gate["recall"]


def _finite_numbers(rows: list[dict], metric: str) -> list[float]:
    values = []
    for row in rows:
        value = row.get(metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            values.append(float(value))
    return values


def _metric_diagnostic(rows: list[dict], metric: str) -> dict[str, Any]:
    values = _finite_numbers(rows, metric)
    return {
        "n": len(values),
        "missing_n": len(rows) - len(values),
        "mean": st.mean(values) if values else None,
        "median": st.median(values) if values else None,
        "stdev": st.stdev(values) if len(values) > 1 else 0.0 if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def _latency_contract(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary", {}) if isinstance(report, dict) else {}
    metadata = report.get("metadata", {}) if isinstance(report, dict) else {}
    value = summary.get("latency") or metadata.get("latency") or {}
    return value if isinstance(value, dict) else {}


def analyze(
    path: str | Path,
    *,
    threshold: float = FACTUAL_RECALL_THRESHOLD,
    facts_contract: str | Path = DEFAULT_FACTS_CONTRACT,
) -> dict[str, Any]:
    report_path = Path(path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    predictions = [item for item in report.get("predictions", []) or [] if isinstance(item, dict)]
    scores = {
        str(item.get("id")): item
        for item in report.get("scores", []) or []
        if isinstance(item, dict)
    }

    groups: dict[str, list[dict]] = {"factual": [], "analytical": []}
    for prediction in predictions:
        bucket = "analytical" if is_analytical(prediction.get("question", "")) else "factual"
        score = scores.get(str(prediction.get("id")))
        if score:
            groups[bucket].append({metric: score.get(metric) for metric in METRICS})

    gate_records = _official_gate_predictions(
        predictions,
        report,
        facts_contract=facts_contract,
    )
    gate = evaluate_factual_recall(gate_records, threshold=threshold)
    rows_by_bucket = {
        bucket: [
            row for row in gate["rows"]
            if row["bucket"] == bucket and row["recall"] is not None
        ]
        for bucket in ("factual", "analytical")
    }

    print("=" * 92)
    print(report_path)
    print(
        f"{'bucket':<12}{'ragas_n':>9}  {'faith':>7}{'ans_rel':>9}"
        f"{'ctx_prec':>10}{'ctx_rec':>9}{'fact_rec':>10}"
    )
    for bucket in ("factual", "analytical"):
        ragas_rows = groups[bucket]
        factual_rows = rows_by_bucket[bucket]

        def mean(metric: str) -> float:
            values = _finite_numbers(ragas_rows, metric)
            return st.mean(values) if values else float("nan")

        fact_values = [row["recall"] for row in factual_rows]
        fact_mean = st.mean(fact_values) if fact_values else float("nan")
        print(
            f"{bucket:<12}{len(ragas_rows):>9}  {mean('faithfulness'):>7.3f}"
            f"{mean('answer_relevancy'):>9.3f}{mean('context_precision'):>10.3f}"
            f"{mean('context_recall'):>9.3f}{fact_mean:>10.3f}"
        )

    print(
        "HARD GATE deterministic_factual_recall "
        f"{gate['recall'] if gate['recall'] is not None else 'n/a'} >= {threshold:.2f}: "
        f"{gate['status'].upper()} ({gate['matched_facts_n']}/{gate['expected_facts_n']} facts)"
    )
    print("RAGAS metrics: diagnostic only; no mean-delta pass/fail threshold is applied.")

    misses = [
        row for row in gate["rows"]
        if row["bucket"] == "factual" and row["missing_facts"]
    ]
    if misses:
        print("Missing deterministic factual evidence:")
        for row in misses:
            print(
                f"    id {row['id']} recall={row['recall']:.3f} "
                f"source={row['fact_source']} missing={json.dumps(row['missing_facts'], ensure_ascii=False)}"
            )

    latency = _latency_contract(report)
    if latency:
        print(
            "Latency: "
            f"valid={latency.get('valid')} "
            f"prediction_p50_ms={latency.get('prediction', {}).get('p50_ms')} "
            f"prediction_p95_ms={latency.get('prediction', {}).get('p95_ms')} "
            f"judge_ms={latency.get('judge', {}).get('duration_ms')}"
        )
        if latency.get("invalid_reasons"):
            print("Latency invalid reasons: " + "; ".join(latency["invalid_reasons"]))

    return {
        "path": str(report_path),
        "quality_gate": {key: value for key, value in gate.items() if key != "rows"},
        "ragas": {
            bucket: {
                metric: _metric_diagnostic(groups[bucket], metric)
                for metric in METRICS
            }
            for bucket in ("factual", "analytical")
        },
        "latency": latency,
        "fingerprints": report.get("metadata", {}).get("fingerprints", {}),
        "run_identity": f"{report_path.resolve()}::{report.get('metadata', {}).get('generated_at', '')}",
        "scores": list(scores.values()),
    }


def summarize_repeated_judge_variance(results: list[dict[str, Any]]) -> dict[str, Any]:
    values_by_sample: dict[tuple[str, str], list[float]] = {}
    for result in results:
        for score in result.get("scores", []):
            sample = str(score.get("sample_fingerprint") or score.get("id") or score.get("question", ""))
            for metric in METRICS:
                value = score.get(metric)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    values_by_sample.setdefault((sample, metric), []).append(float(value))

    output = {}
    for metric in METRICS:
        ranges = [
            max(values) - min(values)
            for (sample, name), values in values_by_sample.items()
            if name == metric and len(values) > 1
        ]
        output[metric] = {
            "repeated_samples_n": len(ranges),
            "mean_range": st.mean(ranges) if ranges else None,
            "max_range": max(ranges) if ranges else None,
        }
    return output


def summarize_clean_latency_baseline(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid_by_identity = {
        result.get("run_identity", result.get("path", "")): result
        for result in results
        if result.get("latency", {}).get("valid") is True
        and result.get("latency", {}).get("baseline_eligible") is True
    }
    valid = list(valid_by_identity.values())
    # The prediction payload is a run output, not an environment/config input.
    # Retrieval order and generated contexts may legitimately differ between
    # otherwise comparable repetitions, so it must not make the clean baseline
    # reject those runs.  Seed/index/prompt/config/environment/git fingerprints
    # remain part of the equality contract.
    fingerprint_keys = {
        json.dumps(
            {
                key: value
                for key, value in (result.get("fingerprints", {}) or {}).items()
                if key != "predictions_sha256"
            },
            sort_keys=True,
            default=str,
        )
        for result in valid
    }
    if len(valid) < 3:
        return {
            "status": "not_established",
            "reason": "fewer_than_3_clean_complete_runs",
            "clean_runs_n": len(valid),
        }
    if len(fingerprint_keys) != 1:
        return {
            "status": "not_established",
            "reason": "fingerprints_differ",
            "clean_runs_n": len(valid),
        }

    def median_field(section: str, field: str) -> float | None:
        values = [
            result["latency"].get(section, {}).get(field)
            for result in valid
            if isinstance(result["latency"].get(section, {}).get(field), (int, float))
        ]
        return st.median(values) if len(values) == len(valid) else None

    return {
        "status": "established",
        "clean_runs_n": len(valid),
        "prediction_p50_ms": median_field("prediction", "p50_ms"),
        "prediction_p95_ms": median_field("prediction", "p95_ms"),
        "mean_tokens": median_field("prediction", "mean_tokens"),
        "judge_latency_excluded": True,
    }


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="*", help="Evaluation report JSON files")
    parser.add_argument(
        "--threshold",
        type=float,
        default=FACTUAL_RECALL_THRESHOLD,
        help="Hard-gate threshold; may be raised but never set below 0.95.",
    )
    parser.add_argument(
        "--facts-contract",
        default=DEFAULT_FACTS_CONTRACT,
        help="Authoritative reviewed six-field factual-recall contract.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not FACTUAL_RECALL_THRESHOLD <= args.threshold <= 1:
        raise SystemExit("--threshold must be between 0.95 and 1")
    paths = args.paths or ["ragas_runs/apec_q181_210_v3.json", "ragas_runs/apec_q211_250.json"]
    try:
        results = [
            analyze(
                path,
                threshold=args.threshold,
                facts_contract=args.facts_contract,
            )
            for path in paths
        ]
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    variance = summarize_repeated_judge_variance(results)
    if len(results) > 1:
        print("Repeated-judge variance (diagnostic only): " + json.dumps(variance, ensure_ascii=False))
    baseline = summarize_clean_latency_baseline(results)
    print("Clean latency baseline: " + json.dumps(baseline, ensure_ascii=False))
    return 0 if all(result["quality_gate"]["status"] == "pass" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
