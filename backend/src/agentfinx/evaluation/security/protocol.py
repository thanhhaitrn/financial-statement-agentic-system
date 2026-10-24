"""The experiment protocols: the main run (``benchmarks/security/main_protocol.json``)
and the controlled-exposure experiment (``controlled_protocol.json``, ``kind:
"controlled"``).

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


def kind(protocol: dict) -> str:
    """``main`` (the registered 640 runs), ``controlled`` (a separate
    controlled-exposure experiment on the same cases and product), ``replay``
    (phase 2's evidence-matched replay) or ``phase2`` (the registration)."""

    return protocol.get("kind", "main")


def exposure(protocol: dict) -> str:
    return {"controlled": "controlled", "replay": "replay", "l5": "structured"}.get(kind(protocol), "natural")


def numeric_test_cases(cases: cases_module.CaseFile) -> list[str]:
    return sorted(case_id for case_id in cases.case_ids(split="test")
                  if cases.case(case_id)["attacker_target"]["kind"] != "refusal")


def controlled_problems(protocol: dict, cases: cases_module.CaseFile) -> list[str]:
    """A controlled-exposure protocol: the numeric test cases, attack only,
    the attack articles placed at the workers' fact input as WEB facts, the
    product and index pinned to the main run's, scoring deferred."""

    import re

    from agentfinx.evaluation.security.runner import CONTROLLED_ENV
    from agentfinx.evaluation.security.worker import CONFIG_FLAGS

    found = []
    if protocol["attack_set"]["sha256"] != cases.sha256:
        found.append("attack set differs from the protocol's")
    if sorted(protocol["case_ids"]) != numeric_test_cases(cases) or \
            len(set(protocol["case_ids"])) != len(protocol["case_ids"]):
        found.append("case ids differ from the numeric-target test cases")
    if not protocol["configs"]:
        found.append("no configuration")
    for config, settings in protocol["configs"].items():
        flags = CONFIG_FLAGS.get(config)
        if config == "A" or flags is None or {key: settings.get(key) for key in flags} != flags:
            found.append(f"config {config}: not an AgentFinX configuration with the worker's switches")
    if list(protocol["conditions"]) != ["attack"]:
        found.append("a controlled-exposure run uses the attack condition only")
    if int(protocol.get("repetitions", 0)) != 1:
        found.append("exactly one repetition")
    if protocol.get("exposure") != "controlled" or protocol.get("environment") != CONTROLLED_ENV:
        found.append("exposure and environment must be the controlled ones")
    for case_id in protocol["case_ids"]:
        if protocol["as_of"].get(case_id) != cases.case(case_id)["as_of"]:
            found.append(f"{case_id}: as_of differs from the attack set")
    if int(protocol["budget"]["model_calls"]) <= 0:
        found.append("budget must be positive")
    if protocol["retrieval"].get("index_mode") != "read_only":
        found.append("the run reads the index only")
    pinned = protocol.get("pinned") or {}
    if not re.fullmatch(r"[0-9a-f]{64}", str(pinned.get("product_code_sha256", ""))):
        found.append("the product version is not pinned")
    for dataset_id in cases.dataset_ids(protocol["case_ids"]):
        if not (pinned.get("datasets") or {}).get(dataset_id, {}).get("index_generation"):
            found.append(f"index of {dataset_id} is not pinned")
    if (protocol.get("scoring") or {}).get("mode") != "deferred":
        found.append("scoring of a controlled run is deferred to a recorded scorer version")
    return found


def replay_problems(protocol: dict, cases: cases_module.CaseFile) -> list[str]:
    """An evidence-matched replay run (phase 2, Việc 2): the 80 test cases,
    clean and attack, the A′ configurations on evidence extracted from the
    main run's D logs, the product pinned to the main run's, scoring deferred
    to the scorer frozen at G1."""

    import re

    from agentfinx.evaluation.security.replay_worker import REPLAY_CONFIG_FLAGS, REPLAY_CONFIGS
    from agentfinx.evaluation.security.runner import REPLAY_ENV

    found = []
    if protocol["attack_set"]["sha256"] != cases.sha256:
        found.append("attack set differs from the protocol's")
    correction = protocol.get("correction_of")
    if correction:
        # A correction run re-asks only the listed cells of an earlier replay run.
        cells = protocol.get("cells") or []
        if not cells or not correction.get("run") or not correction.get("reason"):
            found.append("a correction names its run, its reason and its cells")
        if not set(protocol["case_ids"]) <= set(cases.case_ids(split="test")):
            found.append("case ids must be test cases")
        for cell in cells:
            if cell.get("case_id") not in protocol["case_ids"] or cell.get("config") not in protocol["configs"] \
                    or cell.get("condition") not in protocol["conditions"]:
                found.append(f"cell {cell} is outside the protocol's cases, configs or conditions")
    elif sorted(protocol["case_ids"]) != sorted(cases.case_ids(split="test")) or \
            len(set(protocol["case_ids"])) != len(protocol["case_ids"]):
        found.append("case ids differ from the test split")
    if not protocol["configs"] or (not correction and not {"Apre", "Apost"} <= set(protocol["configs"])):
        found.append("a replay runs at least Apre and Apost")
    for config, settings in protocol["configs"].items():
        flags = REPLAY_CONFIG_FLAGS.get(config)
        if flags is None or {key: settings.get(key) for key in flags} != flags \
                or settings.get("pipeline") != "baseline_replay" \
                or {key: settings.get(key) for key in REPLAY_CONFIGS[config]} != REPLAY_CONFIGS[config]:
            found.append(f"config {config}: not a replay configuration of the worker")
    if list(protocol["conditions"]) != list(cases_module.CONDITIONS):
        found.append("conditions differ from clean/attack")
    if int(protocol.get("repetitions", 0)) != 1:
        found.append("exactly one repetition")
    if protocol.get("exposure") != "replay" or protocol.get("environment") != REPLAY_ENV:
        found.append("exposure and environment must be the replay ones")
    if int(protocol["budget"]["model_calls"]) <= 0:
        found.append("budget must be positive")
    pinned = protocol.get("pinned") or {}
    if not re.fullmatch(r"[0-9a-f]{64}", str(pinned.get("product_code_sha256", ""))):
        found.append("the product version is not pinned")
    evidence = protocol.get("evidence") or {}
    if not evidence.get("dir") or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("summary_sha256", ""))):
        found.append("the extracted evidence is not pinned")
    if not (protocol.get("requires") or {}).get("g1_record"):
        found.append("the G1 record is not named")
    if (protocol.get("scoring") or {}).get("mode") != "deferred":
        found.append("scoring of a replay run is deferred to the scorer frozen at G1")
    return found


def g1_problems(protocol: dict) -> list[str]:
    """Gate G1: the frozen rubric (and scorer) v3.4 recorded before any new run."""

    path = benchmarks.resolve((protocol.get("requires") or {}).get("g1_record", ""))
    if not path.is_file():
        return [f"no G1 record at {path}: rubric and scorer v3.4 are not frozen"]
    record = json.loads(path.read_text(encoding="utf-8"))
    found = []
    if record.get("kind") != "g1_freeze":
        found.append("the G1 record is not a g1_freeze")
    rubric = record.get("rubric") or {}
    rubric_path = benchmarks.resolve(rubric.get("path", ""))
    if not rubric.get("path") or not rubric_path.is_file() or \
            hashlib.sha256(rubric_path.read_bytes()).hexdigest() != rubric.get("sha256"):
        found.append("the rubric differs from the one frozen at G1")
    if len(str((record.get("scorer") or {}).get("sha256", ""))) != 64:
        found.append("the G1 record has no scorer hash")
    return found


PHASE2_EXPERIMENTS = {"human_validation": None, "evidence_matched_replay": {"Apre", "Apost", "ApostG"},
                      "l5_structured_2x2": {"B", "C", "E", "D"}}


def phase2_problems(protocol: dict, cases: cases_module.CaseFile) -> list[str]:
    """The phase-2 registration (``kind: "phase2"``): the 80 test cases of the
    frozen attack set, the three gates, the three experiments and a budget
    whose parts fit its total. It registers the runs; each run still gets its
    own protocol and directory."""

    problems = []
    if kind(protocol) != "phase2":
        problems.append("kind must be phase2")
    if protocol.get("attack_set", {}).get("sha256") != cases.sha256:
        problems.append("attack_set sha256 does not match the case file")
    ids = protocol.get("case_ids") or []
    if len(ids) != len(set(ids)) or set(ids) != set(cases.case_ids(split="test")):
        problems.append("case_ids must be the test split, each once")
    if set(protocol.get("gates") or {}) != {"G1", "G2", "G3"}:
        problems.append("gates must be G1, G2 and G3")
    experiments = protocol.get("experiments") or {}
    if set(experiments) != set(PHASE2_EXPERIMENTS):
        problems.append(f"experiments must be {sorted(PHASE2_EXPERIMENTS)}")
    for name, configs in PHASE2_EXPERIMENTS.items():
        if configs is not None and set((experiments.get(name) or {}).get("configs") or {}) != configs:
            problems.append(f"{name}: configs must be {sorted(configs)}")
    for name, flags in ((experiments.get("l5_structured_2x2") or {}).get("configs") or {}).items():
        if set(flags) != {"prompt_guards_off", "provenance_off"} or not all(isinstance(v, bool) for v in flags.values()):
            problems.append(f"l5_structured_2x2 config {name}: two boolean switches")
    sample = (experiments.get("human_validation") or {}).get("sample") or {}
    if sum(group["n"] for group in (sample.get("groups") or {}).values()) != sample.get("total"):
        problems.append("human_validation: the sample groups do not add up to the total")
    budget = protocol.get("budget") or {}
    fixed = [value for value in (budget.get("by_experiment") or {}).values() if isinstance(value, int)]
    if not isinstance(budget.get("model_calls_total"), int) or sum(fixed) > budget["model_calls_total"]:
        problems.append("budget: the experiments' caps exceed the total")
    rubric = protocol.get("rubric") or {}
    if not rubric.get("path") or not benchmarks.resolve(rubric["path"]).is_file():
        problems.append("rubric file missing")
    return problems


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

    if kind(protocol) == "controlled":
        return controlled_problems(protocol, cases)
    if kind(protocol) == "replay":
        return replay_problems(protocol, cases)
    if kind(protocol) == "l5":
        from agentfinx.evaluation.security.l5 import l5_problems

        return l5_problems(protocol, cases)
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
    if sorted(protocol["configs"]) != ["A", "B", "C", "D"]:
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
        if exposure(protocol) in ("controlled", "replay", "structured"):
            if identity.get("web_evidence_enabled") or identity.get("web_analysis_active"):
                found.append(f"{config}: {exposure(protocol)} exposure needs the product's web path and news "
                             "analysis off")
        elif not identity.get("web_evidence_enabled") or not identity.get("web_analysis_active"):
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
