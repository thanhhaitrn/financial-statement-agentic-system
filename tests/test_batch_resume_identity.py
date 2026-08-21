"""Run-identity and resume safety contracts for dataset batch evaluation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import dataset_batch_runner as batch_runner
from dataset_batch_result import (
    SeedValidationError,
    build_report,
    validate_resume_report,
)


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps([{"id": 1, "question": "q1"}]), encoding="utf-8")
    return path


def _records() -> list[dict]:
    return [
        {"id": 11, "question": "q11", "ground_truth": "a11"},
        {"id": 12, "question": "q12", "ground_truth": "a12"},
    ]


def _dataset_meta(*, index_generation: str = "index-v1") -> dict:
    return {
        "dataset_id": "apec",
        "company": "APEC",
        "ingestion_version": "v2",
        "vector_collection_name": "financial_statement__apec",
        "dataset_generation": "dataset-v1",
        "kb_generation": "kb-v1",
        "index_generation": index_generation,
    }


def _complete_predictions() -> list[dict]:
    return [
        {
            "id": record["id"],
            "question": record["question"],
            "ground_truth": record["ground_truth"],
            "answer": "answer",
            "errors": [],
        }
        for record in _records()
    ]


def _report(seed: Path, **overrides) -> dict:
    kwargs = {
        "seed_file": str(seed),
        "dataset_meta": _dataset_meta(),
        "predictions": _complete_predictions(),
        "scores": [],
        "full": False,
        "limit": 2,
        "offset": 10,
        "selected_records": _records(),
        "skip_eval": True,
    }
    kwargs.update(overrides)
    return build_report(**kwargs)


def test_report_identity_contains_exact_selection_and_all_fingerprints(tmp_path: Path):
    report = _report(_seed(tmp_path))
    metadata = report["metadata"]
    identity = metadata["run_identity"]

    assert identity["selection"] == {
        "full": False,
        "offset": 10,
        "limit": 2,
        "selected_count": 2,
        "selected_query_ids": [11, 12],
        "selected_sample_keys": identity["selection"]["selected_sample_keys"],
    }
    assert all(identity["selection"]["selected_sample_keys"])
    assert identity["dataset"]["dataset_generation"] == "dataset-v1"
    assert identity["dataset"]["kb_generation"] == "kb-v1"
    assert identity["dataset"]["index_generation"] == "index-v1"
    assert set(identity["fingerprints"]) == {
        "seed",
        "selection",
        "query",
        "dataset",
        "index",
        "embedding",
        "prompt",
        "model",
        "config",
    }
    assert metadata["run_fingerprint"] == identity["run_fingerprint"]


def test_resume_accepts_exact_identity_and_rejects_selection_or_index_change(tmp_path: Path):
    seed = _seed(tmp_path)
    report = _report(seed)

    validate_resume_report(
        report,
        seed_file=seed,
        dataset_id="apec",
        selected_records=_records(),
        full=False,
        limit=2,
        offset=10,
        current_dataset_meta=_dataset_meta(),
    )

    with pytest.raises(SeedValidationError, match="selection"):
        validate_resume_report(
            report,
            seed_file=seed,
            dataset_id="apec",
            selected_records=list(reversed(_records())),
            full=False,
            limit=2,
            offset=10,
            current_dataset_meta=_dataset_meta(),
        )
    with pytest.raises(SeedValidationError, match="dataset/index generation"):
        validate_resume_report(
            report,
            seed_file=seed,
            dataset_id="apec",
            selected_records=_records(),
            full=False,
            limit=2,
            offset=10,
            current_dataset_meta=_dataset_meta(index_generation="index-v2"),
        )


def test_resume_rejects_missing_identity_and_model_config_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    seed = _seed(tmp_path)
    report = _report(seed)
    legacy = {"metadata": {"dataset": _dataset_meta()}, "predictions": []}
    with pytest.raises(SeedValidationError, match="no complete run_identity"):
        validate_resume_report(legacy, seed_file=seed, dataset_id="apec")

    monkeypatch.setenv("OLLAMA_MODEL", "different-model")
    with pytest.raises(SeedValidationError, match="model"):
        validate_resume_report(
            report,
            seed_file=seed,
            dataset_id="apec",
            selected_records=_records(),
            full=False,
            limit=2,
            offset=10,
            current_dataset_meta=_dataset_meta(),
        )


def test_resume_keeps_source_incomplete_and_provider_contamination(tmp_path: Path):
    seed = _seed(tmp_path)
    contaminated = _report(
        seed,
        predictions=[
            {
                "id": 11,
                "question": "q11",
                "ground_truth": "a11",
                "answer": "",
                "errors": ["provider quota status code: 429"],
            }
        ],
        run_complete=False,
        eval_error="session_limit",
    )
    resumed = _report(
        seed,
        source_report=contaminated,
        resume_repaired=True,
        run_complete=True,
    )

    assert resumed["metadata"]["run_status"] == "incomplete"
    assert resumed["metadata"]["latency_valid"] is False
    assert any(
        "429" in reason
        for reason in resumed["metadata"]["latency_invalid_reasons"]
    )
    assert resumed["metadata"]["resume_source_status"]["repaired"] is True

    interrupted = _report(seed, run_complete=False, eval_error="interrupted")
    no_op_resume = _report(
        seed,
        source_report=interrupted,
        resume_repaired=False,
        run_complete=True,
    )
    assert no_op_resume["metadata"]["run_complete"] is False
    assert no_op_resume["metadata"]["eval_error"] == "interrupted"


def test_generic_batch_identity_keeps_query_ids_and_generation(monkeypatch):
    query_records = [
        {"id": "q-2", "query": "Revenue?", "reference": "100"},
        {"id": "q-3", "query": "Profit?", "reference": "10"},
    ]
    results = [
        {
            "dataset_id": "apec",
            "dataset_identity": _dataset_meta(),
            "runs": [],
        }
    ]
    identity = batch_runner.build_batch_run_identity(
        query_records=query_records,
        results=results,
        selected_dataset_ids=["apec"],
        debug_trace=False,
        include_trace=False,
    )
    assert identity["selection"]["selected_query_ids"] == ["q-2", "q-3"]
    assert identity["datasets"][0]["index_generation"] == "index-v1"

    monkeypatch.setenv("OLLAMA_MODEL", "identity-change")
    changed = batch_runner.build_batch_run_identity(
        query_records=query_records,
        results=results,
        selected_dataset_ids=["apec"],
        debug_trace=False,
        include_trace=False,
    )
    assert changed["fingerprints"]["model"] != identity["fingerprints"]["model"]
    assert changed["run_fingerprint"] != identity["run_fingerprint"]
