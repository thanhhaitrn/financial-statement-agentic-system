import json
import subprocess
from pathlib import Path

import pytest

from eval_retrieval_recall import (
    evaluate_factual_recall,
    fact_matches,
    facts_from_mappings,
    git_worktree_provenance,
    load_factual_contract_records,
    matched_official_gate_records,
    parse_args,
    prepare_official_gate_records,
    retrieval_queries_for_question,
    score_factual_record,
    select_contract_records,
)
from evaluation.financial_text import normalize_period


FACT = {
    "entity": "Công ty APEC",
    "metric": "Doanh thu",
    "period": "năm hiện tại",
    "value": "10.000",
    "unit": "VND",
    "reference": "V.1",
}


def _write_contract(path: Path, records: list[dict], *, dataset_id: str = "apec") -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": dataset_id,
                "records": records,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _record(record_id: int, question: str = "Doanh thu là bao nhiêu?") -> dict:
    return {
        "id": record_id,
        "question": question,
        "expected_facts": [dict(FACT)],
    }


def test_retrieval_gate_executes_declared_ratio_operand_queries():
    question = (
        "Tỷ lệ dự phòng giảm giá chứng khoán kinh doanh trên "
        "tổng chi phí tài chính là bao nhiêu phần trăm?"
    )

    queries = retrieval_queries_for_question(question)

    assert queries[0] == question
    assert len(queries) == 3
    assert any("dự phòng giảm giá chứng khoán kinh doanh" in query for query in queries[1:])
    assert any("chi phí tài chính" in query for query in queries[1:])


def test_retrieval_gate_does_not_invent_legs_for_plain_lookup():
    question = "Tổng chi phí tài chính là bao nhiêu?"

    assert retrieval_queries_for_question(question) == [question]


def test_cli_defaults_to_contract_only_without_untracked_predictions_report():
    args = parse_args([])

    assert args.predictions_file == ""
    assert args.facts_contract.endswith("apec_q211_250_factual_facts.json")

    metadata, _records = load_factual_contract_records(args.facts_contract)
    assert metadata["seed_file"] == "dau_tu_APEC_ragas_seed.json"


def test_official_gate_prepares_from_contract_alone(tmp_path: Path):
    contract = _write_contract(tmp_path / "facts.json", [_record(211)])

    prepared = prepare_official_gate_records(
        contract,
        expected_dataset_id="apec",
    )

    assert [record["id"] for record in prepared["records"]] == [211]
    assert prepared["report"] == {"metadata": {}, "predictions": []}
    assert prepared["prediction_enrichment"] == {
        "provided": False,
        "matched_contract_records_n": 0,
        "contract_records_without_prediction_n": 1,
        "ignored_out_of_contract_predictions_n": 0,
    }
    assert prepared["contract_metadata"]["expected_facts_n"] == 1


def test_predictions_enrich_but_cannot_change_gate_population_or_denominator(tmp_path: Path):
    contract = _write_contract(tmp_path / "facts.json", [_record(211)])
    predictions = tmp_path / "predictions.json"
    predictions.write_text(
        json.dumps(
            {
                "metadata": {"seed_file": "seed.json"},
                "predictions": [
                    {
                        "id": 211,
                        "question": "Doanh thu là bao nhiêu?",
                        "answer": "10.000 VND",
                        "expected_facts": [{"value": "999.999"}],
                    },
                    {"id": 999, "question": "Ngoài contract", "answer": "ignored"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    prepared = prepare_official_gate_records(contract, predictions_path=predictions)

    assert [record["id"] for record in prepared["records"]] == [211]
    assert prepared["records"][0]["answer"] == "10.000 VND"
    assert prepared["records"][0]["expected_facts"] == [FACT]
    assert prepared["prediction_enrichment"]["matched_contract_records_n"] == 1
    assert prepared["prediction_enrichment"]["ignored_out_of_contract_predictions_n"] == 1


def test_prediction_with_contract_id_and_mismatched_question_fails_clearly(tmp_path: Path):
    contract = _write_contract(tmp_path / "facts.json", [_record(211)])
    predictions = tmp_path / "predictions.json"
    predictions.write_text(
        json.dumps(
            {
                "predictions": [
                    {"id": 211, "question": "Câu hỏi khác", "expected_facts": [FACT]}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"question mismatch.*211"):
        prepare_official_gate_records(contract, predictions_path=predictions)


@pytest.mark.parametrize("ids", ["211,999", "211,,212", "211,211", "abc"])
def test_requested_ids_must_be_unique_valid_contract_ids(tmp_path: Path, ids: str):
    contract = _write_contract(tmp_path / "facts.json", [_record(211), _record(212, "Q2")])
    _metadata, records = load_factual_contract_records(contract)

    with pytest.raises(ValueError, match=r"--ids|positive integer"):
        select_contract_records(records, ids)


def test_requested_id_subset_keeps_contract_order(tmp_path: Path):
    contract = _write_contract(
        tmp_path / "facts.json",
        [_record(211), _record(212, "Q2"), _record(213, "Q3")],
    )

    prepared = prepare_official_gate_records(contract, ids_expression="213,211")

    assert [record["id"] for record in prepared["records"]] == [211, 213]


def test_every_official_contract_identity_is_factual_and_never_legacy_derived(tmp_path: Path):
    contract = _write_contract(
        tmp_path / "facts.json",
        [_record(230, "Phân tích sự thay đổi doanh thu")],
    )
    _metadata, records = load_factual_contract_records(contract)
    records[0]["retrieved_facts"] = [dict(FACT)]

    result = evaluate_factual_recall(records)

    assert result["status"] == "pass"
    assert result["expected_facts_n"] == 1
    assert result["explicit_records_n"] == 1
    assert result["legacy_derived_records_n"] == 0
    assert result["rows"][0]["bucket"] == "factual"
    assert result["rows"][0]["official_contract"] is True


def test_matched_official_records_returns_intersection_only(tmp_path: Path):
    contract = _write_contract(
        tmp_path / "facts.json",
        [_record(211), _record(212, "Q2")],
    )
    _metadata, records = load_factual_contract_records(contract)

    matched = matched_official_gate_records(
        records,
        [
            {"id": "not-an-id", "question": "bad"},
            {"id": 999, "question": "unrelated"},
            {"id": 212, "question": "Q2", "retrieved_contexts": ["context"]},
        ],
    )

    assert [record["id"] for record in matched] == [212]
    assert matched[0]["retrieved_contexts"] == ["context"]
    assert matched[0]["expected_facts"] == [FACT]


def test_contract_rejects_missing_fact_fields_and_dataset_mismatch(tmp_path: Path):
    incomplete = _record(211)
    incomplete["expected_facts"][0].pop("reference")
    contract = _write_contract(tmp_path / "facts.json", [incomplete])

    with pytest.raises(ValueError, match=r"record 211 fact 1.*reference"):
        load_factual_contract_records(contract)

    valid_contract = _write_contract(tmp_path / "valid.json", [_record(211)])
    with pytest.raises(ValueError, match="dataset mismatch"):
        load_factual_contract_records(valid_contract, expected_dataset_id="other")


@pytest.mark.parametrize(
    "aliases",
    [
        "VI.1",
        [],
        ["", "VI.1"],
        ["VI.1", "vi 1"],
        ["V.1"],
    ],
)
def test_contract_reference_aliases_fail_closed(
    tmp_path: Path,
    aliases,
):
    record = _record(211)
    record["expected_facts"][0]["reference_aliases"] = aliases
    contract = _write_contract(tmp_path / "facts.json", [record])

    with pytest.raises(ValueError, match="reference_aliases"):
        load_factual_contract_records(contract)


def test_reference_aliases_are_reviewed_per_fact_and_never_globally_collapsed():
    expected = dict(FACT)
    actual = {
        **FACT,
        "reference": "VI.1",
    }

    assert not fact_matches(expected, actual)
    assert fact_matches(
        {**expected, "reference_aliases": ["VI.1"]},
        actual,
    )
    assert not fact_matches(
        {**expected, "reference_aliases": ["VI.2"]},
        actual,
    )
    # A malformed direct caller cannot turn a string into a permissive alias
    # iterable; official contracts reject this shape at load time as well.
    assert not fact_matches(
        {**expected, "reference_aliases": "VI.1"},
        actual,
    )


def _annual_flow_fact(
    *,
    value: str,
    label: str,
    time_hint: str,
    period_role: str,
    reference: str = "VI.8",
) -> dict:
    return {
        "company": "Công ty APEC",
        "item_name": f"Thu nhập khác | {label}",
        "metric_label": "Thu nhập khác",
        "time_hint": time_hint,
        "period_label": label,
        "column_label": label,
        "period_role": period_role,
        "fiscal_year": "2024",
        "value": value,
        "unit": "VND",
        "reference": reference,
    }


def test_typed_cumulative_period_candidates_use_fiscal_year_for_both_legs():
    current = _annual_flow_fact(
        value="5.930.566.560",
        label="Lũy kế đến quý IV năm 2024",
        time_hint="cuối",
        period_role="current",
    )
    previous = _annual_flow_fact(
        value="4.296.727.076",
        label="Lũy kế đến quý IV năm 2023",
        time_hint="đầu",
        period_role="previous",
    )
    actual_facts = facts_from_mappings([current, previous])
    record = {
        "id": 245,
        "question": "So sánh thu nhập khác năm hiện tại và năm trước.",
        "expected_facts": [
            {
                "entity": "Công ty APEC",
                "metric": "Thu nhập khác",
                "period": "năm hiện tại",
                "value": "5.930.566.560",
                "unit": "VND",
                "reference": "V.8",
                "reference_aliases": ["VI.8"],
            },
            {
                "entity": "Công ty APEC",
                "metric": "Thu nhập khác",
                "period": "năm trước",
                "value": "4.296.727.076",
                "unit": "VND",
                "reference": "V.8",
                "reference_aliases": ["VI.8"],
            },
        ],
    }

    row = score_factual_record(record, actual_facts=actual_facts)

    assert row["matched_n"] == 2
    assert row["recall"] == 1.0


def test_roman_quarter_is_parsed_but_not_promoted_to_full_year():
    quarterly = _annual_flow_fact(
        value="3.544.792.512",
        label="Quý IV năm 2024",
        time_hint="cuối",
        period_role="current",
    )
    actual = facts_from_mappings([quarterly])
    expected_current_year = {
        "entity": "Công ty APEC",
        "metric": "Thu nhập khác",
        "period": "năm hiện tại",
        "value": "3.544.792.512",
        "unit": "VND",
        "reference": "VI.8",
    }

    assert normalize_period("Quý IV năm 2024") == "2024-q4"
    assert any("2024-q4" in fact["_raw_periods"] for fact in actual)
    assert not any(fact_matches(expected_current_year, fact) for fact in actual)


def test_bare_current_period_role_does_not_relabel_balance_sheet_ending():
    balance = {
        "company": "Công ty APEC",
        "item_name": "Lợi nhuận sau thuế chưa phân phối | Số cuối năm",
        "time_hint": "cuối",
        "period_label": "Số cuối năm",
        "column_label": "Số cuối năm",
        "period_role": "current",
        "fiscal_year": "2024",
        "value": "43.404.961.299",
        "unit": "VND",
        "reference": "BẢNG CÂN ĐỐI KẾ TOÁN",
    }
    actual = facts_from_mappings([balance])
    expected = {
        "entity": "Công ty APEC",
        "metric": "Lợi nhuận sau thuế chưa phân phối",
        "period": "cuối kỳ",
        "value": "43.404.961.299",
        "unit": "VND",
        "reference": "V.19a",
        "reference_aliases": ["BẢNG CÂN ĐỐI KẾ TOÁN"],
    }

    assert any(fact_matches(expected, fact) for fact in actual)
    assert not any("current_year" in fact["_raw_periods"] for fact in actual)


def test_worktree_provenance_distinguishes_dirty_code_from_head(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    source = tmp_path / "gate.py"
    source.write_text("THRESHOLD = 0.95\n", encoding="utf-8")
    subprocess.run(["git", "add", "gate.py"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=AgentFinX Test",
            "-c",
            "user.email=agentfinx-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )

    clean = git_worktree_provenance(tmp_path)
    source.write_text("THRESHOLD = 0.96\n", encoding="utf-8")
    dirty = git_worktree_provenance(tmp_path)

    assert clean["worktree_dirty"] is False
    assert dirty["worktree_dirty"] is True
    assert dirty["git_revision"] == clean["git_revision"]
    assert dirty["worktree_diff_sha256"] != clean["worktree_diff_sha256"]
    assert dirty["code_sha256"] != clean["code_sha256"]


def test_worktree_provenance_hashes_untracked_source_but_ignores_runtime_outputs(
    tmp_path: Path,
):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    baseline = tmp_path / "baseline.py"
    baseline.write_text("ENABLED = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "baseline.py"], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=AgentFinX Test",
            "-c",
            "user.email=agentfinx-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "initial",
        ],
        cwd=tmp_path,
        check=True,
    )

    clean = git_worktree_provenance(tmp_path)
    runtime_dir = tmp_path / "ragas_runs"
    runtime_dir.mkdir()
    (runtime_dir / "checkpoint.json").write_text(
        '{"run_complete": false}',
        encoding="utf-8",
    )
    with_runtime_output = git_worktree_provenance(tmp_path)

    assert with_runtime_output["repository_dirty"] is True
    assert with_runtime_output["worktree_dirty"] is False
    assert with_runtime_output["code_sha256"] == clean["code_sha256"]
    assert with_runtime_output["excluded_untracked_files_n"] == 1

    behavior = tmp_path / "new_behavior.py"
    behavior.write_text("THRESHOLD = 0.96\n", encoding="utf-8")
    first_behavior = git_worktree_provenance(tmp_path)
    behavior.write_text("THRESHOLD = 0.97\n", encoding="utf-8")
    second_behavior = git_worktree_provenance(tmp_path)

    assert first_behavior["worktree_dirty"] is True
    assert first_behavior["relevant_untracked_files_n"] == 1
    assert first_behavior["code_sha256"] != clean["code_sha256"]
    assert second_behavior["code_sha256"] != first_behavior["code_sha256"]
