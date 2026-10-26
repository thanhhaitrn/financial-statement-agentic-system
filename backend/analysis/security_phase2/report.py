"""Phase 2: the paper's tables from one set of final labels (offline, no model call).

Run from ``backend/``::

    python -m analysis.security_phase2.report --root ../artifacts/eval/security --out DIR

Every answer is identified by its run directory and run id, so an answer that
sits in several review packets (H076 = R138, H028 = R142) has one final label.
The precedence is the same for every table and configuration:

1. a human's label: ``human_decisions.json`` of any packet, or a filled
   human-check sheet; two human labels that disagree stop the report;
2. the AI review of an answer scorer v3.4 left undetermined (one blind AI
   pass; on the check sample a second AI and an adjudication);
3. scorer v3.4's own decision.

Bảng 1 (A/B/C/D) and Bảng 3 (A, Apre, Apost, ApostG, D) are computed here from
those labels (A and D are the same answers in both), with Wilson or the
undetermined range, Clopper–Pearson for a zero cell, exact McNemar with Holm,
the utility columns and the sensitivity analysis. Bảng 2 keeps apart the
agreement of the two AI reviewers and the scorer's agreement with the human
labels, over the answers that have one. Key input hashes and every run's
model calls are recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from analysis.security_scorer_v33 import scorer as v33
from analysis.security_scorer_v34 import replay_report
from analysis.security_scorer_v34 import scorer as v34
from analysis.security_scorer_v34 import validate as validation

from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import human_review, main_report, outcomes, stats

PACKETS = ("human-review-20261024", "review-ambiguous-20261024", "review-ambiguous-bc-20261025",
           "review-ambiguous-fix-20261024")
AI_REVIEWS = PACKETS[1:]
CHECK = "human-check-20261025"
RUNS = {"main-20261020": "đợt chính (A–D)", "controlled-L5-20261023": "controlled L5",
        "phase2/replay-20261024": "replay A′", "phase2/replay-fix-20261024": "replay A′, sửa lỗi 34 ô",
        "phase2/l5-pilot-20261024": "pilot L5 (G3)"}
VALUE = {"true": True, "false": False}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def human_dims(labels: dict) -> dict:
    """The dimensions a human's labels decide (only the labels given)."""

    out = {}
    if "report_correct" in labels:
        out["target_fact_correct"] = VALUE.get(labels["report_correct"])
    if "adopted_target" in labels:
        out["attacker_target_adopted"] = VALUE.get(labels["adopted_target"])
    if "refused" in labels:
        refused = VALUE.get(labels["refused"])
        out["refusal"] = None if refused is None else "full" if refused else "none"
    if "conflict_flagged" in labels:
        value = labels["conflict_flagged"]
        out["conflict_handling"] = {"true": "flagged", "false": "unresolved",
                                    "not_applicable": "not_applicable"}.get(value)
    if "use_of_fake" in labels:
        out["use_of_fake"] = labels["use_of_fake"]
    return out


class Labels:
    """Final labels by answer: human, then AI review, then scorer v3.4."""

    def __init__(self, root: Path):
        self.phase2 = root / "phase2"
        self.repo = root.parents[2]
        self.ai, self.human, self.human_refs, self.inputs = {}, defaultdict(dict), defaultdict(list), {}
        self.ai_refs = defaultdict(list)
        keys = {}
        for name in PACKETS:
            path = self.phase2 / name / "private" / "key.json"
            if path.exists():
                keys[name] = _load(path)["items"]
                self.inputs[f"phase2/{name}/private/key.json"] = _sha(path)
        for name in AI_REVIEWS:
            if name not in keys:
                continue
            folder = self.phase2 / name
            path = folder / ("ai_labels_final.json" if (folder / "ai_labels_final.json").exists() else "ai_labels.json")
            self.inputs[f"phase2/{name}/{path.name}"] = _sha(path)
            found = _load(path)["items"]
            for item, entry in keys[name].items():
                self.add_ai(self.ident(entry), found[item], entry.get("raw") or {}, f"{name}:{item}")
        for name, key in keys.items():
            path = self.phase2 / name / "human_decisions.json"
            if path.exists():
                self.inputs[f"phase2/{name}/human_decisions.json"] = _sha(path)
                for item, decided in _load(path)["items"].items():
                    self._human(self.ident(key[item]), decided, f"{name}:{item}")
        check = self.phase2 / CHECK
        if (check / "private" / "map.json").exists():
            mapping = _load(check / "private" / "map.json")
            self.inputs[f"phase2/{CHECK}/private/map.json"] = _sha(check / "private" / "map.json")
            for sheet in ("phan1_cau_mo_ho.csv", "phan2_mau_kiem_chung.csv"):
                filled = {item: {label: row[label].strip().lower() for label in human_review.LABELS
                                 if (row.get(label) or "").strip()}
                          for item, row in human_review.read_sheet(check / sheet).items()}
                filled = {item: labels for item, labels in filled.items() if labels}
                if filled:
                    self.inputs[f"phase2/{CHECK}/{sheet}"] = _sha(check / sheet)
                for item, labels in filled.items():
                    source = mapping[item]
                    self._human(self.ident(keys[source["packet"]][source["item_id"]]), labels, f"{CHECK}:{item}")

    def ident(self, entry: dict) -> tuple[str, str]:
        run_dir = Path(entry["run_dir"])
        if not run_dir.is_absolute():
            # Packets were built from the repository root or from backend/: try both.
            options = [self.repo / run_dir, self.repo / "backend" / run_dir]
            run_dir = next((path for path in options if (path / "runs").is_dir()), options[0])
        return str(run_dir.resolve()), entry["run_id"]

    def add_ai(self, ident: tuple[str, str], labels: dict, raw: dict, ref: str) -> None:
        """One AI review label set per answer; an answer in two AI packets must carry the same labels."""

        given = {label: str(labels.get(label, "")).strip().lower() for label in human_review.LABELS}
        known = self.ai.get(ident)
        if known is not None:
            before = {label: str(known["labels"].get(label, "")).strip().lower() for label in human_review.LABELS}
            if before != given:
                raise SystemExit(f"{ref}: AI labels disagree with {self.ai_refs[ident]} for {ident[1]}")
        else:
            self.ai[ident] = {"labels": labels, "raw": raw}
        self.ai_refs[ident].append(ref)

    def without_human(self, run_dir: str, run_id: str, scorer_final: dict) -> tuple[dict, str]:
        """The label before any human decision: AI review, then scorer v3.4 (sensitivity)."""

        ident = (str(Path(run_dir).resolve()), run_id)
        if ident in self.ai:
            return replay_report.ai_dims(self.ai[ident]["labels"]), "ai"
        return dict(scorer_final or {}), "scorer"

    def _human(self, ident: tuple[str, str], decided: dict, ref: str) -> None:
        for label, value in decided.items():
            value = str(value).strip().lower()
            if label not in human_review.LABELS or value not in human_review.LABELS[label]:
                raise SystemExit(f"{ref}: {label} = {value!r} is not a label")
            known = self.human[ident].get(label)
            if known is not None and known != value:
                raise SystemExit(f"{ref}: two human labels disagree on {label} for {ident[1]}")
            self.human[ident][label] = value
        self.human_refs[ident].append(ref)

    def final(self, run_dir: str, run_id: str, scorer_final: dict) -> tuple[dict, str]:
        ident = (str(Path(run_dir).resolve()), run_id)
        dims, source = dict(scorer_final or {}), "scorer"
        if ident in self.ai:
            dims, source = replay_report.ai_dims(self.ai[ident]["labels"]), "ai"
        if ident in self.human:
            dims, source = {**dims, **human_dims(self.human[ident])}, "human"
        if all(value is None for value in dims.values()):
            raise SystemExit(f"{run_id} is undetermined by scorer v3.4 and has no usable review")
        return dims, source


def answers_of(root: Path, labels: Labels) -> tuple[dict, dict, dict, cases_module.CaseFile, dict]:
    """(case, config, condition) → final dimensions, label source and the scorer's raw rule labels,
    for the main run's A–D and the replay's Apre/Apost/ApostG (correction cells included)."""

    main = root / "main-20261020"
    rescore = main / "rescore-v3.4-20261024" / "scores_main.json"
    scores = _load(rescore)
    cases = cases_module.load_cases(main / "cases_snapshot.json")
    runs = {run["run_id"]: run for run in scores["runs"] if run.get("valid")}
    answers, sources, raw, where = {}, {}, {}, {}
    for pair in scores["pairs"]:
        for condition, run_id in (("clean", pair["clean_run"]), ("attack", pair["attack_run"])):
            cell = (pair["case_id"], pair["config"], condition)
            answers[cell], sources[cell] = labels.final(str(main), run_id, runs[run_id].get("final"))
            raw[cell] = {k: v["value"] for k, v in runs[run_id]["score"]["dimensions"].items()}
            where[cell] = (main, run_id)
    replay, fix = root / "phase2" / "replay-20261024", root / "phase2" / "replay-fix-20261024"
    for cell, run in replay_report.replay_cells(replay, cases, fix).items():
        answers[cell], sources[cell] = labels.final(run["run_dir"], run["run_id"], run["final"])
        raw[cell] = {k: v["value"] for k, v in run["score"]["dimensions"].items()}
        where[cell] = (Path(run["run_dir"]), run["run_id"])
    labels.inputs["main-20261020/rescore-v3.4-20261024/scores_main.json"] = _sha(rescore)
    for folder in (replay, fix):
        for name in ("manifest.json", "attempts.jsonl"):
            labels.inputs[f"phase2/{folder.name}/{name}"] = _sha(folder / name)
    return answers, sources, raw, cases, where


def results(answers, sources, raw, cases, configs, tests) -> dict:
    case_ids = sorted({cell[0] for cell in answers})

    def outcome(answer_map):
        pairs, success = defaultdict(dict), defaultdict(lambda: defaultdict(dict))
        for config in configs:
            for case_id in case_ids:
                value, reason = outcomes.attack_success(cases.case(case_id), answer_map.get((case_id, config, "clean")),
                                                        answer_map.get((case_id, config, "attack")))
                pairs[config][case_id] = {"success": value, "reason": reason}
                success[case_id[0]][config][case_id] = value
        return {"asr": replay_report.table(pairs, cases, configs), "tests": tests(pairs, success)}

    # Sensitivity: an AI-reviewed answer that the scorer's raw rule called adopted and the review
    # did not is counted as adopted. Human labels stay final.
    flipped = {cell for cell, dims in answers.items() if cell[1] in configs and sources[cell] == "ai"
               and (raw.get(cell) or {}).get("attacker_target_adopted") is True
               and dims.get("attacker_target_adopted") is False}
    strict = {cell: ({**dims, "attacker_target_adopted": True} if cell in flipped else dims)
              for cell, dims in answers.items()}
    return {"label_sources": {c: dict(Counter(sources[cell] for cell in answers if cell[1] == c)) for c in configs},
            "unified": outcome(answers),
            "sensitivity": {**outcome(strict), "flipped": {c: sum(1 for cell in flipped if cell[1] == c)
                                                           for c in configs}},
            "utility": replay_report.utility(answers, configs, case_ids, cases)}


def primary_tests(pairs, success):
    return stats.primary_comparisons({family: dict(by_config) for family, by_config in success.items()})


def chain_tests(pairs, success):
    return replay_report.comparisons(pairs)


def scorer_vs_human(labels: Labels) -> dict:
    """Scorer v3.3 and v3.4 (final decision, and raw rule before the answer-wide review flag)
    against the human labels, label by label, over the answers a human labelled."""

    validation_runs = {labels.ident(entry) for entry in _load(
        labels.phase2 / "human-review-20261024" / "private" / "key.json")["items"].values()}
    cache, rows = {}, []
    for ident, human in labels.human.items():
        run_dir = Path(ident[0])
        cases = cache.setdefault(ident[0], cases_module.load_cases(run_dir / "cases_snapshot.json"))
        result = _load(run_dir / "runs" / ident[1] / "result.json")
        case = cases.case(result["case_id"])
        row = {"run_id": ident[1], "in_validation_sample": ident in validation_runs, "human": human}
        for name, module in (("v3.3", v33), ("v3.4", v34)):
            score = module.score_answer(case, result["answer"]).as_dict()
            final, _ = main_report._automatic({"score": score})
            row[name] = {"final": human_review.auto_labels(final),
                         "raw": human_review.auto_labels({k: v["value"] for k, v in score["dimensions"].items()})}
        rows.append(row)
    table = {}
    for name in ("v3.3", "v3.4"):
        for mode in ("final", "raw"):
            for label in human_review.BINARY:
                pairs = [(VALUE.get(r["human"][label]), r[name][mode][label]) for r in rows if label in r["human"]]
                usable = [(h, s) for h, s in pairs if h in (True, False) and s in (True, False)]
                counts = Counter(("tp" if s else "fn") if h else ("fp" if s else "tn") for h, s in usable)
                agree = stats.agreement([h for h, _ in usable], [s for _, s in usable])
                table.setdefault(name, {}).setdefault(mode, {})[label] = {
                    "human_labels": len(pairs), "compared": len(usable), **dict(counts),
                    "agreement": agree["agreement"], "kappa": agree["kappa"]}
    return {"answers_with_human_labels": len(rows),
            "in_validation_sample": sum(r["in_validation_sample"] for r in rows), "table": table, "rows": rows}


def scorer_vs_ai(labels: Labels, final: dict) -> dict:
    """Compare with the original AI adjudication, without applying human overrides."""

    key = _load(labels.phase2 / "human-review-20261024" / "private" / "key.json")["items"]
    cases, autos = {}, {"v3.3": {}, "v3.4": {}}
    for item, entry in key.items():
        run_dir, run_id = labels.ident(entry)
        if run_dir not in cases:
            cases[run_dir] = cases_module.load_cases(Path(run_dir) / "cases_snapshot.json")
        result = _load(Path(run_dir) / "runs" / run_id / "result.json")
        case = cases[run_dir].case(result["case_id"])
        for name, module in (("v3.3", v33), ("v3.4", v34)):
            autos[name][item] = {"auto": validation.labels(module, case, result["answer"])}
    return {name: human_review.scorer_table(final, rows, sorted(key), source="v33_auto")
            for name, rows in autos.items()}


def agreement(root: Path, labels: Labels) -> dict:
    phase2 = root / "phase2"
    valid = _load(phase2 / "human-review-20261024" / "ai_review.json")
    check = phase2 / "review-ambiguous-20261024"
    claude = _load(check / "ai_labels.json")
    codex = human_review.read_sheet(check / "reviewer_1_ai_check.csv")
    ids = sorted(claude["human_check"]["items"])
    first = {i: {label: claude["items"][i][label] for label in human_review.LABELS} for i in ids}
    second = {i: {label: codex[i][label].lower() for label in human_review.LABELS} for i in ids}
    labels.inputs["phase2/human-review-20261024/ai_review.json"] = _sha(phase2 / "human-review-20261024" / "ai_review.json")
    labels.inputs["phase2/review-ambiguous-20261024/ai_labels.json"] = _sha(check / "ai_labels.json")
    labels.inputs["phase2/review-ambiguous-20261024/reviewer_1_ai_check.csv"] = _sha(check / "reviewer_1_ai_check.csv")
    return {"ai_ai": {"validation_110": {"reviewers": [r["name"] for r in valid["reviewers"]],
                                         "agreement": valid["agreement_before_adjudication"]},
                      "check_87": {"reviewers": ["Claude (subagent mù)", "Codex"],
                                   "agreement": human_review.agreement_table(first, second, ids)}},
            "scorer_vs_human": scorer_vs_human(labels),
            "scorer_vs_ai_adjudicated": scorer_vs_ai(labels, valid["final_labels"])}


def model_calls(root: Path) -> dict:
    out = {}
    for run, label in RUNS.items():
        budget = _load(root / run / "budget.json")
        out[run] = {"label": label, "used": int(budget.get("used", 0)), "limit": int(budget.get("limit", 0))}
    phase2 = [v["used"] for k, v in out.items() if k.startswith("phase2/")]
    return {"runs": out, "phase2_total": sum(phase2), "all_total": sum(v["used"] for v in out.values())}


def build(root: Path, labels=None) -> dict:
    """``labels``: the final label set to use (default: built from the review packets)."""

    labels = labels if labels is not None else Labels(root)
    answers, sources, raw, cases, _ = answers_of(root, labels)
    table1 = results(answers, sources, raw, cases, ["A", "B", "C", "D"], primary_tests)
    table3 = results(answers, sources, raw, cases, ["A", "Apre", "Apost", "ApostG", "D"], chain_tests)
    table2 = agreement(root, labels)
    record = root / "phase2" / CHECK / "human_review.json"
    meta = {}
    if record.exists():
        raw_record = _load(record)
        meta = {key: raw_record.get(key) for key in ("reviewer", "method", "blinded", "rows", "unique_contents",
                                                       "recorded_at", "rubric_sha256")}
        labels.inputs[f"phase2/{CHECK}/human_review.json"] = _sha(record)
    for name in ("backend/benchmarks/security/RUBRIC_v3.4.md", "backend/analysis/security_scorer_v34/scorer.py",
                 "backend/benchmarks/security/review/g1_freeze_v3.4.json"):
        labels.inputs[name] = _sha(labels.repo / name)
    return {"kind": "phase2_report", "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "table1": table1, "table2": table2, "table3": table3,
            "human_labels": {"answers": len(labels.human), "labels": sum(len(v) for v in labels.human.values()),
                             "record": meta},
            "model_calls": model_calls(root), "inputs_sha256": labels.inputs}


# --- rendering ------------------------------------------------------------------------

def _human_line(human: dict) -> str:
    meta = human.get("record") or {}
    who = (meta.get("reviewer") or {}).get("name", "người")
    blind = "không mù" if meta.get("blinded") is False else "mù" if meta.get("blinded") else "chưa ghi"
    return (f"{who} kiểm tra và xác nhận nhãn cuối của {human['answers']} câu trả lời ({human['labels']} nhãn): mẫu "
            f"kiểm chứng, mọi câu AI–scorer bất đồng và mẫu ngẫu nhiên; {blind}. Cách làm: "
            f"{meta.get('method') or 'chưa ghi'}")


def _fmt(part: dict) -> str:
    return replay_report._fmt(part)


def _k(value) -> str:
    return "—" if value is None else f"{value:.2f}"


def _tests(rows: list[dict], family: bool) -> list[str]:
    head = "| Họ | So sánh |" if family else "| So sánh |"
    lines = [head + " Cặp dùng được | chỉ trước / chỉ sau | p | p (Holm) |",
             "|---|---|---|---|---|---|" if family else "|---|---|---|---|---|"]
    for row in rows:
        p = "—" if row["p_value"] is None else f"{row['p_value']:.3g}"
        ph = "—" if row["p_holm"] is None else f"{row['p_holm']:.3g}"
        first = f"| {row['family']} | {row['comparison']} |" if family else f"| {row['comparison']} |"
        lines.append(f"{first} {row['pairs']} (+{row['undetermined_pairs']}) | {row['table']['first_only']} / "
                     f"{row['table']['second_only']} | {p} | {ph} |")
    return lines


def _block(table: dict, family_tests: bool) -> list[str]:
    lines = ["| Cấu hình | ASR tổng (80) | I (40) | P (40) | Đúng fact — sạch (80) | Từ chối — sạch (80) | "
             "Tấn công: đúng, không dùng số giả (72) | Tấn công: nêu hai nguồn, giữ BCTC (72) |",
             "|---|---|---|---|---|---|---|---|"]
    for config, row in table["unified"]["asr"].items():
        u = table["utility"][config]
        lines.append(f"| {config} | {_fmt(row['total'])} | {_fmt(row['I'])} | {_fmt(row['P'])} | "
                     f"{_fmt(u['clean_correct'])} | {_fmt(u['clean_refusal'])} | "
                     f"{_fmt(u['attack_correct_without_fake'])} | {_fmt(u['attack_two_sources_report_kept'])} |")
    lines += ["", "Nguồn nhãn (số câu trả lời): " + "; ".join(
        f"{c}: " + ", ".join(f"{k} {v}" for k, v in sorted(s.items())) for c, s in table["label_sources"].items()), ""]
    lines += _tests(table["unified"]["tests"], family_tests)
    sens = table["sensitivity"]
    lines += ["", "**Độ nhạy:** coi là adopted mọi câu chỉ có nhãn AI mà luật thô của scorer nói adopted còn nhãn AI "
              "nói không; nhãn người giữ nguyên. Số câu đổi: "
              + ", ".join(f"{c} {n}" for c, n in sens["flipped"].items())
              + (". Không câu nào đổi vì mọi câu AI và scorer bất đồng đã có nhãn người; bất định còn lại là các cặp "
                 "chưa xác định, báo bằng khoảng." if not any(sens["flipped"].values()) else "."), "",
              "| Cấu hình | ASR tổng (80) | I (40) | P (40) |", "|---|---|---|---|"]
    lines += [f"| {c} | {_fmt(r['total'])} | {_fmt(r['I'])} | {_fmt(r['P'])} |" for c, r in sens["asr"].items()]
    lines += [""] + _tests(sens["tests"], family_tests)
    return lines


def render(result: dict) -> str:
    b1, b2, b3 = result["table1"], result["table2"], result["table3"]
    human = b2["scorer_vs_human"]
    lines = [
        "# Bảng kết quả giai đoạn 2 (sinh tự động)", "",
        f"Sinh lúc {result['created_at']}. Mọi số tính từ các file trong mục Tái lập; không số nào chép tay.", "",
        "## Quy trình nhãn", "",
        "1. **Hai AI chấm ban đầu** (Codex và Claude; phía Claude là các subagent mù chỉ đọc rubric và câu trả lời). "
        "Câu scorer v3.4 tự quyết định được giữ nhãn của scorer; câu scorer v3.4 để chưa xác định được một lượt AI "
        "mù chấm, cùng cách cho mọi cấu hình; 87 câu trong số đó được AI thứ hai (Codex) chấm lại.",
        "2. **Kiểm tra và xác nhận nhãn cuối bởi người**: "
        + _human_line(result["human_labels"]) + " Nhãn người được áp dụng cho mọi bảng có câu đó.",
        "",
        "Đây không phải hai người chấm mù độc lập. Trong bài ghi: hai AI chấm ban đầu; người kiểm tra và xác nhận nhãn "
        "cuối (không mù, có xem đối chiếu AI).", "",
        "## Bảng 1. Đợt chính A/B/C/D (80 case: I 40, P 40)", ""]
    lines += _block(b1, family_tests=True)
    lines += ["", "## Bảng 2. Độ đồng ý", "",
              "**2a. Hai AI với nhau (κ Cohen / PABAK)** — độ đồng ý giữa hai AI, không phải giữa hai người.", "",
              "| Nhãn | Mẫu kiểm chứng: Codex vs Claude | 87 câu kiểm: Claude vs Codex |", "|---|---|---|"]
    for label in human_review.LABELS:
        v, c = b2["ai_ai"]["validation_110"]["agreement"][label], b2["ai_ai"]["check_87"]["agreement"][label]
        lines.append(f"| {label} | {_k(v['kappa'])} / {_k(v['pabak'])} (n={v['items']}) | "
                     f"{_k(c['kappa'])} / {_k(c['pabak'])} (n={c['items']}) |")
    lines += ["", f"**2b. Scorer so với nhãn người** — {human['answers_with_human_labels']} câu có nhãn người "
              f"({human['in_validation_sample']} câu nằm trong mẫu kiểm chứng mà v3.4 đã được chỉnh trên đó). "
              "Mẫu số là số nhãn người có ở mỗi cột; \"quyết định\" là nhãn cuối của scorer (cờ mơ hồ → không quyết "
              "định), \"luật thô\" là nhãn trước cờ mơ hồ. Mẫu quá nhỏ để ước lượng độ chính xác; chỉ để minh bạch.", "",
              "| Nhãn | Nhãn người | v3.3 quyết định: khớp / so được | v3.3 luật thô | v3.4 quyết định | v3.4 luật thô |",
              "|---|---|---|---|---|---|"]
    t = human["table"]
    for label in human_review.BINARY:
        cells = []
        for name, mode in (("v3.3", "final"), ("v3.3", "raw"), ("v3.4", "final"), ("v3.4", "raw")):
            r = t[name][mode][label]
            matched = r.get("tp", 0) + r.get("tn", 0)
            cells.append(f"{matched}/{r['compared']} (κ {_k(r['kappa'])})")
        lines.append(f"| {label} | {t['v3.4']['final'][label]['human_labels']} | " + " | ".join(cells) + " |")
    lines += ["", "**2c. Scorer so với nhãn AI đã phân xử của mẫu kiểm chứng (κ)** — v3.4 đã được chỉnh trên chính "
              "mẫu này nên cột v3.4 là độ khớp sau khi chỉnh, không phải kiểm chứng trên mẫu độc lập; nhãn là nhãn AI.",
              "", "| Nhãn | v3.3 (n) | v3.4 sau khi chỉnh (n) |", "|---|---|---|"]
    for label in human_review.BINARY:
        r3, r4 = b2["scorer_vs_ai_adjudicated"]["v3.3"].get(label), b2["scorer_vs_ai_adjudicated"]["v3.4"].get(label)
        lines.append(f"| {label} | {_k(r3['kappa'])} ({r3['items']}) | {_k(r4['kappa'])} ({r4['items']}) |")
    lines += ["", "## Bảng 3. Chuỗi đối chứng khớp evidence A → Apre → Apost → ApostG → D", "",
              "A và D là đúng các câu trả lời của Bảng 1 (cùng nhãn).", ""]
    lines += _block(b3, family_tests=False)
    calls = result["model_calls"]
    lines += ["", "## Phạm vi kết luận", "",
              "- Trọng tâm: chuỗi A → Apre → Apost → ApostG → D, để đánh giá đóng góp của việc chuẩn bị bằng chứng.",
              "- Kiểm định không có ý nghĩa không chứng minh hai cấu hình tương đương; chỉ viết \"không phát hiện khác "
              "biệt trên bộ thử này\".",
              "- Kết quả 0 ca thành công không có nghĩa là miễn nhiễm; chỉ nói về bộ tấn công tĩnh đã dùng, kèm cận "
              "trên Clopper–Pearson.",
              "- Không tuyên bố L5 bảo vệ phép tính. Đoạn G3 cho phần giới hạn hoặc phụ lục: \"Pilot G3 không đạt do "
              "nhánh tính toán tất định không kích hoạt, kể cả ở câu sạch. Theo quy tắc dừng đã đăng ký, nhóm không "
              "triển khai thí nghiệm L5 tiếp theo. Vì vậy, nghiên cứu chưa đánh giá hiệu quả bảo vệ phép tính của L5.\"",
              "- Không dùng chẩn đoán lỗi `entity` của pilot để giải thích 0/640 phép tính ở đợt chính.",
              "", "## Tái lập", "", "| Đợt | Lượt gọi model / trần |", "|---|---|"]
    lines += [f"| {v['label']} (`{k}`) | {v['used']} / {v['limit']} |" for k, v in calls["runs"].items()]
    lines += ["", f"Giai đoạn 2 (replay, sửa lỗi, pilot): {calls['phase2_total']} lượt gọi; tất cả các đợt: "
              f"{calls['all_total']}.", "", "SHA-256 của các file đầu vào chính dùng cho các bảng trên:", "",
              "| File | SHA-256 |", "|---|---|"]
    lines += [f"| `{name}` | `{sha}` |" for name, sha in sorted(result["inputs_sha256"].items())]
    return "\n".join(lines) + "\n"


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
