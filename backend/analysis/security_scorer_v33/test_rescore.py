import pytest

from .rescore import changed_labels, check_destination
from agentfinx.evaluation.security.main_report import _automatic


def test_existing_outputs_and_original_runs_are_protected(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ValueError, match="new directory"):
        check_destination(source, source)
    for name in ("runs", "review"):
        with pytest.raises(ValueError, match="original"):
            check_destination(source, source / name / "nested")
    check_destination(source, source / "fresh-rescore")


def test_change_log_keeps_old_new_evidence_and_answer_wide_unknowns():
    old = {"run_id": "case-D-attack-r0-a1", "case_id": "case", "config": "D", "condition": "attack",
           "score": {"dimensions": {"attacker_target_adopted": {"value": True, "evidence": "quote",
                                                                 "rule": "old"}}, "review_reasons": []},
           "final": {"attacker_target_adopted": True}}
    new = {**old, "score": {"needs_review": True, "review_reasons": ["ambiguous context"],
                           "dimensions": {"attacker_target_adopted": {"value": False, "evidence": "quote",
                                                                        "rule": "new"}}}}
    new["final"], pending = _automatic(new)
    assert len(pending) == 6 and all(v is None for v in new["final"].values())
    changes = changed_labels(old, new)
    assert changes["automatic_label_changes"][0]["old"]["rule"] == "old"
    assert changes["automatic_label_changes"][0]["new"]["value"] is False
    assert changes["final_label_changes"]["attacker_target_adopted"] == {"old": True, "new": None}
    assert old["final"]["attacker_target_adopted"] is True
