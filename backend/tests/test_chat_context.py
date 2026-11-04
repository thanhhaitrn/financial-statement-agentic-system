"""Web context contract and worker regressions; all model calls are fakes."""

import copy
import json
from types import SimpleNamespace

import pytest

from agentfinx.core.config.time_references import time_reference_spans
from agentfinx.datasets import registry
from agentfinx.core.schemas.chat_context import CONTEXT_CONTRACT_VERSION, ContextResolution, ContextTurn, ReportContext
from agentfinx.api import db as webdb
from agentfinx.api.chat_runner import ChatDependencies, process_chat_run
from agentfinx.api.clarify import (MAX_HISTORY_CHARS, MAX_TURNS, bounded_history,
                            clarify_question, rewrite_violations, _default_rewriter, _provider_schema)
from agentfinx.api.context_history import load_context_history
from agentfinx.api.messages import message_out

FIRST = "Vốn chủ sở hữu của công ty là gì"
FOLLOWUP = "vào đầu năm thì sao"
EFFECTIVE = "Vốn chủ sở hữu của công ty vào đầu năm là bao nhiêu?"
HISTORY = [ContextTurn(run_id="prior", effective_question=FIRST)]


def standalone(question):
    return {"status": "standalone", "question": question, "metric": {}, "period": {}}


def resolved(source="prior", question=EFFECTIVE):
    return {
        "status": "resolved", "question": question,
        "metric": {"kept": [{"source_run_id": source, "text": "Vốn chủ sở hữu"}]},
        "period": {"changed": ["đầu năm"]},
    }


def fixed(value):
    return lambda _question, _history: copy.deepcopy(value)


def test_equity_opening_followup_is_resolved():
    result = clarify_question(FOLLOWUP, HISTORY, rewrite=fixed(resolved()))
    assert result.can_continue and result.rewritten and result.question == EFFECTIVE
    assert result.status == result.reason == "resolved"
    assert result.history_run_ids == ("prior",)


def test_provider_schema_requires_nested_fields_and_has_no_defaults():
    schema = _provider_schema()
    assert set(schema["required"]) == set(schema["properties"])
    field = schema["properties"]["metric"]
    assert set(field["required"]) == {"kept", "changed"}
    assert "default" not in json.dumps(schema)
    assert "$ref" not in json.dumps(schema)
    assert field["properties"]["kept"]["items"]["required"] == ["source_run_id", "text"]


def test_context_model_receives_report_metadata_without_paths_or_facts(monkeypatch):
    from agentfinx.core.llm import client

    captured = {}
    question = "vốn của công ty này là bao nhiêu"

    class Model:
        def with_structured_output(self, schema, **kwargs):
            captured["schema"] = schema
            return self

        def invoke(self, messages):
            captured["data"] = json.loads(messages[-1].content)
            return standalone(question)

    monkeypatch.setattr(client, "get_llm", Model)
    metadata = ReportContext.from_dataset(SimpleNamespace(
        company="Công ty Mẫu", ticker="MAU", fiscal_year=2025, scope="separate",
        file_path="PRIVATE_PATH", facts=[{"value": "999999 VND"}],
    ))
    result = _default_rewriter(question, HISTORY, report_context=metadata)
    assert result["question"] == question
    assert captured["data"]["selected_report"] == {
        "company": "Công ty Mẫu", "ticker": "MAU", "fiscal_year": 2025, "scope": "separate",
    }
    assert "PRIVATE_PATH" not in json.dumps(captured["data"])
    assert "999999 VND" not in json.dumps(captured["data"])


def test_report_aware_standalone_does_not_inherit_unrelated_history(monkeypatch):
    from agentfinx.api import clarify

    question = "vốn của công ty này là bao nhiêu"
    metadata = ReportContext(company="Công ty Mẫu", fiscal_year=2025)

    def rewrite(current, history, *, report_context):
        assert current == question and history == HISTORY
        assert report_context == metadata
        return standalone(current)

    monkeypatch.setattr(clarify, "_default_rewriter", rewrite)
    result = clarify_question(question, HISTORY, report_context=metadata)
    assert result.status == "standalone" and result.question == question


def test_old_repeated_yes_does_not_replace_the_pending_proposal(monkeypatch):
    from agentfinx.core.llm import client

    proposal = ContextTurn(run_id="proposal", effective_question="sau thuế",
                           clarification_question="Bạn muốn hỏi lợi nhuận sau thuế năm 2025?")
    repeated = ContextTurn(run_id="repeated", effective_question="đúng",
                           clarification_question="Bạn muốn hỏi về chỉ tiêu, kỳ hoặc công ty nào?")
    captured = {}

    class Model:
        def with_structured_output(self, *_args, **_kwargs):
            return self

        def invoke(self, messages):
            captured.update(json.loads(messages[-1].content))
            return {"status": "needs_clarification", "metric": {}, "period": {},
                    "clarification_question": "fixture"}

    monkeypatch.setattr(client, "get_llm", Model)
    _default_rewriter("đúng", [proposal, repeated])
    assert captured["question"] == "đúng"
    assert [turn["run_id"] for turn in captured["history"]] == ["proposal"]


@pytest.mark.parametrize("status", ["needs_clarification", "failed"])
def test_non_qa_outcomes_never_fall_back(status):
    response = {"status": status, "metric": {}, "period": {}}
    if status == "needs_clarification":
        response["clarification_question"] = "Bạn muốn hỏi chỉ tiêu nào?"
    result = clarify_question(FOLLOWUP, HISTORY, rewrite=fixed(response))
    assert result.status == status and not result.can_continue and result.question == ""


def test_unchanged_text_is_not_an_explicit_standalone_status():
    result = clarify_question(FOLLOWUP, HISTORY, rewrite=fixed(FOLLOWUP))
    assert result.status == "failed" and result.reason == "schema_error"
    assert not result.can_continue and result.question == ""


def test_standalone_is_explicit_and_verbatim_even_when_topic_changes():
    question = "Doanh thu thuần năm 2025 là bao nhiêu?"
    result = clarify_question(question, HISTORY, rewrite=fixed(standalone(question)))
    assert result.status == "standalone" and result.question == question
    bad = clarify_question(question, HISTORY, rewrite=fixed(standalone("Vốn chủ sở hữu năm 2025?")))
    assert bad.status == "failed" and "standalone_changed" in bad.diagnostics


@pytest.mark.parametrize("bad_question,diagnostic", [
    ("Tổng tài sản của công ty vào đầu năm là bao nhiêu?", "metric_inherited_span_missing"),
    ("Vốn chủ sở hữu của công ty vào cuối năm là bao nhiêu?", "time_reference_changed"),
    ("Vốn chủ sở hữu của công ty vào đầu năm 2025 là bao nhiêu?", "new_number_or_sign"),
    ("Vốn chủ sở hữu của công ty vào đầu năm là bao nhiêu VND?", "new_unit"),
])
def test_bad_rewrite_is_not_passed_to_qa(bad_question, diagnostic):
    result = clarify_question(FOLLOWUP, HISTORY, rewrite=fixed(resolved(question=bad_question)))
    assert result.status == "failed" and result.reason == "guard_rejected"
    assert diagnostic in result.diagnostics and result.question == ""


@pytest.mark.parametrize("mutation,diagnostic", [
    (lambda r: r["metric"]["kept"][0].update(source_run_id="foreign"), "unknown_source_run"),
    (lambda r: r["metric"]["kept"][0].update(text="Tổng tài sản"), "metric_source_span_missing"),
    (lambda r: r.update(metric={}, period={}), "missing_inherited_span"),
    (lambda r: r.update(period={}), "period_claim_missing"),
    (lambda r: r["period"].update(changed=["cuối năm"]), "period_change_not_from_user"),
])
def test_span_declarations_must_be_grounded(mutation, diagnostic):
    response = resolved()
    mutation(response)
    result = clarify_question(FOLLOWUP, HISTORY, rewrite=fixed(response))
    assert result.status == "failed" and diagnostic in result.diagnostics


def test_metric_and_period_fields_cannot_be_omitted():
    response = resolved()
    del response["metric"]
    assert clarify_question(FOLLOWUP, HISTORY, rewrite=fixed(response)).reason == "schema_error"


def test_intentional_metric_change_keeps_the_period():
    history = [ContextTurn(run_id="prior", effective_question="Vốn chủ sở hữu cuối năm là bao nhiêu?")]
    response = {
        "status": "resolved", "question": "Còn doanh thu cuối năm là bao nhiêu?",
        "metric": {"changed": ["doanh thu"]},
        "period": {"kept": [{"source_run_id": "prior", "text": "cuối năm"}]},
    }
    result = clarify_question("Còn doanh thu thì sao?", history, rewrite=fixed(response))
    assert result.status == "resolved"


def test_numeric_tokens_from_effective_history_are_allowed_not_from_snapshots():
    history = [ContextTurn(run_id="prior", effective_question="Vốn chủ sở hữu năm 2025?",
                           snapshot={"period": "2024"})]
    response = resolved(question="Vốn chủ sở hữu vào đầu năm 2025 là bao nhiêu?")
    assert clarify_question(FOLLOWUP, history, rewrite=fixed(response)).status == "resolved"
    response["question"] = response["question"].replace("2025", "2024")
    assert "new_number_or_sign" in clarify_question(FOLLOWUP, history, rewrite=fixed(response)).diagnostics


def test_number_sign_unit_and_relative_time_checks_are_not_normalized():
    assert "new_number_or_sign" in rewrite_violations("Còn 2025?", ["Hệ số 1.13 lần"], "Hệ số 113 lần năm 2025?")
    assert "new_number_or_sign" in rewrite_violations("Còn 2025?", ["Hệ số 1.13"], "Hệ số -1.13 năm 2025?")
    assert "new_unit" in rewrite_violations("Còn 2025?", ["Doanh thu tỷ đồng"], "Doanh thu triệu đồng năm 2025?")
    assert "current_number_removed" in rewrite_violations("Còn 2025?", ["Doanh thu 2024"], "Doanh thu 2024?")
    assert "time_reference_changed" in rewrite_violations("Còn năm trước?", [], "Còn năm nay?")
    assert not rewrite_violations("Còn năm trước?", ["Doanh thu"], "Doanh thu năm trước?")


@pytest.mark.parametrize("surface", ["đầu năm", "cuối năm", "đầu kỳ", "cuối kỳ", "năm trước",
                                    "năm ngoái", "quý trước", "cùng kỳ", "31/12/2025",
                                    "đầu   năm", "Đầu năm"])
def test_time_contract_preserves_exact_user_spans(surface):
    assert surface in time_reference_spans(f"Còn {surface} thì sao?")
    assert "time_reference_changed" in rewrite_violations(f"Còn {surface}?", [], "Doanh thu?")


def test_decomposed_vietnamese_keeps_original_offsets():
    import unicodedata
    text = unicodedata.normalize("NFD", "đầu năm")
    assert time_reference_spans(text) == [text]


def test_provider_error_no_history_and_no_retry():
    called = []

    def boom(q, h):
        called.append((q, h))
        raise RuntimeError("SECRET PROVIDER DETAILS")

    result = clarify_question(FOLLOWUP, HISTORY, rewrite=boom)
    assert len(called) == 1 and result.status == "failed" and result.reason == "provider_error"
    assert "SECRET" not in json.dumps(result.payload())
    called.clear()
    first = clarify_question(FIRST, [], rewrite=boom)
    assert first.status == "skipped" and first.reason == "no_history"
    assert first.question == FIRST and first.can_continue and called == []


def test_history_budget_also_bounds_snapshot_payloads():
    turns = [ContextTurn(run_id=str(i), effective_question=f"câu {i}") for i in range(20)]
    assert [t.run_id for t in bounded_history(turns)] == [str(i) for i in range(10, 20)]
    turns = [ContextTurn(run_id=str(i), effective_question="x" * 4000,
                         snapshot={"extra": "y" * 20_000}) for i in range(4)]
    bounded = bounded_history(turns)
    assert len(bounded) <= MAX_TURNS and len(bounded) == 2
    assert sum(len(json.dumps(t.model_dump(), ensure_ascii=False)) for t in bounded) <= MAX_HISTORY_CHARS
    assert all(not t.snapshot for t in bounded)


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    database = webdb.WebDB(tmp_path / "web.sqlite3")
    database.migrate()
    with database.transaction() as conn:
        user = webdb.create_user(conn, name="A", email="context@example.com", password_hash="x")
        session = webdb.create_session(conn, owner_id=user["id"], title="test",
                                       dataset_id="ds-1", dataset_generation="gen-1")
    asked, histories = [], []
    deps = ChatDependencies(
        shared_dataset_ids=frozenset(), context_enabled=True,
        resolve=lambda *_a: SimpleNamespace(record=SimpleNamespace(dataset_id="ds-1")),
        activate=lambda _ds: SimpleNamespace(generation="gen-1", collection=object()),
        execute=lambda _ds, _col, question, **_kw: asked.append(question) or {},
        collect_errors=lambda _state: [],
        format_answer=lambda _state: "Fixture answer: NOT HISTORY 9999 VND",
    )

    def enqueue(question, **overrides):
        with database.transaction() as conn:
            message = webdb.add_message(conn, session_id=session["id"], role="user", content=question)
            args = dict(session_id=session["id"], owner_id=user["id"], idempotency_key=webdb.new_id(),
                        question=question, question_message_id=message,
                        dataset_id="ds-1", dataset_generation="gen-1")
            args.update(overrides)
            run = webdb.create_run(conn, **args)
            webdb.claim_next_run(conn, "fixture")
        return run["id"]

    def get(run_id):
        with database.read() as conn:
            return webdb.get_run(conn, run_id)

    def execute(question, rewrite=None):
        deps.clarify = rewrite
        run_id = enqueue(question)
        process_chat_run(database, run_id, deps)
        return get(run_id)

    return SimpleNamespace(db=database, session=session, user=user, deps=deps,
                           enqueue=enqueue, get=get, execute=execute, asked=asked, histories=histories)


def test_worker_chains_effective_questions_not_originals_or_answers(harness):
    first = harness.execute(FIRST)
    assert first["effective_question"] == FIRST and first["context_reason"] == "no_history"

    def second_resolver(q, history):
        assert [t.effective_question for t in history] == [FIRST]
        assert q == FOLLOWUP and "9999" not in str(history)
        return resolved(source=first["id"])

    second = harness.execute(FOLLOWUP, second_resolver)
    assert second["status"] == "succeeded" and second["effective_question"] == EFFECTIVE

    def third_resolver(q, history):
        assert [t.effective_question for t in history] == [FIRST, EFFECTIVE]
        assert history[-1].run_id == second["id"] and not history[-1].legacy
        return {
            "status": "resolved",
            "question": "Vốn chủ sở hữu tăng bao nhiêu từ đầu năm đến cuối năm?",
            "metric": {"kept": [{"source_run_id": second["id"], "text": "Vốn chủ sở hữu"}]},
            "period": {"kept": [{"source_run_id": second["id"], "text": "đầu năm"}],
                       "changed": ["cuối năm"]},
            "operation": {"changed": ["tăng bao nhiêu"]},
        }

    third = harness.execute("Đến cuối năm tăng bao nhiêu?", third_resolver)
    assert third["status"] == "succeeded"
    assert harness.asked == [FIRST, EFFECTIVE, third["effective_question"]]
    with harness.db.read() as conn:
        messages = [message_out(m) for m in webdb.list_messages(conn, harness.session["id"])]
    assistant = [m for m in messages if m["role"] == "assistant"]
    assert assistant[1]["interpreted_question"] == EFFECTIVE  # survives history reload
    assert assistant[1]["context_status"] == "resolved"


@pytest.mark.parametrize("mode", ["guard", "schema", "provider", "needs_clarification"])
def test_worker_does_not_call_planner_or_pipeline_on_non_qa_context(harness, mode):
    first = harness.execute(FIRST)
    harness.asked.clear()
    harness.deps.discovery_enabled = True
    harness.deps.plan_task = lambda _s: pytest.fail("planner must not run")

    def resolve(_q, _h):
        if mode == "provider":
            raise RuntimeError("PRIVATE PROVIDER ERROR")
        if mode == "schema":
            return FOLLOWUP
        if mode == "needs_clarification":
            return {"status": mode, "metric": {}, "period": {},
                    "clarification_question": "Bạn muốn hỏi chỉ tiêu nào?"}
        return resolved(source=first["id"], question="Tổng tài sản đầu năm là bao nhiêu?")

    run = harness.execute(FOLLOWUP, resolve)
    assert harness.asked == [] and run["effective_question"] == ""
    assert run["status"] == ("succeeded" if mode == "needs_clarification" else "failed")
    assert run["result_kind"] == ("context_clarification" if mode == "needs_clarification" else "context_error")
    with harness.db.read() as conn:
        answer = message_out(webdb.get_message(conn, run["answer_message_id"]))
    assert answer["kind"] == run["result_kind"]
    assert "PRIVATE" not in answer["content"]
    current = harness.get(harness.enqueue("Doanh thu là bao nhiêu?"))
    expected = [first["id"], run["id"]] if mode == "needs_clarification" else [first["id"]]
    assert [t.run_id for t in load_context_history(harness.db, current)] == expected


def test_short_answer_resolves_pending_profit_clarification(harness):
    harness.execute(FIRST)
    initial = "lợi nhuận của công ty là bao nhiêu"
    pending = harness.execute(initial, fixed({
        "status": "needs_clarification", "metric": {}, "period": {},
        "clarification_question": "Bạn muốn hỏi lợi nhuận nào (sau thuế, trước thuế, gộp)?",
    }))
    effective = "lợi nhuận sau thuế của công ty là bao nhiêu?"

    def resolve(question, history):
        assert question == "sau thuế"
        assert history[-1].run_id == pending["id"]
        assert history[-1].effective_question == initial
        assert "lợi nhuận nào" in history[-1].clarification_question
        assert "Fixture answer" not in str(history)
        return {"status": "resolved", "question": effective,
                "metric": {"kept": [{"source_run_id": pending["id"], "text": "lợi nhuận"}],
                           "changed": ["sau thuế"]}, "period": {}}

    answer = harness.execute("sau thuế", resolve)
    assert answer["status"] == "succeeded" and answer["context_status"] == "resolved"
    assert harness.asked[-1] == effective
    current = harness.get(harness.enqueue("Nợ phải trả là bao nhiêu?"))
    history = load_context_history(harness.db, current)
    assert all(not turn.clarification_question for turn in history)
    assert history[-1].effective_question == effective


def test_yes_confirms_pending_proposal_in_selected_report(harness):
    harness.execute(FIRST)
    harness.deps.resolve = lambda *_: SimpleNamespace(record=SimpleNamespace(
        dataset_id="ds-1", company="Công ty Mẫu", fiscal_year=2025))
    harness.execute("lợi nhuận của công ty là bao nhiêu", fixed({
        "status": "needs_clarification", "metric": {}, "period": {},
        "clarification_question": "Bạn muốn hỏi lợi nhuận sau thuế hay trước thuế?",
    }))
    proposal = harness.execute("sau thuế", fixed({
        "status": "needs_clarification", "metric": {}, "period": {},
        "clarification_question": "Bạn muốn hỏi lợi nhuận sau thuế của công ty trong năm 2025?",
    }))
    effective = "lợi nhuận sau thuế của công ty trong năm 2025 là bao nhiêu?"

    def resolve(question, history):
        assert question == "đúng"
        assert history[-1].run_id == proposal["id"]
        assert history[-1].effective_question == "sau thuế"
        assert "lợi nhuận sau thuế" in history[-1].clarification_question
        return {"status": "resolved", "question": effective,
                "metric": {"kept": [{"source_run_id": proposal["id"], "text": "lợi nhuận sau thuế"}]},
                "period": {"kept": [{"source_run_id": proposal["id"], "text": "năm 2025"}]}}

    answer = harness.execute("đúng", resolve)
    assert answer["status"] == "succeeded" and answer["context_status"] == "resolved"
    assert harness.asked[-1] == effective


def test_confirmation_never_inherits_assistant_financial_values():
    pending = [ContextTurn(run_id="pending", effective_question="lợi nhuận?",
                          clarification_question="Lợi nhuận sau thuế 999999 VND năm 2025?")]
    response = {"status": "resolved", "question": "Lợi nhuận sau thuế 999999 VND năm 2025?",
                "metric": {"kept": [{"source_run_id": "pending", "text": "Lợi nhuận sau thuế"}]},
                "period": {"kept": [{"source_run_id": "pending", "text": "năm 2025"}]}}
    result = clarify_question("đúng", pending, rewrite=fixed(response),
                              report_context=ReportContext(fiscal_year=2025))
    assert result.status == "failed"
    assert "new_number_or_sign" in result.diagnostics and "new_unit" in result.diagnostics


def test_confirmation_accepts_sentence_capitalization_without_changing_metric():
    pending = [ContextTurn(run_id="pending", effective_question="sau thuế",
                          clarification_question="Bạn muốn hỏi lợi nhuận sau thuế trong năm 2025?")]
    response = {"status": "resolved", "question": "Lợi nhuận sau thuế trong năm 2025 là bao nhiêu?",
                "metric": {"kept": [{"source_run_id": "pending", "text": "lợi nhuận sau thuế"}]},
                "period": {"kept": [{"source_run_id": "pending", "text": "năm 2025"}]}}
    result = clarify_question("đúng", pending, rewrite=fixed(response),
                              report_context=ReportContext(fiscal_year=2025))
    assert result.status == "resolved"


def test_confirmation_passes_a_lookup_instead_of_asking_the_user_again():
    pending = [ContextTurn(run_id="pending", effective_question="sau thuế",
                          clarification_question="Bạn muốn hỏi lợi nhuận sau thuế trong năm 2025?")]
    response = {"status": "resolved", "question": "Bạn muốn hỏi lợi nhuận sau thuế trong năm 2025?",
                "metric": {"kept": [{"source_run_id": "pending", "text": "lợi nhuận sau thuế"}]},
                "period": {"kept": [{"source_run_id": "pending", "text": "năm 2025"}]}}
    result = clarify_question("đúng", pending, rewrite=fixed(response),
                              report_context=ReportContext(fiscal_year=2025))
    assert result.status == "resolved"
    assert result.question == "lợi nhuận sau thuế trong năm 2025?"


def test_confirmation_binds_report_year_mislabeled_as_a_new_user_change():
    pending = [ContextTurn(run_id="pending", effective_question="sau thuế",
                          clarification_question="Bạn muốn hỏi lợi nhuận sau thuế trong năm 2025?")]
    response = {"status": "resolved", "question": "Lợi nhuận sau thuế trong năm 2025?",
                "metric": {"kept": [{"source_run_id": "pending", "text": "lợi nhuận sau thuế"}]},
                "period": {"changed": ["năm 2025"]}}
    result = clarify_question("đúng", pending, rewrite=fixed(response),
                              report_context=ReportContext(fiscal_year=2025))
    assert result.status == "resolved"
    assert result.resolution["period"] == {"changed": [], "kept": [{"source_run_id": "pending", "text": "năm 2025"}]}
    response["question"] = response["question"].replace("2025", "2024")
    response["period"] = {"changed": ["năm 2024"]}
    invalid = clarify_question("đúng", pending, rewrite=fixed(response),
                               report_context=ReportContext(fiscal_year=2025))
    assert invalid.status == "failed" and "period_change_not_from_user" in invalid.diagnostics


@pytest.mark.parametrize("field,value", [("owner_id", "foreign-owner"), ("dataset_id", "foreign-dataset"),
                                         ("dataset_generation", "foreign-generation"), ("session_id", None)])
def test_pending_clarifications_remain_owner_report_generation_session_scoped(harness, field, value):
    harness.execute(FIRST)
    pending = harness.execute("lợi nhuận?", fixed({"status": "needs_clarification", "metric": {}, "period": {},
                              "clarification_question": "Bạn muốn lợi nhuận nào?"}))
    with harness.db.transaction() as conn:
        if field == "session_id":
            value = webdb.create_session(conn, owner_id=harness.user["id"], title="another",
                                         dataset_id="ds-1", dataset_generation="gen-1")["id"]
        conn.execute(f"UPDATE chat_runs SET {field}=? WHERE id=?", (value, pending["id"]))
    current = harness.get(harness.enqueue("sau thuế"))
    history = load_context_history(harness.db, current)
    assert pending["id"] not in [turn.run_id for turn in history]


def test_context_disabled_preserves_execution_but_records_the_effective_question(harness):
    harness.execute(FIRST)
    harness.deps.context_enabled = False
    run = harness.execute(FOLLOWUP, lambda *_a: pytest.fail("disabled"))
    assert run["context_status"] == "skipped" and run["context_reason"] == "disabled"
    assert run["effective_question"] == FOLLOWUP and harness.asked[-1] == FOLLOWUP


def test_worker_passes_only_owner_scoped_selected_report_metadata(harness, monkeypatch):
    from agentfinx.api import clarify

    harness.execute(FIRST)
    calls = []

    def accessible(dataset_id, owner_id, shared):
        calls.append((dataset_id, owner_id, shared))
        return SimpleNamespace(record=SimpleNamespace(
            dataset_id=dataset_id, company="Công ty Mẫu", fiscal_year=2025,
            fiscal_quarter=2, scope="consolidated", file_path="PRIVATE_PATH",
        ))

    def rewrite(question, history, *, report_context):
        assert report_context.company == "Công ty Mẫu"
        assert report_context.period_label() == "quý 2/2025"
        assert "PRIVATE_PATH" not in report_context.model_dump_json()
        return standalone(question)

    harness.deps.resolve = accessible
    monkeypatch.setattr(clarify, "_default_rewriter", rewrite)
    run = harness.execute("Tổng tài sản của công ty này là bao nhiêu?")
    assert run["status"] == "succeeded" and run["context_status"] == "standalone"
    assert all(call == ("ds-1", harness.user["id"], frozenset()) for call in calls)


def test_context_failure_message_does_not_blame_missing_report_inputs(harness):
    harness.execute(FIRST)
    run = harness.execute(FOLLOWUP, fixed("invalid JSON"))
    with harness.db.read() as conn:
        message = webdb.get_message(conn, run["answer_message_id"])
    assert "Bước xử lý ngữ cảnh gặp lỗi" in message["content"]
    assert "không cần nhập lại công ty hoặc kỳ" in message["content"]


def test_history_is_owner_session_dataset_generation_scoped_and_marks_legacy(harness):
    first = harness.execute(FIRST)
    with harness.db.transaction() as conn:
        conn.execute("UPDATE chat_runs SET context_resolution='', effective_question='' WHERE id=?", (first["id"],))
    second = harness.execute("Doanh thu là bao nhiêu?", lambda q, _h: standalone(q))
    with harness.db.transaction() as conn:
        conn.execute("UPDATE chat_runs SET dataset_generation='gen-old' WHERE id=?", (second["id"],))
    third = harness.execute("Lợi nhuận là bao nhiêu?", lambda q, _h: standalone(q))
    with harness.db.transaction() as conn:
        conn.execute("UPDATE chat_runs SET owner_id='other' WHERE id=?", (third["id"],))
    fourth = harness.execute("Tổng tài sản?", lambda q, _h: standalone(q))
    with harness.db.transaction() as conn:
        conn.execute("UPDATE chat_runs SET task_kind='report_discovery', result_kind='report_candidates' WHERE id=?", (fourth["id"],))
    fifth = harness.execute("Tiền mặt?", lambda q, _h: standalone(q))
    with harness.db.transaction() as conn:
        other = webdb.create_session(conn, owner_id=harness.user["id"], title="other",
                                     dataset_id="ds-1", dataset_generation="gen-1")
        conn.execute("UPDATE chat_runs SET session_id=? WHERE id=?", (other["id"], fifth["id"]))
    sixth = harness.execute("Nợ phải trả?", lambda q, _h: standalone(q))
    with harness.db.transaction() as conn:
        conn.execute("UPDATE chat_runs SET dataset_id='ds-other' WHERE id=?", (sixth["id"],))
    seventh = harness.execute("Hàng tồn kho?", lambda q, _h: standalone(q))
    with harness.db.transaction() as conn:
        conn.execute("UPDATE chat_runs SET status='interrupted' WHERE id=?", (seventh["id"],))
    current = harness.get(harness.enqueue(FOLLOWUP))
    history = load_context_history(harness.db, current)
    assert len(history) == 1 and history[0].run_id == first["id"]
    assert history[0].legacy and history[0].effective_question == FIRST


def test_structured_schema_has_no_silent_status_default():
    with pytest.raises(Exception):
        ContextResolution.model_validate({"question": FIRST, "metric": {}, "period": {}})


def test_worker_persists_intent_snapshot_after_qa_and_exposes_it_only_as_context(harness):
    harness.deps.execute = lambda *_a, **_k: {
        "planner_plan": {"company": "Công ty Mẫu", "time_hint": "cuối năm"},
        "evidence_ledger": {"entries": [{"kind": "retrieval_requirement",
            "requirement": "Vốn chủ sở hữu", "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
            "parsed_query_slots": {"metric": "equity", "period_role": "ending", "operation": "lookup"},
            "selected_facts": [{"value": "9999 VND"}]}]},
        "final_answer": "123456789 VND",
    }
    first = harness.execute(FIRST)
    snapshot = json.loads(first["context_snapshot"])
    assert snapshot["source_run_id"] == first["id"]
    # Neither a fact value nor the answer is kept (full values: a random run id may contain "9999").
    assert "9999 VND" not in first["context_snapshot"] and "123456789" not in first["context_snapshot"]

    def resolver(_q, history):
        assert history[-1].effective_question == FIRST
        assert history[-1].snapshot["requirements"][0]["parsed_query_slots"]["metric"] == "equity"
        return resolved(source=first["id"])

    assert harness.execute(FOLLOWUP, resolver)["status"] == "succeeded"
