"""Extension (26/10/2026): A/D on other answering models, with a stability subset."""

from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from agentfinx.evaluation.security import main_run, runner
from agentfinx.evaluation.security import protocol as protocol_module

ROOT = "benchmarks/security/generalization"
MODELS = ("gpt-oss-120b", "gpt-oss-20b", "gemma4-31b")


@pytest.fixture(scope="module", params=MODELS)
def registered(request):
    data, _, _ = protocol_module.load(f"{ROOT}/{request.param}.json")
    return data, protocol_module.attack_set(data)


def test_protocols_are_valid_and_name_their_model(registered):
    data, cases = registered
    assert protocol_module.problems(data, cases) == []
    assert protocol_module.exposure(data) == "natural"
    assert protocol_module.model_env(data) == {"OLLAMA_MODEL": data["model"]["model"]}
    chosen = data["stability"]["case_ids"]
    assert set(Counter(cases.case(c)["subtype"] for c in chosen).values()) == {2} and len(chosen) == 16


def test_every_model_shares_cases_schedule_and_product():
    loaded = [protocol_module.load(f"{ROOT}/{name}.json")[0] for name in MODELS]
    for key in ("case_ids", "stability", "schedule", "pinned", "configs", "conditions"):
        assert all(p[key] == loaded[0][key] for p in loaded), key
    assert len({p["model"]["model"] for p in loaded}) == 3


def test_pilots_use_dev_cases_only():
    for name in MODELS:
        data, _, _ = protocol_module.load(f"{ROOT}/{name}_pilot.json")
        cases = protocol_module.attack_set(data)
        assert protocol_module.problems(data, cases) == []
        assert set(data["case_ids"]) <= set(cases.case_ids(split="dev")) and "stability" not in data


def test_generalization_problems_catch_drift(registered):
    data, cases = registered
    broken = copy.deepcopy(data)
    broken["configs"]["C"] = {"prompt_guards_off": False, "provenance_off": True}
    broken["model"]["model"] = ""
    broken["stability"]["case_ids"] = broken["stability"]["case_ids"][:-1]
    broken["scoring"]["mode"] = "final"
    problems = " | ".join(protocol_module.problems(broken, cases))
    for expected in ("A and/or D", "not fully named", "every subtype equally", "deferred"):
        assert expected in problems
    pilot = copy.deepcopy(data)
    pilot["pilot"] = True
    assert "a pilot uses dev cases only" in protocol_module.problems(pilot, cases)


def test_schedule_adds_the_stability_runs(tmp_path):
    run = main_run.MainRun(tmp_path / "run", f"{ROOT}/gpt-oss-20b.json", preflight_fn=lambda: {"ok": True})
    schedule = run.schedule()
    main = [item for item in schedule if item["repetition"] == 0]
    extra = [item for item in schedule if item["repetition"] != 0]
    assert len(main) == 80 * 2 * 2 and len(extra) == 16 * 2 * 2 * 2
    assert schedule.index(extra[0]) == len(main)  # the stability runs come after the main pass
    assert {item["case_id"] for item in extra} == set(run.protocol["stability"]["case_ids"])
    assert len({main_run.cell_key(item) for item in schedule}) == len(schedule)
    assert run.extra_env == {"OLLAMA_MODEL": "gpt-oss:20b-cloud"}


def test_worker_environment_carries_the_model():
    env = runner.worker_env("D", {"OLLAMA_MODEL": "gemma4:31b-cloud"})
    assert env["OLLAMA_MODEL"] == "gemma4:31b-cloud"
    assert env["AGENTFINX_ABLATE_PROVENANCE"] == "0" and env["WEB_ANALYSIS_ENABLED"] == "1"


def test_main_protocol_is_untouched():
    data, sha, _ = protocol_module.load("benchmarks/security/main_protocol.json")
    assert protocol_module.model_env(data) == {}
    assert json.loads(json.dumps(data))["model"]["model"] == "gpt-oss:120b-cloud"
