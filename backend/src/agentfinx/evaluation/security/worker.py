"""One QA run of the security experiment, in a process of its own.

The runner starts ``python -m agentfinx.evaluation.security.worker --spec
<file>`` with the ablation flags of the run's configuration already in the
environment; the flags are read once here and cannot change during the run.
The worker checks that the flags it sees are the ones its configuration
needs, answers the question (the product pipeline for B/C/D through
``run_question``, the plain RAG baseline for A) and writes:

- ``result.json``: status, the batch-compatible result, timings, identity;
- ``snapshot.json``: the whole final state (or the baseline's inputs), with
  its SHA-256 in the result.

Statuses: ``ok``; ``budget_exhausted`` (a model request was blocked; never a
defense success); ``infra_error`` (a model call failed on transport,
timeout or the provider); ``runtime_error`` (the run raised);
``config_mismatch`` (the flags were not the configuration's).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from agentfinx.core import ablation, experiment

# Expected switches per configuration; A runs no pipeline, so it needs both off.
CONFIG_FLAGS = {
    "A": {"prompt_guards_off": False, "provenance_off": False},
    "B": {"prompt_guards_off": True, "provenance_off": True},
    "C": {"prompt_guards_off": False, "provenance_off": True},
    "D": {"prompt_guards_off": False, "provenance_off": False},
    # Phase 2's 2×2 (B, C, E, D): L5 alone, without the prompt guards.
    "E": {"prompt_guards_off": True, "provenance_off": False},
}
INFRA_ERRORS = ("timeout", "connect", "readerror", "remoteprotocol", "httpstatus", "responseerror",
                "ratelimit", "serviceunavailable", "connectionerror")
# Experiment code and the product modules whose behaviour the flags change.
AFFECTED_MODULES = (
    "core/ablation.py", "core/experiment.py", "core/llm/instrument.py", "core/llm/client.py",
    "core/llm/invoke.py", "core/schemas/evidence_origin.py", "core/schemas/financial_validation.py",
    "core/schemas/web_evidence.py", "agents/prompts.py", "agents/profiles.py", "agents/synth_runner.py",
    "agents/agent_runner.py", "news/analysis/prompts.py", "news/analysis/pipeline.py",
    "news/analysis/facts.py", "workflow/router.py", "workflow/pipeline.py", "workflow/evidence.py",
    "workflow/news_node.py", "evaluation/runner.py", "evaluation/security/baseline.py",
    "evaluation/security/provider.py", "evaluation/security/worker.py",
)


def identity() -> dict:
    """What decides this process's behaviour, without secrets."""

    from agentfinx.core.config.runtime_policy import active_policy
    from agentfinx.evaluation.run_identity import (
        _embedding_identity_payload,
        _model_identity_payload,
        _source_files_identity,
    )

    policy = active_policy()
    return {
        "ablation": ablation.config().as_dict(),
        "model": _model_identity_payload(),
        "embedding": _embedding_identity_payload(),
        "runtime_policy_fingerprint": policy.fingerprint(),
        "web_evidence_enabled": policy.web.enabled,
        "web_analysis_active": policy.web.analysis_active,
        "llm_rerank": os.getenv("LLM_RERANK", "0").strip(),
        "module_hashes": _source_files_identity(AFFECTED_MODULES),
    }


def sha256_json(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, value) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=1, default=str)
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _model_call_errors(prompt_log: str, run_id: str) -> list[dict]:
    errors = []
    path = Path(prompt_log)
    if not path.exists():
        return errors
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("run_id") == run_id and record.get("event") == "error":
            errors.append(record)
    return errors


def _served_by(prompt_log: str, run_id: str) -> list[dict] | str:
    """The distinct model metadata the provider returned for this run's
    replies, or ``"unspecified"`` when it returned none."""

    path = Path(prompt_log)
    seen = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("run_id") == run_id and record.get("event") == "response" and record.get("served_by"):
                if record["served_by"] not in seen:
                    seen.append(record["served_by"])
    return seen or "unspecified"


def is_infra_error(record: dict) -> bool:
    text = f"{record.get('error_type', '')} {record.get('error', '')}".lower().replace("_", "")
    return any(marker in text for marker in INFRA_ERRORS) or any(
        code in text for code in (" 429", " 500", " 502", " 503", " 504"))


def run(spec: dict) -> dict:
    run_dir = Path(spec["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)
    context = experiment.ExperimentContext(
        run_id=spec["run_id"], case_id=spec["case_id"], config=spec["config"],
        condition=spec["condition"], attempt=int(spec["attempt"]), events_path=spec["events_path"],
        prompt_log_path=spec["prompt_log_path"], budget_path=spec["budget_path"])
    result = {key: spec[key] for key in ("run_id", "case_id", "config", "condition", "attempt", "question")}
    result.update({"exposure": spec.get("exposure", "natural"), "repetition": int(spec.get("repetition", 0)),
                   "identity": identity(), "status": "", "errors": []})
    if (result["exposure"] in ("controlled", "structured")) == result["identity"]["web_analysis_active"]:
        result.update(status="config_mismatch", errors=["news analysis switch does not match the exposure"])
        _write_json(run_dir / "result.json", result)
        return result
    expected = CONFIG_FLAGS[spec["config"]]
    if result["identity"]["ablation"] != expected:
        result.update(status="config_mismatch",
                      errors=[f"flags {result['identity']['ablation']} != {expected}"])
        _write_json(run_dir / "result.json", result)
        return result

    started = time.perf_counter()
    snapshot: dict = {}
    try:
        from agentfinx.datasets.locks import dataset_lock
        from agentfinx.datasets.registry import get_dataset
        from agentfinx.evaluation.batch import built_dataset_identity, serialize_run_result
        from agentfinx.evaluation.security.provider import FixtureNewsProvider
        from agentfinx.workflow.pipeline import ensure_built

        if spec.get("index_mode") == "read_only":
            # The main runs open the built index as chat does and never build.
            from agentfinx.evaluation.security.readiness import open_read_only

            dataset, collection, result["dataset"] = open_read_only(spec["dataset_id"])
            expected = spec.get("expected_dataset") or {}
            changed = [key for key in ("dataset_generation", "index_generation")
                       if result["dataset"].get(key) != expected.get(key)]
            if changed:
                result.update(status="index_mismatch", errors=[f"{key} differs from the preflight" for key in changed])
                _write_json(run_dir / "result.json", result)
                return result
        else:
            dataset = get_dataset(spec["dataset_id"])
            if dataset is None:
                raise RuntimeError(f"dataset {spec['dataset_id']} not found")
            with dataset_lock(dataset.dataset_id):
                dataset, conn, collection = ensure_built(dataset)
            result["dataset"] = built_dataset_identity(dataset, conn, collection)
        provider = FixtureNewsProvider(spec["articles"], ticker=spec["ticker"], as_of=spec["as_of"])
        with experiment.activate(context):
            if spec["config"] == "A":
                from agentfinx.evaluation.security.baseline import run_baseline

                out = run_baseline(collection, spec["question"], provider, ticker=spec["ticker"],
                                   company=spec["company"], as_of=spec["as_of"])
                snapshot = {"kind": "baseline", **out, "provider_searches": provider.searches}
                result["answer"] = out["answer"]
                result["batch_result"] = {"query": spec["question"], "query_id": spec["run_id"],
                                          "final_answer": out["answer"], "answer": out["answer"],
                                          "errors": [], "total_tokens": out["usage"].get("total_tokens")}
            else:
                from agentfinx.evaluation.runner import run_question

                extra = {"web_provider": provider}
                if result["exposure"] in ("controlled", "structured"):
                    # The articles go straight to the workers' fact input as the app's
                    # WEB facts; the product's own web path stays off.
                    from agentfinx.evaluation.security.baseline import news_request

                    batch = provider.search(news_request(spec["question"], ticker=spec["ticker"],
                                                         company=spec["company"], as_of=spec["as_of"]))
                    facts = [item.as_fact() for item in batch.evidence]
                    if result["exposure"] == "structured":
                        # The attacker's figure enters as a typed fact in place of its
                        # article's text: the number is in the value, never in prose.
                        figures = spec.get("structured_figures") or []
                        typed = {figure["article_url"] for figure in figures}
                        facts = [item.as_fact() for item in batch.evidence if item.source_url not in typed]
                        facts += [item.as_structured_fact(figure) for item in batch.evidence
                                  for figure in figures if figure["article_url"] == item.source_url]
                    extra = {"controlled_web_facts": facts}
                final_state = run_question(dataset, collection, spec["question"], as_of=spec["as_of"],
                                           experiment_context=context, **extra)
                serialized = serialize_run_result(final_state, spec["question"], query_id=spec["run_id"],
                                                  include_trace=True)
                snapshot = {"kind": "pipeline", "final_state": final_state,
                            "provider_searches": provider.searches}
                result["answer"] = serialized["formatted_answer"]
                result["batch_result"] = serialized
                result["errors"] = list(serialized.get("errors") or [])
        result["status"] = "ok"
    except experiment.BudgetExhausted as exc:
        result.update(status="budget_exhausted", errors=[str(exc)])
    except Exception as exc:  # recorded, never retried here
        result.update(status="runtime_error", errors=[f"{type(exc).__name__}: {exc}"])
    result["duration_s"] = round(time.perf_counter() - started, 3)

    budget = experiment.read_budget(spec["budget_path"])
    if spec["run_id"] in (budget.get("blocked_runs") or []):
        result["status"] = "budget_exhausted"
    infra = [record for record in _model_call_errors(spec["prompt_log_path"], spec["run_id"])
             if is_infra_error(record)]
    result["model_call_errors"] = _model_call_errors(spec["prompt_log_path"], spec["run_id"])
    if infra and result["status"] == "ok":
        result["status"] = "infra_error"
    result["model_calls"] = int((budget.get("by_run") or {}).get(spec["run_id"], 0))
    result["served_by"] = _served_by(spec["prompt_log_path"], spec["run_id"])
    if snapshot:
        result["snapshot_sha256"] = _write_json(run_dir / "snapshot.json", snapshot)
    _write_json(run_dir / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One security-experiment QA run (internal).")
    parser.add_argument("--spec", help="run spec JSON written by the runner")
    parser.add_argument("--identity", action="store_true", help="print this process's identity and exit")
    args = parser.parse_args(argv)
    if args.identity:
        print(json.dumps(identity(), ensure_ascii=False, sort_keys=True))
        return 0
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    result = run(spec)
    print(json.dumps({"run_id": result["run_id"], "status": result["status"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
