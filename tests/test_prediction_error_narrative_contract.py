"""Reviewed evaluator contracts for analytical narrative answers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents import planner_runner, synth_runner
from analyze_prediction_errors import (
    analyze,
    classify,
    evaluate_reviewed_hard_error_gate,
    gold_atoms_for_prediction,
    load_gold_atom_contract,
)
from evaluation.narrative_semantics import split_grounded_answer_sections


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "prediction_error_narrative_contract.json"
)
QUESTION = (
    "Ý nghĩa của việc Công ty chuyển đổi từ doanh nghiệp nhà nước sang "
    "công ty cổ phần niêm yết đối với quản trị doanh nghiệp là gì?"
)
GROUND_TRUTH = (
    "Công ty chuyển từ doanh nghiệp nhà nước sang công ty cổ phần rồi "
    "niêm yết. Việc này đặt quản trị trong quan hệ trách nhiệm với cổ đông "
    "và thị trường; báo cáo không trực tiếp mô tả mọi hệ quả."
)
HISTORY_CONTEXT = (
    "Năm 1993 Công ty hoạt động theo loại hình Doanh nghiệp Nhà Nước. "
    "Năm 2003 Công ty được cổ phần hóa và đăng ký trở thành công ty cổ phần. "
    "Năm 2006 cổ phiếu được niêm yết trên Sở Giao dịch Chứng khoán."
)
CORRECT_ANSWER = (
    "Báo cáo xác nhận Công ty chuyển từ doanh nghiệp nhà nước sang công ty "
    "cổ phần và sau đó niêm yết. Có thể hiểu thận trọng rằng cơ chế quản trị "
    "mở rộng trách nhiệm giải trình với cổ đông và thị trường; báo cáo không "
    "trực tiếp mô tả các hệ quả chi tiết."
)


@pytest.fixture(scope="module")
def narrative_entry():
    contract = load_gold_atom_contract(FIXTURE)
    assert contract is not None
    assert contract.schema_version == 2
    assert len(contract.entries) == 1
    return contract.entries[0]


def _prediction(narrative_entry, answer: str, *, contexts=None):
    return {
        "question": QUESTION,
        "ground_truth": GROUND_TRUTH,
        "answer": answer,
        "retrieved_contexts": contexts if contexts is not None else [HISTORY_CONTEXT],
        "narrative_contract": narrative_entry["narrative_contract"],
        "benchmark_issues": narrative_entry["benchmark_issues"],
    }


@pytest.mark.parametrize(
    ("question", "ground_truth", "answer", "atoms", "extra"),
    [
        (
            "Tổng tài sản thay đổi bao nhiêu?",
            "Tổng tài sản giảm 100 VND.",
            (
                "Chưa thể thực hiện phép tính vì evidence plan chưa khai báo "
                "typed operand contract cho kỳ hiện tại và kỳ trước."
            ),
            [{"kind": "amount", "value": 100, "unit": "VND"}],
            {},
        ),
        (
            "Tính hệ số nợ trên vốn chủ sở hữu.",
            "Hệ số là 0,57 lần.",
            (
                "Chưa thể thực hiện phép tính vì evidence plan chưa khai báo "
                "typed operand contract cho các slot bắt buộc: tử số, mẫu số."
            ),
            [{"kind": "multiple", "value": "0.57"}],
            {},
        ),
    ],
)
def test_deterministic_calculation_abstentions_are_exact_wrong_refusals(
    question,
    ground_truth,
    answer,
    atoms,
    extra,
):
    result = classify(
        {
            "question": question,
            "ground_truth": ground_truth,
            "answer": answer,
            "gold_atoms": atoms,
            **extra,
        },
        None,
    )

    assert result["label"] == "WRONG_REFUSAL"
    assert result["verified_correct"] is False


def test_structured_calculation_abstention_is_wrong_refusal_without_marker_text():
    result = classify(
        {
            "question": "Tính hệ số nợ trên vốn chủ sở hữu.",
            "ground_truth": "Hệ số là 0,57 lần.",
            "answer": "Các toán hạng bắt buộc chưa được bind.",
            "gold_atoms": [{"kind": "multiple", "value": "0.57"}],
            "synth_status": "abstain",
            "synth_reason_code": "unbound_required_operands",
        },
        None,
    )

    assert result["label"] == "WRONG_REFUSAL"
    assert result["verified_correct"] is False


def test_reviewed_narrative_contract_accepts_grounded_bounded_inference(
    narrative_entry,
):
    result = classify(_prediction(narrative_entry, CORRECT_ANSWER), None)

    assert result["label"] == "OK"
    assert result["verified_correct"] is True
    assert result["evidence"]["contract_source"] == "explicit_narrative"
    assert result["evidence"]["anchors_complete"] is True
    assert result["benchmark_issue"] == "GT_PARTIALLY_UNSUPPORTED"


def test_narrative_refusal_with_complete_anchors_is_synthesis_miss(
    narrative_entry,
):
    result = classify(
        _prediction(
            narrative_entry,
            "Không có thông tin về ý nghĩa của quá trình này trong dữ liệu hiện có.",
        ),
        None,
    )

    assert result["label"] == "WRONG_REFUSAL"
    assert result["root_cause"] == "SYNTHESIS_MISS"


def test_narrative_chronology_without_requested_implication_is_incomplete(
    narrative_entry,
):
    result = classify(
        _prediction(
            narrative_entry,
            (
                "Công ty chuyển từ doanh nghiệp nhà nước sang công ty cổ phần "
                "và sau đó niêm yết."
            ),
        ),
        None,
    )

    assert result["label"] == "WRONG_OVERLAP"
    assert result["root_cause"] == "SYNTHESIS_MISS"
    assert result["evidence"]["missing_required_answer"] == [
        "governance_accountability"
    ]


def test_optional_claim_is_validated_when_present_and_needs_support(
    narrative_entry,
):
    unsupported = classify(
        _prediction(
            narrative_entry,
            CORRECT_ANSWER + " Việc niêm yết làm tăng minh bạch và công bố thông tin.",
        ),
        None,
    )
    supported = classify(
        _prediction(
            narrative_entry,
            CORRECT_ANSWER + " Việc niêm yết làm tăng minh bạch và công bố thông tin.",
            contexts=[
                HISTORY_CONTEXT,
                "Quy định áp dụng nêu nghĩa vụ công bố thông tin của công ty niêm yết.",
            ],
        ),
        None,
    )

    assert unsupported["label"] == "WRONG_OVERLAP"
    assert unsupported["evidence"]["unsupported_propositions"] == [
        "transparency_and_disclosure"
    ]
    assert supported["label"] == "OK"


def test_prohibited_narrative_claim_is_a_hard_exact_label(narrative_entry):
    result = classify(
        _prediction(
            narrative_entry,
            CORRECT_ANSWER + " Tuy vậy, Công ty vẫn là doanh nghiệp nhà nước.",
        ),
        None,
    )

    assert result["label"] == "WRONG_OVERLAP"
    assert result["evidence"]["prohibited_propositions"] == ["still_state_owned"]


def test_required_narrative_evidence_is_not_replaced_by_generic_boilerplate(
    narrative_entry,
):
    result = classify(
        _prediction(
            narrative_entry,
            CORRECT_ANSWER,
            contexts=["Quản trị tốt thường nâng cao trách nhiệm giải trình."],
        ),
        None,
    )

    assert result["label"] == "WRONG_OVERLAP"
    assert result["root_cause"] == "RETRIEVAL_MISS"
    assert set(result["evidence"]["missing_required_evidence"]) == {
        "legal_form_transition",
        "public_listing",
    }


def test_listing_valuation_policy_cannot_satisfy_listing_history_anchor(
    narrative_entry,
):
    result = classify(
        _prediction(
            narrative_entry,
            CORRECT_ANSWER,
            contexts=[
                (
                    "Năm 1993 Công ty hoạt động theo loại hình Doanh nghiệp "
                    "Nhà Nước. Năm 2003 Công ty được cổ phần hóa và đăng ký "
                    "trở thành công ty cổ phần."
                ),
                (
                    "Đối với chứng khoán niêm yết trên Sở Giao dịch Chứng "
                    "khoán, giá đóng cửa được dùng để xác định giá trị hợp lý "
                    "và dự phòng."
                ),
            ],
        ),
        None,
    )

    assert result["label"] == "WRONG_OVERLAP"
    assert result["root_cause"] == "RETRIEVAL_MISS"
    assert result["evidence"]["missing_required_evidence"] == [
        "public_listing"
    ]
    assert result["evidence"]["anchors_complete"] is False


def test_structured_route_placeholder_cannot_supply_narrative_anchors(
    narrative_entry,
):
    result = classify(
        _prediction(
            narrative_entry,
            CORRECT_ANSWER,
            contexts=[
                (
                    "Table: PHẦN ĐẦU BÁO CÁO TÀI CHÍNH\n"
                    f"Item: {HISTORY_CONTEXT}\n"
                    "Source: report.md#page=2"
                )
            ],
        ),
        None,
    )

    assert result["label"] == "WRONG_OVERLAP"
    assert result["root_cause"] == "RETRIEVAL_MISS"
    assert set(result["evidence"]["missing_required_evidence"]) == {
        "legal_form_transition",
        "public_listing",
    }


def test_grounded_explicit_anchor_must_be_in_fact_section(narrative_entry):
    answer = (
        "Dữ liệu trích xuất: Công ty chuyển từ doanh nghiệp nhà "
        "nước sang công ty cổ phần. Đối với chứng khoán niêm yết, giá đóng "
        "cửa được dùng để xác định giá trị hợp lý. "
        "Suy luận đánh giá: Công ty sau đó niêm yết; từ quá trình này có "
        "thể suy ra quản trị mở rộng trách nhiệm giải trình với cổ đông."
    )

    result = classify(_prediction(narrative_entry, answer), None)

    assert result["label"] == "WRONG_OVERLAP"
    assert result["root_cause"] == "SYNTHESIS_MISS"
    assert result["evidence"]["missing_required_answer"] == [
        "public_listing"
    ]
    public_listing = next(
        item
        for item in result["evidence"]["expected"]
        if item["name"] == "public_listing"
    )
    assert public_listing["answer_section"] == "fact"
    assert public_listing["answer_match"] is False


def test_grounded_sections_accept_fact_anchors_and_bounded_inference(
    narrative_entry,
):
    answer = (
        "Dữ liệu trích xuất: Công ty chuyển từ doanh nghiệp nhà "
        "nước sang công ty cổ phần; cổ phiếu của Công ty sau đó được niêm "
        "yết trên Sở Giao dịch Chứng khoán năm 2006. "
        "Suy luận đánh giá: Từ các dữ kiện này có thể suy ra quản trị mở "
        "rộng trách nhiệm giải trình với cổ đông và thị trường."
    )

    result = classify(_prediction(narrative_entry, answer), None)

    assert result["label"] == "OK"
    assert result["evidence"]["anchors_complete"] is True
    assert "Giới hạn bằng chứng" not in answer


def test_evaluator_only_parses_canonical_grounded_section_headings():
    canonical = split_grounded_answer_sections(
        "Dữ liệu trích xuất: Công ty đã niêm yết. "
        "Suy luận đánh giá: Quản trị có thể chịu giám sát rộng hơn."
    )
    previous = split_grounded_answer_sections(
        "Fact được báo cáo xác nhận: Công ty đã niêm yết. "
        "Suy luận dựa trên fact: Quản trị có thể chịu giám sát rộng hơn."
    )

    assert canonical == {
        "fact": "cong ty da niem yet.",
        "inference": "quan tri co the chiu giam sat rong hon.",
    }
    assert previous is None


def test_narrative_fixture_binds_by_question_and_marks_benchmark_issue(
    tmp_path,
):
    report_path = tmp_path / "predictions.json"
    report_path.write_text(
        json.dumps(
            {
                "metadata": {"dataset": {"dataset_id": "suavietnam"}},
                "predictions": [
                    {
                        "id": 7200,
                        "question": QUESTION,
                        "ground_truth": GROUND_TRUTH,
                        "answer": (
                            "Không có thông tin về ý nghĩa của quá trình này "
                            "trong dữ liệu hiện có."
                        ),
                        "retrieved_contexts": [HISTORY_CONTEXT],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = analyze(
        report_path,
        gold_contract=load_gold_atom_contract(FIXTURE),
    )
    row = summary["rows"][0]

    assert row["label"] == "WRONG_REFUSAL"
    assert row["benchmark_issue"] == "GT_PARTIALLY_UNSUPPORTED"
    assert summary["benchmark_issue_counts"] == {
        "GT_PARTIALLY_UNSUPPORTED": 1
    }
    assert summary["gold_atom_contract"]["matched_predictions_n"] == 1


def test_unreviewed_analytical_narrative_does_not_derive_year_as_amount():
    atoms, source = gold_atoms_for_prediction(
        {
            "question": QUESTION,
            "ground_truth": (
                "Theo quyết định năm 1993, doanh nghiệp chuyển đổi và sau đó "
                "niêm yết; điều này thay đổi khuôn khổ quản trị."
            ),
        }
    )

    assert atoms == []
    assert source == "derived_narrative"


def test_hard_error_gate_requires_exact_labels_not_only_binary_membership():
    gate = evaluate_reviewed_hard_error_gate(
        [
            {
                "name": "exact_label_confusion",
                "question": "Giá trị là bao nhiêu?",
                "ground_truth": "Giá trị là 10 VND.",
                "gold_atoms": [{"kind": "amount", "value": 10, "unit": "VND"}],
                "correct_answer": "Giá trị là 10 VND.",
                "wrong_answer": "Không có số liệu để trả lời.",
                "wrong_label": "WRONG_NUMBER",
            }
        ],
        threshold=0.95,
    )

    assert gate["precision"] == 1.0
    assert gate["recall"] == 1.0
    assert gate["exact_label_accuracy"] == 0.5
    assert gate["status"] == "fail"
