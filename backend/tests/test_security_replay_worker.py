"""Phase 2, Việc 2.2: A′ replay cells (Apre, Apost, ApostG) on extracted D evidence."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agentfinx.core.llm import client
from agentfinx.evaluation.security import baseline, main_run, runner, worker
from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import protocol as protocol_module
from agentfinx.evaluation.security import replay_worker as rw
from test_security_harness import CALLBACK, FakeChat

CASES = cases_module.load_cases("benchmarks/security/attack_set.json")
CASE = CASES.case("I1_HPG_001")
EVIDENCE = Path(__file__).resolve().parents[2] / "artifacts" / "eval" / "security" / "phase2" / \
    "replay-evidence-20261024"
needs_evidence = pytest.mark.skipif(not (EVIDENCE / "summary.json").exists(), reason="needs the extracted evidence")


def _fact(name, value, fact_id):
    return {"fact_id": fact_id, "item_name": f"{name} | Kỳ này", "value": value, "unit": "VND",
            "fiscal_year": "2025", "period_label": "Kỳ này", "source": "/x/storage/reports/bctc.md", "source_page": "8"}


def _record(condition="attack", article_text=None, facts=None):
    text = article_text if article_text is not None else "Mở đầu bài. " + CASE["attack_span"]
    facts = facts or {"KQKD": [_fact("Lợi nhuận gộp", "51.136.046.880", "f1"),
                               _fact("Doanh thu thuần", "177.661.419.641", "f2")]}
    news = {"status": "ok", "events": [{
        "event": "Hòa Phát cập nhật kinh doanh", "reported_on": "2025-09-17", "explanation": "",
        "related_metrics": [], "evidence": [{"quote": "giá trị=35.897.505.262 đồng",
                                             "source": {"publisher": "Bản tin", "published": "17/09/2025"}}],
        "confirmation_text": "chưa rõ nguồn xác nhận", "uncertainties": ["Không có doanh thu"]}]}
    return {"run_id": f"I1_HPG_001-D-{condition}-r0-a1", "case_id": "I1_HPG_001", "condition": condition,
            "question": CASE["question"],
            "pre": {"report_facts": facts, "articles": [{"title": "Tin", "publisher": "Bản tin",
                                                         "published_at": "2025-09-17", "source_url": "u",
                                                         "content": text}]},
            "post": {"worker_results": {**{table: {"table": table, "facts": rows} for table, rows in facts.items()},
                                        "web_news_analysis": news}}}


def test_apost_and_apostg_differ_only_in_the_label():
    record = _record()
    plain = rw.build_replay_messages(CASE["question"], record, "Apost")
    labelled = rw.build_replay_messages(CASE["question"], record, "ApostG")
    assert plain[0] == labelled[0] == ("system", baseline.SYSTEM)
    label = rw.untrusted_label()
    assert labelled[1][1].count(label) == 2
    assert labelled[1][1].replace(label, "") == plain[1][1]


def test_apre_keeps_a_prompt_and_every_fact():
    record = _record()
    messages = rw.build_replay_messages(CASE["question"], record, "Apre")
    expected = baseline.build_messages(CASE["question"], *rw.context(record, "Apre")[:2])
    assert messages == expected
    user = messages[1][1]
    assert "- Lợi nhuận gộp | Kỳ này: 51.136.046.880 VND (năm 2025; Kỳ này; nguồn: bctc.md, trang 8)" in user
    assert CASE["attack_span"] in user and "/x/storage" not in user
    assert rw.check_prompt(CASE, record, "Apre") == []
    assert rw.check_prompt(CASE, record, "Apost") == []


def test_long_tables_are_split_between_facts_never_cut():
    facts = {"KQKD": [_fact(f"Chỉ tiêu số {i} " + "x" * 60, f"{i}.000.000", f"f{i}") for i in range(60)]}
    chunks = rw.report_chunks(facts)
    assert len(chunks) > 1 and all(chunk["content_chars"] <= baseline.CHUNK_CHARS for chunk in chunks)
    assert all(chunk["content"].startswith("Bảng: KQKD") for chunk in chunks)
    joined = "\n".join(chunk["content"] for chunk in chunks)
    assert all(rw.fact_line(fact) in joined for fact in facts["KQKD"])


def test_precheck_refuses_a_cut_attack_span():
    record = _record(article_text="đệm " * 1100 + CASE["attack_span"])
    problems = rw.check_prompt(CASE, record, "Apre")
    assert "an article was cut at ARTICLE_CHARS" in problems
    assert "the attack span is in the evidence but not in the prompt" in problems


def test_runner_sends_replay_configs_to_the_replay_worker():
    assert runner.worker_module("Apost") == "agentfinx.evaluation.security.replay_worker"
    assert runner.worker_module("D") == "agentfinx.evaluation.security.worker"
    assert runner.config_env("ApostG") == {"AGENTFINX_ABLATE_PROMPT_GUARDS": "0", "AGENTFINX_ABLATE_PROVENANCE": "0"}
    assert runner.EXPOSURES["replay"]["WEB_ANALYSIS_ENABLED"] == "0"


def _spec(tmp_path, evidence_path, config="ApostG"):
    from agentfinx.core import experiment

    experiment.create_budget(tmp_path / "budget.json", 5)
    return {"run_id": f"I1_HPG_001-{config}-attack-r0-a1", "case_id": "I1_HPG_001", "config": config,
            "condition": "attack", "attempt": 1, "question": CASE["question"], "run_dir": str(tmp_path / "run"),
            "events_path": str(tmp_path / "events.jsonl"), "prompt_log_path": str(tmp_path / "prompt_log.jsonl"),
            "budget_path": str(tmp_path / "budget.json"), "evidence_path": str(evidence_path),
            "evidence_run_id": "I1_HPG_001-D-attack-r0-a1",
            "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest()}


@pytest.fixture()
def replay_identity(monkeypatch):
    ident = {"ablation": {"prompt_guards_off": False, "provenance_off": False}, "web_evidence_enabled": False,
             "web_analysis_active": False}
    monkeypatch.setattr(worker, "identity", lambda: dict(ident))
    return ident


def test_replay_worker_answers_once_and_keeps_the_prompt(tmp_path, monkeypatch, replay_identity):
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_record(), ensure_ascii=False), encoding="utf-8")
    model = FakeChat(responder=lambda messages, kwargs: ("Lợi nhuận gộp là 51.136.046.880 đồng.", []),
                     callbacks=[CALLBACK])
    monkeypatch.setattr(client, "build_llm", lambda **kwargs: model)
    result = rw.run(_spec(tmp_path, evidence_path))
    assert result["status"] == "ok" and result["model_calls"] == 1
    assert result["answer"].startswith("Lợi nhuận gộp")
    snapshot = json.loads((tmp_path / "run" / "snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["kind"] == "baseline_replay" and snapshot["evidence_sha256"] == result["evidence_sha256"]
    assert rw.untrusted_label() in snapshot["prompt"][1]["content"]
    assert len(model.sent) == 1
    requests = [json.loads(line) for line in (tmp_path / "prompt_log.jsonl").read_text().splitlines()
                if json.loads(line).get("event") == "request"]
    assert [r["step"] for r in requests] == ["baseline_replay"]


def test_replay_worker_refuses_other_evidence_or_switches(tmp_path, replay_identity):
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(_record()), encoding="utf-8")
    spec = _spec(tmp_path, evidence_path)
    spec["evidence_sha256"] = "0" * 64
    assert rw.run(spec)["status"] == "config_mismatch"
    replay_identity["ablation"] = {"prompt_guards_off": True, "provenance_off": False}
    spec = _spec(tmp_path, evidence_path)
    assert "flags" in rw.run(spec)["errors"][0]


def test_replay_protocol_is_valid_and_waits_for_g1(tmp_path):
    protocol, _, _ = protocol_module.load("benchmarks/security/replay_protocol.json")
    cases = protocol_module.attack_set(protocol)
    assert protocol_module.problems(protocol, cases) == []
    assert protocol_module.exposure(protocol) == "replay"
    phase2, _, _ = protocol_module.load("benchmarks/security/protocol_phase2.json")
    assert protocol["budget"]["model_calls"] == phase2["budget"]["by_experiment"]["evidence_matched_replay"]
    assert set(protocol["configs"]) == set(phase2["experiments"]["evidence_matched_replay"]["configs"])

    record = tmp_path / "g1.json"
    protocol = {**protocol, "requires": {"g1_record": str(record)}}
    assert "not frozen" in protocol_module.g1_problems(protocol)[0]
    rubric = protocol_module.benchmarks.resolve("benchmarks/security/RUBRIC_v3.4.md")
    record.write_text(json.dumps({"kind": "g1_freeze", "scorer": {"sha256": "a" * 64},
                                  "rubric": {"path": str(rubric),
                                             "sha256": hashlib.sha256(rubric.read_bytes()).hexdigest()}}))
    assert protocol_module.g1_problems(protocol) == []
    record.write_text(json.dumps({"kind": "g1_freeze", "scorer": {"sha256": "a" * 64},
                                  "rubric": {"path": str(rubric), "sha256": "b" * 64}}))
    assert protocol_module.g1_problems(protocol) == ["the rubric differs from the one frozen at G1"]

    broken = {**protocol, "configs": {**protocol["configs"], "Apost": {**protocol["configs"]["Apost"],
                                                                       "evidence": "pre"}}}
    assert "config Apost: not a replay configuration of the worker" in protocol_module.problems(broken, cases)


@needs_evidence
def test_every_extracted_prompt_passes_the_precheck():
    protocol, _, _ = protocol_module.load("benchmarks/security/replay_protocol.json")
    assert main_run.replay_evidence_problems(protocol) == []
    files = main_run.replay_evidence(protocol)
    assert len(files) == 160
    for (case_id, _), path in files.items():
        record = json.loads(path.read_text(encoding="utf-8"))
        for config in rw.REPLAY_CONFIGS:
            assert rw.check_prompt(CASES.case(case_id), record, config) == [], (path.name, config)
