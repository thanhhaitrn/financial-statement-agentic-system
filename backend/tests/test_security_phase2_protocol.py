"""Phase 2, Việc 0: the registration file and the rubric it points to."""

from __future__ import annotations

import copy

import pytest
from statsmodels.stats.proportion import proportion_confint

from agentfinx.evaluation.security import human_review, protocol

PATH = "benchmarks/security/protocol_phase2.json"


@pytest.fixture(scope="module")
def registered():
    data, sha, _ = protocol.load(PATH)
    return data, protocol.attack_set(data)


def test_phase2_protocol_is_valid(registered):
    data, cases = registered
    assert protocol.phase2_problems(data, cases) == []
    assert len(data["case_ids"]) == 80
    assert len(data["rubric"]["sha256_draft"]) == 64


def test_phase2_protocol_catches_drift(registered):
    data, cases = registered
    broken = copy.deepcopy(data)
    broken["case_ids"] = broken["case_ids"][:-1] + broken["case_ids"][:1]
    broken["experiments"]["evidence_matched_replay"]["configs"].pop("ApostG")
    broken["experiments"]["l5_structured_2x2"]["configs"]["E"]["provenance_off"] = "no"
    broken["budget"]["by_experiment"]["evidence_matched_replay"] = 2000
    problems = " | ".join(protocol.phase2_problems(broken, cases))
    for expected in ("case_ids", "evidence_matched_replay: configs", "config E", "budget"):
        assert expected in problems


def test_sample_matches_the_packet_code(registered):
    groups = registered[0]["experiments"]["human_validation"]["sample"]["groups"]
    assert {name: group["n"] for name, group in groups.items()} == human_review.GROUPS
    assert registered[0]["experiments"]["human_validation"]["sample"]["seed"] == human_review.SEED
    assert set(registered[0]["rubric"]["labels"]) == set(human_review.LABELS)


@pytest.mark.parametrize(("n", "upper"), [(80, 0.045), (30, 0.116)])
def test_zero_cell_bounds_in_the_protocol(n, upper):
    # "0/80 → ≤ 4,5%; 0/30 → ≤ 11,6%": two-sided 95% Clopper–Pearson.
    assert proportion_confint(0, n, alpha=0.05, method="beta")[1] == pytest.approx(upper, abs=5e-4)
