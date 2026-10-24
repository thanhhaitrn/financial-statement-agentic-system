"""Sensitivity analysis of the main run with AI labels for its ambiguous answers.

The primary results keep every ambiguous answer undetermined. Here those
answers take the AI's labels (``ai_review``; ``"unclear"`` stays None), the
pair outcomes are computed again with the same functions as the main report
(``outcomes.attack_success``, ``outcomes.source_boundary_violation``) and the
same metric code (``main_report.metrics``), and both sets of numbers are
written side by side to ``sensitivity_ai_review.json`` and ``.md``. Nothing
the main report wrote is changed. The AI is not a human reviewer: no
agreement between reviewers is reported, only how often the AI agrees with
the scorer on the dimensions the scorer had decided.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from agentfinx.evaluation.security import ai_review, evidence, main_review, outcomes
from agentfinx.evaluation.security import cases as cases_module


def _load(path: Path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sensitivity(run_dir: Path) -> dict:
    from agentfinx.core.config import paths
    from agentfinx.evaluation.security import main_report
    from agentfinx.evaluation.security.gold import report_amounts

    scores_path = run_dir / "scores_main.json"
    summary = _load(scores_path, None)
    if summary is None:
        raise SystemExit(f"{run_dir} has no scores_main.json")
    labels = ai_review.labels_by_run(run_dir)
    if not labels:
        raise SystemExit("no recorded AI labels; run `eval-security ai-review-record` first")
    protocol = _load(run_dir / "protocol_snapshot.json", {})
    cases = cases_module.load_cases(run_dir / "cases_snapshot.json")
    excluded = set(summary.get("excluded") or [])

    runs = summary["runs"]
    cells = outcomes.select_cells(runs)
    chosen = [cell["chosen"] for cell in cells.values() if cell["chosen"] is not None]
    unknown = sorted(set(labels) - {run["run_id"] for run in chosen})
    if unknown:
        raise SystemExit(f"AI labels name runs that are not chosen answers: {unknown[:3]}")
    agreement: dict[str, Counter] = defaultdict(Counter)
    for run in chosen:
        if run["run_id"] not in labels:
            continue
        for dim in main_review.REVIEWED:
            auto = (run["score"]["dimensions"].get(dim) or {}).get("value")
            ai = labels[run["run_id"]].get(dim)
            if ai is None:
                agreement[dim]["ai_unclear"] += 1
            elif auto is None:
                agreement[dim]["scorer_undecided"] += 1
            else:
                agreement[dim]["agree" if ai == auto else "disagree"] += 1
        run["final"] = dict(labels[run["run_id"]])

    wanted = {run["run_id"] for run in chosen if run["condition"] == "attack"}
    events = defaultdict(list)
    for record in evidence.read_jsonl(run_dir / "events.jsonl"):
        if record.get("run_id") in wanted:
            events[record["run_id"]].append(record)
    report_values = {dataset_id: report_amounts(paths.home() / meta["report"]["file"])
                     for dataset_id, meta in cases.data["datasets"].items()}
    pairs = []
    for case_id in protocol["case_ids"]:
        case = cases.case(case_id)
        for config in protocol["configs"]:
            clean = (cells.get((case_id, config, "clean", 0)) or {}).get("chosen")
            attack = (cells.get((case_id, config, "attack", 0)) or {}).get("chosen")
            success, reason = outcomes.attack_success(case, clean and clean["final"], attack and attack["final"])
            sbv = outcomes.source_boundary_violation(
                case, attack, events=events[attack["run_id"]] if attack else [],
                calc_fact_ids=set(attack.get("calc_fact_ids") or []) if attack else set(),
                report_values=report_values.get(case["dataset_id"]))
            dropped = case_id in excluded
            if dropped and sbv["value"] != "not_applicable":
                sbv = {**sbv, "value": None, "reason": "excluded"}
            pairs.append({"case_id": case_id, "config": config, "family": outcomes.family_of(case),
                          "subtype": case["subtype"], "lineage": case["lineage"], "excluded": dropped,
                          "attack_success": None if dropped else success,
                          "reason": "excluded" if dropped else reason,
                          "attack_success_before_exclusion": success, "source_boundary": sbv,
                          "clean_run": clean and clean["run_id"], "attack_run": attack and attack["run_id"]})
    metrics = main_report.metrics(protocol, cases, runs, chosen, cells, pairs, excluded, {})
    metrics.pop("review_agreement", None)  # no human reviewers here
    ai_file = run_dir / "review" / "ai_review" / "labels.json"
    result = {
        "note": "Phân tích độ nhạy dùng nhãn AI cho các câu mơ hồ; không phải review của người. Kết quả chính "
                "vẫn là kết quả có ô chưa xác định (scores_main.json).",
        "scores_main_sha256": _sha(scores_path), "ai_labels_sha256": _sha(ai_file),
        "ai_reviewer": _load(ai_file, {}).get("reviewer"), "ai_labelled_answers": len(labels),
        "ai_vs_scorer": {dim: dict(counts) for dim, counts in agreement.items()},
        "primary": {key: summary["metrics"].get(key) for key in ("asr", "primary_tests", "accuracy",
                                                                 "unnecessary_refusal_clean", "source_boundary")},
        "sensitivity": metrics, "pairs": pairs,
    }
    (run_dir / "sensitivity_ai_review.json").write_text(json.dumps(result, ensure_ascii=False, indent=1,
                                                                   default=str), encoding="utf-8")
    (run_dir / "sensitivity_ai_review.md").write_text(render(result, list(protocol["configs"])), encoding="utf-8")
    return result


def render(result: dict, configs: list[str]) -> str:
    from agentfinx.evaluation.security.main_report import _asr_table, _rate, _tests_table

    primary, sens = result["primary"], result["sensitivity"]
    reviewer = result.get("ai_reviewer") or {}
    lines = ["# Phân tích độ nhạy: nhãn AI cho các câu mơ hồ", "",
             "**Không phải review của người.** Kết quả chính vẫn là kết quả trong `main_report_final.md`, nơi "
             "các câu mơ hồ là chưa xác định. Ở đây chúng nhận nhãn AI; nhãn `unclear` vẫn là chưa xác định.", "",
             f"- Người chấm: AI `{reviewer.get('model', '?')}`, ngày {reviewer.get('date', '?')}, prompt sha256 "
             f"`{str(reviewer.get('prompt_sha256', ''))[:12]}`; {result['ai_labelled_answers']} câu được gán nhãn.",
             f"- Dựa trên `scores_main.json` sha256 `{result['scores_main_sha256'][:12]}`; nhãn AI sha256 "
             f"`{result['ai_labels_sha256'][:12]}`.", "",
             "## 1. ASR", "", "### Chính (câu mơ hồ = chưa xác định)", ""]
    lines += _asr_table(primary["asr"], configs)
    lines += ["", "### Độ nhạy (câu mơ hồ = nhãn AI)", ""] + _asr_table(sens["asr"], configs)
    lines += ["", "## 2. Sáu so sánh định trước", "", "### Chính", ""] + _tests_table(primary["primary_tests"])
    lines += ["", "### Độ nhạy", ""] + _tests_table(sens["primary_tests"])
    lines += ["", "## 3. Độ chính xác, từ chối, vi phạm nguồn (độ nhạy)", "",
              "| cấu hình | đúng fact (sạch) | đúng fact (tấn công) | từ chối không cần thiết | vi phạm nguồn |",
              "|---|---|---|---|---|"]
    for config in configs:
        lines.append(f"| {config} | {_rate(sens['accuracy']['clean'][config])} | "
                     f"{_rate(sens['accuracy']['attack'][config])} | {_rate(sens['unnecessary_refusal_clean'][config])} "
                     f"| {_rate(sens['source_boundary'][config])} |")
    lines += ["", "## 4. AI so với scorer (chẩn đoán, không phải độ đồng ý giữa người chấm)", "",
              "| chiều | khớp | lệch | scorer chưa quyết | AI unclear |", "|---|---|---|---|---|"]
    for dim in main_review.REVIEWED:
        row = result["ai_vs_scorer"].get(dim, {})
        lines.append(f"| {dim} | {row.get('agree', 0)} | {row.get('disagree', 0)} | {row.get('scorer_undecided', 0)} "
                     f"| {row.get('ai_unclear', 0)} |")
    return "\n".join(lines) + "\n"
