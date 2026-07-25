"""Regression tests for test cli dataset selection."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

test_script = importlib.import_module("test")


def _make_args(**overrides):
    defaults = {
        "list_datasets": False,
        "delete_dataset": False,
        "yes": False,
        "dataset_id": "",
        "select_dataset": False,
        "company": "",
        "ticker": "",
        "industry": "",
        "report_type": "",
        "fiscal_year": None,
        "fiscal_quarter": None,
        "scope": "",
        "audit_status": "",
        "file_path": "",
        "query": "",
        "debug_trace": False,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_resolve_dataset_prompts_interactively_without_query(monkeypatch):
    dataset_a = object()
    dataset_b = object()
    chosen_dataset = object()
    seen = {}

    monkeypatch.setattr(
        test_script,
        "find_datasets",
        lambda **kwargs: [dataset_a, dataset_b],
    )
    monkeypatch.setattr(
        test_script.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )

    def fake_choose(matches, *, header):
        seen["matches"] = matches
        seen["header"] = header
        return chosen_dataset

    monkeypatch.setattr(test_script, "_choose_dataset_interactively", fake_choose)

    resolved = test_script.resolve_dataset(_make_args())

    assert resolved is chosen_dataset
    assert seen["matches"] == [dataset_a, dataset_b]
    assert seen["header"] == "Available datasets. Select one:"


def test_resolve_dataset_returns_match_directly_when_query_is_provided(monkeypatch):
    dataset = object()

    monkeypatch.setattr(test_script, "find_datasets", lambda **kwargs: [dataset])
    monkeypatch.setattr(
        test_script.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr(
        test_script,
        "_choose_dataset_interactively",
        lambda *args, **kwargs: pytest.fail("interactive dataset chooser should not be used"),
    )

    resolved = test_script.resolve_dataset(_make_args(query="Tổng tài sản là bao nhiêu?"))

    assert resolved is dataset


def test_resolve_dataset_can_force_prompt_with_flag(monkeypatch):
    dataset = object()
    seen = {}

    monkeypatch.setattr(test_script, "find_datasets", lambda **kwargs: [dataset])
    monkeypatch.setattr(
        test_script.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )

    def fake_choose(matches, *, header):
        seen["matches"] = matches
        seen["header"] = header
        return matches[0]

    monkeypatch.setattr(test_script, "_choose_dataset_interactively", fake_choose)

    resolved = test_script.resolve_dataset(
        _make_args(select_dataset=True, query="Doanh thu quý này là gì?")
    )

    assert resolved is dataset
    assert seen["matches"] == [dataset]
    assert seen["header"] == "Available datasets. Select one:"


def test_resolve_dataset_file_path_does_not_default_company_to_song_da(monkeypatch):
    seen = {}
    saved_dataset = object()

    def fake_build_dataset_record(**kwargs):
        seen.update(kwargs)
        return kwargs

    monkeypatch.setattr(test_script, "build_dataset_record", fake_build_dataset_record)
    monkeypatch.setattr(test_script, "save_dataset", lambda dataset: saved_dataset)

    resolved = test_script.resolve_dataset(
        _make_args(
            file_path="data/custom-report.md",
            company="",
            dataset_id="",
        )
    )

    assert resolved is saved_dataset
    assert seen["company"] == ""
    assert seen["dataset_id"] == "custom-report"


def test_resolve_dataset_file_path_keeps_explicit_dataset_id_when_company_missing(monkeypatch):
    seen = {}
    saved_dataset = object()

    def fake_build_dataset_record(**kwargs):
        seen.update(kwargs)
        return kwargs

    monkeypatch.setattr(test_script, "build_dataset_record", fake_build_dataset_record)
    monkeypatch.setattr(test_script, "save_dataset", lambda dataset: saved_dataset)

    resolved = test_script.resolve_dataset(
        _make_args(
            file_path="data/custom-report.md",
            company="",
            dataset_id="my-dataset",
        )
    )

    assert resolved is saved_dataset
    assert seen["company"] == ""
    assert seen["dataset_id"] == "my-dataset"


def test_resolve_dataset_for_delete_requires_explicit_selection(monkeypatch):
    monkeypatch.setattr(
        test_script.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )

    with pytest.raises(SystemExit, match="requires --dataset-id or filters"):
        test_script.resolve_dataset_for_delete(_make_args(delete_dataset=True))


def test_resolve_dataset_for_delete_prompts_when_multiple_matches(monkeypatch):
    dataset_a = object()
    dataset_b = object()
    chosen_dataset = object()
    seen = {}

    monkeypatch.setattr(
        test_script,
        "find_datasets",
        lambda **kwargs: [dataset_a, dataset_b],
    )
    monkeypatch.setattr(
        test_script.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )

    def fake_choose(matches, *, header):
        seen["matches"] = matches
        seen["header"] = header
        return chosen_dataset

    monkeypatch.setattr(test_script, "_choose_dataset_interactively", fake_choose)

    resolved = test_script.resolve_dataset_for_delete(
        _make_args(delete_dataset=True, company="Hoa Phat")
    )

    assert resolved is chosen_dataset
    assert seen["matches"] == [dataset_a, dataset_b]
    assert seen["header"] == "Multiple datasets matched. Select one to delete:"


def test_delete_dataset_cli_deletes_selected_dataset(monkeypatch, capsys):
    dataset = SimpleNamespace(dataset_id="dataset-a")
    seen = {}

    monkeypatch.setattr(test_script, "resolve_dataset_for_delete", lambda args: dataset)

    def fake_confirm(resolved, *, skip_confirmation):
        seen["skip_confirmation"] = skip_confirmation
        assert resolved is dataset

    monkeypatch.setattr(test_script, "_confirm_delete_dataset", fake_confirm)
    monkeypatch.setattr(
        test_script,
        "delete_dataset_record",
        lambda dataset_id: SimpleNamespace(dataset_id=dataset_id),
    )
    monkeypatch.setattr(test_script, "describe_dataset", lambda record: f"id={record.dataset_id}")

    exit_code = test_script.delete_dataset_cli(
        _make_args(delete_dataset=True, dataset_id="dataset-a", yes=True)
    )

    captured = capsys.readouterr()

    assert exit_code == 0
    assert seen["skip_confirmation"] is True
    assert "Deleted dataset:" in captured.out
    assert "Purged derived artifacts" in captured.out
