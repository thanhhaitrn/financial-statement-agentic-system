"""The main experiment's preflight and runner (Bước 4).

``preflight`` checks, without a model call, everything the 640 runs need: the
protocol against the attack set, an intact freeze (which pins the protocol,
code and dependencies), the completed dev review, the test-case sign-off, the
source reports, each
dataset's index (current and opened read-only, never rebuilt) and every
configuration's worker identity against the protocol.

:class:`MainRun` runs the protocol's cells one process each, on the pilot's
machinery (worker processes, key failover, budget ledger) with these rules:

- the schedule is drawn once from the protocol's seed (case order shuffled,
  then the eight cells of each case) and written before the first call; one
  case's cells finish before the next case starts;
- a cell is ``(case_id, config, condition, repetition=0)``; its result is the
  first valid attempt and every other attempt is kept;
- an infrastructure failure or a missing required artifact is retried once;
  provider quota moves to the next key and retries the same cell without
  using that retry; no key left, budget spent, a configuration or index
  mismatch stop the run with its state saved; a valid answer is never re-run;
- the budget is the protocol's (its own ledger, not the pilot's 300) and is
  never raised on resume; resume needs the same protocol, schedule and
  fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from agentfinx.core import experiment
from agentfinx.evaluation.contracts import atomic_write_json, stable_json_fingerprint
from agentfinx.evaluation.security import protocol as protocol_module
from agentfinx.evaluation.security.key_rotation import quota_reason
from agentfinx.evaluation.security.runner import RUN_TIMEOUT_SECONDS, Pilot, worker_identity
from agentfinx.evaluation.security.scorer import SCORER_VERSION

RETRY_STATUSES = {"infra_error", "runtime_error", "timeout", "worker_crashed"}
STOP_STATUSES = {"config_mismatch", "index_mismatch"}
MAX_FAILED_ATTEMPTS = 2  # the first try and one retry


def main_schedule(case_ids: list[str], configs, conditions, *, seed: int, repetition: int = 0) -> list[dict]:
    rng = random.Random(seed)
    order = list(case_ids)
    rng.shuffle(order)
    schedule = []
    for case_id in order:
        group = [{"case_id": case_id, "config": config, "condition": condition, "repetition": repetition}
                 for config in configs for condition in conditions]
        rng.shuffle(group)
        schedule.extend(group)
    return schedule


def cell_key(item: dict) -> tuple:
    return item["case_id"], item["config"], item["condition"], int(item.get("repetition", 0))


# --- run records ------------------------------------------------------------

class PromptIndex:
    """The prompt log by run, read incrementally (the log only grows)."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.by_run: dict[str, list[dict]] = {}

    def records(self, run_id: str) -> list[dict]:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as handle:
                handle.seek(self.offset)
                for line in handle:
                    if line.strip():
                        record = json.loads(line)
                        self.by_run.setdefault(record.get("run_id"), []).append(record)
                self.offset = handle.tell()
        return self.by_run.get(run_id, [])


def technical_problems(result: dict, run_dir: Path, prompts: list[dict]) -> list[str]:
    """Why an attempt is not a valid answer (empty when it is)."""

    problems = []
    if result.get("status") != "ok":
        problems.append(f"status:{result.get('status')}")
        return problems
    if not str(result.get("answer") or "").strip():
        problems.append("empty_answer")
    snapshot = run_dir / "snapshot.json"
    if not snapshot.exists():
        problems.append("missing_snapshot")
    elif hashlib.sha256(snapshot.read_bytes()).hexdigest() != result.get("snapshot_sha256"):
        problems.append("snapshot_hash_mismatch")
    requests = [record for record in prompts if record.get("event") == "request"]
    replies = {record.get("call_id") for record in prompts if record.get("event") in {"response", "error"}}
    if not requests:
        problems.append("missing_prompt_log")
    elif len(requests) != int(result.get("model_calls", -1)):
        problems.append(f"prompt_log_count:{len(requests)}!={result.get('model_calls')}")
    if any(record.get("call_id") not in replies for record in requests):
        problems.append("request_without_reply")
    return problems


# --- preflight ----------------------------------------------------------------

def preflight(protocol_path: str | Path | None = None, *, identities: dict | None = None,
              datasets: dict | None = None) -> dict:
    """Every precondition of the main run; no model call. ``identities`` and
    ``datasets`` are for tests."""

    from agentfinx.evaluation.security import checks
    from agentfinx.evaluation.security import freeze as freeze_module

    protocol, protocol_sha, path = protocol_module.load(protocol_path)
    cases = protocol_module.attack_set(protocol)
    problems: list[str] = [f"protocol: {p}" for p in protocol_module.problems(protocol, cases)]
    problems += [f"freeze: {p}" for p in freeze_module.frozen_problems(cases)]
    problems += [f"dev review: {p}" for p in freeze_module.dev_review_problems(cases)]
    signoff_path = cases.path.parent / "review" / "test_case_signoff.json"
    signoff = json.loads(signoff_path.read_text(encoding="utf-8")) if signoff_path.exists() else {}
    problems += [f"test-case review: {p}" for p in freeze_module.signoff_problems(cases, signoff)]
    problems += [f"report {dataset}: {p}" for dataset, items in checks.check_datasets(cases).items() for p in items]
    if datasets is None:
        datasets = {}
        from agentfinx.evaluation.security import readiness

        for dataset_id in cases.dataset_ids(protocol["case_ids"]):
            fresh = readiness.freshness(dataset_id)
            if not fresh["current"]:
                problems.append(f"index {dataset_id}: would be rebuilt ({fresh['reason']})")
                continue
            try:
                datasets[dataset_id] = readiness.open_read_only(dataset_id)[2]
            except Exception as exc:  # reported, never built
                problems.append(f"index {dataset_id}: {type(exc).__name__}: {exc}")
    if identities is None:
        identities = {config: worker_identity(config) for config in protocol["configs"]}
    problems += [f"config {p}" for p in protocol_module.identity_problems(protocol, identities)]
    try:
        import statsmodels  # noqa: F401
    except ImportError:
        problems.append("statsmodels is not installed (needed for the pre-registered statistics)")
    excluded = freeze_module.excluded(cases)
    return {"ok": not problems, "problems": problems, "protocol": str(path), "protocol_sha256": protocol_sha,
            "attack_set_sha256": cases.sha256, "datasets": datasets, "identities": identities,
            "dependencies": protocol_module.dependencies(), "excluded": sorted(excluded),
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def main_fingerprint(protocol: dict, protocol_sha: str, check: dict, schedule: list[dict]) -> dict:
    identities = check["identities"]
    sample = identities["D"]
    payload = {
        "protocol_sha256": protocol_sha,
        "attack_set_sha256": check["attack_set_sha256"],
        "effective_configs": {name: identities[name]["ablation"] for name in sorted(identities)},
        "runtime_policy": {name: identities[name]["runtime_policy_fingerprint"] for name in sorted(identities)},
        "module_hashes": sample["module_hashes"],
        "model": sample["model"], "embedding": sample["embedding"], "llm_rerank": sample["llm_rerank"],
        "datasets": check["datasets"],
        "dependencies_sha256": check["dependencies"]["sha256"],
        "scorer_version": SCORER_VERSION,
        "case_ids": sorted(protocol["case_ids"]),
        "configs": sorted(protocol["configs"]), "conditions": list(protocol["conditions"]),
        "budget": int(protocol["budget"]["model_calls"]), "seed": int(protocol["schedule"]["seed"]),
        "schedule_sha256": stable_json_fingerprint(schedule),
    }
    return {"sha256": stable_json_fingerprint(payload), "payload": payload}


# --- runner -------------------------------------------------------------------

class MainRun(Pilot):
    def __init__(self, out_dir: Path, protocol_path: str | Path | None = None, *, keys: list[str] | None = None,
                 retry_exhausted_keys: bool = False, run_timeout: int = RUN_TIMEOUT_SECONDS,
                 preflight_fn=None):
        protocol, protocol_sha, path = protocol_module.load(protocol_path)
        cases = protocol_module.attack_set(protocol)
        super().__init__(out_dir, cases, case_ids=list(protocol["case_ids"]),
                         budget=int(protocol["budget"]["model_calls"]), run_timeout=run_timeout, keys=keys,
                         retry_exhausted_keys=retry_exhausted_keys, configs=tuple(protocol["configs"]),
                         conditions=tuple(protocol["conditions"]))
        self.protocol, self.protocol_sha, self.protocol_path = protocol, protocol_sha, path
        self.seed = int(protocol["schedule"]["seed"])
        self.preflight_fn = preflight_fn or (lambda: preflight(path))
        self.attempts_path = out_dir / "attempts.jsonl"
        self.prompt_index = PromptIndex(self.prompt_log_path)

    def schedule(self) -> list[dict]:
        return main_schedule(self.case_ids, self.configs, self.conditions, seed=self.seed)

    # --- manifest -------------------------------------------------------------
    def prepare(self, *, fingerprint_value: dict | None = None) -> dict:
        check = self.preflight_fn()
        if not check["ok"]:
            raise SystemExit("preflight failed:\n- " + "\n- ".join(check["problems"]))
        schedule = self.schedule()
        fingerprint_value = fingerprint_value or main_fingerprint(self.protocol, self.protocol_sha, check, schedule)
        if self.manifest_path.exists():
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            changed = sorted(key for key in fingerprint_value["payload"]
                             if manifest["fingerprint"]["payload"].get(key) != fingerprint_value["payload"][key])
            if manifest["fingerprint"]["sha256"] != fingerprint_value["sha256"] or changed:
                raise SystemExit(f"{self.out} was made under another protocol or fingerprint; refusing to resume. "
                                 "Changed: " + ", ".join(changed or ["fingerprint"]))
            if manifest["schedule"] != schedule:
                raise SystemExit("the saved schedule differs from the protocol's; refusing to resume")
            budget = self.budget_state()
            if int(budget.get("limit", -1)) != self.budget:
                raise SystemExit(f"the budget ledger says {budget.get('limit')}, the protocol {self.budget}; "
                                 "a budget is never changed on resume")
            return manifest
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "cases_snapshot.json").write_bytes(self.cases.path.read_bytes())
        (self.out / "protocol_snapshot.json").write_bytes(self.protocol_path.read_bytes())
        atomic_write_json(self.out / "preflight.json", check)
        manifest = {
            "experiment": self.protocol["experiment"],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "protocol": str(self.protocol_path), "protocol_sha256": self.protocol_sha,
            "seed": self.seed, "exposure": "natural",
            "case_ids": self.case_ids, "configs": {name: self.protocol["configs"][name] for name in self.configs},
            "conditions": list(self.conditions), "repetition": 0,
            "schedule": schedule, "budget": self.budget, "pilot_env": self.protocol["environment"],
            "cases_file": str(self.cases.path), "fingerprint": fingerprint_value,
            "files": {"budget": "budget.json", "events": "events.jsonl", "prompt_log": "prompt_log.jsonl",
                      "attempts": "attempts.jsonl", "status": "main_status.json",
                      "runs": "runs/<run_id>/result.json, snapshot.json, spec.json, worker.log"},
        }
        # The schedule is on disk before the first model call.
        atomic_write_json(self.manifest_path, manifest)
        experiment.create_budget(self.budget_path, self.budget)
        return manifest

    # --- runs -----------------------------------------------------------------
    def run_id(self, item: dict, attempt: int) -> str:
        return f"{item['case_id']}-{item['config']}-{item['condition']}-r{int(item.get('repetition', 0))}-a{attempt}"

    def _spec(self, item: dict, attempt: int) -> dict:
        spec = super()._spec(item, attempt)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        dataset_id = self.cases.case(item["case_id"])["dataset_id"]
        spec.update(repetition=int(item.get("repetition", 0)), index_mode="read_only",
                    expected_dataset=manifest["fingerprint"]["payload"]["datasets"][dataset_id])
        return spec

    def _attempts(self, item: dict, done: dict[str, dict]) -> list[dict]:
        found, attempt = [], 1
        while self.run_id(item, attempt) in done:
            found.append(done[self.run_id(item, attempt)])
            attempt += 1
        return found

    def _record_attempt(self, item: dict, result: dict, problems: list[str]) -> None:
        experiment.append_jsonl(self.attempts_path, {
            "ts": datetime.now(timezone.utc).isoformat(), "run_id": result["run_id"], "cell": list(cell_key(item)),
            "attempt": result.get("attempt"), "status": result.get("status"), "valid": not problems,
            "problems": problems, "model_calls": result.get("model_calls"), "duration_s": result.get("duration_s"),
            "key_id": result.get("key_id")})

    def _problems(self, result: dict) -> list[str]:
        run_dir = self.out / "runs" / result["run_id"]
        return technical_problems(result, run_dir, self.prompt_index.records(result["run_id"]))

    def _is_valid(self, result: dict) -> bool:
        return not self._problems(result)

    def _is_failure(self, result: dict) -> bool:
        """An attempt that uses the cell's one retry: an infrastructure error
        or an answer missing a required artifact. Quota, budget and mismatch
        stops do not."""

        status = result.get("status")
        return status in RETRY_STATUSES or (status == "ok" and not self._is_valid(result))

    def _run_schedule(self, *, log=print) -> dict:
        manifest = self.prepare()
        done = self.results()
        stopped = ""
        for item in manifest["schedule"]:
            if stopped:
                break
            attempts = self._attempts(item, done)
            if any(self._is_valid(result) for result in attempts):
                continue  # the cell already has its valid answer: never re-run
            while not stopped:
                if sum(1 for result in attempts if self._is_failure(result)) >= MAX_FAILED_ATTEMPTS:
                    break
                budget = self.budget_state()
                if budget.get("exhausted") or budget.get("used", 0) >= budget.get("limit", self.budget):
                    stopped = "budget_exhausted"
                    break
                selected = self.key_pool.select() if self.key_pool is not None else None
                if self.key_pool is not None and selected is None:
                    stopped = "all_keys_exhausted"
                    break
                attempt = len(attempts) + 1
                spec = self._spec(item, attempt)
                if selected:
                    spec["key_id"] = selected[0]
                log(f"run {spec['run_id']} …")
                result = self._launch(spec)
                if selected:
                    result["key_id"] = selected[0]
                reason = quota_reason(result)
                if reason:
                    result.update(worker_status=result["status"], status="provider_quota", quota_reason=reason)
                atomic_write_json(Path(spec["run_dir"]) / "result.json", result)
                problems = self._problems(result)
                self._record_attempt(item, result, problems)
                attempts.append(result)
                log(f"run {spec['run_id']}: {result['status']} ({result.get('model_calls', '?')} calls, "
                    f"{result.get('duration_s', '?')} s){'' if not problems else ' — ' + ', '.join(problems)}")
                if not problems:
                    break
                if result["status"] == "provider_quota":
                    if self.key_pool is None:
                        stopped = "provider_quota"
                        break
                    self.key_pool.exhausted(result.get("key_id"), result["run_id"])
                    experiment.append_jsonl(self.out / "key_rotation_events.jsonl", {
                        "event": "provider_quota", "run_id": result["run_id"], "key_id": result.get("key_id"),
                        "reason": reason})
                    continue
                if result["status"] == "budget_exhausted":
                    stopped = "budget_exhausted"
                elif result["status"] in STOP_STATUSES:
                    stopped = result["status"]
                elif result["status"] not in RETRY_STATUSES and result["status"] != "ok":
                    break  # not retryable; the cell stays without a valid answer
        status = self.status(manifest, stopped)
        atomic_write_json(self.out / "main_status.json", status)
        return status

    def status(self, manifest: dict, stopped: str) -> dict:
        done = self.results()
        cells, missing = 0, []
        for item in manifest["schedule"]:
            attempts = self._attempts(item, done)
            if any(self._is_valid(result) for result in attempts):
                cells += 1
            else:
                missing.append({"cell": list(cell_key(item)), "attempts": len(attempts),
                                "last_status": attempts[-1].get("status") if attempts else "not_run"})
        return {"stopped": stopped, "budget": self.budget_state(), "cells": len(manifest["schedule"]),
                "valid_cells": cells, "missing": missing, "attempts": len(done)}
