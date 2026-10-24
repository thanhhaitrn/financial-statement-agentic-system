"""The pre-registered statistics of the main experiment (Bước 4).

- A proportion is reported with a Wilson 95% interval only when every case of
  its denominator is determined; with ``u`` undetermined cases it is reported
  as the plausible range ``[s/N, (s+u)/N]`` and the count missing. Errors are
  never counted as failed attacks and the denominator never shrinks quietly.
- The six primary comparisons (A–D, B–C, C–D for I and for P) pair the
  attack outcome by case id and use the exact McNemar test; raw and
  Holm-adjusted p values are both reported. A comparison without a single
  determined pair has no p value (NA), which never reads as "no difference".
- Case-level intervals and tests do not model the dependence between cases
  of one lineage; :func:`leave_one_lineage_out` is the sensitivity check.

statsmodels does the interval, the test and the adjustment
(``proportion_confint(..., method="wilson")``, ``mcnemar(..., exact=True)``,
``multipletests(..., method="holm")``).
"""

from __future__ import annotations

from collections import Counter

PRIMARY_PAIRS = (("A", "D"), ("B", "C"), ("C", "D"))
FAMILIES = ("I", "P")
ALPHA = 0.05


def wilson(successes: int, n: int) -> tuple[float, float] | None:
    if n <= 0:
        return None
    from statsmodels.stats.proportion import proportion_confint

    low, high = proportion_confint(successes, n, alpha=ALPHA, method="wilson")
    return float(low), float(high)


def proportion(values: list) -> dict:
    """``values`` holds True, False or None (undetermined) for every case of
    the denominator. The rate is fixed only when nothing is undetermined."""

    unexpected = [value for value in values if value not in (True, False, None) or isinstance(value, int)
                  and not isinstance(value, bool)]
    if unexpected:
        raise ValueError(f"a proportion takes True, False or None, not {unexpected[0]!r}")
    n = len(values)
    s = sum(1 for value in values if value is True)
    u = sum(1 for value in values if value is None)
    out = {"n": n, "successes": s, "undetermined": u, "range": [s / n, (s + u) / n] if n else None}
    if u == 0 and n:
        out.update(rate=s / n, wilson95=wilson(s, n), fully_determined=True)
    else:
        out.update(rate=None, wilson95=None, fully_determined=False)
    return out


def mcnemar_exact(first: dict, second: dict) -> dict:
    """``first``/``second``: case id → True/False/None. Pairs with an
    undetermined side are counted, not tested."""

    ids = sorted(set(first) | set(second))
    paired = [(first.get(i), second.get(i)) for i in ids]
    usable = [(a, b) for a, b in paired if a is not None and b is not None]
    table = Counter((a, b) for a, b in usable)
    both, only_first = table[(True, True)], table[(True, False)]
    only_second, neither = table[(False, True)], table[(False, False)]
    if not usable:
        p_value = None  # no pair to compare: not "no difference"
    elif only_first + only_second == 0:
        p_value = 1.0
    else:
        from statsmodels.stats.contingency_tables import mcnemar

        p_value = float(mcnemar([[both, only_first], [only_second, neither]], exact=True).pvalue)
    n = len(usable)
    return {"pairs": n, "undetermined_pairs": len(paired) - n,
            "table": {"both": both, "first_only": only_first, "second_only": only_second, "neither": neither},
            "rate_first": (both + only_first) / n if n else None,
            "rate_second": (both + only_second) / n if n else None,
            "difference": (only_first - only_second) / n if n else None,
            "p_value": p_value}


def holm(p_values: list[float | None]) -> list[float | None]:
    """Holm over the tests that have a p value; a test without one stays None."""

    present = [index for index, value in enumerate(p_values) if value is not None]
    adjusted: list[float | None] = [None] * len(p_values)
    if not present:
        return adjusted
    from statsmodels.stats.multitest import multipletests

    for index, value in zip(present, multipletests([p_values[i] for i in present], alpha=ALPHA,
                                                    method="holm")[1], strict=True):
        adjusted[index] = float(value)
    return adjusted


def primary_comparisons(success: dict[str, dict[str, dict]]) -> list[dict]:
    """``success[family][config]`` maps case id → attack_success. Six tests,
    Holm-adjusted together."""

    rows = []
    for family in FAMILIES:
        for first, second in PRIMARY_PAIRS:
            result = mcnemar_exact(success.get(family, {}).get(first, {}), success.get(family, {}).get(second, {}))
            rows.append({"family": family, "comparison": f"{first}–{second}", **result})
    for row, adjusted in zip(rows, holm([row["p_value"] for row in rows]), strict=True):
        row["p_holm"] = adjusted
    return rows


def leave_one_lineage_out(success: dict[str, dict[str, dict]], lineage_of: dict[str, str]) -> list[dict]:
    """ASR and the paired test again with each lineage of a family left out."""

    rows = []
    for family in FAMILIES:
        by_config = success.get(family, {})
        lineages = sorted({lineage_of[case_id] for values in by_config.values() for case_id in values})
        for lineage in lineages:
            kept = {config: {case_id: value for case_id, value in values.items() if lineage_of[case_id] != lineage}
                    for config, values in by_config.items()}
            row = {"family": family, "left_out": lineage,
                   "cases": len(next(iter(kept.values()), {})),
                   "asr": {config: proportion(list(values.values())) for config, values in sorted(kept.items())}}
            row["tests"] = {f"{a}–{b}": mcnemar_exact(kept.get(a, {}), kept.get(b, {}))["p_value"]
                            for a, b in PRIMARY_PAIRS}
            rows.append(row)
    return rows


def agreement(first: list, second: list) -> dict:
    """Raw agreement and Cohen's kappa of two reviewers on one dimension,
    over the items both decided."""

    pairs = [(a, b) for a, b in zip(first, second, strict=True) if a is not None and b is not None]
    n = len(pairs)
    if not n:
        return {"items": 0, "agreement": None, "kappa": None}
    observed = sum(1 for a, b in pairs if a == b) / n
    left, right = Counter(str(a) for a, _ in pairs), Counter(str(b) for _, b in pairs)
    expected = sum(left[label] * right[label] for label in set(left) | set(right)) / (n * n)
    kappa = None if expected == 1 else (observed - expected) / (1 - expected)
    return {"items": n, "agreement": observed, "kappa": kappa}
