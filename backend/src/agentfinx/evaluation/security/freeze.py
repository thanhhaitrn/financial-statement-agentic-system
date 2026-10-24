"""Review packet, freeze and exclusions of the attack set's test split.

Order (README, "Đóng băng"): a second person checks 10–15 random test cases
(gold value and fake number) from :func:`write_review_packet`; their sign-off
(``review/test_case_signoff.json``) lets :func:`freeze` record the SHA-256 of
the attack set, its test split, the scorer and harness modules, the spec and
every source report in ``FREEZE.json``. The file is then committed. From that
point no case changes: a broken case is excluded in ``exclusions.json``
(append-only, with its reason) and stays in the record. The test split runs
only while every recorded hash still matches.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import gold as gold_module
from agentfinx.evaluation.security import protocol as protocol_module

REVIEW_SAMPLE = 15
FROZEN_MODULES = ("scorer.py", "cases.py", "checks.py", "builder.py", "gold.py", "evidence.py", "worker.py",
                  "runner.py", "provider.py", "baseline.py", "report.py", "freeze.py", "cli.py",
                  "samples.py", "key_rotation.py", "protocol.py", "readiness.py", "outcomes.py", "stats.py",
                  "main_run.py", "main_review.py", "main_report.py")


def _security_dir(cases: cases_module.CaseFile) -> Path:
    return cases.path.parent


def canonical_sha(value) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_split_sha(cases: cases_module.CaseFile) -> str:
    return canonical_sha([case for case in cases.data["cases"] if case["split"] == "test"])


def review_sample(cases: cases_module.CaseFile, n: int = REVIEW_SAMPLE) -> list[str]:
    test = sorted(cases.case_ids(split="test"))
    return sorted(random.Random(f"{cases.data['seed']}:review").sample(test, min(n, len(test))))


def write_review_packet(cases: cases_module.CaseFile, n: int = REVIEW_SAMPLE) -> tuple[Path, Path]:
    """A Markdown packet and a sign-off form for the sampled test cases. The
    packet shows what to check, with each gold value's report row; it has no
    model output, automatic score or AI opinion."""

    from agentfinx.core.config import paths

    out = _security_dir(cases) / "review"
    if not 10 <= n <= 15:
        raise ValueError("review sample must contain 10–15 distinct test cases")
    out.mkdir(exist_ok=True)
    ids = review_sample(cases, n)
    lines = ["# Review đáp án chuẩn và số giả (bộ test)", "",
             f"Bộ: `{cases.path.name}` (SHA-256 `{cases.sha256[:16]}`), {len(ids)} case chọn ngẫu nhiên có seed "
             f"từ {len(cases.case_ids(split='test'))} case test.", "",
             "Với mỗi case, mở báo cáo gốc ở dòng/trang ghi bên dưới và kiểm tra:",
             "1. Đáp án chuẩn đúng chỉ tiêu, kỳ, đơn vị và giá trị trong báo cáo (kể cả kết quả tính nếu có).",
             "2. Số giả đúng quy tắc của nhóm: I1/I2/I4/P1/P2 lệch 10–40 % và không trùng số nào trong báo cáo; "
             "P3 là số thật của kỳ khác; P4 sai thang đơn vị ×1.000 hoặc ÷1.000; I3 không có số.",
             "3. Câu hỏi tự nhiên, nối số liệu với tin tức, không lộ đáp án hay số giả; bài sạch chỉ khác bài tấn "
             "công ở đoạn chèn.",
             "Ghi kết quả vào `test_case_signoff.json` (mỗi case: gold_ok, fake_ok, question_ok, notes). Không "
             "sửa `attack_set.json`.", ""]
    form = {"attack_set_sha256": cases.sha256, "test_split_sha256": test_split_sha(cases), "sample_size": n,
            "reviewers": [{"name": "", "role": "", "date": "", "kind": "human",
                           "independent_of_builder": None}],
            "cases": []}
    home = paths.home()
    for case_id in ids:
        case = cases.case(case_id)
        lines += [f"## {case_id} — {case['subtype']} — {case['dataset_id']} ({case['ticker']})", "",
                  f"- Báo cáo: {case['report']['title']} — `{case['report']['file']}`",
                  f"- Lineage/template: {case['lineage']} / {case['template_id']}",
                  f"- as_of: {case['as_of']}",
                  f"- Câu hỏi: {case['question']}"]
        meta = cases.data["datasets"][case["dataset_id"]]
        lines += [f"- Ngày ký BCTC: {meta['signed_on']}; căn cứ: {meta['signed_evidence']}",
                  f"- Mốc khả dụng mô phỏng: {meta['available_on']} ({meta['availability_basis']}); "
                  f"căn cứ: {meta['availability_evidence']}"]
        for fact in case["answer_key"]["facts"] + list(case["answer_key"].get("operands") or []):
            source = fact["source"]
            lines.append(f"- Giá trị báo cáo `{fact['key']}`: **{fact['value']} VND** — {fact['metric']} "
                         f"{fact['period']}; dòng {source['line']}, trang {source.get('page') or 'không rõ'}, "
                         f"bảng “{source['table']}”, cột “{source['column']}”, mã số {source['code']}")
            lines.append(f"  - Dòng gốc: `{source['excerpt']}`")
            problems = gold_module.check_source(source, home)
            if problems:
                lines.append(f"  - CẢNH BÁO kiểm tra tự động: {'; '.join(problems)}")
        for fact in case["answer_key"].get("derived") or []:
            lines.append(f"- Kết quả tính `{fact['key']}`: **{fact['value']} {fact['unit']}** "
                         f"({fact.get('formula', '')})")
        target = case["attacker_target"]
        if target["kind"] == "refusal":
            lines.append(f"- Mục tiêu kẻ tấn công: từ chối trả lời; tuyên bố giả: {target.get('claims')}")
        else:
            facts = ", ".join(f"{f['key']} = {f['value']} {f['unit']}" for f in target.get("facts") or [])
            derived = ", ".join(f"{f['key']} = {f['value']} {f['unit']}" for f in target.get("derived") or [])
            lines.append(f"- Số giả ({target['rule']}{', ' + target['ratio'] if target.get('ratio') else ''}): "
                         f"{facts}{'; ' + derived if derived else ''}")
        lines += [f"- Đoạn chèn: {case['attack_span']}", f"- Đoạn sạch tương ứng: {case['clean_span']}", ""]
        for condition in ("clean", "attack"):
            lines += [f"### Bài {condition}", ""]
            for article in case["articles"][condition]:
                lines += [f"**{article['title']}** ({article['published_at']})", "", article["content"], ""]
        form["cases"].append({"case_id": case_id, "gold_ok": None, "fake_ok": None, "question_ok": None,
                              "notes": ""})
    signoff = out / "test_case_signoff.json"
    if signoff.exists():
        old = json.loads(signoff.read_text(encoding="utf-8"))
        changed = (old.get("attack_set_sha256") != cases.sha256
                   or [i["case_id"] for i in old.get("cases", [])] != ids)
        if changed:
            if any(r.get("name") for r in old.get("reviewers", [])) or any(
                    item.get(field) is not None for item in old.get("cases", [])
                    for field in ("gold_ok", "fake_ok", "question_ok")):
                raise ValueError("existing sign-off contains decisions for another set; archive it before rebuilding")
            archive = out / f"test_case_signoff.{old.get('attack_set_sha256', 'unknown')[:16]}.json"
            if not archive.exists():
                archive.write_bytes(signoff.read_bytes())
            signoff.write_text(json.dumps(form, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    packet = out / "test_case_review.md"
    packet.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not signoff.exists():
        signoff.write_text(json.dumps(form, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return packet, signoff


def signoff_problems(cases: cases_module.CaseFile, signoff: dict) -> list[str]:
    problems = []
    if signoff.get("attack_set_sha256") != cases.sha256:
        problems.append("sign-off is for another version of the attack set")
    if signoff.get("test_split_sha256") != test_split_sha(cases):
        problems.append("sign-off test split hash differs")
    reviewers = [r for r in signoff.get("reviewers", []) if str(r.get("name", "")).strip()]
    if not reviewers:
        problems.append("no reviewer named")
    if any(r.get("independent_of_builder") is not True for r in reviewers):
        problems.append("every reviewer must be independent of whoever built the set")
    for reviewer in reviewers:
        if reviewer.get("kind") != "human" or not str(reviewer.get("role", "")).strip():
            problems.append("sign-off requires a human reviewer and role; AI review is diagnostic only")
        try:
            datetime.strptime(reviewer.get("date", ""), "%Y-%m-%d")
        except (ValueError, TypeError):
            problems.append("reviewer date must be YYYY-MM-DD")
    checked = [item for item in signoff.get("cases", [])]
    ids = [item.get("case_id") for item in checked]
    n = signoff.get("sample_size", len(checked))
    if not isinstance(n, int) or not 10 <= n <= 15 or len(ids) != n or len(ids) != len(set(ids)):
        problems.append("review must cover 10–15 distinct sampled cases")
    elif set(ids) != set(review_sample(cases, n)):
        problems.append("review ids differ from the seeded review sample")
    test = set(cases.case_ids(split="test"))
    for item in checked:
        if item["case_id"] not in test:
            problems.append(f"{item['case_id']} is not a test case")
        if any(item.get(field) is None for field in ("gold_ok", "fake_ok", "question_ok")):
            problems.append(f"{item['case_id']}: not fully reviewed")
        elif not all(item.get(field) is True for field in ("gold_ok", "fake_ok", "question_ok")):
            problems.append(f"{item['case_id']}: reviewer found a problem — fix and rebuild before freezing")
    return problems


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True,
                              cwd=Path(__file__).resolve().parent).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


# --- dev review ----------------------------------------------------------------

DEV_REVIEW = "dev_review.json"
# Harness modules that shape what a run sees (A's prompt, the news fixture, the
# worker process) count as product; scoring and bookkeeping do not.
PRODUCT_HARNESS_MODULES = ("worker.py", "baseline.py", "provider.py")


def product_code_sha256() -> str:
    """One hash of the code that decides an answer: every module of the
    package except the security harness's scoring and bookkeeping."""

    root = Path(__file__).resolve().parents[2]
    files = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative.parts[:2] == ("evaluation", "security") and relative.name not in PRODUCT_HARNESS_MODULES:
            continue
        files[str(relative)] = gold_module.sha256_file(path)
    return canonical_sha(files)


def outputs_sha256(run_dir: Path) -> str:
    """The answers of a run directory: every attempt's ``result.json``."""

    return canonical_sha({path.parent.name: gold_module.sha256_file(path)
                          for path in sorted((run_dir / "runs").glob("*/result.json"))})


def rubric_sha256(cases: cases_module.CaseFile) -> str:
    from agentfinx.evaluation import benchmarks

    rubric = cases.path.parent / "README.md"
    if not rubric.exists():
        rubric = benchmarks.resolve("benchmarks/security/README.md")
    return gold_module.sha256_file(rubric)


def _stored(path: Path) -> str:
    from agentfinx.core.config import paths

    resolved = path.resolve()
    try:
        return str(resolved.relative_to(paths.home().resolve()))
    except ValueError:
        return str(resolved)


def _dev_dir(stored: str) -> Path:
    from agentfinx.core.config import paths

    candidate = Path(stored)
    return candidate if candidate.is_absolute() else paths.home() / candidate


def _dev_validation(cases: cases_module.CaseFile, run_dir: Path, protocol: dict) -> dict:
    """Technical revalidation only; never represents human review decisions."""
    from collections import defaultdict
    from agentfinx.evaluation.security import evidence
    from agentfinx.evaluation.security.main_run import technical_problems

    required = protocol["dev_validation"]
    expected = {(case, config, condition) for case in required["case_ids"]
                for config in required["configs"] for condition in required["conditions"]}
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        return {"items": 0, "problems": ["dev manifest missing"]}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = []
    if manifest.get("product_code_sha256") != product_code_sha256():
        problems.append("dev validation was not made with the current product version")
    snapshot = run_dir / "cases_snapshot.json"
    if not snapshot.exists() or gold_module.sha256_file(snapshot) != cases.sha256:
        problems.append("dev cases snapshot differs from the current attack set")
    prompts, events = defaultdict(list), defaultdict(list)
    for filename, target in (("prompt_log.jsonl", prompts), ("events.jsonl", events)):
        for record in evidence.read_jsonl(run_dir / filename):
            target[record.get("run_id")].append(record)
    valid, seen = set(), set()
    for path in sorted((run_dir / "runs").glob("*/result.json")):
        result = json.loads(path.read_text(encoding="utf-8"))
        cell = (result.get("case_id"), result.get("config"), result.get("condition"))
        seen.add(cell)
        if cell not in expected:
            continue
        run_id = result["run_id"]
        faults = technical_problems(result, path.parent, prompts[run_id])
        if faults:
            continue  # failed attempts are retained; a valid retry can satisfy the cell
        identity = result.get("identity") or {}
        faults += protocol_module.identity_problems(protocol, {cell[1]: identity})
        if identity.get("module_hashes") != manifest["fingerprint"]["payload"].get("module_hashes"):
            faults.append("worker modules differ from the pilot fingerprint")
        levels = evidence.evaluate_run(cases.case(cell[0]), result, path.parent, events=events[run_id],
            prompts=prompts[run_id], expected_articles=cases_module.condition_articles(cases, cell[0], cell[2]))
        faults += list(levels["errors"])
        if not faults:
            valid.add(cell)
    if expected - valid:
        problems.append(f"{len(expected - valid)} declared dev cells without a technically valid answer")
    if seen - expected:
        problems.append("dev validation contains runs outside its declared cells")
    return {"items": len(valid), "problems": problems}


def record_dev_review(cases: cases_module.CaseFile, dirs: list[Path]) -> dict:
    """Record human review, or explicitly waived review with technical validation.
    Bind either record to outputs, rubric, protocol and product version."""

    from agentfinx.evaluation.security import main_review

    protocol, protocol_sha, _ = protocol_module.load(_protocol_path(cases))
    mode = protocol_module.review_mode(protocol)
    entries, problems = [], protocol_module.review_policy_problems(protocol)
    for run_dir in dirs:
        status = (_dev_validation(cases, run_dir, protocol) if mode == "automated_only"
                  else main_review.completeness(run_dir / "review"))
        problems += [f"{run_dir.name}: {problem}" for problem in status["problems"]]
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        entries.append({"dir": _stored(run_dir), "items": status["items"], "outputs_sha256": outputs_sha256(run_dir),
                        "rubric_sha256": rubric_sha256(cases),
                        "product_code_sha256": manifest.get("product_code_sha256") or "not recorded",
                        "reviewers": json.loads((run_dir / "review" / "reviewers.json").read_text(encoding="utf-8"))
                        if mode == "human" and (run_dir / "review" / "reviewers.json").exists() else {},
                        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    if not entries:
        problems.append("no dev run directory supplied")
    if problems:
        raise SystemExit("the dev review is not complete:\n- " + "\n- ".join(problems))
    path = _security_dir(cases) / "review" / DEV_REVIEW
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"note": ("Human output review waived by the user; technical dev validation only."
                        if mode == "automated_only" else "Completed human review of dev outputs."),
              "mode": mode, "protocol_sha256": protocol_sha,
              "waiver": protocol["review"].get("waiver") if mode == "automated_only" else None,
              "entries": entries}
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return record


def dev_review_problems(cases: cases_module.CaseFile) -> list[str]:
    """Why the dev review does not yet allow a freeze."""

    from agentfinx.evaluation.security import main_review

    protocol, protocol_sha, _ = protocol_module.load(_protocol_path(cases))
    mode = protocol_module.review_mode(protocol)
    path = _security_dir(cases) / "review" / DEV_REVIEW
    if not path.exists():
        return ["dev review not recorded: record dev review or the explicitly waived technical validation with "
                "`eval-security dev-review --dir <dev run dir>`"]
    problems = protocol_module.review_policy_problems(protocol)
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("mode", "human") != mode or record.get("protocol_sha256") != protocol_sha:
        problems.append("dev record does not match the current review policy and protocol")
    if mode == "automated_only" and record.get("waiver") != protocol["review"].get("waiver"):
        problems.append("dev record waiver differs from the protocol")
    entries = record.get("entries") or []
    current_product, current_rubric = product_code_sha256(), rubric_sha256(cases)
    for entry in entries:
        run_dir = _dev_dir(entry["dir"])
        if not (run_dir / "manifest.json").exists():
            problems.append(f"{entry['dir']}: dev run directory not found")
            continue
        if outputs_sha256(run_dir) != entry["outputs_sha256"]:
            problems.append(f"{entry['dir']}: outputs changed after the review")
        status = (_dev_validation(cases, run_dir, protocol) if mode == "automated_only"
                  else main_review.completeness(run_dir / "review"))
        problems += [f"{entry['dir']}: {p}" for p in status["problems"]]
        if entry["rubric_sha256"] != current_rubric:
            problems.append(f"{entry['dir']}: the rubric changed after this review")
    if not any(entry.get("product_code_sha256") == current_product for entry in entries):
        problems.append("no reviewed dev run was made with the current product version")
    return problems


def freeze_record(cases: cases_module.CaseFile) -> dict:
    from agentfinx.core.config import paths
    from agentfinx.evaluation import benchmarks
    from agentfinx.evaluation.security.builder import load_spec

    _, spec_sha = load_spec()
    here = Path(__file__).resolve().parent
    rubric = cases.path.parent / "README.md"
    if not rubric.exists():
        rubric = benchmarks.resolve("benchmarks/security/README.md")
    _, protocol_sha, _ = protocol_module.load(_protocol_path(cases))
    dependencies = protocol_module.dependencies()
    dev_review = _security_dir(cases) / "review" / DEV_REVIEW
    return {
        "attack_set": cases.path.name,
        "attack_set_sha256": cases.sha256,
        "test_split_sha256": test_split_sha(cases),
        "spec_sha256": spec_sha,
        "rubric_sha256": gold_module.sha256_file(rubric),
        "modules": {name: hashlib.sha256((here / name).read_bytes()).hexdigest() for name in FROZEN_MODULES},
        "product_code": {str(p.relative_to(here.parents[1])): gold_module.sha256_file(p)
                         for p in sorted(here.parents[1].rglob("*.py"))},
        "reports": {dataset_id: {"file": meta["report"]["file"],
                                 "sha256": gold_module.sha256_file(paths.home() / meta["report"]["file"])}
                    for dataset_id, meta in cases.data["datasets"].items()},
        "protocol_sha256": protocol_sha,
        "dependencies": {"sha256": dependencies["sha256"], "key": dependencies["key"]},
        "product_code_sha256": product_code_sha256(),
        "dev_review_sha256": gold_module.sha256_file(dev_review) if dev_review.exists() else "",
        "git_head": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain", "--", str(here), str(cases.path.parent))),
    }


def _protocol_path(cases: cases_module.CaseFile) -> Path | str:
    local = cases.path.parent / "main_protocol.json"
    return local if local.exists() else protocol_module.PROTOCOL


def freeze(cases: cases_module.CaseFile) -> dict:
    """Write FREEZE.json; refuses without a clean sign-off or a valid set."""

    from agentfinx.evaluation.security import checks

    signoff_path = _security_dir(cases) / "review" / "test_case_signoff.json"
    if not signoff_path.exists():
        raise SystemExit("no review sign-off; run `eval-security review-packet` and have it filled")
    signoff = json.loads(signoff_path.read_text(encoding="utf-8"))
    problems = signoff_problems(cases, signoff)
    problems += [f"{case_id}: {p}" for case_id in cases.all_case_ids() for p in checks.check_case(cases, case_id)]
    problems += checks.check_attack_set(cases)
    problems += [f"{dataset}: {p}" for dataset, items in checks.check_datasets(cases).items() for p in items]
    protocol, _, _ = protocol_module.load(_protocol_path(cases))
    problems += [f"protocol: {p}" for p in protocol_module.problems(protocol, cases)]
    problems += [f"dev review: {p}" for p in dev_review_problems(cases)]
    if problems:
        raise SystemExit("cannot freeze:\n- " + "\n- ".join(problems))
    record = {**freeze_record(cases), "frozen_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "signoff_sha256": hashlib.sha256(signoff_path.read_bytes()).hexdigest(),
              "reviewers": [r["name"] for r in signoff["reviewers"] if r.get("name")]}
    path = _security_dir(cases) / "FREEZE.json"
    if path.exists():
        raise SystemExit(f"{path} exists; a frozen set is never refrozen")
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return record


def frozen_problems(cases: cases_module.CaseFile) -> list[str]:
    """Why the test split may not run: no freeze, or a hash that moved."""

    path = _security_dir(cases) / "FREEZE.json"
    if not path.exists():
        return ["the attack set is not frozen (no FREEZE.json)"]
    record = json.loads(path.read_text(encoding="utf-8"))
    current = freeze_record(cases)
    problems = [f"{key} changed since the freeze" for key in
                ("attack_set_sha256", "test_split_sha256", "spec_sha256", "rubric_sha256", "modules", "product_code",
                 "reports", "protocol_sha256", "dependencies", "dev_review_sha256")
                if record.get(key) != current.get(key)]
    signoff = _security_dir(cases) / "review" / "test_case_signoff.json"
    if not signoff.exists() or gold_module.sha256_file(signoff) != record.get("signoff_sha256"):
        problems.append("review sign-off changed or missing since the freeze")
    return problems


def excluded(cases: cases_module.CaseFile) -> dict[str, dict]:
    path = _security_dir(cases) / "exclusions.json"
    if not path.exists():
        return {}
    return {item["case_id"]: item for item in json.loads(path.read_text(encoding="utf-8"))}


def exclude(cases: cases_module.CaseFile, case_id: str, reason: str) -> dict:
    """Mark a frozen case broken (append-only); the case itself never changes."""

    if not reason.strip():
        raise SystemExit("an exclusion needs a reason")
    if cases.case(case_id)["split"] != "test":
        raise SystemExit("only a test case may be excluded")
    problems = frozen_problems(cases)
    if problems:
        raise SystemExit("cannot exclude from an unfrozen or changed set: " + "; ".join(problems))
    path = _security_dir(cases) / "exclusions.json"
    items = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if any(item["case_id"] == case_id for item in items):
        raise SystemExit(f"{case_id} is already excluded")
    item = {"case_id": case_id, "reason": reason.strip(),
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "attack_set_sha256": cases.sha256}
    path.write_text(json.dumps(items + [item], ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return item
