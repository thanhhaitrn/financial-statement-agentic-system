"""Dataset-scoped query-slot vocabulary derived from canonical SQLite facts."""

from __future__ import annotations

import pytest

from kb.sqlite_repo import (
    derive_query_slot_lexicon,
    init_db,
    insert_financial_facts,
)
from schemas.table_names import TABLE_NOTE
from tools.query_routing import (
    dataset_slot_lexicon_version,
    fact_matches_required_slots,
    parse_query_slots,
    set_dataset_slot_lexicon,
)
from schemas.requirements import requirement_name_matches_fact


@pytest.fixture(autouse=True)
def _reset_dataset_slot_lexicon():
    set_dataset_slot_lexicon({})
    yield
    set_dataset_slot_lexicon({})


def _typed_fact(
    *,
    metric: str,
    entity: str = "",
    counterparty: str = "",
    block_id: str,
) -> dict:
    row_label = entity or metric
    return {
        "company": "Công ty kiểm thử",
        "fiscal_year": "2025",
        "heading": TABLE_NOTE,
        "item_code": "note_row",
        "note_ref": "N.1",
        "subheading": "1. Lịch biểu kiểm thử",
        "item_name": f"{metric} | {row_label} | Năm 2025",
        "value": "100",
        "raw_value": "100",
        "normalized_value": "100",
        "source": "lexicon_fixture.md",
        "period": "cuối",
        "value_type": "",
        "unit": "VND",
        "row_label": row_label,
        "column_label": "Năm 2025",
        "value_kind": "amount",
        "parsed_value": "100",
        "period_label": "Năm 2025",
        "period_role": "current",
        "aggregation_level": "component",
        "section_path": "THUYẾT MINH > Lịch biểu kiểm thử",
        "block_id": block_id,
        "source_page": "1",
        "metric_label": metric,
        "entity_label": entity,
        "scope_label": "Lịch biểu kiểm thử",
        "counterparty": counterparty,
    }


@pytest.fixture()
def lexicon_conn():
    conn = init_db(":memory:")
    rows = [
        _typed_fact(
            metric="Nguyên giá",
            entity="Dây chuyền tiệt trùng",
            block_id="asset-short",
        ),
        _typed_fact(
            metric="Nguyên giá",
            entity="Dây chuyền tiệt trùng UHT",
            block_id="asset-long",
        ),
        _typed_fact(
            metric="Chi phí hiệu chuẩn",
            block_id="metric-short",
        ),
        _typed_fact(
            metric="Chi phí hiệu chuẩn dây chuyền vô trùng",
            counterparty="Nhà cung cấp Thiết bị Alpha",
            block_id="metric-long",
        ),
        _typed_fact(metric="Giá trị", entity="TRANG", block_id="noise-page"),
        _typed_fact(
            metric="(200 = 210 + 220)",
            entity="5 – 50 năm",
            block_id="noise-formula",
        ),
        _typed_fact(
            metric="TRANG",
            entity="Mua tài sản cố định",
            block_id="noise-transaction",
        ),
        _typed_fact(metric="Tổng", entity="TổngVND", block_id="noise-total"),
    ]
    insert_financial_facts(conn, rows)
    yield conn
    conn.close()


def test_derive_slot_lexicon_uses_only_clean_canonical_axes(lexicon_conn):
    lexicon = derive_query_slot_lexicon(lexicon_conn)

    assert "nguyên giá" in lexicon["metric"]
    assert "chi phí hiệu chuẩn dây chuyền vô trùng" in lexicon["metric"]
    assert "dây chuyền tiệt trùng uht" in lexicon["entity"]
    assert "nhà cung cấp thiết bị alpha" in lexicon["entity"]

    assert "giá trị" not in lexicon["metric"]
    assert "tổng" not in lexicon["metric"]
    assert "trang" not in lexicon["entity"]
    assert "5 – 50 năm" not in lexicon["entity"]
    assert "mua tài sản cố định" not in lexicon["entity"]
    assert "tổngvnd" not in lexicon["entity"]


def test_active_lexicon_binds_longest_exact_asset_entity_and_invalidates_cache(
    lexicon_conn,
):
    query = "Nguyên giá dây chuyền tiệt trùng UHT cuối kỳ là bao nhiêu?"

    before = parse_query_slots(query)
    assert before.entity == ""
    version_before = dataset_slot_lexicon_version()

    set_dataset_slot_lexicon(
        derive_query_slot_lexicon(lexicon_conn),
        dataset_id="fixture-a",
    )
    assert dataset_slot_lexicon_version() == version_before + 1

    after = parse_query_slots(query)
    assert after.metric == "nguyên giá"
    assert after.entity == "dây chuyền tiệt trùng uht"
    assert after.aggregation == "component"
    assert fact_matches_required_slots(
        after,
        {
            "metric_label": "Nguyên giá",
            "entity_label": "Dây chuyền tiệt trùng UHT",
            "item_name": (
                "Nguyên giá | Dây chuyền tiệt trùng UHT | Số cuối kỳ"
            ),
            "period": "cuối",
            "aggregation_level": "component",
        },
        "100",
    )

    # Replacing the active dataset must not return the cached entity from the
    # previous dataset for the same query text.
    set_dataset_slot_lexicon({}, dataset_id="fixture-b")
    assert parse_query_slots(query).entity == ""


def test_longest_exact_dataset_metric_wins_before_static_fallback(lexicon_conn):
    set_dataset_slot_lexicon(derive_query_slot_lexicon(lexicon_conn))

    slots = parse_query_slots(
        "Chi phí hiệu chuẩn dây chuyền vô trùng năm nay là bao nhiêu?"
    )

    assert slots.metric == "chi phí hiệu chuẩn dây chuyền vô trùng"


def test_dataset_metric_matches_safe_discontinuous_paraphrase():
    set_dataset_slot_lexicon(
        {
            "metric": (
                "doanh thu bán bất động sản",
                "doanh thu bán hàng hóa",
                "doanh thu",
            )
        },
        dataset_id="property-report",
    )

    slots = parse_query_slots(
        "Doanh thu gộp từ bất động sản giữ để bán trong năm hiện tại "
        "là bao nhiêu?"
    )

    assert slots.metric == "doanh thu bán bất động sản"
    assert slots.period_role == "current"
    assert requirement_name_matches_fact(
        (
            "Doanh thu gộp từ bất động sản giữ để bán trong năm hiện tại "
            "là bao nhiêu?"
        ),
        {
            "table": "THUYẾT MINH BÁO CÁO TÀI CHÍNH",
            "metric_label": "Doanh thu bán bất động sản",
            "item_name": "Doanh thu bán bất động sản | Năm nay",
            "period_role": "current",
            "transaction_type": "sale",
            "raw_value": "100",
        },
    )
