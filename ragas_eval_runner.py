"""Evaluate an existing answer/retrieved-context report with RAGAs."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from numbers import Real
from pathlib import Path
from typing import Any

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


REPO_ROOT = Path(__file__).resolve().parent
LOCAL_DATASETS_DIR = REPO_ROOT / "datasets"
DEFAULT_PREDICTIONS_FILE = "ragas_runs/apec_predictions.json"
DEFAULT_METRIC_SLEEP_SECONDS = 2.0
DEFAULT_SAMPLE_SLEEP_SECONDS = 1.0
RETIRED_METRIC_NAMES = ("answer_correctness",)


def build_ragas_evaluation_samples(predictions: list[dict]) -> list[dict]:
    samples = []
    for item in build_ragas_rows(predictions):
        samples.append(
            {
                "user_input": item["question"],
                "response": item["answer"],
                "retrieved_contexts": item["contexts"],
                "reference": item["ground_truth"],
            }
        )
    return samples


def _ragas_available() -> bool:
    return importlib.util.find_spec("ragas") is not None


def _path_entry_is_repo_root(entry: str) -> bool:
    path_text = str(entry or "").strip()
    candidate = Path(path_text or os.getcwd())
    try:
        return candidate.resolve() == REPO_ROOT
    except OSError:
        return False


def _path_is_inside(path: str | Path, directory: Path) -> bool:
    try:
        Path(path).resolve().relative_to(directory.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_local_datasets_module(module: Any) -> bool:
    module_file = str(getattr(module, "__file__", "") or "")
    if module_file and _path_is_inside(module_file, LOCAL_DATASETS_DIR):
        return True

    module_paths = getattr(module, "__path__", []) or []
    return any(_path_is_inside(path, LOCAL_DATASETS_DIR) for path in module_paths)


@contextmanager
def _prefer_huggingface_datasets_import():
    """Temporarily prevent the repo's local datasets package from shadowing HF datasets."""

    original_path = list(sys.path)
    saved_local_modules = {}

    for name in list(sys.modules):
        if name != "datasets" and not name.startswith("datasets."):
            continue
        module = sys.modules.get(name)
        if module is not None and _is_local_datasets_module(module):
            saved_local_modules[name] = module
            sys.modules.pop(name, None)

    sys.path[:] = [
        entry
        for entry in sys.path
        if not _path_entry_is_repo_root(str(entry or ""))
    ]
    importlib.invalidate_caches()

    try:
        yield
    finally:
        for name in list(sys.modules):
            if name != "datasets" and not name.startswith("datasets."):
                continue
            module = sys.modules.get(name)
            if module is not None and not _is_local_datasets_module(module):
                sys.modules.pop(name, None)

        sys.modules.update(saved_local_modules)
        sys.path[:] = original_path
        importlib.invalidate_caches()


def _set_metric_name(metric: Any, name: str) -> Any:
    if not name:
        return metric
    try:
        metric.name = name
    except Exception:
        try:
            object.__setattr__(metric, "name", name)
        except Exception:
            pass
    return metric


def _instantiate_metric(metric_cls: Any, *, name: str = "") -> Any:
    if name:
        try:
            return metric_cls(name=name)
        except TypeError:
            pass
    return _set_metric_name(metric_cls(), name)


def _metric_from_module(module: Any, instance_name: str, class_names: list[str], *, name: str) -> Any:
    candidate = getattr(module, instance_name, None)
    if candidate is not None:
        if isinstance(candidate, type):
            return _instantiate_metric(candidate, name=name)
        return _set_metric_name(candidate, name)

    for class_name in class_names:
        candidate = getattr(module, class_name, None)
        if candidate is not None:
            return _instantiate_metric(candidate, name=name)

    options = ", ".join([instance_name, *class_names])
    raise ImportError(f"Cannot find RAGAs metric: {options}")


def _load_metric_objects() -> list[Any]:
    import ragas.metrics as metrics

    return [
        _metric_from_module(metrics, "faithfulness", ["Faithfulness"], name="faithfulness"),
        _metric_from_module(metrics, "answer_relevancy", ["ResponseRelevancy"], name="answer_relevancy"),
        _metric_from_module(
            metrics,
            "context_precision",
            ["LLMContextPrecisionWithReference", "LLMContextPrecisionWithoutReference"],
            name="context_precision",
        ),
        _metric_from_module(metrics, "context_recall", ["LLMContextRecall"], name="context_recall"),
    ]


def _build_evaluator_embeddings():
    from llm.client import LLM_REQUEST_TIMEOUT_SECONDS
    from langchain_ollama import OllamaEmbeddings
    from vectorstore.qdrant_store import EMBEDDING_BASE_URL, EMBEDDING_MODEL, _ollama_client_kwargs

    base_url = (
        os.getenv("OLLAMA_EVAL_EMBEDDING_BASE_URL", "").strip()
        or os.getenv("OLLAMA_EMBEDDING_BASE_URL", "").strip()
        or os.getenv("OLLAMA_BASE_URL", "").strip()
        or EMBEDDING_BASE_URL
    )
    model = os.getenv("OLLAMA_EVAL_EMBEDDING_MODEL", "").strip() or EMBEDDING_MODEL

    return OllamaEmbeddings(
        model=model,
        base_url=base_url,
        client_kwargs=_ollama_client_kwargs(base_url),
        sync_client_kwargs={"timeout": LLM_REQUEST_TIMEOUT_SECONDS},
        async_client_kwargs={"timeout": LLM_REQUEST_TIMEOUT_SECONDS},
    )


def _build_evaluator_llm():
    from dotenv import load_dotenv

    load_dotenv()
    timeout = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "900"))

    # Opt-in Claude judge (dormant unless ANTHROPIC_API_KEY is set — needs an
    # Anthropic API key, which is separate from a Claude Pro subscription). The
    # local gpt-oss judge proved unreliable for Vietnamese (scored faithfulness=0
    # on answers grounded verbatim in the context). When a key is available this
    # gives trustworthy scores and isn't session-rate-limited.
    judge = os.getenv("RAGAS_JUDGE", "").strip().lower()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if judge == "anthropic" or (judge != "ollama" and anthropic_key):
        if not anthropic_key:
            raise RuntimeError("Missing ANTHROPIC_API_KEY env var for the RAGAs Claude judge")
        from langchain_anthropic import ChatAnthropic

        # No temperature: Claude Opus 4.8/4.7 reject sampling params (400).
        # Override the model with RAGAS_JUDGE_MODEL (e.g. claude-sonnet-4-6).
        return ChatAnthropic(
            model=os.getenv("RAGAS_JUDGE_MODEL", "claude-opus-4-8"),
            max_tokens=int(os.getenv("RAGAS_JUDGE_MAX_TOKENS", "4096")),
            timeout=timeout,
            max_retries=int(os.getenv("RAGAS_JUDGE_MAX_RETRIES", "4")),
            api_key=anthropic_key,
        )

    from langchain_ollama import ChatOllama

    api_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing OLLAMA_API_KEY env var")
    # RAGAS_JUDGE_MODEL overrides the judge model independently of the agent's
    # OLLAMA_MODEL. qwen3-coder:480b judged Vietnamese far better than
    # gpt-oss:120b-cloud (which floored faithfulness/recall to 0 on answers
    # grounded verbatim in the context) but was retired by Ollama Cloud on
    # 2026-07-15. minimax-m3 reproduced qwen3-coder's reference scores on an
    # A/B probe (exact match on faithfulness/context_precision/context_recall)
    # and is the strongest judge still on the free tier.
    return ChatOllama(
        model=os.getenv("RAGAS_JUDGE_MODEL", "").strip() or os.getenv("OLLAMA_MODEL", "minimax-m3"),
        temperature=float(os.getenv("OLLAMA_TEMPERATURE", "0")),
        base_url=os.getenv("OLLAMA_BASE_URL", "https://ollama.com"),
        headers={"Authorization": f"Bearer {api_key}"},
        sync_client_kwargs={"timeout": timeout},
        async_client_kwargs={"timeout": timeout},
    )


def _metric_name(metric: Any) -> str:
    return str(getattr(metric, "name", "") or metric.__class__.__name__).strip()


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


def _scores_by_key(scores: list[dict]) -> dict[str, dict]:
    output = {}
    for score in scores or []:
        key = prediction_key(score)
        if key:
            output[key] = _normalize_score_row(score)
    return output


def merge_scores_for_predictions(
    predictions: list[dict],
    *,
    existing_scores: list[dict] | None = None,
    new_scores: list[dict] | None = None,
) -> list[dict]:
    by_key = _scores_by_key(existing_scores or [])
    for score in new_scores or []:
        key = prediction_key(score)
        if key:
            by_key[key] = _normalize_score_row(score)
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
) -> Any:
    from ragas import evaluate
    from ragas.dataset_schema import EvaluationDataset

    metric_name = _metric_name(metric)
    dataset = EvaluationDataset.from_list(build_ragas_evaluation_samples([prediction]))

    for attempt in range(2):
        evaluator_llm = _build_evaluator_llm()
        evaluator_embeddings = _build_evaluator_embeddings()

        try:
            result = evaluate(
                dataset,
                metrics=[metric],
                llm=evaluator_llm,
                embeddings=evaluator_embeddings,
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
            "python -m pip install -r requirements.txt"
        )

    _build_evaluator_embeddings()
    existing_scores = list(existing_scores or [])
    existing_scores_by_key = _scores_by_key(existing_scores)
    new_scores_by_key: dict[str, dict] = {}

    def merged_scores() -> list[dict]:
        return merge_scores_for_predictions(
            predictions,
            existing_scores=existing_scores,
            new_scores=list(new_scores_by_key.values()),
        )

    with _prefer_huggingface_datasets_import():
        metrics = _load_metric_objects()

        for index, prediction in enumerate(predictions, start=1):
            key = prediction_key(prediction)
            score = _normalize_score_row(existing_scores_by_key.get(key, {}))
            if _score_complete(score):
                continue

            print(f"[RAGAs {index}/{len(predictions)}] {prediction.get('question', '')}")
            sys.stdout.flush()

            score.update({"id": prediction.get("id"), "question": prediction.get("question", "")})
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
                    on_checkpoint(merged_scores(), "")

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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.predictions_file)
    output_path = Path(args.output or args.predictions_file)

    report = load_report(input_path)
    predictions = predictions_from_report(report)
    if not predictions:
        raise SystemExit(f"No predictions found in: {input_path}")

    existing_output_report = load_report(output_path) if output_path.exists() and not args.force else {}
    existing_scores = [] if args.force else (existing_output_report.get("scores", []) or report.get("scores", []) or [])
    eval_error = ""
    exit_code = 0

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
        write_json_report(checkpoint_report, output_path)
        write_csv_report(checkpoint_report, output_path)

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
    json_path = write_json_report(scored_report, output_path)
    csv_path = write_csv_report(scored_report, json_path)

    print(f"Wrote JSON report: {json_path}")
    print(f"Wrote CSV report: {csv_path}")
    print(json.dumps(scored_report["summary"], ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
