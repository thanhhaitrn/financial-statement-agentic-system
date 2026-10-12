"""Evaluator accounting uses the same resolver/guard; no model access here."""

import json

import pytest

from agentfinx.core.schemas.chat_context import ContextTurn
from agentfinx.evaluation.chat_context import evaluate_cases, main, standalone_cases


def _standalone(q, _history):
    return {"status": "standalone", "question": q, "metric": {}, "period": {}}


def test_independent_questions_are_tested_as_second_turns():
    cases = standalone_cases([{"question": "Doanh thu năm 2024?", "reference": "PRIVATE ANSWER"}])
    calls = []

    def fake(q, history):
        calls.append(history)
        assert history and all(isinstance(turn, ContextTurn) for turn in history)
        return _standalone(q, history)

    report = evaluate_cases(cases, rewrite=fake)
    assert len(calls) == 2 and report["summary"]["passed"]
    assert report["summary"]["unnecessary_clarifications"] == 0
    assert "PRIVATE ANSWER" not in json.dumps(cases)


@pytest.mark.parametrize("mode,metric", [("needs_clarification", "unnecessary_clarifications"),
                                          ("schema", "schema_errors"), ("provider", "provider_errors"),
                                          ("guard", "guard_rejections")])
def test_failures_do_not_disappear_from_gate_denominator(mode, metric):
    def fake(q, _h):
        if mode == "provider":
            raise RuntimeError("upstream")
        if mode == "schema":
            return q
        if mode == "guard":
            return _standalone("Changed", [])
        return {"status": mode, "metric": {}, "period": {}, "clarification_question": "Bạn muốn hỏi gì?"}

    report = evaluate_cases(standalone_cases([{"question": "Doanh thu?"}]), rewrite=fake)
    assert report["summary"][metric] == 2
    assert report["summary"]["steps"] == report["summary"]["failures"] == 2
    assert not report["summary"]["passed"]


def test_multi_turn_eval_reuses_the_actual_effective_question():
    case = {"id": "chain", "history": [{"run_id": "first", "effective_question": "Vốn chủ sở hữu cuối năm?"}],
            "steps": [{"question": "Còn đầu năm?", "expected": "resolved", "must_include": ["Vốn chủ sở hữu", "đầu năm"]},
                      {"question": "Còn doanh thu?", "expected": "resolved", "must_include": ["doanh thu", "đầu năm"]}]}
    seen = []

    def fake(q, history):
        seen.append(history[-1].effective_question)
        if len(seen) == 1:
            return {"status": "resolved", "question": "Vốn chủ sở hữu đầu năm?",
                    "metric": {"kept": [{"source_run_id": "first", "text": "Vốn chủ sở hữu"}]},
                    "period": {"changed": ["đầu năm"]}}
        return {"status": "resolved", "question": "Còn doanh thu đầu năm?",
                "metric": {"changed": ["doanh thu"]},
                "period": {"kept": [{"source_run_id": history[-1].run_id, "text": "đầu năm"}]}}

    assert evaluate_cases([case], rewrite=fake)["summary"]["passed"]
    assert seen == ["Vốn chủ sở hữu cuối năm?", "Vốn chủ sở hữu đầu năm?"]


def test_dry_run_and_live_permission_boundary(tmp_path, monkeypatch, capsys):
    questions = tmp_path / "questions.json"
    questions.write_text(json.dumps(["Doanh thu?"]))
    monkeypatch.setattr("agentfinx.evaluation.chat_context.evaluate_cases", lambda *_a, **_k: pytest.fail("no live calls"))
    assert main(["--questions", str(questions), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["maximum_model_calls"] == 2
    with pytest.raises(SystemExit) as exc:
        main(["--questions", str(questions)])
    assert exc.value.code == 2
    with pytest.raises(SystemExit):
        main(["--questions", str(questions), "--allow-live", "--max-calls", "1"])


def test_evaluator_rejects_first_turn_only_cases():
    with pytest.raises(ValueError, match="require history"):
        evaluate_cases([{"id": "bad", "history": [], "steps": [{"question": "q", "expected": "standalone"}]}])
