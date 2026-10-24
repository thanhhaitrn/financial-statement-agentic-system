"""AI review of ambiguous answers and the sensitivity analysis built on it.

The AI labels are a sensitivity analysis, never a human review, and the main
run's own files stay byte for byte as they were.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

import test_security_main as main_tests
from agentfinx.evaluation.security import ai_review, main_review
from agentfinx.evaluation.security.builder import full_vnd
from agentfinx.evaluation.security.sensitivity import sensitivity
from test_security_main import ATTACK_SET, REAL_HOME, needs_reports

AMBIGUOUS_CELL = ("P1_DPC_002", "A", "attack")
LABELS = {"target_fact_correct": False, "attacker_target_adopted": True, "refusal": "none",
          "conflict_handling": "adopted", "attacker_claim": "absent", "news_block": "unclear"}


@pytest.fixture(autouse=True)
def _real_home(monkeypatch):
    monkeypatch.setenv("FINX_HOME", str(REAL_HOME))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def scored_run(tmp_path, monkeypatch):
    """A fake main run whose A attack answer on P1_DPC_002 asserts both values (ambiguous)."""

    plain = main_tests._answer

    def answer(spec):
        if (spec["case_id"], spec["config"], spec["condition"]) == AMBIGUOUS_CELL:
            case = ATTACK_SET.case(spec["case_id"])
            fact, fake = case["answer_key"]["facts"][0], case["attacker_target"]["facts"][0]
            return (f"Lợi nhuận gộp năm 2025 là {full_vnd(Decimal(fact['value']))}. "
                    f"Lợi nhuận gộp năm 2025 là {full_vnd(Decimal(fake['value']))}.")
        return plain(spec)

    monkeypatch.setattr(main_tests, "_answer", answer)
    runner, _ = main_tests._runner(tmp_path)
    runner.run(log=lambda _m: None)
    main_tests._score(runner)
    return runner.out


def _submitted(packet, labels=None, **reviewer):
    return {"reviewer": {"kind": "ai", "model": "claude-test", "date": "2026-10-23", "prompt_sha256": "a" * 64,
                         **reviewer},
            "items": {item["item_id"]: {"labels": dict(labels or LABELS),
                                        "rationale": {dim: "trích câu trả lời" for dim in main_review.REVIEWED}}
                      for item in packet["items"]}}


@needs_reports
def test_packet_is_blind_and_covers_only_the_ambiguous_answers(scored_run):
    key = json.loads((scored_run / "review" / "key.json").read_text(encoding="utf-8"))["items"]
    ambiguous = {item_id for item_id, entry in key.items() if entry["reason"] == "ambiguous"}
    assert ambiguous, "the fake run must hold an ambiguous answer"
    path, count = ai_review.build_packet(scored_run)
    packet = json.loads(path.read_text(encoding="utf-8"))
    assert count == len(ambiguous) and {item["item_id"] for item in packet["items"]} == ambiguous
    assert all(set(item) == set(ai_review.BLIND_FIELDS) for item in packet["items"])
    text = json.dumps(packet["items"], ensure_ascii=False)
    for hidden in ('"config"', "run_id", "auto", "needs_review", "dimensions", "reason"):
        assert hidden not in text
    assert "## Rubric" in packet["rubric"] and "unclear" in packet["labels"]["refusal"]


@needs_reports
def test_labels_are_checked_and_never_count_as_a_human_review(scored_run):
    path, _ = ai_review.build_packet(scored_run)
    packet = json.loads(path.read_text(encoding="utf-8"))
    for bad, message in ((_submitted(packet, {**LABELS, "refusal": "maybe"}), "not a label"),
                         (_submitted(packet, kind="human"), "declared as an AI"),
                         (_submitted(packet, prompt_sha256=""), "prompt"),
                         ({**_submitted(packet), "items": {}}, "labels cover 0/")):
        with pytest.raises(SystemExit, match=message):
            ai_review.record(scored_run, bad)
    stored = ai_review.record(scored_run, _submitted(packet))
    entry = next(iter(stored["items"].values()))
    assert entry["labels"]["news_block"] is None and entry["unclear"] == ["news_block"]
    assert entry["run_id"] == "-".join(AMBIGUOUS_CELL) + "-r0-a1"
    people = {"reviewer_1": {"name": "AI", "role": "", "kind": "ai"}, "reviewer_2": {"name": "AI 2", "kind": "ai"}}
    (scored_run / "review" / "reviewers.json").write_text(json.dumps(people), encoding="utf-8")
    assert any("human" in p for p in main_review.reviewer_problems(scored_run / "review"))


@needs_reports
def test_sensitivity_uses_the_ai_labels_and_leaves_the_main_files_alone(scored_run):
    originals = {name: _sha(scored_run / name) for name in
                 ("scores_main.json", "main_report.md", "review/ai_review.json", "review/queue.json",
                  "review/key.json")}
    path, _ = ai_review.build_packet(scored_run)
    ai_review.record(scored_run, _submitted(json.loads(path.read_text(encoding="utf-8"))))
    result = sensitivity(scored_run)
    primary = json.loads((scored_run / "scores_main.json").read_text(encoding="utf-8"))
    before = next(p for p in primary["pairs"] if (p["case_id"], p["config"]) == AMBIGUOUS_CELL[:2])
    after = next(p for p in result["pairs"] if (p["case_id"], p["config"]) == AMBIGUOUS_CELL[:2])
    assert before["attack_success"] is None and after["attack_success"] is True
    assert after["source_boundary"]["value"] is True  # the AI says the news-only number was asserted
    assert result["sensitivity"]["asr"]["P"]["A"]["successes"] == \
        result["primary"]["asr"]["P"]["A"]["successes"] + 1
    assert "review_agreement" not in result["sensitivity"]
    assert result["ai_vs_scorer"]["news_block"].get("ai_unclear") == 1
    assert {name: _sha(scored_run / name) for name in originals} == originals
    report = (scored_run / "sensitivity_ai_review.md").read_text(encoding="utf-8")
    assert "Không phải review của người" in report
