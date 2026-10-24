"""Scorer v3.3 and v3.4 against the adjudicated validation labels (offline).

Run from ``backend/``: ``python -m analysis.security_scorer_v34.validate --packet DIR``.
The 110 sampled answers are rescored with each scorer; the answer-wide review
flag leaves all labels undecided (as in the main report), and the binary
labels are compared with the adjudicated ones. v3.4 was revised on this same
sample, so its figures are a fit, not an independent accuracy estimate; v3.3's
are the honest estimate for the scorer the main run used.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis.security_scorer_v33 import scorer as v33
from analysis.security_scorer_v34 import scorer as v34

from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import human_review, main_report


def labels(scorer, case: dict, answer: str) -> dict:
    score = scorer.score_answer(case, answer).as_dict()
    final, _ = main_report._automatic({"score": score})
    return human_review.auto_labels(final)


def validate(packet: Path) -> dict:
    key = json.loads((packet / "private" / "key.json").read_text(encoding="utf-8"))["items"]
    record = json.loads((packet / "ai_review.json").read_text(encoding="utf-8"))
    final = record["final_labels"]
    cases = {}
    autos = {"v33": {}, "v34": {}}
    for item_id, entry in key.items():
        run_dir = Path(entry["run_dir"])
        if not run_dir.is_absolute() and not run_dir.exists():
            run_dir = Path(__file__).resolve().parents[3] / run_dir  # recorded relative to the repository root
        if entry["run_dir"] not in cases:
            cases[entry["run_dir"]] = cases_module.load_cases(run_dir / "cases_snapshot.json")
        result = json.loads((run_dir / "runs" / entry["run_id"] / "result.json").read_text(encoding="utf-8"))
        case = cases[entry["run_dir"]].case(result["case_id"])
        autos["v33"][item_id] = {"auto": labels(v33, case, result["answer"])}
        autos["v34"][item_id] = {"auto": labels(v34, case, result["answer"])}
    ids = sorted(key)
    return {version: human_review.scorer_table(final, table, ids, source="v33_auto")
            for version, table in autos.items()} | {"kind": record["kind"], "items": len(ids),
                                                    "note": "v3.4 was revised on this sample: a fit, not a "
                                                            "held-out estimate"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = validate(args.packet)
    for version in ("v33", "v34"):
        for label, row in result[version].items():
            fmt = lambda x: "—" if x is None else f"{x:.3f}"  # noqa: E731
            print(f"{version} {label:17s} n={row['items']:3d} undecided={row.get('scorer_undecided', 0):2d} "
                  f"tp={row.get('tp', 0)} fn={row.get('fn', 0)} fp={row.get('fp', 0)} tn={row.get('tn', 0)} "
                  f"sens={fmt(row['sensitivity'])} spec={fmt(row['specificity'])} kappa={fmt(row['kappa'])}")
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
