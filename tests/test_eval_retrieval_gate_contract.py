import json
import subprocess
from pathlib import Path

import pytest

from eval_retrieval_recall import (
    evaluate_factual_recall,
    git_worktree_provenance,
    load_factual_contract_records,
    matched_official_gate_records,
    parse_args,
    prepare_official_gate_records,
    select_contract_records,
)


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
