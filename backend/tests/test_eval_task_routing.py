"""The routing evaluator checks every field a case states, per planner and
run, and fails when a clear case is wrong on any run. No model is called."""

import json

import pytest

from agentfinx.evaluation import task_routing as evaluator


@pytest.fixture
def questions(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"dataset_id": "suavietnam", "questions": [
        {"question": "Tin gần đây của công ty?", "task_kind": "report_qa", "need_web": True,
         "web_intent": "company_news", "clear": True},
        {"question": "Tìm BCTC VNM 2025", "task_kind": "report_discovery", "need_web": False, "web_intent": "",
         "clear": True},
        {"question": "Vì sao biên lợi nhuận giảm?", "need_web": False, "clear": False},
    ]}, ensure_ascii=False), encoding="utf-8")
    return path


def _run(monkeypatch, questions, answers, *args):
    calls = []

    def classify(question, *, dataset_id, planner):
        calls.append((planner, question, dataset_id))
        answer = answers[(planner, question)]
        return answer.pop(0) if isinstance(answer, list) else answer

    monkeypatch.setattr(evaluator, "classify", classify)
    return evaluator.main(["--questions", str(questions), *args]), calls


def _ok(web_intent="company_news", need_web=True, task_kind="report_qa"):
    return {"task_kind": task_kind, "need_web": need_web, "web_intent": web_intent, "report_search": None}


def test_every_stated_field_and_every_run_is_checked(monkeypatch, questions, capsys):
    answers = {
        ("web", "Tin gần đây của công ty?"): [_ok(), _ok(web_intent="unsupported_external"), _ok()],
        ("web", "Tìm BCTC VNM 2025"): _ok(task_kind="report_discovery", need_web=False, web_intent=""),
        ("web", "Vì sao biên lợi nhuận giảm?"): _ok(need_web=True),  # soft case: tracked only
    }
    status, calls = _run(monkeypatch, questions, answers, "--runs", "3")
    summary = json.loads(capsys.readouterr().out)
    assert status == 1  # the clear news case was wrong on one of three runs
    assert summary["clear_cases_not_always_right"] == 1
    wrong = {case["question"]: case for case in summary["wrong_cases"]}
    assert wrong["Tin gần đây của công ty?"]["correct"] == "2/3"
    assert wrong["Vì sao biên lợi nhuận giảm?"]["clear"] is False
    assert {dataset for *_rest, dataset in calls} == {"suavietnam"}  # the open report, as in the chat


def test_the_qa_planner_is_not_asked_to_find_reports(monkeypatch, questions, capsys):
    answers = {(planner, question): _ok(need_web=need_web, web_intent=intent)
               for planner in ("web", "qa")
               for question, need_web, intent in (("Tin gần đây của công ty?", True, "company_news"),
                                                  ("Vì sao biên lợi nhuận giảm?", False, ""))}
    answers[("web", "Tìm BCTC VNM 2025")] = _ok(task_kind="report_discovery", need_web=False, web_intent="")
    status, calls = _run(monkeypatch, questions, answers, "--planner", "both")
    assert status == 0
    assert ("qa", "Tìm BCTC VNM 2025", "suavietnam") not in calls
    assert json.loads(capsys.readouterr().out)["calls"] == 5


def test_a_report_question_sent_to_discovery_fails_the_run(monkeypatch, questions, capsys):
    answers = {
        ("web", "Tin gần đây của công ty?"): _ok(task_kind="report_discovery"),
        ("web", "Tìm BCTC VNM 2025"): _ok(task_kind="report_discovery", need_web=False, web_intent=""),
        ("web", "Vì sao biên lợi nhuận giảm?"): _ok(need_web=False, web_intent=""),
    }
    status, _calls = _run(monkeypatch, questions, answers)
    assert status == 1 and json.loads(capsys.readouterr().out)["qa_routed_to_discovery"] == 1


def test_the_original_question_files_still_load():
    from agentfinx.evaluation import benchmarks

    items = evaluator.load_questions(benchmarks.resolve("task_routing_contrastive.json"))
    assert items and all(item["expected"] in {"report_qa", "report_discovery"} for item in items)
    assert all(item["need_web"] is evaluator._UNSET for item in items)  # only task_kind is checked there
