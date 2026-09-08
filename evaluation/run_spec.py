"""Declarative description of one evaluation run.

Dataset, seed, cohort and thresholds are properties of a *run*, not of the
runner. Keeping them here means a shared runner never carries one dataset's
identity in its own logic, and a benchmark cohort has to be declared explicitly
instead of being recognised by a hardcoded id range.
"""
# Code note: Evaluation modules define reproducibility contracts; comments here explain gate semantics.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence


@dataclass(frozen=True)
class BenchmarkCohort:
    """The exact sample set a latency baseline is allowed to be published from."""

    name: str
    sample_ids: tuple[int, ...] = ()

    @property
    def expected_samples_n(self) -> int:
        return len(self.sample_ids)

    def matches(self, sample_ids: Sequence) -> bool:
        if not self.sample_ids:
            return False
        return list(sample_ids) == list(self.sample_ids)


@dataclass(frozen=True)
class EvaluationRunSpec:
    """Everything that identifies what is being evaluated, and against what."""

    name: str
    dataset_id: str = ""
    seed_file: str = ""
    predictions_file: str = ""
    facts_contract: str = ""
    cohort: Optional[BenchmarkCohort] = None
    thresholds: Mapping[str, float] = field(default_factory=dict)

    def cohort_report(self, sample_ids: Sequence) -> dict:
        """Cohort block for the report; unset means no baseline may be claimed."""

        if self.cohort is None:
            return {
                "name": "",
                "expected_samples_n": 0,
                "matches": False,
                "declared": False,
            }
        return {
            "name": self.cohort.name,
            "expected_samples_n": self.cohort.expected_samples_n,
            "matches": self.cohort.matches(sample_ids),
            "declared": True,
        }


def _id_range(start: int, stop_inclusive: int) -> tuple[int, ...]:
    return tuple(range(start, stop_inclusive + 1))


# APEC stays available as a named regression profile. It is no longer the
# implicit default of any shared runner: a run must ask for it by name.
APEC_Q181_250 = EvaluationRunSpec(
    name="apec_q181_250",
    dataset_id="apec",
    seed_file="dau_tu_APEC_ragas_seed.json",
    predictions_file="ragas_runs/apec_predictions.json",
    facts_contract="tests/fixtures/apec_q211_250_factual_facts.json",
    cohort=BenchmarkCohort(name="apec_q181_250", sample_ids=_id_range(181, 250)),
    thresholds={"factual_recall": 0.95},
)

UNSPECIFIED = EvaluationRunSpec(name="unspecified")

EVALUATION_PROFILES: dict[str, EvaluationRunSpec] = {
    spec.name: spec for spec in (APEC_Q181_250,)
}


def profile_names() -> Iterable[str]:
    return sorted(EVALUATION_PROFILES)


def get_run_spec(name: str) -> EvaluationRunSpec:
    """Look up a declared profile; an unknown name is a run-configuration error."""

    key = str(name or "").strip()
    if not key:
        return UNSPECIFIED
    try:
        return EVALUATION_PROFILES[key]
    except KeyError:
        raise KeyError(
            f"Unknown evaluation profile {key!r}; declared profiles: "
            f"{sorted(EVALUATION_PROFILES)}"
        ) from None
