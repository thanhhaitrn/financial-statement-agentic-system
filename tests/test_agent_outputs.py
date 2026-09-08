"""Regression tests for test agent outputs."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from schemas.agent_outputs import EvidencePlanItem, parse_worker_response_payload


def test_parse_worker_response_payload_allows_missing_fact_source():
    parsed = parse_worker_response_payload(
        {
            "kind": "answer",
            "table": "BẢNG CÂN ĐỐI KẾ TOÁN",
            "facts": [
                {
                    "item_name": "Tổng cộng tài sản",
                    "value": "81.793.076.515.644",
                }
            ],
        }
    )

    payload = parsed.model_dump()

    assert payload["facts"][0]["item_name"] == "Tổng cộng tài sản"
    assert payload["facts"][0]["value"] == "81.793.076.515.644"
    assert payload["facts"][0]["source"] == ""
    assert payload["facts"][0]["status"] == "found"


def test_parse_worker_response_payload_allows_missing_answer_table():
    parsed = parse_worker_response_payload(
        {
            "kind": "answer",
            "facts": [
                {
                    "item_name": "Lợi nhuận sau thuế thu nhập doanh nghiệp",
                    "value": "3.318.000.000.000",
                }
            ],
        }
    )

    payload = parsed.model_dump()

    assert payload["table"] == ""
    assert payload["facts"][0]["item_name"] == "Lợi nhuận sau thuế thu nhập doanh nghiệp"
    assert payload["facts"][0]["source"] == ""


def test_parse_worker_response_payload_preserves_fact_status_values():
    parsed = parse_worker_response_payload(
        {
            "kind": "answer",
            "facts": [
                {
                    "item_name": "Chi phí bán hàng",
                    "value": "",
                    "status": "not_found_after_search",
                    "interpretation_hint": "Không tìm thấy dòng chi phí bán hàng.",
                },
                {
                    "item_name": "Doanh thu",
                    "value": "100 hoặc 101",
                    "status": "ambiguous",
                },
            ],
        }
    )

    payload = parsed.model_dump()

    assert payload["facts"][0]["status"] == "not_found_after_search"
    assert payload["facts"][1]["status"] == "ambiguous"


def test_worker_fact_preserves_canonical_semantic_dimensions():
    parsed = parse_worker_response_payload(
        {
            "kind": "answer",
            "facts": [
                {
                    "item_name": "Mua hàng hóa | Công ty A",
                    "value": "100",
                    "metric_label": "Mua hàng hóa",
                    "entity_label": "Công ty A",
                    "scope_label": "Giao dịch với bên liên quan",
                    "counterparty": "Công ty A",
                    "transaction_type": "purchase",
                    "movement_type": "",
                    "geography": "",
                    "policy_topic": "",
                }
            ],
        }
    )

    fact = parsed.model_dump()["facts"][0]
    assert fact["counterparty"] == "Công ty A"
    assert fact["transaction_type"] == "purchase"
    assert fact["metric_label"] == "Mua hàng hóa"


def test_evidence_plan_preserves_typed_calculation_operands():
    item = EvidencePlanItem.model_validate(
        {
            "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
            "query": "cam kết thuê tăng bao nhiêu lần",
            "operation": "multiple",
            "operands": [
                {
                    "role": "current",
                    "metric": "cam kết thuê",
                    "entity": "Nhà máy Alpha",
                    "period": "cuối",
                    "scope_label": "Cam kết thuê hoạt động",
                    "counterparty": "Bên cho thuê Alpha",
                    "transaction_type": "lease",
                    "movement_type": "closing_balance",
                    "geography": "Miền Bắc",
                    "policy_topic": "lease_commitment",
                },
                {
                    "role": "previous",
                    "metric": "cam kết thuê",
                    "period": "đầu",
                },
            ],
        }
    )

    payload = item.model_dump(exclude_none=True)
    assert payload["operation"] == "multiple"
    assert [operand["role"] for operand in payload["operands"]] == [
        "current",
        "previous",
    ]
    assert payload["operands"][0] == {
        "role": "current",
        "query": "",
        "metric": "cam kết thuê",
            "entity": "Nhà máy Alpha",
            "period": "cuối",
            "period_role": "",
            "reporting_basis": "",
            "period_label": "",
            "value_type": "",
        "aggregation_level": "",
        "scope_label": "Cam kết thuê hoạt động",
        "counterparty": "Bên cho thuê Alpha",
        "transaction_type": "lease",
        "movement_type": "closing_balance",
        "geography": "Miền Bắc",
            "policy_topic": "lease_commitment",
            "section_key": "",
        }
