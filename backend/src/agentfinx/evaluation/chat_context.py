"""Evaluate the real web resolver, not a keyword substitute or a first-turn bypass.

Live calls require --allow-live. Only questions, effective history and intent
snapshots are submitted; reference answers in the question file are discarded.
This is a context/routing gate, NOT a factual-recall or WRONG_REFUSAL evaluator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Callable

from agentfinx.core.schemas.chat_context import CONTEXT_CONTRACT_VERSION, ContextTurn
from agentfinx.evaluation import benchmarks
from agentfinx.evaluation.task_routing import load_questions
from agentfinx.api.clarify import clarify_question, context_fingerprint

STANDALONE_HISTORIES = (
    ContextTurn(run_id="other-topic", effective_question="Tiền và các khoản tương đương tiền cuối năm 2023 là bao nhiêu?"),
    ContextTurn(run_id="other-period", effective_question="Vốn chủ sở hữu đầu năm 2025 là bao nhiêu?"),
)


def standalone_cases(items: list[dict]) -> list[dict]:
    return [
        {"id": f"standalone-{index}-{history.run_id}", "history": [history.model_dump()],
         "steps": [{"question": item["question"], "expected": "standalone"}]}
        for index, item in enumerate(items) for history in STANDALONE_HISTORIES
    ]


def evaluate_cases(cases: list[dict], *, rewrite: Callable | None = None) -> dict:
    records = []
    for case in cases:
        history = [ContextTurn.model_validate(t) for t in case["history"]]
        if not history:
            raise ValueError("context cases require history; first-turn bypass is not a gate")
        blocked = False
        for index, step in enumerate(case["steps"]):
            start = time.monotonic()
            base = {"case_id": case["id"], "step": index, "question": step["question"],
                    "expected": step["expected"], "ok": False}
            if blocked:
                records.append({**base, "status": "blocked", "reason": "prior_step_failed"})
                continue
            result = clarify_question(step["question"], history, rewrite=rewrite)
            expected = step["expected"]
            wrong_rewrite = False
            if expected == "standalone":
                wrong_rewrite = result.can_continue and result.question != step["question"]
            elif expected == "resolved" and result.can_continue:
                wrong_rewrite = (
                    any(text not in result.question for text in step.get("must_include", []))
                    or any(text in result.question for text in step.get("must_not_include", []))
                )
            ok = result.status == expected and not wrong_rewrite
            records.append({
                **base, "status": result.status, "reason": result.reason,
                "effective_question": result.question, "wrong_rewrite": wrong_rewrite,
                "ok": ok, "duration_ms": round((time.monotonic() - start) * 1000),
                **result.payload(),
            })
            if result.can_continue:
                history.append(ContextTurn(run_id=f"{case['id']}-step-{index}",
                                           effective_question=result.question))
            else:
                blocked = True
    statuses = Counter(r["status"] for r in records)
    reasons = Counter(r["reason"] for r in records)
    summary = {
        "cases": len(cases), "steps": len(records), "statuses": dict(statuses),
        "unnecessary_clarifications": sum(r["expected"] == "standalone" and r["status"] == "needs_clarification" for r in records),
        "standalone_misclassified": sum(r["expected"] == "standalone" and r["status"] not in {"standalone", "failed", "blocked"} for r in records),
        "guard_rejections": reasons["guard_rejected"], "schema_errors": reasons["schema_error"],
        "provider_errors": reasons["provider_error"], "model_failed": reasons["model_failed"],
        "wrong_rewrites": sum(r.get("wrong_rewrite", False) for r in records),
        "blocked_steps": statuses["blocked"],
        "failures": sum(not r["ok"] for r in records),
    }
    # Provider/guard/schema failures also fail this gate, never disappear from
    # the denominator merely because they are not needs_clarification.
    summary["passed"] = bool(records) and summary["failures"] == 0
    return {"summary": summary, "results": records}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=benchmarks.resolve, action="append", default=[],
                        help="Reviewed standalone question set (e.g. queries_test.json); answers are not sent.")
    parser.add_argument("--scenarios", type=benchmarks.resolve, action="append", default=[],
                        help="Annotated multi-turn contrastive scenarios.")
    parser.add_argument("--limit", type=int, default=0, help="Diagnostic subset only; not eligible for release gate.")
    parser.add_argument("--max-calls", type=int, default=200)
    parser.add_argument("--allow-live", action="store_true", help="Explicitly permit calls to the configured LLM.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned call count; no model calls or file writes.")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    cases = standalone_cases([item for path in args.questions for item in load_questions(path)])
    for path in args.scenarios:
        cases.extend(json.loads(path.read_text(encoding="utf-8")))
    if not cases:
        parser.error("provide --questions and/or --scenarios")
    if args.limit < 0 or args.max_calls < 1:
        parser.error("limit must be nonnegative and max-calls must be positive")
    full_cases = len(cases)
    if args.limit:
        cases = cases[:args.limit]
    # Validate before any live call, including per-scenario history.
    for case in cases:
        if not case.get("history") or not case.get("steps"):
            parser.error("every case needs history and steps")
        for turn in case["history"]:
            ContextTurn.model_validate(turn)
        for step in case["steps"]:
            if not step.get("question") or step.get("expected") not in {"standalone", "resolved", "needs_clarification", "failed"}:
                parser.error("each step needs a question and expected resolver status")
    calls = sum(len(case["steps"]) for case in cases)
    if args.dry_run:
        print(json.dumps({"cases": len(cases), "full_cases": full_cases, "maximum_model_calls": calls,
                          "contract_version": CONTEXT_CONTRACT_VERSION, "live_calls": 0}))
        return 0
    if not args.allow_live:
        parser.error("live evaluation requires --allow-live (use --dry-run to inspect scope)")
    if calls > args.max_calls:
        parser.error(f"{calls} calls exceed --max-calls={args.max_calls}; explicitly raise the cap")
    from dotenv import load_dotenv
    load_dotenv()
    from agentfinx.core.llm.client import get_llm_identity
    report = evaluate_cases(cases)
    report.update({
        "mode": "live", "contract_version": CONTEXT_CONTRACT_VERSION,
        "context_fingerprint": context_fingerprint(),
        "model_identity_sha256": hashlib.sha256(json.dumps(get_llm_identity()).encode()).hexdigest(),
        "cases_sha256": hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "partial": bool(args.limit),
        "release_gate_passed": (report["summary"]["passed"] and not args.limit
                                 and bool(args.questions) and bool(args.scenarios)),
        "factual_recall_verified": False, "wrong_refusal_verified": False,
    })
    print(json.dumps({"summary": report["summary"], "partial": report["partial"],
                      "release_gate_passed": report["release_gate_passed"]}, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["summary"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
