"""Write the generalization protocols: A/D × 80 test cases × clean/attack per model, a
pre-chosen stability subset run twice more, and a dev pilot per model.

Run from ``backend/`` of the worktree (no model call)::

    python -m analysis.security_generalization.make_protocols

Everything comes from the main protocol, the attack set, the controlled protocol's
pinned indexes and the current product hash; the stability cases are drawn with a
fixed seed before any output exists.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

from agentfinx.evaluation.security import cases as cases_module
from agentfinx.evaluation.security import freeze
from agentfinx.evaluation.security.runner import PILOT_ENV

MODELS = {"gpt-oss-120b": "gpt-oss:120b-cloud", "gpt-oss-20b": "gpt-oss:20b-cloud", "gemma4-31b": "gemma4:31b-cloud"}
PILOT_CASES = ["I1_DPC_001", "I3_VNM_001", "P2_GER_001", "P4_DPC_001"]
SEED = 20261026
OUT = Path("benchmarks/security/generalization")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stability_cases(cases: cases_module.CaseFile, per_subtype: int = 2) -> list[str]:
    by_subtype = defaultdict(list)
    for case_id in sorted(cases.case_ids(split="test")):
        by_subtype[cases.case(case_id)["subtype"]].append(case_id)
    rng = random.Random(f"{SEED}:stability")
    return sorted(case_id for subtype in sorted(by_subtype) for case_id in rng.sample(by_subtype[subtype], per_subtype))


def protocol(name: str, model: str, *, pilot: bool) -> dict:
    main = json.loads(Path("benchmarks/security/main_protocol.json").read_text(encoding="utf-8"))
    controlled = json.loads(Path("benchmarks/security/controlled_protocol.json").read_text(encoding="utf-8"))
    cases = cases_module.load_cases(main["attack_set"]["path"])
    case_ids = PILOT_CASES if pilot else list(main["case_ids"])
    out = {
        "protocol_version": f"security-generalization-{'pilot-' if pilot else ''}v1",
        "kind": "generalization",
        "experiment": f"agentfinx-security-generalization-{name}{'-pilot' if pilot else ''}",
        "description": ("Mở rộng (26/10/2026): khả năng khái quát của kết quả A/D sang model trả lời khác. "
                        "Workflow tự nhiên như đợt chính; chỉ đổi model trả lời (OLLAMA_MODEL). "
                        + ("Pilot trên 4 case dev: đo pipeline có chạy được, thời gian, tỉ lệ câu scorer v3.4 "
                           "không quyết định được, để khóa kế hoạch chấm trước đợt test."
                           if pilot else "Một lượt chính trên 80 case test; 16 case chọn trước chạy thêm hai lượt "
                                         "để đo độ ổn định, báo riêng.")),
        "pilot": pilot,
        "registered_under": {"plan": "docs/security_generalization.md", "main_protocol_sha256":
                             sha(Path("benchmarks/security/main_protocol.json"))},
        "attack_set": {**main["attack_set"], "split": "dev" if pilot else "test"},
        "case_ids": case_ids,
        "conditions": ["clean", "attack"],
        "repetitions": 1,
        "configs": {config: main["configs"][config] for config in ("A", "D")},
        "exposure": "natural",
        "environment": dict(PILOT_ENV),
        "model": {**main["model"], "model": model},
        "embedding": main["embedding"],
        "retrieval": main["retrieval"],
        "as_of": {case_id: cases.case(case_id)["as_of"] for case_id in case_ids},
        "pinned": {"product_code_sha256": freeze.product_code_sha256(),
                   "datasets": controlled["pinned"]["datasets"],
                   "product_note": "khác mã đợt chính (3c579b10…) chỉ ở web_evidence.as_structured_fact và chế độ "
                                   "structured/cấu hình E của worker.py (Việc 3); A/D ở workflow tự nhiên không chạy "
                                   "qua các đoạn này. Không gồm bản sửa router của nhánh mở rộng."},
        "requires": {"g1_record": "benchmarks/security/review/g1_freeze_v3.4.json"},
        "budget": {"model_calls": 200 if pilot else 2000,
                   "estimate": ("16 ô × ~3 lần gọi" if pilot else
                                "160 ô A × 1 + 160 ô D × ~4,5 lần gọi, cộng 128 ô ổn định; ~1.250"),
                   "ledger": "budget.json riêng trong thư mục chạy", "increase": "không bao giờ tự tăng"},
        "schedule": {"seed": SEED, "order": "xáo thứ tự case, rồi xáo các ô của mỗi case; lượt ổn định chạy sau "
                                           "lượt chính, mỗi lượt một seed riêng"},
        "cell": main["cell"], "validity": main["validity"], "retry": main["retry"], "resume": main["resume"],
        "scoring": {"mode": "deferred",
                    "rule": "chấm bằng scorer v3.4 đã đóng băng ở G1; câu v3.4 không quyết định được và mẫu kiểm theo "
                            "kế hoạch chấm đã khóa sau pilot; ẩn model và cấu hình khi chấm"},
    }
    if not pilot:
        out["stability"] = {"case_ids": stability_cases(cases), "per_subtype": 2, "repetitions": [1, 2],
                            "selection": f"random.Random('{SEED}:stability'), 2 case mỗi subtype, chọn trước khi chạy"}
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, model in MODELS.items():
        for pilot in (True, False):
            path = OUT / f"{name}{'_pilot' if pilot else ''}.json"
            path.write_text(json.dumps(protocol(name, model, pilot=pilot), ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
            print(path, sha(path)[:12])


if __name__ == "__main__":
    main()
