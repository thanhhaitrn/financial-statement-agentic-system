"""Run-identity and resume safety contracts for dataset batch evaluation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import dataset_batch_runner as batch_runner
import dataset_batch_result as batch_result
from dataset_batch_result import (
    SeedValidationError,
    build_report,
    select_records,
    validate_resume_report,
)
from evaluation.contracts import stable_json_fingerprint
from evaluation import run_identity as run_identity_module
from evaluation.run_identity import build_run_identity


def _seed(tmp_path: Path) -> Path:
    path = tmp_path / "seed.json"
    path.write_text(json.dumps([{"id": 1, "question": "q1"}]), encoding="utf-8")
    return path


def test_output_formatter_participates_in_runtime_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(run_identity_module, "ROOT_DIR", tmp_path)
    before = run_identity_module.build_runtime_fingerprints()["config"]

    (tmp_path / "output_formatter.py").write_text(
        "FORMAT_VERSION = 1\n",
        encoding="utf-8",
    )
    after = run_identity_module.build_runtime_fingerprints()["config"]

    assert run_identity_module.RUN_IDENTITY_VERSION == 4
    assert "output_formatter.py" in run_identity_module._CONFIG_IDENTITY_FILES
    assert before != after


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
        "seed_population_count": 1,
        "is_full_seed_selection": False,
        "selected_query_ids": [11, 12],
        "selected_sample_keys": identity["selection"]["selected_sample_keys"],
    }
    assert all(identity["selection"]["selected_sample_keys"])
    assert identity["dataset"]["dataset_generation"] == "dataset-v1"
    assert identity["dataset"]["kb_generation"] == "kb-v1"
    assert identity["dataset"]["index_generation"] == "index-v1"
    assert set(identity["fingerprints"]) == {
        "code",
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
    assert len(identity["fingerprints"]["code"]) == 64
    assert metadata["code_sha256"] == identity["fingerprints"]["code"]
    assert metadata["code_fingerprint_kind"] == (
        "git-head-plus-worktree-diff-v1"
    )
    assert metadata["worktree_dirty"] == identity["code_provenance"]["worktree_dirty"]
    assert metadata["run_fingerprint"] == identity["run_fingerprint"]


def test_run_identity_changes_between_uncommitted_behavioral_states_but_not_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess_args = {
        "cwd": repository,
        "check": True,
    }
    subprocess.run(["git", "init", "-q"], **subprocess_args)
    baseline = repository / "baseline.py"
    baseline.write_text("ENABLED = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "baseline.py"], **subprocess_args)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=AgentFinX Test",
            "-c",
            "user.email=agentfinx-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        **subprocess_args,
    )
    monkeypatch.setattr(run_identity_module, "ROOT_DIR", repository)

    seed = _seed(tmp_path)
    identity_kwargs = {
        "seed_file": seed,
        "dataset_meta": _dataset_meta(),
        "selected_records": _records(),
        "full": False,
        "limit": 2,
        "offset": 10,
        "debug_trace": False,
        "skip_eval": True,
    }
    baseline_identity = build_run_identity(**identity_kwargs)

    behavior = repository / "new_behavior.py"
    behavior.write_text("THRESHOLD = 0.96\n", encoding="utf-8")
    first_behavior = build_run_identity(**identity_kwargs)
    behavior.write_text("THRESHOLD = 0.97\n", encoding="utf-8")
    second_behavior = build_run_identity(**identity_kwargs)

    assert (
        baseline_identity["fingerprints"]["code"]
        != first_behavior["fingerprints"]["code"]
    )
    assert (
        first_behavior["fingerprints"]["code"]
        != second_behavior["fingerprints"]["code"]
    )
    assert first_behavior["run_fingerprint"] != second_behavior["run_fingerprint"]

    runtime_dir = repository / "ragas_runs"
    runtime_dir.mkdir()
    (runtime_dir / "checkpoint.json").write_text(
        '{"run_complete": false}',
        encoding="utf-8",
    )
    after_checkpoint = build_run_identity(**identity_kwargs)

    assert (
        after_checkpoint["fingerprints"]["code"]
        == second_behavior["fingerprints"]["code"]
    )
    assert after_checkpoint["run_fingerprint"] == second_behavior["run_fingerprint"]


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


def test_full_selection_means_the_entire_seed_and_clean_means_one_shot(tmp_path: Path):
    seed = tmp_path / "seed-full.json"
    seed.write_text(
        json.dumps(
            [
                {"id": 11, "question": "q11"},
                {"id": 12, "question": "q12"},
            ]
        ),
        encoding="utf-8",
    )
    frozen = build_run_identity(
        seed_file=seed,
        dataset_meta=_dataset_meta(),
        selected_records=_records(),
        full=True,
        limit=None,
        offset=0,
        debug_trace=False,
        skip_eval=True,
    )

    clean = _report(
        seed,
        full=True,
        limit=None,
        offset=0,
        frozen_run_identity=frozen,
    )
    resumed = _report(
        seed,
        full=True,
        limit=None,
        offset=0,
        source_report=clean,
        resume_repaired=True,
    )
    incomplete_claim = _report(
        seed,
        full=True,
        limit=None,
        offset=0,
        selected_records=_records()[:1],
        predictions=_complete_predictions()[:1],
    )

    assert clean["metadata"]["selection_contract"]["is_full_seed_selection"] is True
    assert clean["metadata"]["run_complete"] is True
    assert clean["metadata"]["clean_full_run"] is True
    assert clean["metadata"]["execution_contract"]["one_shot"] is True

    assert resumed["metadata"]["run_complete"] is True
    assert resumed["metadata"]["clean_full_run"] is False
    assert resumed["metadata"]["execution_contract"]["resumed"] is True

    assert incomplete_claim["metadata"]["run_complete"] is False
    assert incomplete_claim["metadata"]["clean_full_run"] is False
    assert (
        incomplete_claim["metadata"]["eval_error"]
        == "full_selection_not_seed_complete"
    )

    with pytest.raises(ValueError, match="offset 0"):
        select_records(_records(), full=True, offset=1)


def test_frozen_clean_run_identity_rejects_code_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    seed = tmp_path / "seed-full.json"
    seed.write_text(
        json.dumps(
            [
                {"id": 11, "question": "q11"},
                {"id": 12, "question": "q12"},
            ]
        ),
        encoding="utf-8",
    )
    identity_kwargs = {
        "seed_file": seed,
        "dataset_meta": _dataset_meta(),
        "selected_records": _records(),
        "full": True,
        "limit": None,
        "offset": 0,
        "debug_trace": False,
        "skip_eval": True,
    }
    frozen = build_run_identity(**identity_kwargs)
    changed = json.loads(json.dumps(frozen))
    changed["fingerprints"]["code"] = "f" * 64
    unsigned_changed = dict(changed)
    unsigned_changed.pop("run_fingerprint")
    changed["run_fingerprint"] = stable_json_fingerprint(unsigned_changed)
    monkeypatch.setattr(batch_result, "build_run_identity", lambda **_kwargs: changed)

    report = _report(
        seed,
        full=True,
        limit=None,
        offset=0,
        frozen_run_identity=frozen,
    )
    metadata = report["metadata"]

    assert metadata["run_fingerprint"] == frozen["run_fingerprint"]
    assert metadata["run_identity"] == frozen
    assert metadata["run_identity_guard"] == {
        "enabled": True,
        "frozen_before_predictions": True,
        "verified_at_report_write": True,
        "stable": False,
        "violations": ["code"],
        "observed_run_fingerprint": changed["run_fingerprint"],
    }
    assert metadata["run_complete"] is False
    assert metadata["clean_full_run"] is False
    assert metadata["eval_error"] == "run_identity_changed: code"


def test_config_mutation_seen_at_checkpoint_remains_failed_at_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    seed = tmp_path / "seed-full.json"
    seed.write_text(
        json.dumps(
            [
                {"id": 11, "question": "q11"},
                {"id": 12, "question": "q12"},
            ]
        ),
        encoding="utf-8",
    )
    identity_kwargs = {
        "seed_file": seed,
        "dataset_meta": _dataset_meta(),
        "selected_records": _records(),
        "full": True,
        "limit": None,
        "offset": 0,
        "debug_trace": False,
        "skip_eval": True,
    }
    frozen = build_run_identity(**identity_kwargs)
    original_limit = os.environ.get("SCHEDULE_LIMIT")
    changed_limit = "987" if original_limit != "987" else "986"
    monkeypatch.setenv("SCHEDULE_LIMIT", changed_limit)

    checkpoint = _report(
        seed,
        full=True,
        limit=None,
        offset=0,
        run_complete=False,
        frozen_run_identity=frozen,
    )
    checkpoint_guard = checkpoint["metadata"]["run_identity_guard"]
    assert checkpoint_guard["violations"] == ["config"]
    assert checkpoint["metadata"]["run_fingerprint"] == frozen["run_fingerprint"]

    if original_limit is None:
        monkeypatch.delenv("SCHEDULE_LIMIT", raising=False)
    else:
        monkeypatch.setenv("SCHEDULE_LIMIT", original_limit)
    final = _report(
        seed,
        full=True,
        limit=None,
        offset=0,
        frozen_run_identity=frozen,
        identity_guard_violations=checkpoint_guard["violations"],
    )

    assert (
        final["metadata"]["run_identity_guard"]["observed_run_fingerprint"]
        == frozen["run_fingerprint"]
    )
    assert final["metadata"]["run_identity_guard"]["violations"] == ["config"]
    assert final["metadata"]["run_identity_guard"]["stable"] is False
    assert final["metadata"]["run_complete"] is False
    assert final["metadata"]["clean_full_run"] is False


def test_main_freezes_identity_before_first_prediction_and_stamps_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            [
                {
                    "id": 11,
                    "question": "q11",
                    "ground_truth": "a11",
                    "contexts": ["seed fact"],
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "predictions.json"
    dataset_meta = _dataset_meta()
    runtime = (
        SimpleNamespace(dataset_id="apec"),
        object(),
        object(),
        dataset_meta,
    )
    events: list[str] = []
    real_build_run_identity = batch_result.build_run_identity

    def fake_prepare(dataset_id: str):
        assert dataset_id == "apec"
        events.append("prepare")
        return runtime

    def tracked_build_run_identity(**kwargs):
        events.append("identity")
        return real_build_run_identity(**kwargs)

    def fake_run_predictions(**kwargs):
        assert kwargs["prepared_runtime"] is runtime
        # The first identity event is the freeze; no query may start before it.
        assert events[:2] == ["prepare", "identity"]
        events.append("prediction")
        prediction = {
            "id": 11,
            "question": "q11",
            "ground_truth": "a11",
            "answer": "a11",
            "retrieved_contexts": ["retrieved fact"],
            "errors": [],
        }
        kwargs["on_checkpoint"](dataset_meta, [prediction])
        return dataset_meta, [prediction]

    monkeypatch.setattr(batch_result, "prepare_dataset_runtime", fake_prepare)
    monkeypatch.setattr(
        batch_result,
        "build_run_identity",
        tracked_build_run_identity,
    )
    monkeypatch.setattr(batch_result, "run_predictions", fake_run_predictions)

    exit_code = batch_result.main(
        [
            "--dataset-id",
            "apec",
            "--seed-file",
            str(seed),
            "--output",
            str(output),
            "--full",
        ]
    )
    report = json.loads(output.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert events[:3] == ["prepare", "identity", "prediction"]
    assert report["metadata"]["run_identity_guard"]["enabled"] is True
    assert report["metadata"]["run_identity_guard"]["stable"] is True
    assert report["metadata"]["execution_contract"]["run_identity_frozen"] is True
    assert report["metadata"]["execution_contract"]["resume_repaired"] is False
    assert report["metadata"]["clean_full_run"] is True


def test_source_eval_provider_failure_contaminates_repaired_latency(tmp_path: Path):
    seed = _seed(tmp_path)
    source = _report(seed)
    source["metadata"]["eval_error"] = (
        "SessionLimitError: reached your session usage limit"
    )
    source["metadata"]["latency_valid"] = True
    source["metadata"]["latency_invalid_reasons"] = []

    repaired = _report(
        seed,
        source_report=source,
        resume_repaired=True,
    )

    assert repaired["metadata"]["latency_valid"] is False
    assert repaired["metadata"]["run_complete"] is False
    assert any(
        reason.startswith("source_eval_error:")
        for reason in repaired["metadata"]["latency_invalid_reasons"]
    )


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
