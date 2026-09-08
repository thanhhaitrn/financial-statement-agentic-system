import pytest

import tools.tools as retrieval_tools
from schemas.table_names import TABLE_NOTE
from tools.query_routing import (
    fact_matches_required_slots,
    fact_slot_score,
    parse_query_slots,
    targeted_retry_query,
)
from tools.tools import get_related_info
from vectorstore.lexical_index import reset_lexical_index


class SemanticStructuredCollection:
    def __init__(self, corpus, dense_hits=()):
        self.name = "semantic-structured-contract"
        self.corpus = list(corpus)
        self.dense_hits = list(dense_hits)
        self.get_calls = []
        self.query_calls = 0

    def get(self, where=None, include=None):
        where = dict(where or {})
        self.get_calls.append(where)
        rows = [
            (doc, meta)
            for doc, meta in self.corpus
            if all(
                str(meta.get(key, "") or "") == str(value)
                for key, value in where.items()
            )
        ]
        return {
            "documents": [doc for doc, _meta in rows],
            "metadatas": [meta for _doc, meta in rows],
        }

    def query(self, query_embeddings, n_results, where=None):
        self.query_calls += 1
        return {
            "documents": [[doc for doc, _meta in self.dense_hits]],
            "metadatas": [[meta for _doc, meta in self.dense_hits]],
        }


@pytest.fixture(autouse=True)
def _deterministic_retrieval(monkeypatch):
    reset_lexical_index()
    monkeypatch.setattr(retrieval_tools, "embed_query_text", lambda _query: [0.0])
    yield
    reset_lexical_index()


def test_semantic_transaction_filter_runs_exact_first_and_python_checks_counterparty():
    query = (
        "Mua hàng hóa từ bên liên quan Công ty Cổ phần APIS là bao nhiêu?"
    )
    correct = (
        "Mua hàng hóa | Công ty Cổ phần APIS: 100",
        {
            "heading": TABLE_NOTE,
            "item_name": "Mua hàng hóa | Công ty Cổ phần APIS",
            "metric_label": "Mua hàng hóa",
            "entity_label": "Công ty Cổ phần APIS",
            "scope_label": "Bên liên quan",
            "counterparty": "Công ty Cổ phần APIS",
            "transaction_type": "purchase",
            "aggregation_level": "component",
            "raw_value": "100",
        },
    )
    wrong_counterparty = (
        "Mua hàng hóa | Công ty Khác: 200",
        {
            **correct[1],
            "item_name": "Mua hàng hóa | Công ty Khác",
            "entity_label": "Công ty Khác",
            "counterparty": "Công ty Khác",
            "raw_value": "200",
        },
    )
    wrong_direction = (
        "Bán hàng hóa | Công ty Cổ phần APIS: 300",
        {
            **correct[1],
            "item_name": "Bán hàng hóa | Công ty Cổ phần APIS",
            "metric_label": "Bán hàng hóa",
            "transaction_type": "sale",
            "raw_value": "300",
        },
    )
    collection = SemanticStructuredCollection(
        [correct, wrong_counterparty, wrong_direction]
    )

    result = get_related_info(
        query,
        TABLE_NOTE,
        collection,
        intent=query,
    )

    assert collection.query_calls == 0
    assert result["retrieval_mode"] == "structured_slots"
    assert result["documents"] == [correct[0]]
    assert {
        "heading": TABLE_NOTE,
        "aggregation_level": "component",
        "transaction_type": "purchase",
    } in collection.get_calls


@pytest.mark.parametrize(
    ("query", "field", "value"),
    [
        ("Khoản hoàn nhập trong năm là bao nhiêu?", "movement_type", "reversal"),
        (
            "Thời gian khấu hao máy móc là bao nhiêu?",
            "policy_topic",
            "depreciation_period",
        ),
    ],
)
def test_semantic_only_canonical_slot_triggers_structured_probe(
    query,
    field,
    value,
):
    metadata = {
        "heading": TABLE_NOTE,
        "item_name": query,
        "row_label": query,
        "metric_label": (
            "Thời gian hữu dụng" if field == "policy_topic" else ""
        ),
        "entity_label": "máy móc" if field == "policy_topic" else "",
        "scope_label": (
            "Chính sách kế toán" if field == "policy_topic" else ""
        ),
        "aggregation_level": (
            "component" if field == "policy_topic" else ""
        ),
        field: value,
        "raw_value": "100",
    }
    collection = SemanticStructuredCollection([("semantic fact: 100", metadata)])

    result = get_related_info(
        query,
        TABLE_NOTE,
        collection,
        intent=query,
    )

    assert collection.query_calls == 0
    assert result["retrieval_mode"] == "structured_slots"
    assert any(call.get(field) == value for call in collection.get_calls)


def test_semantic_structured_miss_retains_dense_fallback():
    query = "Khoản hoàn nhập trong năm là bao nhiêu?"
    dense = (
        "Trích lập dự phòng trong năm: 100",
        {
            "heading": TABLE_NOTE,
            "item_name": "Trích lập dự phòng trong năm",
            "movement_type": "provision_charge",
            "raw_value": "100",
        },
    )
    collection = SemanticStructuredCollection([dense], dense_hits=[dense])

    result = get_related_info(
        query,
        TABLE_NOTE,
        collection,
        intent=query,
    )

    assert collection.query_calls == 1
    assert result.get("retrieval_mode") != "structured_slots"


def test_flow_comparison_structured_probe_rejects_stock_schedule_and_keeps_flow_pair():
    query = "So sánh lãi cho vay năm hiện tại và năm trước"
    common = {
        "heading": TABLE_NOTE,
        "metric_label": "Lãi cho vay",
        "transaction_type": "lending",
        "aggregation_level": "component",
        "raw_value": "100",
    }
    wrong_stock = [
        (
            "Dự thu lãi cho vay | Số cuối kỳ: 100",
            {
                **common,
                "block_id": "interest-receivable",
                "note_ref": "N.6",
                "scope_label": "Các khoản phải thu khác",
                "item_name": "Dự thu lãi cho vay | Số cuối kỳ",
                "period": "cuối",
                "period_role": "current",
            },
        ),
        (
            "Dự thu lãi cho vay | Số đầu kỳ: 90",
            {
                **common,
                "block_id": "interest-receivable",
                "note_ref": "N.6",
                "scope_label": "Các khoản phải thu khác",
                "item_name": "Dự thu lãi cho vay | Số đầu kỳ",
                "period": "đầu",
                "period_role": "previous",
                "raw_value": "90",
            },
        ),
    ]
    flow_pair = [
        (
            "Lãi tiền gửi, lãi cho vay | Năm nay: 30",
            {
                **common,
                "block_id": "finance-income",
                "note_ref": "N.4",
                "scope_label": "Doanh thu hoạt động tài chính",
                    "item_name": "Lãi tiền gửi, lãi cho vay | Năm nay",
                    "period_label": "Năm nay",
                    "period_role": "current",
                    "raw_value": "30",
            },
        ),
        (
            "Lãi tiền gửi, lãi cho vay | Năm trước: 40",
            {
                **common,
                "block_id": "finance-income",
                "note_ref": "N.4",
                "scope_label": "Doanh thu hoạt động tài chính",
                    "item_name": "Lãi tiền gửi, lãi cho vay | Năm trước",
                    "period_label": "Năm trước",
                    "period_role": "previous",
                    "raw_value": "40",
            },
        ),
    ]
    collection = SemanticStructuredCollection([*wrong_stock, *flow_pair])

    result = get_related_info(
        query,
        TABLE_NOTE,
        collection,
        intent=query,
    )

    assert collection.query_calls == 0
    assert result["retrieval_mode"] == "structured_slots"
    assert {meta["block_id"] for meta in result["metadatas"]} == {
        "finance-income"
    }


def test_direct_percent_fact_lookup_is_not_misclassified_as_ratio():
    direct = parse_query_slots("Tỷ lệ thuế suất là bao nhiêu?")
    direct_share_label = parse_query_slots(
        "Tỷ trọng sở hữu của cổ đông là bao nhiêu?"
    )
    directional = parse_query_slots(
        "Tỷ lệ lợi nhuận sau thuế trên doanh thu thuần là bao nhiêu?"
    )
    percent_change = parse_query_slots(
        "Tỷ lệ thay đổi doanh thu năm nay so với năm trước là bao nhiêu?"
    )

    assert direct.operation == "lookup"
    assert direct.operands == ()
    assert direct_share_label.operation == "lookup"
    assert direct_share_label.operands == ()
    assert directional.operation == "ratio"
    assert [operand.role for operand in directional.operands] == [
        "numerator",
        "denominator",
    ]
    assert percent_change.operation == "percent_change"


def test_absolute_year_is_preserved_and_enforced_over_report_fiscal_year():
    slots = parse_query_slots(
        "Doanh thu thuần năm 2024 là bao nhiêu?"
    )
    correct = {
        "metric_label": "Doanh thu thuần về bán hàng và cung cấp dịch vụ",
        "period_label": "Năm 2024",
        "fiscal_year": "2025",
    }
    wrong = {
        **correct,
        "period_label": "Năm 2023",
        # The report year must not override the source column's specific year.
        "fiscal_year": "2024",
    }

    assert slots.period_labels == ("2024",)
    assert fact_matches_required_slots(slots, correct)
    assert not fact_matches_required_slots(slots, wrong)
    assert fact_slot_score(slots, correct) > fact_slot_score(slots, wrong)
    assert "2024" in targeted_retry_query(
        "Doanh thu thuần năm 2024 là bao nhiêu?"
    )


def test_absolute_dates_normalize_zero_padding_and_reject_wrong_year():
    slots = parse_query_slots("Số dư tiền tại 1/1/2025 là bao nhiêu?")
    correct = {
        "metric_label": "Tiền",
        "period": "đầu",
        "period_label": "01/01/2025 VND",
    }
    wrong = {
        **correct,
        "period_label": "01/01/2024 VND",
    }

    assert slots.period_labels == ("01/01/2025",)
    assert fact_matches_required_slots(slots, correct)
    assert not fact_matches_required_slots(slots, wrong)


def test_multiple_explicit_years_allow_each_requested_leg_but_no_distractor():
    slots = parse_query_slots(
        "So sánh doanh thu thuần năm 2025 và năm 2024"
    )
    current = {
        "metric_label": "Doanh thu thuần về bán hàng và cung cấp dịch vụ",
        "period": "cuối",
        "period_label": "Năm 2025",
    }
    previous = {
        **current,
        "period": "đầu",
        "period_label": "Năm 2024",
    }
    distractor = {
        **previous,
        "period_label": "Năm 2023",
    }

    assert slots.period_labels == ("2025", "2024")
    assert slots.period == ""
    assert slots.period_role == ""
    assert fact_matches_required_slots(slots, current)
    assert fact_matches_required_slots(slots, previous)
    assert not fact_matches_required_slots(slots, distractor)


@pytest.mark.parametrize(
    ("query", "correct_metric", "wrong_metric", "correct_aggregation", "wrong_aggregation"),
    [
        (
            "Tổng tài sản là bao nhiêu?",
            "Tổng cộng tài sản",
            "Tài sản ngắn hạn",
            "total",
            "component",
        ),
        (
            "Tài sản ngắn hạn là bao nhiêu?",
            "Tài sản ngắn hạn",
            "Tổng cộng tài sản",
            "component",
            "total",
        ),
        (
            "Lợi nhuận sau thuế là bao nhiêu?",
            "Lợi nhuận sau thuế thu nhập doanh nghiệp",
            "Lợi nhuận kế toán trước thuế",
            "",
            "",
        ),
        (
            "Lợi nhuận sau thuế là bao nhiêu?",
            "Lợi nhuận sau thuế thu nhập doanh nghiệp",
            "Lợi nhuận sau thuế chưa phân phối",
            "",
            "",
        ),
        (
            "Lợi nhuận trước thuế là bao nhiêu?",
            "Lợi nhuận kế toán trước thuế",
            "Lợi nhuận sau thuế thu nhập doanh nghiệp",
            "",
            "",
        ),
        (
            "Lợi nhuận gộp là bao nhiêu?",
            "Lợi nhuận gộp",
            "Lợi nhuận thuần từ hoạt động kinh doanh",
            "",
            "",
        ),
        (
            "Lợi nhuận thuần từ hoạt động kinh doanh là bao nhiêu?",
            "Lợi nhuận thuần từ hoạt động kinh doanh",
            "Lợi nhuận gộp",
            "",
            "",
        ),
    ],
)
def test_contrastive_statement_semantics_reject_nearby_wrong_metric(
    query,
    correct_metric,
    wrong_metric,
    correct_aggregation,
    wrong_aggregation,
):
    slots = parse_query_slots(query)
    correct = {
        "metric_label": correct_metric,
        "item_name": correct_metric,
        "aggregation_level": correct_aggregation,
    }
    wrong = {
        "metric_label": wrong_metric,
        "item_name": wrong_metric,
        "aggregation_level": wrong_aggregation,
    }

    assert slots.metric
    assert fact_matches_required_slots(slots, correct)
    assert not fact_matches_required_slots(slots, wrong)
    assert fact_slot_score(slots, wrong) == -200.0
