"""Live steps of a chat run: recorded while the worker answers, returned with the
run, closed when it ends, and silent when no run is active."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("argon2")
pytest.importorskip("jwt")

from test_chat_discovery import QA_PLAN, Harness, _configure_registry  # noqa: E402

from agentfinx.api.worker import run_chat_once  # noqa: E402
from agentfinx.core import progress  # noqa: E402
from agentfinx.workflow.live_steps import track, track_analysis  # noqa: E402


@pytest.fixture
def harness(monkeypatch, tmp_path):
    _configure_registry(monkeypatch, tmp_path)
    return Harness(tmp_path)


def test_a_run_reports_its_steps_in_order_and_closes_them(harness):
    h = harness
    run_id = h.ask(h.session(with_report=True), "Doanh thu năm 2025?")
    deps = h.deps(QA_PLAN)
    base = deps.execute

    def execute(dataset, collection, question, **kwargs):
        progress.begin("evidence", "Tìm số liệu trong báo cáo")
        progress.end("evidence", "12 số liệu")
        progress.begin("news", "Đọc và phân tích tin tức")  # never closed by the pipeline
        return base(dataset, collection, question, **kwargs)

    deps.execute = execute
    run_chat_once(h.db, "w", deps)
    run = h.run(run_id)
    assert run["status"] == "succeeded"
    steps = {s["key"]: s for s in run["steps"]}
    assert [s["key"] for s in run["steps"]] == ["plan", "dataset", "evidence", "news"]
    assert steps["evidence"]["status"] == "done" and steps["evidence"]["detail"] == "12 số liệu"
    assert steps["plan"]["status"] == "done" and steps["plan"]["finished_at"]
    assert steps["news"]["status"] == "skipped"  # a finished run leaves nothing spinning


def test_a_failed_run_marks_its_open_steps_failed(harness):
    h = harness
    run_id = h.ask(h.session(with_report=True), "Doanh thu năm 2025?")
    deps = h.deps(QA_PLAN)

    def execute(dataset, collection, question, **kwargs):
        progress.begin("synth", "Tổng hợp câu trả lời")
        raise RuntimeError("boom")

    deps.execute = execute
    run_chat_once(h.db, "w", deps)
    run = h.run(run_id)
    assert run["status"] == "failed"
    assert {s["key"]: s["status"] for s in run["steps"]}["synth"] == "failed"


def test_steps_are_silent_without_an_active_run():
    calls = []
    node = track("agent_synth", lambda state: calls.append(state) or {"ok": True})
    assert node({"q": 1}) == {"ok": True} and calls == [{"q": 1}]
    assert not progress.active()


def test_an_analysis_agent_is_one_step_across_its_tool_loop():
    events = []
    agent, tool, finalize = track_analysis("agent_profitability", lambda s: {}, lambda s: {}, lambda s: {})
    with progress.activate(events.append):
        agent({})
        tool({})
        agent({})
        tool({})
        finalize({})
    assert {e["key"] for e in events} == {"agent_profitability"}
    assert events[0]["status"] == "running" and "sinh lời" in events[0]["label"]
    assert events[-1] == {"key": "agent_profitability", "status": "done", "detail": "Đã tra cứu 2 lần"}


def test_a_failing_node_marks_its_step_failed_and_reraises():
    events = []

    def broken(state):
        raise ValueError("x")

    node = track("build_evidence", broken)
    with progress.activate(events.append), pytest.raises(ValueError):
        node({})
    assert events[-1] == {"key": "evidence", "status": "failed"}


def test_a_broken_sink_never_fails_the_answer():
    def sink(event):
        raise OSError("disk full")

    node = track("agent_synth", lambda state: {"answer": "ok"})
    with progress.activate(sink):
        assert node({}) == {"answer": "ok"}


def test_an_analysis_step_carries_the_agents_last_answer():
    events = []
    answers = iter([{}, {"worker_results": {"agent_cashflow_analysis": {"answer": "CFO dương **12 tỷ**"}}}])
    agent, tool, finalize = track_analysis("agent_cashflow_analysis", lambda s: next(answers), lambda s: {}, lambda s: {})
    with progress.activate(events.append):
        agent({})
        tool({})
        agent({})
        finalize({})
    assert events[-1]["output"] == "CFO dương **12 tỷ**"


def test_a_broken_output_formatter_never_fails_the_node(monkeypatch):
    from agentfinx.workflow import live_steps

    monkeypatch.setitem(live_steps.SIMPLE, "agent_router", live_steps._simple(
        "route", "Chọn hướng phân tích", output=lambda out: 1 / 0))
    node = live_steps.track("agent_router", lambda state: {"worker_plan": {}})
    with progress.activate(lambda event: None):
        assert node({}) == {"worker_plan": {}}


def test_step_output_is_stored_capped_and_returned(harness):
    h = harness
    run_id = h.ask(h.session(with_report=True), "Doanh thu năm 2025?")
    deps = h.deps(QA_PLAN)
    base = deps.execute

    def execute(dataset, collection, question, **kwargs):
        progress.begin("evidence", "Tìm số liệu trong báo cáo")
        progress.end("evidence", "1 số liệu", output="- Doanh thu: **10 tỷ**\n" + "x" * 9000)
        return base(dataset, collection, question, **kwargs)

    deps.execute = execute
    run_chat_once(h.db, "w", deps)
    step = {s["key"]: s for s in h.run(run_id)["steps"]}["evidence"]
    assert step["output"].startswith("- Doanh thu: **10 tỷ**") and len(step["output"]) <= 6002
