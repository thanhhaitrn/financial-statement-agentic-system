"""The controlled-exposure experiment for L5: its protocol, preflight and run.

No model call: the worker is the fake of ``test_security_main``.
"""

from __future__ import annotations

import copy
import json

import pytest

from agentfinx.evaluation.security import cli, main_run, runner
from agentfinx.evaluation.security import freeze as freeze_module
from agentfinx.evaluation.security import protocol as protocol_module
from test_security_main import ATTACK_SET, REAL_HOME, FakeWorker, needs_reports

CONTROLLED, CONTROLLED_SHA, CONTROLLED_PATH = protocol_module.load("benchmarks/security/controlled_protocol.json")
MAIN, _, _ = protocol_module.load()


@pytest.fixture(autouse=True)
def _real_home(monkeypatch):
    monkeypatch.setenv("FINX_HOME", str(REAL_HOME))


def _identity(config, *, web=False):
    return {"ablation": {key: CONTROLLED["configs"][config][key] for key in ("prompt_guards_off", "provenance_off")},
            "runtime_policy_fingerprint": "policy", "module_hashes": {}, "llm_rerank": "0",
            "model": {key: CONTROLLED["model"][key] for key in ("provider", "model", "temperature", "base_url")},
            "embedding": {"model": "bge-m3"}, "web_evidence_enabled": web, "web_analysis_active": web}


def test_controlled_protocol_isolates_l5_on_the_numeric_cases():
    assert protocol_module.problems(CONTROLLED, ATTACK_SET) == []
    assert CONTROLLED["case_ids"] == protocol_module.numeric_test_cases(ATTACK_SET) and len(CONTROLLED["case_ids"]) == 72
    assert all(ATTACK_SET.case(case_id)["subtype"] != "I3" for case_id in CONTROLLED["case_ids"])
    assert sorted(CONTROLLED["configs"]) == ["C", "D"] and CONTROLLED["conditions"] == ["attack"]
    # C and D differ in the provenance switch only.
    c, d = CONTROLLED["configs"]["C"], CONTROLLED["configs"]["D"]
    assert (c["prompt_guards_off"], c["provenance_off"], d["prompt_guards_off"], d["provenance_off"]) == \
        (False, True, False, False)
    assert CONTROLLED["environment"] == runner.CONTROLLED_ENV and CONTROLLED["scoring"]["mode"] == "deferred"
    for mutate, message in (
            (lambda p: p["configs"].update(A=MAIN["configs"]["A"]), "config A"),
            (lambda p: p.update(conditions=["clean", "attack"]), "attack condition only"),
            (lambda p: p.update(case_ids=p["case_ids"][1:]), "numeric-target"),
            (lambda p: p["pinned"].update(product_code_sha256=""), "product version"),
            (lambda p: p["scoring"].update(mode="immediate"), "deferred"),
            (lambda p: p.update(environment=runner.PILOT_ENV), "controlled ones")):
        broken = copy.deepcopy(CONTROLLED)
        mutate(broken)
        assert any(message in problem for problem in protocol_module.problems(broken, ATTACK_SET)), message
    # The main protocol keeps its own checks.
    assert protocol_module.problems(MAIN, ATTACK_SET) == []


def test_identity_checks_follow_the_exposure():
    good = {config: _identity(config) for config in ("C", "D")}
    assert protocol_module.identity_problems(CONTROLLED, good) == []
    news_on = {config: _identity(config, web=True) for config in ("C", "D")}
    assert any("controlled exposure" in p for p in protocol_module.identity_problems(CONTROLLED, news_on))
    natural = {config: {**_identity(config), "ablation": {key: MAIN["configs"][config][key] for key in
                                                          ("prompt_guards_off", "provenance_off")}}
               for config in ("C", "D")}
    assert any("natural workflow" in p for p in protocol_module.identity_problems(MAIN, natural))


@needs_reports
def test_controlled_preflight_pins_the_product_and_the_index(monkeypatch):
    identities = {config: _identity(config) for config in ("C", "D")}
    pinned = copy.deepcopy(CONTROLLED["pinned"]["datasets"])
    monkeypatch.setattr(freeze_module, "product_code_sha256", lambda: CONTROLLED["pinned"]["product_code_sha256"])
    check = main_run.preflight(CONTROLLED_PATH, identities=identities, datasets=pinned)
    assert check["ok"], check["problems"]

    moved = copy.deepcopy(pinned)
    moved["apec"]["index_generation"] = "another"
    assert any("apec: differs from the pinned" in p for p in
               main_run.preflight(CONTROLLED_PATH, identities=identities, datasets=moved)["problems"])
    news_on = {config: _identity(config, web=True) for config in ("C", "D")}
    assert not main_run.preflight(CONTROLLED_PATH, identities=news_on, datasets=pinned)["ok"]
    monkeypatch.setattr(freeze_module, "product_code_sha256", lambda: "0" * 64)
    assert any(p.startswith("product:") for p in
               main_run.preflight(CONTROLLED_PATH, identities=identities, datasets=pinned)["problems"])


def _small_protocol(tmp_path, case_ids):
    protocol = copy.deepcopy(CONTROLLED)
    protocol["case_ids"] = case_ids
    path = tmp_path / "controlled.json"
    path.write_text(json.dumps(protocol, ensure_ascii=False), encoding="utf-8")
    return path


def _stub_preflight(case_ids):
    def check():
        return {"ok": True, "problems": [], "identities": {config: _identity(config) for config in ("C", "D")},
                "attack_set_sha256": ATTACK_SET.sha256,
                "datasets": {d: CONTROLLED["pinned"]["datasets"][d] for d in ATTACK_SET.dataset_ids(case_ids)},
                "dependencies": {"sha256": "deps"}}
    return check


def test_controlled_run_places_news_at_the_worker_input_and_survives_a_scorer_change(tmp_path, monkeypatch):
    case_ids = CONTROLLED["case_ids"][:2]
    path = _small_protocol(tmp_path, case_ids)
    run = main_run.MainRun(tmp_path / "out", path, preflight_fn=_stub_preflight(case_ids))
    worker = FakeWorker(run)
    seen = []

    def launch(spec):
        seen.append(spec)
        return worker(spec)

    run._launch = launch
    status = run.run(log=lambda _m: None)
    assert status["valid_cells"] == 4 and status["missing"] == []
    assert {spec["exposure"] for spec in seen} == {"controlled"} and {spec["condition"] for spec in seen} == {"attack"}
    assert {spec["config"] for spec in seen} == {"C", "D"} and all(spec["index_mode"] == "read_only" for spec in seen)
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "controlled" and manifest["exposure"] == "controlled"
    payload = manifest["fingerprint"]["payload"]
    assert "scorer_version" not in payload
    assert payload["product_code_sha256"] == CONTROLLED["pinned"]["product_code_sha256"]
    assert runner.worker_env("C", exposure="controlled")["WEB_ANALYSIS_ENABLED"] == "0"

    # A new scorer version does not block resuming the data collection.
    monkeypatch.setattr(main_run, "SCORER_VERSION", "security-scorer-v9")
    again = main_run.MainRun(tmp_path / "out", path, preflight_fn=_stub_preflight(case_ids))
    again._launch = launch
    assert again.run(log=lambda _m: None)["valid_cells"] == 4 and len(seen) == 4


def test_run_command_does_not_score_a_deferred_protocol(tmp_path, monkeypatch):
    from agentfinx.evaluation.security import main_report

    class StubRun:
        protocol = CONTROLLED

        def __init__(self, *args, **kwargs):
            pass

        def run(self, log):
            return {"stopped": "", "valid_cells": 144, "missing": []}

    def no_scoring(*args, **kwargs):
        raise AssertionError("a deferred protocol must not be scored at the end of the run")

    monkeypatch.setattr(main_run, "MainRun", StubRun)
    monkeypatch.setattr(main_report, "score_main", no_scoring)
    monkeypatch.setattr(cli, "_keys", lambda args: [])
    assert cli.main(["run", "--protocol", str(CONTROLLED_PATH), "--out", str(tmp_path / "x"), "--allow-live"]) == 0
