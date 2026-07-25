"""Regression tests for test dataset batch runner."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

batch_runner = importlib.import_module("dataset_batch_runner")


def test_resolve_queries_combines_cli_and_file(tmp_path: Path):
    queries_file = tmp_path / "queries.txt"
    queries_file.write_text(
        "ROE là bao nhiêu?\n\nROA là bao nhiêu?\nROE là bao nhiêu?\n",
        encoding="utf-8",
    )

    queries = batch_runner.resolve_queries(
        ["Tổng tài sản là bao nhiêu?", "ROE là bao nhiêu?"],
        str(queries_file),
    )

    assert queries == [
        "Tổng tài sản là bao nhiêu?",
        "ROE là bao nhiêu?",
        "ROA là bao nhiêu?",
    ]


def test_load_queries_from_json_object(tmp_path: Path):
    queries_file = tmp_path / "queries.json"
    queries_file.write_text(
        json.dumps(
            {
                "queries": [
                    "Doanh thu là bao nhiêu?",
                    "Lợi nhuận sau thuế là bao nhiêu?",
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    queries = batch_runner.load_queries_from_file(str(queries_file))

    assert queries == [
        "Doanh thu là bao nhiêu?",
        "Lợi nhuận sau thuế là bao nhiêu?",
    ]


def test_load_query_records_from_json_list_with_references(tmp_path: Path):
    queries_file = tmp_path / "queries.json"
    queries_file.write_text(
        json.dumps(
            [
                {
                    "query": "Doanh thu là bao nhiêu?",
                    "reference": "100 tỷ VND.",
                },
                {
                    "query": "Lợi nhuận sau thuế là bao nhiêu?",
                    "reference": "10 tỷ VND.",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    records = batch_runner.load_query_records_from_file(str(queries_file))

    assert records == [
        {
            "query": "Doanh thu là bao nhiêu?",
            "reference": "100 tỷ VND.",
        },
        {
            "query": "Lợi nhuận sau thuế là bao nhiêu?",
            "reference": "10 tỷ VND.",
        },
    ]


def test_serialize_run_result_can_include_trace():
    final_state = {
        "synth_decision": {
            "status": "answer",
            "answer": "ROE khoảng 6,46%.",
            "missing": [],
        },
        "trace": [
            {"event": "planner:done"},
            {
                "event": "run:done",
                "duration_ms": 1234,
                "trace_events_n": 2,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
        ],
    }

    result = batch_runner.serialize_run_result(
        final_state,
        "ROE là bao nhiêu?",
        include_trace=True,
    )

    assert result["query"] == "ROE là bao nhiêu?"
    assert result["synth_status"] == "answer"
    assert result["answer"] == "ROE khoảng 6,46%."
    assert result["final_answer"] == "ROE khoảng 6,46%."
    assert "reference" not in result
    assert result["references"] == ""
    assert result["runtime"] == 1234
    assert result["total_tokens"] == 150
    assert result["formatted_answer"] == "=== FINAL ANSWER ===\nANSWER: ROE khoảng 6,46%."
    assert result["run_summary"]["event"] == "run:done"
    assert result["trace"] == final_state["trace"]


def test_build_output_document_appends_different_queries_to_one_file():
    existing = {
        "updated_at": "2026-04-08T10:00:00+00:00",
        "queries_n": 1,
        "queries": ["ROE là bao nhiêu?"],
        "query_reports": [
            {
                "query": "ROE là bao nhiêu?",
                "generated_at": "2026-04-08T10:00:00+00:00",
                "results": [{"dataset_id": "dataset-a", "run": {"query": "ROE là bao nhiêu?"}}],
            }
        ],
    }
    latest = {
        "generated_at": "2026-04-09T10:00:00+00:00",
        "queries": ["ROA là bao nhiêu?"],
        "results": [
            {
                "dataset_id": "dataset-a",
                "runs": [{"query": "ROA là bao nhiêu?", "answer": "5%"}],
            }
        ],
    }

    output = batch_runner.build_output_document(
        latest,
        existing_output=existing,
        overwrite=False,
    )

    assert output["updated_at"] == latest["generated_at"]
    assert output["queries_n"] == 2
    assert [item["query"] for item in output["queries"]] == [
        "ROE là bao nhiêu?",
        "ROA là bao nhiêu?",
    ]
    assert output["queries"][1]["final_answer"] == "5%"
    assert output["queries"][1]["total_tokens"] == 0
    assert set(output["queries"][1]) == {
        "query",
        "final_answer",
        "references",
        "runtime",
        "total_tokens",
        "dataset_summaries",
    }
    assert output["queries"][1]["dataset_summaries"] == [
        {
            "dataset_id": "dataset-a",
            "final_answer": "5%",
            "runtime": None,
            "total_tokens": 0,
            "errors": [],
        }
    ]
    assert output["query_reports"][0]["query"] == "ROE là bao nhiêu?"
    assert output["query_reports"][1]["query"] == "ROA là bao nhiêu?"


def test_build_output_document_never_selects_dataset_zero_as_multi_dataset_answer():
    latest = {
        "generated_at": "2026-04-09T10:00:00+00:00",
        "queries": ["ROE là bao nhiêu?"],
        "results": [
            {
                "dataset_id": "dataset-a",
                "runs": [
                    {
                        "query": "ROE là bao nhiêu?",
                        "final_answer": "ROE A là 6%.",
                        "runtime": 100,
                        "total_tokens": 10,
                    }
                ],
            },
            {
                "dataset_id": "dataset-b",
                "runs": [
                    {
                        "query": "ROE là bao nhiêu?",
                        "final_answer": "ROE B là 8%.",
                        "runtime": 200,
                        "total_tokens": 20,
                    }
                ],
            },
        ],
    }

    output = batch_runner.build_output_document(latest, overwrite=True)
    record = output["queries"][0]

    assert record["final_answer"] == ""
    assert record["runtime"] == 300
    assert record["total_tokens"] == 30
    assert [item["dataset_id"] for item in record["dataset_summaries"]] == [
        "dataset-a",
        "dataset-b",
    ]
    assert [item["final_answer"] for item in record["dataset_summaries"]] == [
        "ROE A là 6%.",
        "ROE B là 8%.",
    ]


def test_build_output_document_replaces_existing_query_result():
    existing = {
        "updated_at": "2026-04-08T10:00:00+00:00",
        "queries_n": 1,
        "queries": ["ROE là bao nhiêu?"],
        "query_reports": [
            {
                "query": "ROE là bao nhiêu?",
                "generated_at": "2026-04-08T10:00:00+00:00",
                "results": [{"dataset_id": "dataset-a", "run": {"query": "ROE là bao nhiêu?", "answer": "old"}}],
            }
        ],
    }
    latest = {
        "generated_at": "2026-04-09T10:00:00+00:00",
        "queries": ["ROE là bao nhiêu?"],
        "results": [
            {
                "dataset_id": "dataset-a",
                "runs": [{"query": "ROE là bao nhiêu?", "answer": "new"}],
            }
        ],
    }

    output = batch_runner.build_output_document(
        latest,
        existing_output=existing,
        overwrite=False,
    )

    assert output["queries_n"] == 1
    assert output["query_reports"][0]["query"] == "ROE là bao nhiêu?"
    assert output["query_reports"][0]["results"][0]["run"]["answer"] == "new"


def test_build_output_document_can_overwrite_all_queries():
    existing = {
        "updated_at": "2026-04-08T10:00:00+00:00",
        "queries_n": 2,
        "queries": ["ROE là bao nhiêu?", "ROA là bao nhiêu?"],
        "query_reports": [
            {"query": "ROE là bao nhiêu?", "generated_at": "2026-04-08T10:00:00+00:00", "results": []},
            {"query": "ROA là bao nhiêu?", "generated_at": "2026-04-08T10:00:00+00:00", "results": []},
        ],
    }
    latest = {
        "generated_at": "2026-04-09T10:00:00+00:00",
        "queries": ["Doanh thu là bao nhiêu?"],
        "results": [],
    }

    output = batch_runner.build_output_document(
        latest,
        existing_output=existing,
        overwrite=True,
    )

    assert output["updated_at"] == latest["generated_at"]
    assert [item["query"] for item in output["queries"]] == ["Doanh thu là bao nhiêu?"]
    assert output["queries_n"] == 1


def test_resolve_datasets_filters_by_dataset_id():
    datasets = [
        SimpleNamespace(dataset_id="dataset-a"),
        SimpleNamespace(dataset_id="dataset-b"),
        SimpleNamespace(dataset_id="dataset-c"),
    ]

    selected = batch_runner.resolve_datasets(
        datasets,
        ["dataset-c", "dataset-a", "dataset-c"],
    )

    assert [item.dataset_id for item in selected] == ["dataset-c", "dataset-a"]


def test_resolve_datasets_raises_for_unknown_dataset_id():
    datasets = [SimpleNamespace(dataset_id="dataset-a")]

    try:
        batch_runner.resolve_datasets(datasets, ["dataset-b"])
    except SystemExit as exc:
        assert "Unknown dataset id(s): dataset-b" in str(exc)
    else:
        raise AssertionError("Expected resolve_datasets() to raise SystemExit for unknown dataset id")


def test_build_output_document_updates_only_selected_dataset_for_same_query():
    existing = {
        "updated_at": "2026-04-08T10:00:00+00:00",
        "queries_n": 1,
        "queries": ["ROE là bao nhiêu?"],
        "query_reports": [
            {
                "query": "ROE là bao nhiêu?",
                "generated_at": "2026-04-08T10:00:00+00:00",
                "datasets_n": 2,
                "total_runs": 2,
                "runs_with_errors": 0,
                "datasets_with_setup_error": 0,
                "results": [
                    {"dataset_id": "dataset-a", "run": {"query": "ROE là bao nhiêu?", "answer": "old-a"}},
                    {"dataset_id": "dataset-b", "run": {"query": "ROE là bao nhiêu?", "answer": "old-b"}},
                ],
            }
        ],
    }
    latest = {
        "generated_at": "2026-04-09T10:00:00+00:00",
        "queries": ["ROE là bao nhiêu?"],
        "selected_dataset_ids": ["dataset-a"],
        "results": [
            {
                "dataset_id": "dataset-a",
                "runs": [{"query": "ROE là bao nhiêu?", "answer": "new-a"}],
            }
        ],
    }

    output = batch_runner.build_output_document(
        latest,
        existing_output=existing,
        overwrite=False,
    )

    results = output["query_reports"][0]["results"]

    assert output["queries_n"] == 1
    assert [item["dataset_id"] for item in results] == ["dataset-a", "dataset-b"]
    assert results[0]["run"]["answer"] == "new-a"
    assert results[1]["run"]["answer"] == "old-b"


def test_normalize_existing_query_reports_can_migrate_old_batch_format():
    existing = {
        "generated_at": "2026-04-08T10:00:00+00:00",
        "queries": ["ROE là bao nhiêu?", "ROA là bao nhiêu?"],
        "history": [
            {
                "generated_at": "2026-04-07T10:00:00+00:00",
                "queries": ["Doanh thu là bao nhiêu?"],
                "results": [
                    {
                        "dataset_id": "dataset-a",
                        "company": "Hoa Phat",
                        "runs": [
                            {"query": "Doanh thu là bao nhiêu?", "answer": "100"},
                        ],
                    }
                ],
            }
        ],
        "results": [
            {
                "dataset_id": "dataset-a",
                "company": "Hoa Phat",
                "runs": [
                    {"query": "ROE là bao nhiêu?", "answer": "6%"},
                    {"query": "ROA là bao nhiêu?", "answer": "3%"},
                ],
            }
        ],
    }

    reports = batch_runner.normalize_existing_query_reports(existing)

    assert [item["query"] for item in reports] == [
        "Doanh thu là bao nhiêu?",
        "ROE là bao nhiêu?",
        "ROA là bao nhiêu?",
    ]
    assert reports[0]["results"][0]["run"]["answer"] == "100"
    assert reports[1]["results"][0]["run"]["answer"] == "6%"
    assert reports[2]["results"][0]["run"]["answer"] == "3%"
