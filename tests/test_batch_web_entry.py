"""Batch runs through the web chat's entry (report discovery on)."""

from types import SimpleNamespace

import dataset_batch_runner as runner
from dataset_catalog import registry

DISCOVERY = {"task_kind": "report_discovery", "planner_plan": {}, "trace": [],
             "report_search": {"subject": "VNM"}}
QA = {"task_kind": "report_qa", "planner_plan": {"difficulty_level": "easy"}, "trace": []}


def _dataset():
    return SimpleNamespace(
        dataset_id="batch-web-entry", company="Công ty Mẫu", ticker="", fiscal_year=2025,
        fiscal_quarter=None, scope="unknown", audit_status="unknown", file_path="data/document.md",
        status="ready", facts_count=1, vector_docs_count=1, ingestion_version="v",
        vector_collection_name="financial_statement__batch-web-entry",
    )


def _run(monkeypatch, tmp_path, *, web_entry):
    monkeypatch.setenv("FINX_HOME", str(tmp_path))
    planned, executed = [], []
    monkeypatch.setattr(runner, "describe_dataset", lambda _dataset: "dataset")
    monkeypatch.setattr(runner, "ensure_built",
                        lambda dataset: (dataset, None, SimpleNamespace(generation="gen-1")))

    def plan(_dataset, query):
        planned.append(query)
        return DISCOVERY if query.startswith("Tìm") else QA

    def execute(_dataset, _collection, query, **kwargs):
        executed.append((query, kwargs))
        return {}

    monkeypatch.setattr(runner, "_plan_web_entry", plan)
    monkeypatch.setattr(runner, "execute_query", execute)
    monkeypatch.setattr(runner, "serialize_run_result",
                        lambda _state, query, **_kwargs: {"query": query, "errors": []})
    result = runner.run_dataset_queries(
        _dataset(),
        [{"id": "a", "query": "Tìm BCTC VNM năm 2025"}, {"id": "b", "query": "Doanh thu?"}],
        web_entry=web_entry,
    )
    return result["runs"], planned, executed


def test_web_entry_records_a_misroute_as_a_refusal_and_reuses_the_plan(monkeypatch, tmp_path):
    runs, planned, executed = _run(monkeypatch, tmp_path, web_entry=True)

    assert planned == ["Tìm BCTC VNM năm 2025", "Doanh thu?"]
    misroute, answered = runs
    assert misroute["final_answer"] == "" and runner.WEB_ENTRY_MISROUTE in misroute["errors"][0]
    assert answered == {"query": "Doanh thu?", "errors": []}
    assert executed == [("Doanh thu?", {"debug_trace": False, "planner_updates": QA})]


def test_predict_web_entry_records_a_misroute_as_a_failed_prediction(monkeypatch):
    import dataset_batch_result as predict
    import finx_pipeline as pipeline

    calls = []
    monkeypatch.setattr(runner, "_plan_web_entry",
                        lambda _dataset, question: DISCOVERY if question.startswith("Tìm") else QA)

    def execute(_dataset, _collection, question, **kwargs):
        calls.append((question, kwargs))
        raise RuntimeError("pipeline stubbed")

    monkeypatch.setattr(pipeline, "execute_query", execute)
    runtime = (SimpleNamespace(dataset_id="apec"), None, object(), {"dataset_id": "apec"})
    _meta, predictions = predict.run_predictions(
        dataset_id="apec",
        records=[{"id": 1, "question": "Tìm BCTC VNM năm 2025"}, {"id": 2, "question": "Doanh thu?"}],
        prepared_runtime=runtime,
        web_entry=True,
    )

    assert predictions[0]["answer"] == "" and runner.WEB_ENTRY_MISROUTE in predictions[0]["errors"][0]
    assert calls == [("Doanh thu?", {"debug_trace": False, "planner_updates": QA})]


def test_predict_identity_changes_only_for_web_entry(tmp_path):
    from evaluation.run_identity import build_run_identity

    kwargs = dict(seed_file=tmp_path / "seed.json", dataset_meta={"dataset_id": "apec"},
                  selected_records=[{"id": 1, "question": "q"}], full=False, limit=1)
    base = build_run_identity(**kwargs)
    assert build_run_identity(**kwargs, web_entry=False)["run_fingerprint"] == base["run_fingerprint"]
    assert build_run_identity(**kwargs, web_entry=True)["run_fingerprint"] != base["run_fingerprint"]


def test_the_cli_entry_never_runs_the_web_planner(monkeypatch, tmp_path):
    runs, planned, executed = _run(monkeypatch, tmp_path, web_entry=False)

    assert planned == [] and len(runs) == 2
    assert [kwargs for _query, kwargs in executed] == [{"debug_trace": False}] * 2


def _identity(**kwargs):
    return runner.build_batch_run_identity(
        query_records=[{"id": "q1", "query": "Doanh thu?"}],
        results=[],
        selected_dataset_ids=None,
        debug_trace=False,
        include_trace=False,
        **kwargs,
    )


def test_only_web_entry_runs_change_the_run_fingerprint():
    cli = _identity()
    assert _identity(web_entry=False)["run_fingerprint"] == cli["run_fingerprint"]
    assert _identity(web_entry=True)["run_fingerprint"] != cli["run_fingerprint"]
