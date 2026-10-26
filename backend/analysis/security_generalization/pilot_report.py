"""Read the dev pilots of the generalization runs (offline, no model call).

Run from ``backend/`` of the worktree::

    python -m analysis.security_generalization.pilot_report --root ../../../../artifacts/eval/security/extension-20261026

For each model: valid cells, attempts and statuses, model calls and time per cell
(A and D), and the share of answers scorer v3.4 leaves undetermined, which sizes
the review the test runs will need. It does not report attack success: the pilot
decides only whether the pipeline runs and how much review to plan.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

from analysis.security_scorer_v34 import scorer

from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import main_report


def pilot(run_dir: Path) -> dict:
    cases = cases_module.load_cases(run_dir / "cases_snapshot.json")
    attempts = [json.loads(line) for line in (run_dir / "attempts.jsonl").read_text(encoding="utf-8").splitlines()]
    status = json.loads((run_dir / "main_status.json").read_text(encoding="utf-8"))
    chosen = {}
    for record in attempts:
        if record["valid"]:
            chosen.setdefault(tuple(record["cell"][:3]), record["run_id"])
    calls, seconds, undetermined, problems = defaultdict(list), defaultdict(list), Counter(), Counter()
    for (case_id, config, condition), run_id in chosen.items():
        result = json.loads((run_dir / "runs" / run_id / "result.json").read_text(encoding="utf-8"))
        calls[config].append(int(result.get("model_calls") or 0))
        seconds[config].append(float(result.get("duration_s") or 0))
        score = scorer.score_answer(cases.case(case_id), result["answer"]).as_dict()
        final, _ = main_report._automatic({"score": score})
        if all(value is None for value in final.values()):
            undetermined[(config, condition)] += 1
    for record in attempts:
        if not record["valid"]:
            problems[record["status"]] += 1
    cells = status["cells"]
    return {"cells": cells, "valid": len(chosen), "attempts": len(attempts),
            "invalid_attempts": dict(problems), "stopped": status.get("stopped"),
            "model_calls_total": status["budget"].get("used"),
            "calls_per_cell": {c: round(statistics.mean(v), 2) for c, v in calls.items()},
            "seconds_per_cell": {c: round(statistics.median(v), 1) for c, v in seconds.items()},
            "undetermined_v34": {f"{c}/{k}": n for (c, k), n in sorted(undetermined.items())},
            "undetermined_share": round(sum(undetermined.values()) / max(len(chosen), 1), 3)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args()
    out = {path.name: pilot(path) for path in sorted(args.root.glob("pilot-*")) if (path / "main_status.json").exists()}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    (args.root / "pilot_report.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
