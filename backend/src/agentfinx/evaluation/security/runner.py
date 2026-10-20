"""The pilot schedule: manifest, fingerprint, budget, one process per QA run.

Order: the required cases first, each case group finished across A/B/C/D ×
clean/attack before the next; inside a group the eight runs are shuffled
with the manifest's seed. Every run is a fresh ``worker`` process whose
environment carries the configuration's switches (only this runner sets
them) and a fixed news window (``as_of``). A run that fails on
infrastructure is retried once, as a new attempt, while budget remains; both
attempts are kept. Provider quota advances to the next configured credential
and retries the same slot; exhausted credentials stay exhausted on resume.
When the budget blocks a request the schedule stops and
the rest is recorded as not run. Nothing is re-run to improve a result.

Resume reuses an output directory only when its fingerprint equals the
current one; otherwise it refuses.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from agentfinx.core import experiment
from agentfinx.evaluation.contracts import atomic_write_json, stable_json_fingerprint
from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security.freeze import product_code_sha256
from agentfinx.evaluation.security.key_rotation import KeyPool, quota_reason
from agentfinx.evaluation.security.scorer import SCORER_VERSION
from agentfinx.evaluation.security.worker import CONFIG_FLAGS

SEED = 20261020
DEFAULT_BUDGET = 300
RUN_TIMEOUT_SECONDS = 1_800
PILOT_ENV = {
    "WEB_EVIDENCE_ENABLED": "1",
    "WEB_ANALYSIS_ENABLED": "1",
    "LLM_RERANK": "0",
}
# Controlled exposure (a separately labelled check, not the natural pilot):
# the provider's articles are placed at the workers' fact input as the app's
# WEB facts (worker_results["WEB"], where the provenance gates read), with the
# news analysis and the product's own web path off.
CONTROLLED_ENV = {**PILOT_ENV, "WEB_ANALYSIS_ENABLED": "0", "WEB_EVIDENCE_ENABLED": "0"}
EXPOSURES = {"natural": PILOT_ENV, "controlled": CONTROLLED_ENV}
RETRYABLE = {"infra_error", "runtime_error", "timeout"}


def config_env(config: str) -> dict:
    flags = CONFIG_FLAGS[config]
    return {"AGENTFINX_ABLATE_PROMPT_GUARDS": "1" if flags["prompt_guards_off"] else "0",
            "AGENTFINX_ABLATE_PROVENANCE": "1" if flags["provenance_off"] else "0"}


def worker_env(config: str, extra: dict | None = None, *, exposure: str = "natural") -> dict:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("AGENTFINX_ABLATE_") and key not in {experiment.RUN_CONTEXT_ENV}}
    env.update(EXPOSURES[exposure])
    env.update(config_env(config))
    env.update(extra or {})
    return env


def worker_identity(config: str, exposure: str = "natural") -> dict:
    """The identity a worker of ``config`` reports, from a real worker process."""

    output = subprocess.run([sys.executable, "-m", "agentfinx.evaluation.security.worker", "--identity"],
                            env=worker_env(config, exposure=exposure), capture_output=True, text=True,
                            timeout=300, check=True)
    return json.loads(output.stdout.strip().splitlines()[-1])


def fingerprint(cases: cases_module.CaseFile, *, case_ids: list[str], budget: int,
                identities: dict, dataset: dict) -> dict:
    """Effective configuration, module hashes, scorer version, fixture hash,
    dataset/index, model, temperature and ``as_of``."""

    sample = identities["D"]
    payload = {
        "effective_configs": {name: identities[name]["ablation"] for name in identities},
        "runtime_policy": {name: identities[name]["runtime_policy_fingerprint"] for name in identities},
        "module_hashes": sample["module_hashes"],
        "scorer_version": SCORER_VERSION,
        "fixture_sha256": cases.sha256,
        "dataset": dataset,
        "model": sample["model"],
        "temperature": sample["model"].get("temperature"),
        "embedding": sample["embedding"],
        "llm_rerank": sample["llm_rerank"],
        "as_of": {case_id: cases.case(case_id)["as_of"] for case_id in case_ids},
        "case_ids": case_ids,
        "budget": budget,
        "seed": SEED,
    }
    return {"sha256": stable_json_fingerprint(payload), "payload": payload}


def schedule(case_ids: list[str], *, seed: int = SEED, configs=cases_module.CONFIGS,
             conditions=cases_module.CONDITIONS) -> list[dict]:
    rng = random.Random(seed)
    order = []
    for case_id in case_ids:
        group = [{"case_id": case_id, "config": config, "condition": condition}
                 for config in configs for condition in conditions]
        rng.shuffle(group)
        order.extend(group)
    return order


def _dataset_identity(dataset_id: str) -> dict:
    """Dataset and index generation, read in a clean process (no flags)."""

    code = ("import json,sys\n"
            "from agentfinx.datasets.registry import get_dataset\n"
            "from agentfinx.datasets.locks import dataset_lock\n"
            "from agentfinx.workflow.pipeline import ensure_built\n"
            "from agentfinx.evaluation.batch import built_dataset_identity\n"
            f"d = get_dataset({dataset_id!r})\n"
            "with dataset_lock(d.dataset_id):\n"
            "    d, conn, col = ensure_built(d)\n"
            "print(json.dumps(built_dataset_identity(d, conn, col)))\n")
    output = subprocess.run([sys.executable, "-c", code], env=worker_env("D"), capture_output=True,
                            text=True, timeout=900, check=True)
    return json.loads(output.stdout.strip().splitlines()[-1])


class Pilot:
    def __init__(self, out_dir: Path, cases: cases_module.CaseFile, *, case_ids: list[str],
                 budget: int = DEFAULT_BUDGET, run_timeout: int = RUN_TIMEOUT_SECONDS,
                 keys: list[str] | None = None, retry_exhausted_keys: bool = False,
                 exposure: str = "natural", configs=cases_module.CONFIGS,
                 conditions=cases_module.CONDITIONS, parent: str = ""):
        if exposure not in EXPOSURES:
            raise ValueError(f"exposure must be one of {sorted(EXPOSURES)}")
        self.exposure = exposure
        self.configs = tuple(configs)
        self.conditions = tuple(conditions)
        self.parent = parent
        self.out = out_dir
        self.cases = cases
        self.case_ids = case_ids
        self.budget = budget
        self.run_timeout = run_timeout
        self.manifest_path = out_dir / "manifest.json"
        self.budget_path = out_dir / "budget.json"
        self.events_path = out_dir / "events.jsonl"
        self.prompt_log_path = out_dir / "prompt_log.jsonl"
        self.key_pool = KeyPool(keys, out_dir / "key_rotation.json") if keys is not None else None
        self.retry_exhausted_keys = retry_exhausted_keys

    # --- manifest -------------------------------------------------------------
    def prepare(self, *, fingerprint_value: dict | None = None) -> dict:
        """Create or check the manifest. ``fingerprint_value`` is for tests."""

        if self.cases.schema >= 2 and any(self.cases.case(cid)["split"] == "test" for cid in self.case_ids):
            from agentfinx.evaluation.security.freeze import frozen_problems

            problems = frozen_problems(self.cases)
            if problems:
                raise SystemExit("test cases require an intact freeze: " + "; ".join(problems))
        if fingerprint_value is None:
            identities = {config: worker_identity(config, self.exposure) for config in cases_module.CONFIGS}
            fingerprint_value = fingerprint(self.cases, case_ids=self.case_ids, budget=self.budget,
                                            identities=identities,
                                            dataset={dataset_id: _dataset_identity(dataset_id) for dataset_id
                                                     in self.cases.dataset_ids(self.case_ids)})
        if self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if manifest["fingerprint"]["sha256"] != fingerprint_value["sha256"]:
                raise SystemExit(
                    f"{self.out} was made with another fingerprint; refusing to resume. "
                    "Changed: " + ", ".join(sorted(
                        key for key in fingerprint_value["payload"]
                        if manifest["fingerprint"]["payload"].get(key) != fingerprint_value["payload"][key])))
            return manifest
        self.out.mkdir(parents=True, exist_ok=True)
        # Keep the exact inputs even when a draft is rebuilt after a dev run.
        (self.out / "cases_snapshot.json").write_bytes(self.cases.path.read_bytes())
        manifest = {
            "experiment": "agentfinx-security-pilot",
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "seed": SEED,
            "exposure": self.exposure,
            "parent": self.parent,
            "case_ids": self.case_ids,
            "schedule": schedule(self.case_ids, configs=self.configs, conditions=self.conditions),
            "configs": {name: {**CONFIG_FLAGS[name], "pipeline": "baseline" if name == "A" else "agentfinx"}
                        for name in cases_module.CONFIGS},
            "pilot_env": EXPOSURES[self.exposure],
            "budget": self.budget,
            "cases_file": str(self.cases.path),
            "fingerprint": fingerprint_value,
            # The product version a dev review of these outputs is bound to.
            "product_code_sha256": product_code_sha256(),
            "files": {"budget": "budget.json", "events": "events.jsonl", "prompt_log": "prompt_log.jsonl",
                      "runs": "runs/<run_id>/result.json, snapshot.json, spec.json"},
        }
        self.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        experiment.create_budget(self.budget_path, self.budget)
        return manifest

    # --- runs -----------------------------------------------------------------
    def run_id(self, item: dict, attempt: int) -> str:
        return f"{item['case_id']}-{item['config']}-{item['condition']}-a{attempt}"

    def results(self) -> dict[str, dict]:
        found = {}
        for path in sorted((self.out / "runs").glob("*/result.json")):
            result = json.loads(path.read_text(encoding="utf-8"))
            found[result["run_id"]] = result
        return found

    def _spec(self, item: dict, attempt: int) -> dict:
        run_id = self.run_id(item, attempt)
        case = self.cases.case(item["case_id"])
        run_dir = self.out / "runs" / run_id
        return {
            "run_id": run_id, "case_id": item["case_id"], "config": item["config"],
            "condition": item["condition"], "attempt": attempt, "question": case["question"],
            "exposure": self.exposure,
            "dataset_id": case["dataset_id"], "ticker": case["ticker"],
            "company": case["company"], "as_of": case["as_of"],
            "articles": cases_module.condition_articles(self.cases, item["case_id"], item["condition"]),
            "run_dir": str(run_dir), "events_path": str(self.events_path),
            "prompt_log_path": str(self.prompt_log_path), "budget_path": str(self.budget_path),
        }

    def _launch(self, spec: dict) -> dict:
        run_dir = Path(spec["run_dir"])
        run_dir.mkdir(parents=True, exist_ok=True)
        spec_path = run_dir / "spec.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        env = worker_env(spec["config"], {experiment.PROMPT_LOG_ENV: spec["prompt_log_path"]},
                         exposure=self.exposure)
        if self.key_pool is not None:
            selected = self.key_pool.select()
            if selected is None:
                raise RuntimeError("All configured Ollama keys have exhausted quota")
            key_id, key = selected
            if spec.get("key_id") != key_id:
                raise RuntimeError("Credential selection changed before launch")
            env["OLLAMA_API_KEY"] = key
        started = time.perf_counter()
        with open(run_dir / "worker.log", "w", encoding="utf-8") as log:
            try:
                subprocess.run([sys.executable, "-m", "agentfinx.evaluation.security.worker",
                                "--spec", str(spec_path)], env=env, stdout=log, stderr=subprocess.STDOUT,
                               timeout=self.run_timeout, check=False)
            except subprocess.TimeoutExpired:
                pass
        result_path = run_dir / "result.json"
        if result_path.exists():
            return json.loads(result_path.read_text(encoding="utf-8"))
        result = {key: spec[key] for key in ("run_id", "case_id", "config", "condition", "attempt", "question")}
        result.update(status="timeout" if time.perf_counter() - started >= self.run_timeout else "worker_crashed",
                      errors=["worker wrote no result; see worker.log"],
                      duration_s=round(time.perf_counter() - started, 3))
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return result

    def budget_state(self) -> dict:
        return experiment.read_budget(self.budget_path)

    def run(self, *, log=print) -> dict:
        self.out.mkdir(parents=True, exist_ok=True)
        with open(self.out / "pilot.lock", "a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("Another pilot runner is active in this directory") from exc
            if self.key_pool is not None:
                self.key_pool.reload(retry_exhausted=self.retry_exhausted_keys)
                self.retry_exhausted_keys = False
            # Orchestration policy is audited separately from model/prompt identity,
            # so adding credential failover does not invalidate completed answers.
            files = [Path(__file__), Path(__file__).with_name("key_rotation.py"),
                     Path(__file__).with_name("cli.py"),
                     Path(__file__).parents[2] / "core" / "llm" / "keys.py"]
            experiment.append_jsonl(self.out / "orchestration.jsonl", {
                "ts": datetime.now(timezone.utc).isoformat(),
                "policy": "quota-key-failover-v1", "key_count": self.key_pool.count if self.key_pool else 0,
                "module_hashes": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in files},
            })
            return self._run_schedule(log=log)

    def _run_schedule(self, *, log=print) -> dict:
        manifest = self.prepare() if not self.manifest_path.exists() else json.loads(
            self.manifest_path.read_text(encoding="utf-8"))
        done = self.results()
        stopped = ""
        for item in manifest["schedule"]:
            if stopped:
                break
            attempt, infra_failures = 1, 0
            while True:
                run_id = self.run_id(item, attempt)
                if run_id in done:
                    result = done[run_id]
                else:
                    budget = self.budget_state()
                    if budget.get("exhausted") or budget.get("used", 0) >= budget.get("limit", self.budget):
                        stopped = "budget_exhausted"
                        break
                    selected = self.key_pool.select() if self.key_pool is not None else None
                    if self.key_pool is not None and selected is None:
                        stopped = "all_keys_exhausted"
                        log("All configured Ollama keys have exhausted quota; add a key or use "
                            "--retry-exhausted-keys after quota resets.")
                        break
                    spec = self._spec(item, attempt)
                    if selected:
                        spec["key_id"] = selected[0]  # hash only; the secret goes in the worker environment
                    log(f"run {run_id} …")
                    result = self._launch(spec)
                    if selected:
                        result["key_id"] = selected[0]
                    reason = quota_reason(result)
                    if reason:
                        result["worker_status"] = result["status"]
                        result["status"] = "provider_quota"
                        result["quota_reason"] = reason
                    atomic_write_json(Path(spec["run_dir"]) / "result.json", result)
                    log(f"run {run_id}: {result['status']} ({result.get('model_calls', '?')} calls, "
                        f"{result.get('duration_s', '?')} s)")
                if result["status"] == "budget_exhausted":
                    stopped = "budget_exhausted"
                    break
                if quota_reason(result) and self.key_pool is not None:
                    if run_id not in self.key_pool.state["quota_runs"]:
                        selected = self.key_pool.select()
                        self.key_pool.exhausted(result.get("key_id") or (selected[0] if selected else None), run_id)
                        experiment.append_jsonl(self.out / "key_rotation_events.jsonl", {
                            "event": "provider_quota", "run_id": run_id,
                            "key_id": result.get("key_id"), "reason": quota_reason(result),
                        })
                    log(f"run {run_id}: provider quota; trying the next available key")
                    attempt += 1
                    continue
                if result["status"] not in RETRYABLE:
                    break
                infra_failures += 1
                if infra_failures >= 2:
                    break
                if self.budget_state().get("exhausted"):
                    stopped = "budget_exhausted"
                    break
                attempt += 1
        status = {"stopped": stopped or "", "budget": self.budget_state(), "runs": len(self.results())}
        (self.out / "pilot_status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")
        return status
