"""Offline, append-only rescore of an automated-only frozen main experiment.

Run from the repository root with ``python -m
backend.analysis.security_scorer_v34.rescore --source ... --out ...``. Scorer v3.4 is
the one frozen at gate G1 (phase 2); the frozen v3.2 scores are reproduced first.
No run/worker/LLM execution is available through this entry point.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from agentfinx.core.config import paths
from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import freeze, main_report, main_review, outcomes
from agentfinx.evaluation.security import protocol as protocol_module
from agentfinx.evaluation.security.gold import report_amounts

from . import scorer


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def input_hashes(source):
    names = ("manifest.json", "protocol_snapshot.json", "cases_snapshot.json", "budget.json", "main_status.json",
             "scores_main.json", "main_report_final.md", "events.jsonl", "prompt_log.jsonl")
    files = [source / name for name in names]
    files += sorted((source / "runs").glob("*/result.json"))
    files += sorted((source / "runs").glob("*/snapshot.json"))
    return {str(path.relative_to(source)): sha(path) for path in files}


def check_destination(source, out):
    # A fresh child folder is allowed. Never write back into runs/review, or
    # take over an existing directory, even one belonging to a previous rescore.
    if out.exists():
        raise ValueError("output must be a new directory; existing files are never overwritten")
    if source in out.parents and out.relative_to(source).parts[0] in {"runs", "review"}:
        raise ValueError("output cannot be inside the original runs or review directory")


def changed_labels(old, new):
    changes = []
    for dim in main_review.REVIEWED:
        a = old.get("score", {}).get("dimensions", {}).get(dim, {})
        b = new.get("score", {}).get("dimensions", {}).get(dim, {})
        if a.get("value") != b.get("value"):
            changes.append({"dimension": dim, "old": a, "new": b})
    final = {dim: {"old": old.get("final", {}).get(dim), "new": new.get("final", {}).get(dim)}
             for dim in main_review.REVIEWED
             if old.get("final", {}).get(dim) != new.get("final", {}).get(dim)}
    return {"run_id": new["run_id"], "case_id": new["case_id"], "config": new["config"],
            "condition": new["condition"], "automatic_label_changes": changes, "final_label_changes": final,
            "old_review_reasons": old["score"].get("review_reasons", []),
            "new_review_reasons": new["score"].get("review_reasons", [])}


def compare_report(old, new, changes, pair_changes):
    lines = ["# Chấm lại ngoại tuyến — scorer v3.4 (cổng G1, giai đoạn 2)", "",
             "Giữ nguyên dữ liệu, câu trả lời, cấu hình, attempt được chọn, mẫu số và protocol gốc. "
             "Đây là phân tích sửa lỗi sau test, không thay thế âm thầm kết quả đã freeze. "
             "Không gọi thêm model; không áp dụng quyết định chấm tay hay AI cho riêng từng case.", "",
             "| Cấu hình | ASR cũ: có / chưa xác định | ASR mới: có / chưa xác định | "
             "Đúng fact sạch cũ → mới | Đúng fact tấn công cũ → mới |", "|---|---|---|---|---|"]
    for config in "ABCD":
        a, b = old["metrics"]["asr"]["total"][config], new["metrics"]["asr"]["total"][config]
        cells = []
        for condition in ("clean", "attack"):
            x = old["metrics"]["accuracy"][condition][config]
            y = new["metrics"]["accuracy"][condition][config]
            cells.append(f"{x['successes']}/{x['n']} (chưa rõ {x['undetermined']}) → "
                         f"{y['successes']}/{y['n']} (chưa rõ {y['undetermined']})")
        lines.append(f"| {config} | {a['successes']}/{a['n']} / {a['undetermined']} | "
                     f"{b['successes']}/{b['n']} / {b['undetermined']} | " + " | ".join(cells) + " |")
    old_amb = sum(bool(r.get("score", {}).get("needs_review")) for r in old["runs"] if r["valid"])
    new_amb = sum(bool(r.get("score", {}).get("needs_review")) for r in new["runs"] if r["valid"])
    lines += ["", f"- Đầu ra có cờ mơ hồ: {old_amb} → {new_amb}. Những câu này vẫn giữ cả sáu chiều "
              "chưa xác định theo chính sách gốc; không coi là phòng thủ thành công.",
              f"- {len(changes)} đầu ra có thay đổi nhãn hoặc lý do review; "
              f"{len(pair_changes)} cặp có thay đổi quyết định ASR hoặc source-boundary.",
              "- Các số ASR là nhãn tự động trên mẫu số 80, không phải ước lượng hoàn chỉnh khi còn nhãn thiếu. "
              "Khoảng khả dĩ do nhãn thiếu không phải khoảng tin cậy 95%.",
              "- Exposure E1–E3 và recall truy xuất giữ thuật toán gốc; sửa lần này áp dụng vào câu trả lời cuối. "
              "Không có dữ liệu mới về hiệu quả riêng của cổng provenance.",
              "- Gói review mới được tạo riêng và chưa có người chấm. Không đo độ đồng ý của hai người.", "",
              "Chi tiết: [bảng kết quả](report.md), [nhãn thay đổi](score_changes.json), "
              "[cặp thay đổi](pair_changes.json), [nguồn gốc và hash](provenance.json).", ""]
    return "\n".join(lines)


def rescore(source: Path, out: Path, *, frozen_benchmarks: Path | None = None):
    source, out = source.resolve(), out.resolve()
    check_destination(source, out)
    manifest, original = read(source / "manifest.json"), read(source / "scores_main.json")
    protocol = read(source / "protocol_snapshot.json")
    if protocol_module.review_mode(protocol) != "automated_only":
        raise ValueError("this correction entry point supports automated_only protocols only")
    problems = main_report.integrity_problems(source, manifest)
    # The working tree's benchmarks/security/README.md (the old rubric file) gained the
    # post-test sections after the freeze; the frozen runtime keeps the frozen copy.
    frozen_cases = (frozen_benchmarks / "security" / Path(manifest["cases_file"]).name
                    if frozen_benchmarks else manifest["cases_file"])
    problems += freeze.frozen_problems(cases_module.load_cases(frozen_cases))
    if problems:
        raise ValueError("original experiment integrity failed: " + "; ".join(problems))
    hashes_before = input_hashes(source)
    cases = cases_module.load_cases(source / "cases_snapshot.json")
    runs = main_report.score_runs(source, cases, manifest["fingerprint"]["payload"])
    old_by_id = {r["run_id"]: r for r in original["runs"]}
    if set(old_by_id) != {r["run_id"] for r in runs}:
        raise ValueError("attempt inventory differs from the original scores")
    for run in runs:
        old = old_by_id[run["run_id"]]
        if old["score"] != run["score"] or old["valid"] != run["valid"]:
            raise ValueError(f"frozen scoring/validity is not reproducible: {run['run_id']}")
        run["score"] = scorer.score_answer(cases.case(run["case_id"]), run["answer"],
                                          status=run["status"]).as_dict()
    cells = outcomes.select_cells(runs)
    chosen = [c["chosen"] for c in cells.values() if c["chosen"] is not None]
    expected = {tuple(item[k] for k in ("case_id", "config", "condition", "repetition"))
                for item in manifest["schedule"]}
    if set(cells) != expected or len(chosen) != len(expected):
        raise ValueError("rescore requires a complete experiment with no unscheduled cells")
    old_chosen = {p[k] for p in original["pairs"] for k in ("clean_run", "attack_run")}
    if {r["run_id"] for r in chosen} != old_chosen:
        raise ValueError("the selected first valid attempts changed")
    waiting = {}
    for run in chosen:
        run["final"], pending = main_report._automatic(run)
        if pending:
            waiting[run["run_id"]] = pending
    excluded = set(original["excluded"])
    report_values = {dataset: report_amounts(paths.home() / meta["report"]["file"])
                     for dataset, meta in cases.data["datasets"].items()}
    pairs, evidence_items = [], []
    for old_pair in original["pairs"]:
        case_id, config = old_pair["case_id"], old_pair["config"]
        case = cases.case(case_id)
        clean = cells[(case_id, config, "clean", 0)]["chosen"]
        attack = cells[(case_id, config, "attack", 0)]["chosen"]
        success, reason = outcomes.attack_success(case, clean["final"], attack["final"])
        sbv = outcomes.source_boundary_violation(case, attack, events=attack["_events"],
                calc_fact_ids=set(attack["calc_fact_ids"]), report_values=report_values[case["dataset_id"]])
        if sbv["value"] is None and sbv["reason"] not in {"answer_undetermined", "no_valid_attack_answer"}:
            evidence_items.append({"run_id": attack["run_id"], "case": case, "answer": attack["answer"],
                                   "events": attack["_events"]})
        dropped = case_id in excluded
        if dropped and sbv["value"] != "not_applicable":
            sbv = {**sbv, "value": None, "reason": "excluded"}
        pairs.append({**old_pair, "attack_success": None if dropped else success,
                      "reason": "excluded" if dropped else reason,
                      "attack_success_before_exclusion": success, "source_boundary": sbv})
    reasons = main_review.sample(chosen, seed=int(protocol["review"]["sample_seed"]),
                                 rate=float(protocol["review"]["sample_rate"]))
    metrics = main_report.metrics(protocol, cases, runs, chosen, cells, pairs, excluded, {})
    metrics["review_agreement"] = {}
    for field in ("exposure", "retrieval_recall", "infrastructure"):
        # Wilson/range tuples are lists in the original JSON representation.
        if json.loads(json.dumps(metrics[field])) != original["metrics"][field]:
            raise ValueError(f"non-scoring measurement changed: {field}")
    if input_hashes(source) != hashes_before:
        raise ValueError("source files changed during scoring")

    # Only after all integrity checks pass, create a fresh output directory.
    out.mkdir(parents=True, exist_ok=False)
    salt = hashlib.sha256((manifest["fingerprint"]["sha256"] + scorer.SCORER_VERSION).encode()).hexdigest()[:12]
    packs = main_review.write_packs(out / "review", chosen, reasons, cases, salt=salt, evidence_items=evidence_items)
    summary = {**original, "scorer_version": scorer.SCORER_VERSION,
               "analysis_type": "post_test_scorer_correction", "preregistered_scorer": False,
               "review": {**original["review"], "human_decisions_applied": False, "waiting": waiting,
                          "sampled": Counter(reasons.values()), "blocking": [], **packs},
               "metrics": metrics, "pairs": pairs,
               "runs": [{k: v for k, v in r.items() if k not in {"answer", "_events"}} for r in runs]}
    changes = [changed_labels(old_by_id[r["run_id"]], r) for r in chosen]
    changes = [c for c in changes if c["automatic_label_changes"] or c["final_label_changes"]
               or c["old_review_reasons"] != c["new_review_reasons"]]
    pair_changes = [{"case_id": a["case_id"], "config": a["config"], "old": a, "new": b}
                    for a, b in zip(original["pairs"], pairs, strict=True)
                    if a["attack_success"] != b["attack_success"] or a["source_boundary"] != b["source_boundary"]]
    write(out / "scores_main.json", summary)
    write(out / "score_changes.json", changes)
    write(out / "pair_changes.json", pair_changes)
    write(out / "inputs_sha256.json", hashes_before)
    banner = ("> **Scorer v3.4 (cổng G1, 24/10/2026).** Protocol gốc dùng v3.2; báo cáo này dùng v3.4 trên cùng "
              "câu trả lời, nhãn tự động. v3.4 được sửa theo mẫu kiểm chứng 110 câu (hai reviewer AI, đã phân xử). "
              "Không có lượt gọi model mới; không thay thế hồ sơ gốc. Câu đã chấm trong mẫu kiểm chứng dùng nhãn "
              "đã phân xử ở bảng riêng (phase2).\n\n")
    (out / "report.md").write_text(banner + main_report.render(summary, protocol, manifest), encoding="utf-8")
    (out / "comparison.md").write_text(compare_report(original, summary, changes, pair_changes), encoding="utf-8")
    code = Path(__file__).parent
    frozen_scorer = Path(main_report.__file__).with_name("scorer.py")
    snapshot = out / "analysis_code"
    snapshot.mkdir()
    for path in sorted(code.glob("*.py")) + sorted(code.glob("*.md")):
        (snapshot / path.name).write_bytes(path.read_bytes())
    diff = difflib.unified_diff(frozen_scorer.read_text().splitlines(keepends=True),
                                Path(scorer.__file__).read_text().splitlines(keepends=True),
                                fromfile="frozen/scorer-v3.2.py", tofile="posttest/scorer-v3.3.py")
    (out / "scorer.patch").write_text("".join(diff), encoding="utf-8")
    write(out / "provenance.json", {
        "analysis_type": "phase2_g1_scorer", "created_at": datetime.now(timezone.utc).isoformat(),
        "source_directory": str(source), "original_scorer_version": original["scorer_version"],
        "frozen_runtime_source": str(paths.source_root()),
        "new_scorer_version": scorer.SCORER_VERSION, "additional_model_calls": 0,
        "human_decisions_applied": False, "case_specific_overrides": False,
        "frozen_score_reproduction": "all original automatic decisions reproduced before correction",
        "first_valid_attempts_unchanged": True, "exposure_recall_infrastructure_unchanged": True,
        "source_files_unchanged": input_hashes(source) == hashes_before,
        "original_scorer_sha256": sha(frozen_scorer),
        "analysis_code_sha256": {p.name: sha(p) for p in sorted(snapshot.iterdir())},
        "input_hash_manifest_sha256": sha(out / "inputs_sha256.json"),
        "scores_sha256": sha(out / "scores_main.json"),
        "score_changes": len(changes), "pair_changes": len(pair_changes),
        "limitations": ["post-test rule correction informed by observed failures",
                        "automated output labels; unresolved answers remain unknown",
                        "E1-E3 exposure matching retains the frozen algorithm",
                        "no new evidence that the natural-workflow WEB provenance gate was activated"]})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--frozen-benchmarks", type=Path, default=None,
                        help="the frozen runtime's backend/benchmarks, checked against FREEZE.json")
    args = parser.parse_args()
    summary = rescore(args.source, args.out, frozen_benchmarks=args.frozen_benchmarks)
    print(json.dumps({"out": str(args.out), "scorer_version": summary["scorer_version"],
                      "valid_cells": summary["cells"]["valid"], "model_calls_added": 0,
                      "asr": summary["metrics"]["asr"]["total"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
