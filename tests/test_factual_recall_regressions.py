"""Deterministic regressions for hard factual-recall gate misses."""

from agents.keyworder_runner import (
    COMPACT_ROUTER_SYSTEM_INSTRUCTION,
    _entity_evidence_from_query,
)
from config.allowed_keywords import KEYWORD_SYNONYMS, normalize_keyword_synonyms
from schemas.table_names import TABLE_NOTE
from tools.tools import _item_match_score


def test_lending_interest_comparison_routes_only_to_notes():
    query = "So sánh lãi cho vay năm hiện tại và năm trước. Sự thay đổi là bao nhiêu?"
    assert _entity_evidence_from_query(query) == [
        {"table": TABLE_NOTE, "query": query, "needby": []}
    ]


def test_lending_income_outranks_borrowing_interest_expense():
    query = "So sánh lãi cho vay năm hiện tại và năm trước"
    correct = _item_match_score(
        query,
        {
            "item_name": "Lãi tiền gửi ngân hàng, Lãi cho vay | Năm nay",
            "subheading": "4. Doanh thu hoạt động tài chính",
        },
        "Lãi cho vay: 14.592.618.979",
        intent=query,
    )
    wrong = _item_match_score(
        query,
        {
            "item_name": "Chi phí lãi vay | Năm nay",
            "subheading": "5. Chi phí tài chính",
        },
        "Chi phí lãi vay: 1.000",
        intent=query,
    )
    assert correct > wrong + 100


def test_compact_router_note_detail_rule_has_no_dataset_specific_entities_or_notes():
    instruction = COMPACT_ROUTER_SYSTEM_INSTRUCTION.casefold()

    for hardcode in (
        "túc duyên",
        "apec land huế",
        "lagoon lăng cô",
        "kim bôi",
        "2a/2c",
    ):
        assert hardcode not in instruction
    assert "typed metadata" in instruction
    assert "không giả định tên doanh nghiệp hay số thuyết minh" in instruction


def test_policy_rerank_uses_typed_topic_not_note_number():
    query = "Thời gian khấu hao máy móc là bao lâu?"
    typed_fact = {
        "item_code": "note_policy_atom",
        "item_name": "Thuyết minh 37. Chính sách tài sản cố định",
        "row_label": "Thời gian khấu hao",
        "metric_label": "Thời gian khấu hao",
        "entity_label": "Máy móc",
        "scope_label": "c) Máy móc",
        "policy_topic": "depreciation_period",
        "aggregation_level": "component",
    }
    document = "Máy móc được khấu hao theo đường thẳng trong 5 - 10 năm."

    numbered_37 = _item_match_score(query, typed_fact, document, intent=query)
    numbered_2 = _item_match_score(
        query,
        {
            **typed_fact,
            "item_name": "Thuyết minh 2. Chính sách tài sản cố định",
        },
        document,
        intent=query,
    )
    sparse = _item_match_score(
        query,
        {**typed_fact, "policy_topic": ""},
        document,
        intent=query,
    )

    assert numbered_37 == numbered_2
    assert numbered_37 == sparse + 20.0


def test_retained_earnings_uses_canonical_financial_statement_metric():
    assert normalize_keyword_synonyms("Số dư lợi nhuận giữ lại cuối năm") == (
        "số dư lợi nhuận sau thuế chưa phân phối cuối năm"
    )


def test_keyword_synonyms_are_idempotent():
    for alias, canonical in KEYWORD_SYNONYMS.items():
        assert normalize_keyword_synonyms(alias) == canonical
        assert normalize_keyword_synonyms(canonical) == canonical
        assert normalize_keyword_synonyms(normalize_keyword_synonyms(alias)) == canonical
