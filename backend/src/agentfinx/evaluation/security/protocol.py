"""The main experiment's protocol (``benchmarks/security/main_protocol.json``).

The protocol fixes what the 640 runs are before the first one: configurations
and their switches, the test case ids, model and temperature, retrieval mode,
each case's ``as_of``, the budget, seed, retry rule, review and statistics.
Its SHA-256 is part of the freeze and of every run's fingerprint, so a run
directory made under another protocol is refused.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agentfinx.evaluation import benchmarks
from agentfinx.evaluation.security import cases as cases_module

PROTOCOL = "benchmarks/security/main_protocol.json"
# Distributions whose version changes how a run behaves or is scored.
KEY_DISTRIBUTIONS = ("langchain-core", "langchain-ollama", "langgraph", "ollama", "qdrant-client", "pydantic",
                     "httpx", "numpy", "pandas", "statsmodels", "scipy")


def load(path: str | Path | None = None) -> tuple[dict, str, Path]:
    resolved = benchmarks.resolve(path or PROTOCOL)
    raw = resolved.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest(), resolved


def attack_set(protocol: dict) -> cases_module.CaseFile:
    return cases_module.load_cases(protocol["attack_set"]["path"])


def review_mode(protocol: dict) -> str:
    return protocol.get("review", {}).get("mode", "human")


def review_policy_problems(protocol: dict) -> list[str]:
    mode = review_mode(protocol)
    if mode not in {"human", "automated_only"}:
        return [f"unknown output review mode: {mode}"]
    if mode == "automated_only":
        waiver = protocol["review"].get("waiver") or {}
        if waiver.get("requested_by") != "user" or not all(
                str(waiver.get(key, "")).strip() for key in ("instruction", "recorded_at")):
            return ["automated_only requires a recorded explicit user waiver of output review"]
        validation = protocol.get("dev_validation") or {}
        if not validation.get("case_ids") or validation.get("configs") != ["A", "B", "C", "D"] \
                or validation.get("conditions") != ["clean", "attack"]:
            return ["automated_only requires a declared complete A–D clean/attack dev validation"]
    return []


def problems(protocol: dict, cases: cases_module.CaseFile) -> list[str]:
    """Inconsistencies between the protocol, the attack set and the harness."""

    from agentfinx.evaluation.security.scorer import SCORER_VERSION
    from agentfinx.evaluation.security.worker import CONFIG_FLAGS

    found = review_policy_problems(protocol)
    for case_id in (protocol.get("dev_validation") or {}).get("case_ids", []):
        if case_id not in cases.case_ids(split="dev"):
            found.append(f"dev validation case is not in dev: {case_id}")
    if protocol["attack_set"]["sha256"] != cases.sha256:
        found.append("attack set differs from the protocol's")
    test = sorted(cases.case_ids(split=protocol["attack_set"]["split"]))
    if sorted(protocol["case_ids"]) != test:
        found.append("case ids differ from the test split")
    if len(set(protocol["case_ids"])) != len(protocol["case_ids"]):
        found.append("duplicate case ids")
    for config, settings in protocol["configs"].items():
        flags = CONFIG_FLAGS.get(config)
        if flags is None or {key: settings.get(key) for key in flags} != flags:
            found.append(f"config {config}: switches differ from the worker's")
    if sorted(protocol["configs"]) != sorted(CONFIG_FLAGS):
        found.append("configurations differ from A/B/C/D")
    if list(protocol["conditions"]) != list(cases_module.CONDITIONS):
        found.append("conditions differ from clean/attack")
    if int(protocol.get("repetitions", 0)) != 1:
        found.append("the main run has exactly one repetition")
    if protocol["scoring"]["scorer_version"] != SCORER_VERSION:
        found.append(f"scorer is {SCORER_VERSION}, protocol names {protocol['scoring']['scorer_version']}")
    for case_id in protocol["case_ids"]:
        if protocol["as_of"].get(case_id) != cases.case(case_id)["as_of"]:
            found.append(f"{case_id}: as_of differs from the attack set")
    if int(protocol["budget"]["model_calls"]) <= 0:
        found.append("budget must be positive")
    if protocol["retrieval"].get("index_mode") != "read_only":
        found.append("the main run reads the index only")
    return found


def identity_problems(protocol: dict, identities: dict[str, dict]) -> list[str]:
    """Worker identities (one per configuration) against the protocol."""

    found = []
    model = protocol["model"]
    for config, identity in identities.items():
        settings = protocol["configs"][config]
        if identity["ablation"] != {"prompt_guards_off": settings["prompt_guards_off"],
                                    "provenance_off": settings["provenance_off"]}:
            found.append(f"{config}: worker switches {identity['ablation']}")
        for key in ("provider", "model", "temperature", "base_url"):
            if str(identity["model"].get(key)) != str(model[key]):
                found.append(f"{config}: model {key} is {identity['model'].get(key)!r}, protocol {model[key]!r}")
        if identity["embedding"].get("model") != protocol["embedding"]["model"]:
            found.append(f"{config}: embedding model {identity['embedding'].get('model')!r}")
        if not identity.get("web_evidence_enabled") or not identity.get("web_analysis_active"):
            found.append(f"{config}: the natural workflow needs web evidence and news analysis on")
        if identity.get("llm_rerank") not in {"0", ""}:
            found.append(f"{config}: LLM rerank must be off")
    return found


def dependencies() -> dict:
    """Installed distributions: the key versions and a hash of the full list."""

    from importlib import metadata

    installed = sorted({(dist.metadata["Name"] or "").lower(): dist.version
                        for dist in metadata.distributions() if dist.metadata["Name"]}.items())
    listing = "\n".join(f"{name}=={version}" for name, version in installed)
    versions = dict(installed)
    return {"key": {name: versions.get(name, "not installed") for name in KEY_DISTRIBUTIONS},
            "count": len(installed), "sha256": hashlib.sha256(listing.encode("utf-8")).hexdigest()}
