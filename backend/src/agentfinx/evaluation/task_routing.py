"""Measure how the planners classify questions: the task (report QA vs report
discovery) and the web decision (need_web, web_intent).

Each question runs through the planner the way the product runs it, with the
open report as context: the web planner exactly as the chat worker calls it
(``FINX_REPORT_DISCOVERY_ENABLED`` on), and/or the QA planner as the graph's
first node calls it (CLI and chat with discovery off). It calls the configured
LLM, so run it deliberately (not in CI):

    agentfinx eval-routing --questions task_routing_contrastive.json
    agentfinx eval-routing --questions web_intent_contrastive.json --planner both --runs 5
    agentfinx eval-routing --questions BATCH_QUESTIONS_FILE --expect report_qa

The reviewed question sets ship with the package (``evaluation/data/``); a
bare file name finds them, see ``evaluation.benchmarks``.

A question file is a list or an object with ``questions``; an item is a
string or has ``question`` plus any of ``expected``/``task_kind``,
``need_web``, ``web_intent``, ``clear`` and ``dataset_id`` (an object may set
``dataset_id`` for all). Only fields an item states are checked.

Exit status 1 when a report_qa question was routed to report_discovery, or a
``clear`` question was wrong on any run (the acceptance targets are zero).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from agentfinx.evaluation import benchmarks

_UNSET = object()


def load_questions(path: Path, *, expect: Optional[str] = None) -> list[dict]:
    """JSON (a list, or an object with ``questions``/``queries``), JSONL or
    CSV; each item is a string or has ``question``/``query``/``user_query``."""

    text = path.read_text(encoding="utf-8")
    default_dataset = ""
    if path.suffix.lower() == ".csv":
        rows: list = list(csv.DictReader(text.splitlines()))
    elif path.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        payload = json.loads(text)
        rows = payload
        if isinstance(payload, dict):
            rows = payload.get("questions") or payload.get("queries") or []
            default_dataset = str(payload.get("dataset_id", "") or "")
    items = []
    for row in rows:
        if isinstance(row, str):
            row = {"question": row}
        if not isinstance(row, dict):
            continue
        question = " ".join(str(row.get("question") or row.get("query") or row.get("user_query") or "").split())
        if not question:
            continue
        items.append({
            "question": question,
            "expected": row.get("task_kind") or row.get("expected") or expect,
            "need_web": row.get("need_web", _UNSET),
            "web_intent": row.get("web_intent", _UNSET),
            "clear": bool(row.get("clear", False)),
            "dataset_id": str(row.get("dataset_id", default_dataset) or ""),
        })
    return items


def _dataset(dataset_id: str):
    if not dataset_id:
        return None
    from agentfinx.datasets import registry

    return registry.get_dataset(dataset_id)


def classify(question: str, *, dataset_id: str = "", planner: str = "web") -> dict:
    """One planner call with the state the product builds for it."""

    from agentfinx.workflow.state import WorkflowServices
    from agentfinx.workflow.graph import run_with_services

    if planner == "web":
        from agentfinx.agents.planner_runner import run_web_planner
        from agentfinx.api.chat_runner import _planner_state

        state = _planner_state({"dataset_id": dataset_id, "id": "routing-eval"}, question)
        updates = run_with_services(run_web_planner, WorkflowServices(collection=None), state)
        task_kind = updates.get("task_kind", "report_qa")
    else:
        from agentfinx.agents.planner_runner import run_planner
        from agentfinx.workflow.pipeline import _build_initial_state

        dataset = _dataset(dataset_id)
        if dataset is not None:
            state = _build_initial_state(dataset, question)
        else:
            state = {"user_query": question, "dataset_id": "", "trace": [], "planner_plan": {}}
        updates = run_with_services(run_planner, WorkflowServices(collection=None), state)
        task_kind = "report_qa"
    plan = updates.get("planner_plan") or {}
    return {
        "task_kind": task_kind,
        "need_web": bool(plan.get("need_web", False)),
        "web_intent": str(plan.get("web_intent", "") or ""),
        "report_search": updates.get("report_search"),
    }


def _mismatches(item: dict, outcome: dict, planner: str) -> list[str]:
    wrong = []
    if planner == "web" and item["expected"] and outcome["task_kind"] != item["expected"]:
        wrong.append("task_kind")
    if item["need_web"] is not _UNSET and outcome["need_web"] != bool(item["need_web"]):
        wrong.append("need_web")
    if item["web_intent"] is not _UNSET and outcome["web_intent"] != (item["web_intent"] or ""):
        wrong.append("web_intent")
    return wrong


def _applies(item: dict, planner: str) -> bool:
    # The QA planner has no discovery task: it is never asked to find a report.
    return planner == "web" or item["expected"] != "report_discovery"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--questions", type=benchmarks.resolve, action="append", required=True)
    parser.add_argument("--expect", choices=["report_qa", "report_discovery"],
                        help="Expected task for items that do not state one")
    parser.add_argument("--dataset-id", default=None, help="Open report for every item (overrides the file)")
    parser.add_argument("--planner", choices=["web", "qa", "both"], default="web")
    parser.add_argument("--runs", type=int, default=1, help="Runs per question, for stability")
    parser.add_argument("--workers", type=int, default=1, help="Planner calls in flight")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--output", type=Path, help="Write the per-run results as JSON")
    args = parser.parse_args(argv)

    from agentfinx.core.config import paths

    paths.load_env()  # model settings; never printed
    items = [item for path in args.questions for item in load_questions(path, expect=args.expect)]
    if args.dataset_id is not None:
        for item in items:
            item["dataset_id"] = args.dataset_id
    if args.limit:
        items = items[: args.limit]
    planners = ["web", "qa"] if args.planner == "both" else [args.planner]
    jobs = [(planner, item, run) for planner in planners for item in items if _applies(item, planner)
            for run in range(max(args.runs, 1))]

    def run_one(job):
        planner, item, run = job
        started = time.perf_counter()
        try:
            outcome = classify(item["question"], dataset_id=item["dataset_id"], planner=planner)
            wrong = _mismatches(item, outcome, planner)
        except Exception as exc:  # a failed call is a wrong run, not a crash
            outcome = {"task_kind": "", "need_web": None, "web_intent": "", "error": type(exc).__name__}
            wrong = ["error"]
        return {"planner": planner, "run": run, "question": item["question"], "clear": item["clear"],
                "expected": {"task_kind": item["expected"],
                             "need_web": None if item["need_web"] is _UNSET else item["need_web"],
                             "web_intent": None if item["web_intent"] is _UNSET else item["web_intent"]},
                **outcome, "wrong": wrong, "ok": not wrong,
                "duration_ms": int((time.perf_counter() - started) * 1000)}

    with ThreadPoolExecutor(max_workers=max(args.workers, 1)) as pool:
        results = list(pool.map(run_one, jobs))

    per_case: dict[tuple[str, str], dict] = {}
    for result in results:
        case = per_case.setdefault((result["planner"], result["question"]), {
            "planner": result["planner"], "question": result["question"], "clear": result["clear"],
            "expected": result["expected"], "runs": 0, "correct": 0, "got": Counter()})
        case["runs"] += 1
        case["correct"] += int(result["ok"])
        case["got"][f"{result['task_kind']}|need_web={result['need_web']}|{result['web_intent'] or '-'}"] += 1
    cases = [{**case, "got": dict(case["got"])} for case in per_case.values()]
    qa_to_discovery = [r for r in results if r["planner"] == "web" and r["expected"]["task_kind"] == "report_qa"
                       and r["task_kind"] == "report_discovery"]
    unstable_clear = [case for case in cases if case["clear"] and case["correct"] < case["runs"]]
    summary = {
        "planners": planners,
        "runs_per_question": max(args.runs, 1),
        "questions": len(items),
        "calls": len(results),
        "correct_calls": sum(r["ok"] for r in results),
        "qa_routed_to_discovery": len(qa_to_discovery),
        "clear_cases": sum(case["clear"] for case in cases),
        "clear_cases_not_always_right": len(unstable_clear),
        "wrong_cases": [
            {"planner": case["planner"], "question": case["question"], "clear": case["clear"],
             "correct": f"{case['correct']}/{case['runs']}", "expected": case["expected"], "got": case["got"]}
            for case in cases if case["correct"] < case["runs"]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({"summary": summary, "cases": cases, "results": results},
                                          ensure_ascii=False, indent=2), encoding="utf-8")
    return 1 if qa_to_discovery or unstable_clear else 0


if __name__ == "__main__":
    sys.exit(main())
