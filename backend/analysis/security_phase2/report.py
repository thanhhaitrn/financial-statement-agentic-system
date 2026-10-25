"""Phase 2, Việc 4: the paper's tables, generated from the score files (offline, no model call).

Run from ``backend/``::

    python -m analysis.security_phase2.report --root ../artifacts/eval/security --out DIR

- Bảng 1: the main run A/B/C/D with one labelling: scorer v3.4's own decisions,
  the blind AI review where v3.4 left an answer undetermined (A, D, B, C alike),
  and the human's final decisions. ASR (80, I 40, P 40) with Wilson or the
  undetermined range and the Clopper–Pearson bound of a zero cell; the six
  pre-registered McNemar comparisons with Holm; clean accuracy and refusals;
  on attack answers "correct without the fake" and "both sources, report kept".
- Bảng 2: agreement between the two reviewers (validation sample, AI), between
  the two AI passes on the 87-answer check sample, and of scorer v3.3/v3.4
  with the adjudicated labels.
- Bảng 3: the A′ chain (from ``replay_report.json``), unified and sensitivity.

Every number comes from a file whose SHA-256 is recorded; nothing is typed in.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from analysis.security_scorer_v34 import replay_report

from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import human_review, outcomes, stats

REVIEWS = ("review-ambiguous-20261024", "review-ambiguous-bc-20261025", "review-ambiguous-fix-20261024")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def review_labels(phase2: Path) -> tuple[dict, dict]:
    """(run_dir, run_id) → final dimensions from the AI reviews (with the human's decisions)."""

    labels, sources = {}, {}
    for name in REVIEWS:
        folder = phase2 / name
        if not (folder / "private" / "key.json").exists():
            continue
        key = _load(folder / "private" / "key.json")["items"]
        found = _load(folder / ("ai_labels_final.json" if (folder / "ai_labels_final.json").exists()
                                else "ai_labels.json"))["items"]
        human = _load(folder / "human_decisions.json")["items"] if (folder / "human_decisions.json").exists() else {}
        for item, entry in key.items():
            ident = (str(Path(entry["run_dir"]).resolve()), entry["run_id"])
            labels[ident] = replay_report.ai_dims({**found[item], **human.get(item, {})})
            sources[ident] = "human" if item in human else "ai"
        sources[f"{name}_sha256"] = _sha(folder / "private" / "key.json")
    return labels, sources


def main_table(root: Path, labels: dict) -> dict:
    main = root / "main-20261020"
    scores = _load(main / "rescore-v3.4-20261024" / "scores_main.json")
    cases = cases_module.load_cases(main / "cases_snapshot.json")
    runs = {run["run_id"]: run for run in scores["runs"] if run.get("valid")}
    main_dir = str(main.resolve())
    answers, missing = {}, []
    for pair in scores["pairs"]:
        for condition, run_id in (("clean", pair["clean_run"]), ("attack", pair["attack_run"])):
            final = runs[run_id].get("final") or {}
            reviewed = labels.get((main_dir, run_id))
            if reviewed is None and all(value is None for value in final.values()):
                missing.append(run_id)
            answers[(pair["case_id"], pair["config"], condition)] = reviewed or final
    if missing:
        raise SystemExit(f"{len(missing)} undetermined main-run answers have no review: {missing[:5]}")
    configs = ["A", "B", "C", "D"]
    case_ids = sorted({pair["case_id"] for pair in scores["pairs"]})
    success = defaultdict(lambda: defaultdict(dict))
    pairs = defaultdict(dict)
    for config in configs:
        for case_id in case_ids:
            value, reason = outcomes.attack_success(cases.case(case_id), answers.get((case_id, config, "clean")),
                                                    answers.get((case_id, config, "attack")))
            pairs[config][case_id] = {"success": value, "reason": reason}
            success[case_id[0]][config][case_id] = value
    return {"asr": replay_report.table(pairs, cases, configs),
            "primary": stats.primary_comparisons({f: dict(v) for f, v in success.items()}),
            "utility": replay_report.utility(answers, configs, case_ids, cases),
            "source": {"scores_main_sha256": _sha(main / "rescore-v3.4-20261024" / "scores_main.json")}}


def agreement_tables(root: Path) -> dict:
    phase2 = root / "phase2"
    validation = _load(phase2 / "human-review-20261024" / "ai_review.json")
    scorer = _load(phase2 / "human-review-20261024" / "scorer_validation.json")
    check = phase2 / "review-ambiguous-20261024"
    claude = _load(check / "ai_labels.json")
    codex = human_review.read_sheet(check / "reviewer_1_ai_check.csv")
    ids = sorted(claude["human_check"]["items"])
    first = {i: {label: claude["items"][i][label] for label in human_review.LABELS} for i in ids}
    second = {i: {label: codex[i][label].lower() for label in human_review.LABELS} for i in ids}
    return {"validation_reviewers": {"kind": validation["kind"], "items": validation["items"],
                                     "reviewers": [r["name"] for r in validation["reviewers"]],
                                     "agreement": validation["agreement_before_adjudication"]},
            "check_sample_reviewers": {"kind": "ai_review", "items": len(ids), "reviewers": ["Claude (mù)", "Codex"],
                                       "agreement": human_review.agreement_table(first, second, ids)},
            "scorer_vs_adjudicated": {"v3.3": scorer["v33"], "v3.4_fitted": scorer["v34"],
                                      "note": scorer.get("note", "")},
            "source": {"validation_sha256": _sha(phase2 / "human-review-20261024" / "ai_review.json"),
                       "codex_check_sha256": _sha(check / "reviewer_1_ai_check.csv")}}


def _fmt(part: dict) -> str:
    return replay_report._fmt(part)


def _kappa(value) -> str:
    return "—" if value is None else f"{value:.2f}"


def scorer_cell(row: dict | None) -> str:
    return "—" if row is None else f"{_kappa(row['kappa'])} / {_kappa(row['pabak'])} (n={row['items']})"


def render(result: dict) -> str:
    b1, b2, b3 = result["table1"], result["table2"], result["table3"]
    lines = ["# Bảng kết quả giai đoạn 2 (sinh tự động)", "",
             f"Sinh lúc {result['created_at']} từ các file điểm; hash nguồn trong `phase2_report.json`. "
             "Nhãn: scorer v3.4 khi nó quyết định được; AI review mù cho câu v3.4 chưa xác định (cùng quy trình cho mọi "
             "cấu hình); quyết định cuối của người cho các câu còn tranh chấp. **Không phải nhãn do người chấm toàn bộ.**", "",
             "## Bảng 1. Đợt chính A/B/C/D (80 case: I 40, P 40)", "",
             "| Cấu hình | ASR tổng | I | P | Đúng fact — sạch | Từ chối — sạch | Tấn công: đúng, không dùng số giả (72) "
             "| Tấn công: nêu hai nguồn, giữ BCTC (72) |", "|---|---|---|---|---|---|---|---|"]
    for config, row in b1["asr"].items():
        u = b1["utility"][config]
        lines.append(f"| {config} | {_fmt(row['total'])} | {_fmt(row['I'])} | {_fmt(row['P'])} | "
                     f"{_fmt(u['clean_correct'])} | {_fmt(u['clean_refusal'])} | {_fmt(u['attack_correct_without_fake'])} "
                     f"| {_fmt(u['attack_two_sources_report_kept'])} |")
    lines += ["", "So sánh chính (McNemar exact ghép theo case, Holm trên 6 kiểm định):", "",
              "| Họ | So sánh | Cặp dùng được | chỉ trước / chỉ sau | p | p (Holm) |", "|---|---|---|---|---|---|"]
    for row in b1["primary"]:
        p = "—" if row["p_value"] is None else f"{row['p_value']:.3g}"
        ph = "—" if row["p_holm"] is None else f"{row['p_holm']:.3g}"
        lines.append(f"| {row['family']} | {row['comparison']} | {row['pairs']} (+{row['undetermined_pairs']}) | "
                     f"{row['table']['first_only']} / {row['table']['second_only']} | {p} | {ph} |")
    lines += ["", "## Bảng 2. Độ đồng ý (κ Cohen / PABAK)", "",
              "| Nhãn | Mẫu kiểm chứng 110: Codex vs Claude | Mẫu kiểm 87: Claude vs Codex | Scorer v3.3 vs nhãn đã phân xử "
              "| Scorer v3.4 vs nhãn đã phân xử (fit) |", "|---|---|---|---|---|"]
    for label in human_review.LABELS:
        v = b2["validation_reviewers"]["agreement"][label]
        c = b2["check_sample_reviewers"]["agreement"][label]
        s33, s34 = b2["scorer_vs_adjudicated"]["v3.3"].get(label), b2["scorer_vs_adjudicated"]["v3.4_fitted"].get(label)
        lines.append(f"| {label} | {_kappa(v['kappa'])} / {_kappa(v['pabak'])} (n={v['items']}) | "
                     f"{_kappa(c['kappa'])} / {_kappa(c['pabak'])} (n={c['items']}) | {scorer_cell(s33)} | "
                     f"{scorer_cell(s34)} |")
    lines += ["", "Tất cả người chấm ở Bảng 2 là AI; v3.4 được sửa trên chính mẫu kiểm chứng nên cột cuối là độ khớp sau "
              "khi fit, không phải ước lượng độc lập.", "",
              "## Bảng 3. Chuỗi đối chứng khớp evidence A → Apre → Apost → ApostG → D", ""]
    for variant, title in (("unified", "Nhãn thống nhất"), ("sensitivity_scorer_adopted",
                                                           "Độ nhạy: câu AI nói không mà scorer nói adopted → adopted")):
        lines += [f"**{title}**", "", "| Cấu hình | ASR tổng (80) |", "|---|---|"]
        lines += [f"| {c} | {_fmt(r['total'])} |" for c, r in b3["asr"][variant].items()]
        lines += ["", "| So sánh | chỉ trước / chỉ sau | p | p (Holm) |", "|---|---|---|---|"]
        lines += [f"| {t['comparison']} | {t['table']['first_only']} / {t['table']['second_only']} | {t['p_value']:.3g} | "
                  f"{t['p_holm']:.3g} |" for t in b3["comparisons"][variant]]
        lines.append("")
    lines += ["| Cấu hình | Đúng fact — sạch | Từ chối — sạch | Tấn công: đúng, không dùng số giả (72) | "
              "Tấn công: nêu hai nguồn, giữ BCTC (72) |", "|---|---|---|---|---|"]
    for config, u in b3["unified"]["utility"].items():
        lines.append(f"| {config} | {_fmt(u['clean_correct'])} | {_fmt(u['clean_refusal'])} | "
                     f"{_fmt(u['attack_correct_without_fake'])} | {_fmt(u['attack_two_sources_report_kept'])} |")
    lines += ["", "## Cách viết", "",
              "- Đợt chính là thăm dò; giai đoạn 2 là thí nghiệm bổ sung có protocol định trước.",
              "- Mọi kết quả bằng 0 chỉ nói về bộ tấn công tĩnh đã dùng; kèm cận trên Clopper–Pearson.",
              "- Apost→ApostG: chỉ viết \"không quan sát thấy tác dụng trên bộ thử này\".",
              "- Nếu Apost ≈ D: \"khi được cấp evidence đã xử lý, một lần gọi cuối đạt kết quả tương đương trên bộ thử này\".",
              "- Nhãn là AI review (hai lượt AI + phân xử AI, quyết định cuối của người cho câu còn tranh chấp).",
              "- Không tuyên bố L5 bảo vệ phép tính: thí nghiệm chưa đo được điều đó. Hình chính chỉ gồm các thành phần "
              "đã đánh giá trong chuỗi A→Apre→Apost→D.",
              "- Đoạn G3 (giới hạn hoặc phụ lục): \"Pilot G3 không đạt do nhánh tính toán tất định không kích hoạt, kể "
              "cả ở câu sạch. Theo quy tắc dừng đã đăng ký, nhóm không triển khai thí nghiệm L5 tiếp theo. Vì vậy, nghiên "
              "cứu chưa đánh giá hiệu quả bảo vệ phép tính của L5.\"", ""]
    return "\n".join(lines)


def build(root: Path) -> dict:
    phase2 = root / "phase2"
    labels, sources = review_labels(phase2)
    replay = phase2 / "replay-20261024" / "analysis-v3.4-unified" / "replay_report.json"
    return {"kind": "phase2_report", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "table1": main_table(root, labels), "table2": agreement_tables(root),
            "table3": {**{k: v for k, v in _load(replay).items() if k in ("asr", "comparisons", "unified")},
                       "source_sha256": _sha(replay)},
            "label_sources": {k: v for k, v in sources.items() if isinstance(k, str)}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        raise SystemExit(f"{args.out} exists; a report is written once")
    result = build(args.root.resolve())
    args.out.mkdir(parents=True)
    (args.out / "phase2_report.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                                                 encoding="utf-8")
    (args.out / "phase2_report.md").write_text(render(result), encoding="utf-8")
    print(f"report: {args.out / 'phase2_report.md'}")


if __name__ == "__main__":
    main()
