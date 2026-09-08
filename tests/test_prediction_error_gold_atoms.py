"""Contract tests for typed deterministic prediction-error triage."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from analyze_prediction_errors import (
    _is_refusal,
    analyze,
    classify,
    evaluate_reviewed_hard_error_gate,
    gold_atoms_for_prediction,
    is_verified_recovery,
    load_adjudications,
    load_gold_atom_contract,
    parse_args,
)
from dataset_batch_result import load_seed_records, run_predictions
from evaluation.contracts import provider_limit_reason


CONTRACT_PATH = Path(__file__).parent / "fixtures" / "prediction_error_gold_atom_contract.json"


def _prediction(case: dict, answer_key: str, *, contexts: list[str] | None = None) -> dict:
    return {
        "question": case["question"],
        "ground_truth": case["ground_truth"],
        "answer": case[answer_key],
        "gold_atoms": case["gold_atoms"],
        "retrieved_contexts": contexts or [],
    }


@pytest.fixture(scope="module")
def gold_atom_cases() -> list[dict]:
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert {case["source_family"] for case in payload["cases"]} == {"VNM", "APEC"}
    return payload["cases"]


def test_reviewed_contract_accepts_only_answers_covering_all_atoms(gold_atom_cases):
    for case in gold_atom_cases:
        correct = classify(_prediction(case, "correct_answer"), None)
        wrong = classify(_prediction(case, "wrong_answer"), None)

        assert correct["label"] == "OK", case["name"]
        assert correct["verified_correct"] is True, case["name"]
        assert correct["evidence"]["answer_coverage"] == 1.0, case["name"]
        assert correct["evidence"]["contract_source"] == "explicit", case["name"]

        assert wrong["label"] == case["wrong_label"], case["name"]
        assert wrong["verified_correct"] is False, case["name"]
        if wrong["label"] == "ERROR":
            assert wrong["root_cause"] == "INFRA_ERROR", case["name"]
        else:
            assert wrong["evidence"]["answer_coverage"] < 1.0, case["name"]


def test_reviewed_contract_hard_error_precision_and_recall_gate(gold_atom_cases):
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    gate_config = payload["hard_error_gate"]
    threshold = max(
        gate_config["min_precision"],
        gate_config["min_recall"],
    )

    gate = evaluate_reviewed_hard_error_gate(
        gold_atom_cases,
        threshold=threshold,
    )

    assert gate["status"] == "pass", gate["mismatches"]
    assert gate["precision"] >= gate_config["min_precision"]
    assert gate["recall"] >= gate_config["min_recall"]
    assert gate["expected_hard_errors_n"] == len(gold_atom_cases)
    assert gate["expected_ok_n"] == len(gold_atom_cases)


def test_reviewed_operand_roles_reject_swapped_values(gold_atom_cases):
    case = next(
        case
        for case in gold_atom_cases
        if case["name"] == "apec_ratio_operand_roles"
    )

    correct = classify(_prediction(case, "correct_answer"), None)
    swapped = classify(_prediction(case, "wrong_answer"), None)

    assert correct["label"] == "OK"
    assert correct["evidence"]["answer_matches"] == [True, True, True]
    assert swapped["label"] == "WRONG_NUMBER"
    assert swapped["verified_correct"] is False
    assert swapped["evidence"]["answer_matches"] == [False, False, True]


def test_typed_context_coverage_drives_root_cause(gold_atom_cases):
    case = next(case for case in gold_atom_cases if case["name"] == "apec_factory_count")
    synthesis_miss = classify(
        _prediction(case, "wrong_answer", contexts=[case["correct_answer"]]),
        None,
    )
    retrieval_miss = classify(
        _prediction(case, "wrong_answer", contexts=["Doanh nghiệp có các cơ sở sản xuất."]),
        None,
    )

    assert synthesis_miss["root_cause"] == "SYNTHESIS_MISS"
    assert synthesis_miss["evidence"]["context_matches"] == [True]
    assert retrieval_miss["root_cause"] == "RETRIEVAL_MISS"
    assert retrieval_miss["evidence"]["context_matches"] == [False]


def test_partial_contract_labels_the_kind_of_the_missing_atom(gold_atom_cases):
    case = next(
        case
        for case in gold_atom_cases
        if case["name"] == "vnm_audit_report_identifier_and_date"
    )
    result = classify(
        {
            **_prediction(case, "correct_answer"),
            "answer": "Số báo cáo là 149/2025/BCKT-AASC, ký ngày 27-03-2025.",
        },
        None,
    )

    assert result["evidence"]["answer_matches"] == [True, False]
    assert result["label"] == "WRONG_OVERLAP"
    assert result["verified_correct"] is False


def test_zero_balance_is_a_factual_zero_not_a_refusal(gold_atom_cases):
    case = next(case for case in gold_atom_cases if case["name"] == "vnm_zero_balance")
    assert _is_refusal(case["ground_truth"]) is False

    atoms, source = gold_atoms_for_prediction(
        {"ground_truth": "Khoản phải thu không còn số dư cuối năm."}
    )
    assert source == "derived"
    assert [atom.as_dict() for atom in atoms] == [{"kind": "amount", "value": "0"}]

    result = classify(_prediction(case, "correct_answer"), None)
    assert result["label"] == "OK"
    assert result["verified_correct"] is True


def test_missing_data_language_remains_a_refusal():
    prediction = {
        "question": "Số dư cuối năm là bao nhiêu?",
        "ground_truth": "Số dư cuối năm là 0 đồng.",
        "answer": "Không có số liệu về số dư cuối năm.",
        "gold_atoms": [{"kind": "amount", "value": 0, "unit": "VND"}],
    }

    assert _is_refusal(prediction["answer"]) is True
    assert classify(prediction, None)["label"] == "WRONG_REFUSAL"


def test_auxiliary_missing_data_caveat_does_not_override_complete_requested_slot():
    prediction = {
        "question": "Số báo cáo kiểm toán là gì?",
        "ground_truth": "Số báo cáo kiểm toán là 25-01-00430-26-1.",
        "answer": (
            "Số báo cáo kiểm toán là 25-01-00430-26-1; "
            "không có thông tin về số điện thoại trong dữ liệu hiện có."
        ),
        "gold_atoms": [
            {"kind": "identifier", "value": "25-01-00430-26-1"}
        ],
    }

    result = classify(prediction, None)

    assert _is_refusal(prediction["answer"]) is True
    assert result["label"] == "OK"
    assert result["verified_correct"] is True


@pytest.mark.parametrize(
    "answer",
    [
        "Không có số dư vay dài hạn trong dữ liệu hiện có.",
        "Không có số chi phí lãi vay được trình bày trong báo cáo.",
    ],
)
def test_epistemic_absence_is_not_misread_as_a_factual_zero(answer):
    prediction = {
        "question": "Số dư hoặc chi phí là bao nhiêu?",
        "ground_truth": "Giá trị là 10 triệu đồng.",
        "answer": answer,
        "gold_atoms": [
            {"kind": "amount", "value": 10, "unit": "triệu đồng"}
        ],
    }

    result = classify(prediction, None)

    assert _is_refusal(answer) is True
    assert result["label"] == "WRONG_REFUSAL"
    assert result["verified_correct"] is False


def test_only_context_for_each_missing_required_atom_can_attribute_synthesis():
    prediction = {
        "question": "Số và ngày báo cáo là gì?",
        "ground_truth": "Báo cáo số 149/2025/BCKT-AASC ngày 28/03/2025.",
        "answer": "Báo cáo số 149/2025/BCKT-AASC ngày 27/03/2025.",
        "gold_atoms": [
            {"kind": "identifier", "value": "149/2025/BCKT-AASC"},
            {"kind": "date", "value": "28/03/2025"},
        ],
        "retrieved_contexts": ["Số báo cáo 149/2025/BCKT-AASC."],
    }

    retrieval_miss = classify(prediction, None)
    synthesis_miss = classify(
        {
            **prediction,
            "retrieved_contexts": [
                "Số báo cáo 149/2025/BCKT-AASC.",
                "Ngày báo cáo 28/03/2025.",
            ],
        },
        None,
    )

    assert retrieval_miss["label"] == "WRONG_OVERLAP"
    assert retrieval_miss["root_cause"] == "RETRIEVAL_MISS"
    assert synthesis_miss["root_cause"] == "SYNTHESIS_MISS"


def test_optional_atoms_are_reported_but_do_not_block_verified_correctness():
    result = classify(
        {
            "question": "Đơn vị kiểm toán là ai?",
            "ground_truth": "KPMG Việt Nam ký ngày 28/03/2025.",
            "answer": "Đơn vị kiểm toán là KPMG Việt Nam.",
            "gold_atoms": [
                {"kind": "entity", "value": "KPMG Việt Nam"},
                {
                    "kind": "date",
                    "value": "28/03/2025",
                    "required": False,
                    "role": "supporting_date",
                },
            ],
        },
        None,
    )

    assert result["label"] == "OK"
    assert result["verified_correct"] is True
    assert result["evidence"]["answer_coverage"] == 1.0
    assert result["evidence"]["all_answer_coverage"] == 0.5
    assert result["evidence"]["optional_atoms_n"] == 1


def test_accounting_magnitude_policy_is_explicit_and_exact_is_default():
    base = {
        "question": "Tiền đã chi là bao nhiêu?",
        "ground_truth": "Tiền chi là (100) triệu đồng.",
        "answer": "Tiền đã chi là 100 triệu đồng.",
    }
    exact = classify(
        {
            **base,
            "gold_atoms": [
                {"kind": "amount", "value": -100, "unit": "triệu đồng"}
            ],
        },
        None,
    )
    accounting = classify(
        {
            **base,
            "gold_atoms": [
                {
                    "kind": "amount",
                    "value": -100,
                    "unit": "triệu đồng",
                    "sign_policy": "accounting_magnitude",
                }
            ],
        },
        None,
    )

    assert exact["label"] == "WRONG_NUMBER"
    assert accounting["label"] == "OK"
    assert accounting["verified_correct"] is True


def test_provider_limit_text_is_an_infra_error_not_a_model_answer():
    result = classify(
        {
            "question": "Doanh thu là bao nhiêu?",
            "ground_truth": "Doanh thu là 100 triệu đồng.",
            "answer": "ResponseError: reached your session usage limit (status code: 429)",
            "gold_atoms": [
                {"kind": "amount", "value": 100, "unit": "triệu đồng"}
            ],
        },
        None,
    )

    assert result["label"] == "ERROR"
    assert result["root_cause"] == "INFRA_ERROR"


@pytest.mark.parametrize(
    "message",
    [
        "You've reached the maximum number of follow-up questions.",
        "The conversation session limit has been reached.",
        "RateLimitError: API rate limit exceeded for model gpt-oss.",
        "ResourceExhausted: provider quota exhausted for the inference model.",
        "metric_error (context_recall): provider quota",
        "Hệ thống đã đạt giới hạn follow-up cho phiên này.",
        "Đã hết lượt hỏi tiếp trong phiên hiện tại.",
        "Phiên đã vượt giới hạn sử dụng.",
    ],
)
def test_session_and_follow_up_limit_variants_are_provider_failures(message):
    assert provider_limit_reason(message)

    result = classify(
        {
            "question": "Doanh thu là bao nhiêu?",
            "ground_truth": "Doanh thu là 100 triệu đồng.",
            "answer": message,
            "gold_atoms": [
                {"kind": "amount", "value": 100, "unit": "triệu đồng"}
            ],
        },
        None,
    )

    assert result["label"] == "ERROR"
    assert result["root_cause"] == "INFRA_ERROR"


@pytest.mark.parametrize(
    "message",
    [
        "Dữ liệu chỉ giới hạn ở năm 2025; doanh thu là 100 triệu đồng.",
        "Không đủ dữ liệu để tính doanh thu.",
        "The available financial data is limited to fiscal year 2025.",
        "The company acquired an import quota of 100 tonnes.",
        "The loan agreement sets an interest rate limit of 10%.",
        "The interest_rate_limit field is 10%.",
        "The annual sales quota is VND 100 billion.",
        "Không có số liệu doanh thu trong báo cáo.",
    ],
)
def test_data_scope_or_missing_data_is_not_a_provider_limit(message):
    assert provider_limit_reason(message) == ""


@pytest.mark.parametrize(
    ("answer", "gold_atom"),
    [
        (
            "The company acquired an import quota of 100 tonnes.",
            {"kind": "amount", "value": 100},
        ),
        (
            "The loan agreement sets an interest rate limit of 10%.",
            {"kind": "percent", "value": 10},
        ),
    ],
)
def test_financial_quota_and_rate_limit_answers_are_not_infra_errors(
    answer,
    gold_atom,
):
    result = classify(
        {
            "question": "What limit did the agreement or transaction specify?",
            "ground_truth": answer,
            "answer": answer,
            "gold_atoms": [gold_atom],
        },
        None,
    )

    assert result["label"] == "OK"
    assert result["root_cause"] is None


def test_legacy_derivation_keeps_year_and_numeric_registration_id_typed():
    atoms, source = gold_atoms_for_prediction(
        {
            "ground_truth": (
                "Giấy đăng ký số 0300588569 được cấp ngày 13 tháng 8 năm 2025."
            )
        }
    )

    assert source == "derived"
    assert [atom.as_dict() for atom in atoms] == [
        {"kind": "date", "value": "2025-08-13"},
        {"kind": "identifier", "value": "0300588569"},
    ]


def test_refusal_flip_to_wrong_number_is_not_a_verified_recovery(gold_atom_cases):
    case = next(case for case in gold_atom_cases if case["name"] == "apec_factory_count")
    before = classify(
        {
            **_prediction(case, "correct_answer"),
            "answer": "Không có thông tin để trả lời.",
        },
        None,
    )
    wrong_numeric_after = classify(_prediction(case, "wrong_answer"), None)
    correct_after = classify(_prediction(case, "correct_answer"), None)

    assert before["label"] == "WRONG_REFUSAL"
    assert is_verified_recovery(before, wrong_numeric_after) is False
    assert is_verified_recovery(before, correct_after) is True


def test_reviewed_amount_unit_cannot_match_an_untyped_count():
    result = classify(
        {
            "question": "Giá trị khoản mục là bao nhiêu?",
            "ground_truth": "Giá trị khoản mục là 3 triệu đồng.",
            "answer": "Doanh nghiệp có 3 nhà máy.",
            "gold_atoms": [
                {"kind": "amount", "value": 3, "unit": "triệu đồng"}
            ],
        },
        None,
    )

    assert result["label"] == "WRONG_NUMBER"
    assert result["evidence"]["answer_matches"] == [False]


def test_invalid_explicit_contract_fails_closed():
    with pytest.raises(ValueError, match="unsupported gold atom kind"):
        gold_atoms_for_prediction(
            {
                "ground_truth": "Ba nhà máy.",
                "gold_atoms": [{"kind": "number", "value": 3}],
            }
        )

    with pytest.raises(ValueError, match="required atom"):
        gold_atoms_for_prediction(
            {
                "ground_truth": "KPMG.",
                "gold_atoms": [
                    {"kind": "entity", "value": "KPMG", "required": False}
                ],
            }
        )

    with pytest.raises(ValueError, match="sign_policy"):
        gold_atoms_for_prediction(
            {
                "ground_truth": "100.",
                "gold_atoms": [
                    {"kind": "amount", "value": 100, "sign_policy": "ignore"}
                ],
            }
        )

    with pytest.raises(ValueError, match="role and operand_role"):
        gold_atoms_for_prediction(
            {
                "ground_truth": "Tử số là 100.",
                "gold_atoms": [
                    {
                        "kind": "amount",
                        "value": 100,
                        "role": "numerator",
                        "operand_role": "denominator",
                    }
                ],
            }
        )


def test_reviewed_false_labels_are_excluded_only_from_product_baseline(tmp_path):
    report_path = tmp_path / "predictions.json"
    report_path.write_text(
        json.dumps(
            {
                "predictions": [
                    {
                        "id": 14,
                        "question": "Số lượng nhà máy?",
                        "ground_truth": "Có 3 nhà máy.",
                        "answer": "Không có thông tin.",
                        "gold_atoms": [{"kind": "count", "value": 3}],
                    },
                    {
                        "id": 15,
                        "question": "Số lượng nhà máy?",
                        "ground_truth": "Có 3 nhà máy.",
                        "answer": "Không có thông tin.",
                        "gold_atoms": [{"kind": "count", "value": 3}],
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    adjudication_path = (
        Path(__file__).parent
        / "fixtures"
        / "vnm_evaluation_adjudications.json"
    )

    summary = analyze(
        report_path,
        adjudications=load_adjudications(adjudication_path),
    )

    assert summary["label_counts"]["WRONG_REFUSAL"] == 2
    assert summary["product_error_baseline"]["label_counts"]["WRONG_REFUSAL"] == 1
    assert summary["product_error_baseline"]["excluded_ids"] == [14]


def test_seed_gold_atoms_survive_success_and_error_prediction_artifacts(
    tmp_path,
    monkeypatch,
):
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "question": "Doanh nghiệp có bao nhiêu nhà máy?",
                    "ground_truth": "Doanh nghiệp có 3 nhà máy.",
                    "contexts": ["Doanh nghiệp có ba nhà máy."],
                    "expected_atoms": [
                        {"kind": "count", "value": 3, "unit": "nhà máy"}
                    ],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    records = load_seed_records(seed_path)
    assert records[0]["gold_atoms"] == [
        {"kind": "count", "value": 3, "unit": "nhà máy"}
    ]
    assert "expected_atoms" not in records[0]

    import test as workflow_entrypoint

    monkeypatch.setattr(
        workflow_entrypoint,
        "execute_query",
        lambda *_args, **_kwargs: {
            "synth_decision": {
                "answer": "Doanh nghiệp có 3 nhà máy.",
                "status": "success",
            },
            "ragas_facts_by_table": {
                "REPORT": {
                    "facts": [
                        {
                            "fact_id": "factory-count",
                            "table": "REPORT",
                            "item_name": "Số nhà máy",
                            "value": "3",
                            "source": "report.md",
                        }
                    ]
                }
            },
        },
    )
    monkeypatch.setattr(
        workflow_entrypoint,
        "collect_pipeline_errors",
        lambda _state: [],
    )
    monkeypatch.setattr(
        workflow_entrypoint,
        "extract_run_summary",
        lambda _state: {"duration_ms": 5, "total_tokens": 7},
    )
    prepared_runtime = (
        SimpleNamespace(dataset_id="vnm"),
        None,
        None,
        {"dataset_id": "vnm"},
    )

    _, successful = run_predictions(
        dataset_id="vnm",
        records=records,
        prepared_runtime=prepared_runtime,
    )
    assert successful[0]["gold_atoms"] == records[0]["gold_atoms"]

    monkeypatch.setattr(
        workflow_entrypoint,
        "execute_query",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _, failed = run_predictions(
        dataset_id="vnm",
        records=records,
        prepared_runtime=prepared_runtime,
    )
    assert failed[0]["gold_atoms"] == records[0]["gold_atoms"]
    assert failed[0]["synth_status"] == "error"


def test_external_gold_contract_maps_legacy_report_by_exact_question(tmp_path):
    case = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))["cases"][1]
    report_path = tmp_path / "legacy_predictions.json"
    report_path.write_text(
        json.dumps(
            {
                "metadata": {"dataset": {"dataset_id": "apec"}},
                "predictions": [
                    {
                        "id": 77,
                        "question": case["question"],
                        "ground_truth": case["ground_truth"],
                        "answer": case["correct_answer"],
                        "retrieved_contexts": [case["correct_answer"]],
                    }
                ],
                "scores": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = analyze(
        report_path,
        gold_contract=load_gold_atom_contract(CONTRACT_PATH),
    )

    assert summary["rows"][0]["label"] == "OK"
    assert summary["rows"][0]["verified_correct"] is True
    assert summary["rows"][0]["evidence"]["contract_source"] == "explicit"
    assert summary["gold_atom_contract"]["matched_predictions_n"] == 1
    assert summary["gold_atom_contract"]["unmatched_predictions_n"] == 0
    args = parse_args(
        ["--gold-contract", str(CONTRACT_PATH), str(report_path)]
    )
    assert args.gold_contract == str(CONTRACT_PATH)


def test_external_gold_contract_can_map_an_id_only_record(tmp_path):
    contract_path = tmp_path / "id_contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "id": 9,
                        "gold_atoms": [
                            {"kind": "count", "value": 3, "unit": "nhà máy"}
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "id_report.json"
    report_path.write_text(
        json.dumps(
            {
                "predictions": [
                    {
                        "id": 9,
                        "question": "Một câu hỏi có thể được biên tập lại",
                        "ground_truth": "Có 3 nhà máy.",
                        "answer": "Có 3 nhà máy.",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = analyze(
        report_path,
        gold_contract=load_gold_atom_contract(contract_path),
    )

    assert summary["rows"][0]["verified_correct"] is True
    assert summary["gold_atom_contract"]["matched_predictions_n"] == 1


def test_external_gold_contract_fails_closed_on_identity_or_atom_conflict(
    tmp_path,
):
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "vnm",
                "records": [
                    {
                        "id": 1,
                        "question": "Doanh nghiệp có bao nhiêu nhà máy?",
                        "gold_atoms": [
                            {"kind": "count", "value": 3, "unit": "nhà máy"}
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    contract = load_gold_atom_contract(contract_path)
    report_path = tmp_path / "predictions.json"

    report_path.write_text(
        json.dumps(
            {
                "metadata": {"dataset": {"dataset_id": "vnm"}},
                "predictions": [
                    {
                        "id": 1,
                        "question": "Một câu hỏi khác",
                        "ground_truth": "Có 3 nhà máy.",
                        "answer": "Có 3 nhà máy.",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="question disagrees"):
        analyze(report_path, gold_contract=contract)

    report_path.write_text(
        json.dumps(
            {
                "metadata": {"dataset": {"dataset_id": "vnm"}},
                "predictions": [
                    {
                        "id": 1,
                        "question": "Doanh nghiệp có bao nhiêu nhà máy?",
                        "ground_truth": "Có 3 nhà máy.",
                        "answer": "Có 3 nhà máy.",
                        "gold_atoms": [
                            {"kind": "count", "value": 2, "unit": "nhà máy"}
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="embedded gold atoms disagree"):
        analyze(report_path, gold_contract=contract)
