"""Focused contracts for statement aggregates and schedule semantics."""

import json
from pathlib import Path

import pandas as pd

from ingestion.kb_builder import build_fact_rows
from ingestion.period_normalize import section_total_key
from ingestion.table_parser import attach_context
from kb.sqlite_repo import init_db, insert_financial_facts
from schemas.table_names import TABLE_NOTE
from vectorstore.text_builder import (
    build_documents_and_metadata,
    validate_canonical_vector_metadata,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _facts(markdown: str):
    return build_fact_rows(
        attach_context(markdown),
        company="Công ty kiểm thử",
        source="fixture.md",
        fiscal_year=2025,
    )


def test_balance_sheet_section_codes_create_canonical_total_facts():
    cases = json.loads(
        (
            FIXTURE_DIR / "balance_sheet_section_semantics_cases.json"
        ).read_text(encoding="utf-8")
    )

    for case in cases:
        rows = _facts(case["markdown"])
        for item_code, (expected_key, expected_metric) in case[
            "expected"
        ].items():
            matches = [row for row in rows if row[3] == item_code]
            assert matches, (case["name"], item_code)
            assert {row[20] for row in matches} == {"total"}
            assert {row[24] for row in matches} == {expected_metric}
            assert {row[25] for row in matches} == {""}
            assert {row[32] for row in matches} == {expected_key}
            assert all(row[6].startswith(f"{expected_metric} |") for row in matches)
            assert all(expected_metric in row[21] for row in matches)
            assert all(
                section_total_key(
                    row[14],
                    table_scope=row[2],
                    item_code=row[3],
                    equity_410_fallback=(
                        item_code in case.get("fallback_codes", [])
                    ),
                )
                == expected_key
                for row in matches
            )

        for item_code in case["component_codes"]:
            matches = [row for row in rows if row[3] == item_code]
            assert matches, (case["name"], item_code)
            assert {row[20] for row in matches} == {"component"}
            assert {row[32] for row in matches} == {""}

        assert {
            row[15]
            for row in rows
            if row[3] in case["expected"]
        } == set(case["clean_columns"])


def test_balance_sheet_code_mapping_is_scope_guarded_and_preserves_other_assets():
    assert (
        section_total_key(
            "Khoản vay chi tiết",
            table_scope=TABLE_NOTE,
            item_code="300",
        )
        == ""
    )
    assert (
        section_total_key(
            "Tổng tài sản thuế thu nhập hoãn lại",
            table_scope=TABLE_NOTE,
        )
        == ""
    )
    assert section_total_key("Tài sản ngắn hạn khác") == ""
    assert (
        section_total_key(
            "Tổng tài sản cố định hữu hình",
            table_scope="BẢNG CÂN ĐỐI KẾ TOÁN",
            item_code="211",
        )
        == ""
    )
    assert (
        section_total_key(
            "Tổng nợ phải trả người bán",
            table_scope="BẢNG CÂN ĐỐI KẾ TOÁN",
            item_code="311",
        )
        == ""
    )


def test_non_section_line_item_totals_never_receive_a_section_key():
    markdown = """# BÁO CÁO TÌNH HÌNH TÀI CHÍNH

### TÀI SẢN

| Chỉ tiêu | Mã số | 31/12/2025VND |
| --- | --- | ---: |
| Tổng tài sản cố định hữu hình | 211 | 100 |

### NGUỒN VỐN

| Chỉ tiêu | Mã số | 31/12/2025VND |
| --- | --- | ---: |
| Tổng nợ phải trả người bán | 311 | 50 |
"""
    rows = _facts(markdown)
    matched = [row for row in rows if row[3] in {"211", "311"}]

    assert matched
    assert {row[32] for row in matched} == {""}


def test_revenue_children_and_geographic_columns_keep_parent_semantics():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 1. Doanh thu bán hàng và cung cấp dịch vụ

Doanh thu thuần bao gồm:

| Chỉ tiêu | 2025VND | 2024VND |
| --- | ---: | ---: |
| Tổng doanh thu |  |  |
| - Bán thành phẩm | 80 | 70 |
| - Bán hàng hóa | 20 | 15 |
|  | 100 | 85 |

## 2. Báo cáo bộ phận

### Bộ phận theo khu vực địa lý

| Chỉ tiêu | Trong nước<br/>2025VND | Nước ngoài<br/>2025VND | Tổng<br/>2025VND |
| --- | ---: | ---: | ---: |
| Doanh thu thuần | 80 | 20 | 100 |
"""

    rows = _facts(markdown)
    finished_goods = [
        row for row in rows if row[14] == "- Bán thành phẩm"
    ]
    assert len(finished_goods) == 2
    assert {row[24] for row in finished_goods} == {
        "Doanh thu bán thành phẩm"
    }
    assert {row[28] for row in finished_goods} == {"sale"}
    assert all(
        row[6].startswith("Doanh thu bán thành phẩm |")
        for row in finished_goods
    )

    foreign = next(
        row
        for row in rows
        if row[14] == "Doanh thu thuần"
        and "Nước ngoài" in row[15]
    )
    total = next(
        row
        for row in rows
        if row[14] == "Doanh thu thuần"
        and row[15].startswith("Tổng")
    )

    assert foreign[25] == "Nước ngoài"
    assert foreign[30] == "Nước ngoài"
    assert foreign[20] == "component"
    assert total[25] == ""
    assert total[30] == ""
    assert total[20] == "total"


def test_section_key_has_sqlite_vector_and_document_parity():
    markdown = """# BÁO CÁO TÌNH HÌNH TÀI CHÍNH

### TÀI SẢN

| Tài sản dài hạn | Mã số<br/>Tài sản dài hạn | 31/12/2025VND<br/>Tài sản dài hạn |
| --- | --- | ---: |
| TỔNG TÀI SẢN (270 = 100 + 200) | 270 | 100 |
"""
    rows = _facts(markdown)
    connection = init_db(":memory:")
    insert_financial_facts(connection, rows)
    dataframe = pd.read_sql_query(
        "SELECT * FROM financial_facts ORDER BY rowid",
        connection,
    )
    documents, metadatas, _ids = build_documents_and_metadata(dataframe)
    validate_canonical_vector_metadata(metadatas)

    assert dataframe.loc[0, "section_key"] == "tong_tai_san"
    assert metadatas[0]["section_key"] == "tong_tai_san"
    assert "Khóa phần báo cáo: tong_tai_san" in documents[0]
