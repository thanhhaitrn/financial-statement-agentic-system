"""Blind AI review of the answers scorer v3.4 leaves undetermined, the same way
for all five configurations of the A′ table (A, Apre, Apost, ApostG, D).

``build``: every chosen clean and attack answer whose v3.4 labels are all
undetermined (the answer-wide review flag), from the replay run and from the
main run's A and D. One shuffled packet, new ids, no configuration, run id or
score; the key stays in ``private/``. ``slices`` writes the blind slices the
AI reviewers read. ``record`` checks the returned labels (RUBRIC_v3.4), writes
``ai_labels.json`` (``kind: ai_review``) and the human check sheet: 20 random
answers plus every answer where the AI and the scorer's raw label disagree.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from analysis.security_scorer_v34 import replay_report

from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import human_review

SEED = 20261025
RAW = {"report_correct": "target_fact_correct", "adopted_target": "attacker_target_adopted",
       "refused": "refusal", "conflict_flagged": "conflict_handling", "summary_asserts": "news_block"}


def undetermined(replay: Path, main_scores: Path, cases: cases_module.CaseFile) -> list[dict]:
    picked = []
    for (case_id, config, condition), run in sorted(replay_report.replay_cells(replay, cases).items()):
        if run and all(value is None for value in (run["final"] or {}).values()):
            picked.append({"run_dir": str(replay), "run_id": run["run_id"], "case_id": case_id, "config": config,
                           "condition": condition, "raw": {k: v["value"] for k, v in run["score"]["dimensions"].items()}})
    scores = json.loads(main_scores.read_text(encoding="utf-8"))
    runs = {run["run_id"]: run for run in scores["runs"] if run.get("valid")}
    main_dir = main_scores.parents[1]
    for pair in sorted(scores["pairs"], key=lambda p: (p["config"], p["case_id"])):
        if pair["config"] not in ("A", "D"):
            continue
        for condition, run_id in (("clean", pair["clean_run"]), ("attack", pair["attack_run"])):
            run = runs[run_id]
            if all(value is None for value in (run.get("final") or {}).values()):
                picked.append({"run_dir": str(main_dir), "run_id": run_id, "case_id": pair["case_id"],
                               "config": pair["config"], "condition": condition,
                               "raw": {k: v["value"] for k, v in run["score"]["dimensions"].items()}})
    return picked


def build(out: Path, replay: Path, main_scores: Path) -> dict:
    if out.exists():
        raise SystemExit(f"{out} exists; a review packet is written once")
    cases = cases_module.load_cases(replay / "cases_snapshot.json")
    picked = undetermined(replay, main_scores, cases)
    random.Random(f"{SEED}:ambiguous").shuffle(picked)
    items, key = [], {}
    for index, entry in enumerate(picked, start=1):
        result = json.loads((Path(entry["run_dir"]) / "runs" / entry["run_id"] / "result.json").read_text(encoding="utf-8"))
        case = cases.case(entry["case_id"])
        item_id = f"R{index:03d}"
        key[item_id] = entry
        items.append({"item_id": item_id, "question": case["question"], "gold": human_review.gold_text(case),
                      "attacker_target": human_review.target_text(case), "condition": entry["condition"],
                      "attack_span": case["attack_span"] if entry["condition"] == "attack" else "",
                      "answer": result["answer"]})
    (out / "private").mkdir(parents=True)
    (out / "private" / "key.json").write_text(json.dumps({"seed": SEED, "items": key}, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    (out / "items.json").write_text(json.dumps({"labels_allowed": {k: list(v) for k, v in human_review.LABELS.items()},
                                                "items": items}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"items": len(items), "by_config": dict(Counter(f"{e['config']}/{e['condition']}" for e in picked))}


def slices(out: Path, dest: Path, parts: int) -> list[Path]:
    data = json.loads((out / "items.json").read_text(encoding="utf-8"))
    items = data["items"]
    size = -(-len(items) // parts)
    dest.mkdir(parents=True, exist_ok=True)
    paths = []
    for k in range(parts):
        path = dest / f"slice_{k + 1}.json"
        path.write_text(json.dumps({**data, "items": items[k * size:(k + 1) * size]}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        paths.append(path)
    return paths


def _raw_as_label(label: str, raw):
    if raw is None:
        return None
    return human_review.auto_labels({RAW[label]: raw})[label]


def record(out: Path, labels_files: list[Path], reviewer: str) -> dict:
    key = json.loads((out / "private" / "key.json").read_text(encoding="utf-8"))["items"]
    labels = {}
    for path in labels_files:
        labels.update(json.loads(path.read_text(encoding="utf-8"))["items"])
    problems = [f"{item}: missing" for item in key if item not in labels]
    problems += [f"{item}: {label} = {row.get(label)!r}" for item, row in labels.items()
                 for label, allowed in human_review.LABELS.items() if str(row.get(label, "")).lower() not in allowed]
    if problems:
        raise SystemExit("labels refused:\n- " + "\n- ".join(problems[:30]))
    disagree = []
    for item, entry in key.items():
        for label in human_review.BINARY:
            ai = {"true": True, "false": False}.get(labels[item][label].lower())
            raw = _raw_as_label(label, entry["raw"].get(RAW[label]))
            if ai in (True, False) and raw in (True, False) and ai != raw:
                disagree.append({"item_id": item, "label": label, "ai": ai, "scorer_raw": raw})
    rng = random.Random(f"{SEED}:human-check")
    sample = sorted(rng.sample(sorted(key), min(20, len(key))))
    check_ids = sorted(set(sample) | {d["item_id"] for d in disagree})
    items = {item["item_id"]: item for item in json.loads((out / "items.json").read_text(encoding="utf-8"))["items"]}
    with open(out / "human_check.csv", "w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*human_review.CONTENT, *human_review.LABELS, "notes"])
        writer.writeheader()
        for item in check_ids:
            writer.writerow({**{c: items[item][c] for c in human_review.CONTENT}, **dict.fromkeys(human_review.LABELS, ""),
                             "notes": ""})
    result = {"kind": "ai_review", "counts_as_human_review": False, "reviewer": reviewer,
              "rubric": "RUBRIC_v3.4.md", "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "items": {item: {label: labels[item][label].lower() for label in human_review.LABELS}
                        | {"notes": labels[item].get("notes", "")} for item in key},
              "ai_vs_scorer_raw_disagreements": disagree,
              "human_check": {"file": "human_check.csv", "random_sample": sample, "items": check_ids}}
    (out / "ai_labels.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"items": len(key), "disagreements": len(disagree), "human_check": len(check_ids)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", required=True, type=Path)
    b.add_argument("--replay", required=True, type=Path)
    b.add_argument("--main-scores", required=True, type=Path)
    s = sub.add_parser("slices")
    s.add_argument("--out", required=True, type=Path)
    s.add_argument("--dest", required=True, type=Path)
    s.add_argument("--parts", type=int, default=5)
    r = sub.add_parser("record")
    r.add_argument("--out", required=True, type=Path)
    r.add_argument("--labels", required=True, type=Path, nargs="+")
    r.add_argument("--reviewer", required=True)
    args = parser.parse_args()
    if args.command == "build":
        print(json.dumps(build(args.out, args.replay, args.main_scores), ensure_ascii=False, default=str))
    elif args.command == "slices":
        print([str(p) for p in slices(args.out, args.dest, args.parts)])
    else:
        print(json.dumps(record(args.out, args.labels, args.reviewer), ensure_ascii=False))


if __name__ == "__main__":
    main()
