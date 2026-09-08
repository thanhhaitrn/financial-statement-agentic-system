"""Dataset identity belongs to the run spec, not to the shared runner."""

import pytest

from evaluation.run_spec import (
    APEC_Q181_250,
    UNSPECIFIED,
    BenchmarkCohort,
    EvaluationRunSpec,
    get_run_spec,
)
from ragas_eval_runner import build_latency_contract


def _predictions(ids):
    return [
        {
            "id": sample_id,
            "runtime": 1.0,
            "tokens": 10,
            "latency_breakdown": {
                "retrieval_local_ms": 10.0,
                "model_generation_ms": 20.0,
            },
        }
        for sample_id in ids
    ]


def _contract(ids, run_spec):
    return build_latency_contract(
        predictions=_predictions(ids),
        scores=[],
        eval_error="",
        judge_duration_ms=1,
        clean_environment_attested=True,
        run_spec=run_spec,
    )


def test_an_undeclared_cohort_can_never_claim_a_baseline():
    contract = _contract(range(181, 251), UNSPECIFIED)

    assert contract["valid"] is True
    assert contract["baseline_eligible"] is False
    assert contract["benchmark_cohort"]["declared"] is False
    assert contract["benchmark_cohort"]["name"] == ""


def test_declared_cohort_gates_the_baseline_on_the_exact_sample_set():
    eligible = _contract(range(181, 251), APEC_Q181_250)
    assert eligible["baseline_eligible"] is True
    assert eligible["benchmark_cohort"] == {
        "name": "apec_q181_250",
        "expected_samples_n": 70,
        "matches": True,
        "declared": True,
    }

    partial = _contract(range(181, 250), APEC_Q181_250)
    assert partial["baseline_eligible"] is False
    assert partial["benchmark_cohort"]["matches"] is False


def test_another_dataset_reuses_the_runner_with_its_own_cohort():
    spec = EvaluationRunSpec(
        name="vnm_200",
        dataset_id="vnm",
        cohort=BenchmarkCohort(name="vnm_200", sample_ids=tuple(range(1, 201))),
    )
    contract = _contract(range(1, 201), spec)

    assert contract["baseline_eligible"] is True
    assert contract["benchmark_cohort"]["name"] == "vnm_200"
    # The APEC profile is a regression profile, not a rule the runner applies.
    assert _contract(range(1, 201), APEC_Q181_250)["baseline_eligible"] is False


def test_unknown_profile_is_a_configuration_error():
    assert get_run_spec("") is UNSPECIFIED
    assert get_run_spec("apec_q181_250") is APEC_Q181_250
    with pytest.raises(KeyError):
        get_run_spec("apec_q999")
