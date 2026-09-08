"""Regression contracts for canonical typed-cell ingestion."""

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ingestion.kb_builder import build_fact_rows
from ingestion.frontmatter_parser import build_frontmatter_rows
from ingestion.note_parser import build_note_rows
from ingestion.pipeline import _resolve_ingestion_metadata
from ingestion.table_parser import attach_context
from kb.sqlite_repo import (
    SQLITE_SCHEMA_VERSION,
    init_db,
    insert_financial_facts,
    normalize_financial_fact_rows,
)
from vectorstore.index_builder import _stable_vector_ids
from vectorstore.qdrant_store import (
    VECTOR_INDEX_SCHEMA_VERSION,
    _collection_metadata,
)
from vectorstore.text_builder import (
    build_documents_and_metadata,
    validate_canonical_vector_metadata,
)
from tools.query_routing import fact_matches_required_slots, parse_query_slots


TYPED_FIELDS = (
    "row_label",
    "column_label",
    "value_kind",
    "parsed_value",
    "period_label",
    "period_role",
    "aggregation_level",
    "section_path",
    "block_id",
    "source_page",
    "metric_label",
    "entity_label",
    "scope_label",
    "counterparty",
    "transaction_type",
    "movement_type",
    "geography",
    "policy_topic",
    "section_key",
)


def _facts(markdown: str):
    return build_fact_rows(
        attach_context(markdown),
        company="Công ty A",
        source="fixture.md",
        fiscal_year=2025,
    )


def test_cross_report_excerpts_preserve_numeric_recall_and_typed_semantics():
    """Contracts copied from VNM/APEC/CNC/GER layouts, not question-specific GT."""

    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "typed_fact_cross_report_cases.json"
    )
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert {case["name"].split("_", 1)[0] for case in cases} == {
        "vnm",
        "apec",
        "cnc",
        "ger",
    }
    for case in cases:
        rows = build_fact_rows(
            attach_context(case["markdown"]),
            company="Công ty kiểm thử",
            source=case["source_format"],
            fiscal_year=case["fiscal_year"],
        )
        assert len(rows) == case["expected_numeric_fact_count"], case["name"]

        expected = case["expected"]
        matches = [
            row
            for row in rows
            if row[17] == expected["parsed_value"]
            and row[14] == expected["row_label"]
            and expected["column_contains"] in row[15]
        ]
        assert len(matches) == 1, case["name"]
        match = matches[0]
        assert match[16] == expected["value_kind"], case["name"]
        assert match[19] == expected["period_role"], case["name"]
        assert match[20] == expected["aggregation_level"], case["name"]
        if "value_type" in expected:
            assert match[12] == expected["value_type"], case["name"]


def test_entity_table_cells_are_canonical_facts_without_row_blobs():
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "typed_fact_text_entity_cases.json"
    )
    cases = json.loads(fixture_path.read_text(encoding="utf-8"))

    for case in cases:
        rows = build_fact_rows(
            attach_context(case["markdown"]),
            company="Công ty kiểm thử",
            source=case["source_format"],
            fiscal_year=case["fiscal_year"],
        )
        assert len(rows) == case["expected_fact_count"], case["name"]
        assert all(row[3] != "note_table" for row in rows)
        assert {row[14] for row in rows} == {case["row_label"]}

        for expected in case["expected_cells"]:
            matches = [
                row
                for row in rows
                if row[15] == expected["column_label"]
                and expected["value_contains"] in row[7]
            ]
            assert len(matches) == 1, (case["name"], expected)
            assert matches[0][16] == expected["value_kind"]
            assert matches[0][20] == "component"
            if expected["value_kind"] in {"entity", "text"}:
                assert matches[0][17] == matches[0][7]


def test_total_column_is_total_but_entity_cell_on_total_row_is_component():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 5. Danh sách đơn vị

| Tên | Trụ sở | Tổng |
| --- | --- | ---: |
| Tổng Công ty A | Hà Nội | 10 |
"""

    rows = _facts(markdown)
    by_column = {row[15]: row for row in rows}

    assert by_column["Tổng"][16] == "amount"
    assert by_column["Tổng"][20] == "total"
    assert by_column["Trụ sở"][16] == "entity"
    assert by_column["Trụ sở"][20] == "component"


def test_front_assurance_atoms_survive_plain_text_toc_entries():
    cases = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "topic_atomization_cases.json"
        ).read_text(encoding="utf-8")
    )
    case = next(case for case in cases if case["name"].startswith("ger_"))
    rows = build_frontmatter_rows(
        case["markdown"],
        "Công ty kiểm thử",
        case["source_format"],
        case["fiscal_year"],
    )

    identifier = [
        row
        for row in rows
        if row[3] == "report_section_identifier"
        and row[7] == case["expected_identifier"]
    ]
    entity = [
        row
        for row in rows
        if row[3] == "report_section_entity"
        and row[7] == case["expected_entity"]
    ]
    report_date = [
        row
        for row in rows
        if row[3] == "report_section_date"
        and row[7] == case["expected_date"]
    ]

    assert len(identifier) == 1
    assert identifier[0][16] == "identifier"
    assert len(entity) == 1
    assert entity[0][16] == "entity"
    assert len(report_date) == 1
    assert report_date[0][16] == "date"
    assert any("Ban Tổng Giám đốc trình bày" in row[7] for row in rows)
    assert not any("Tiền | 100" in row[7] for row in rows)


def test_numbered_letter_bold_headings_and_safe_sentence_atoms():
    cases = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "topic_atomization_cases.json"
        ).read_text(encoding="utf-8")
    )
    case = next(case for case in cases if case["name"].startswith("vnm_"))
    rows = build_note_rows(
        case["markdown"],
        "Công ty kiểm thử",
        case["source_format"],
        case["fiscal_year"],
        include_table_rows=False,
    )

    duration_atoms = [
        row
        for row in rows
        if row[3] == "note_policy_atom"
        and row[14] == "Thời gian khấu hao"
        and case["expected_duration_text"] in row[7]
    ]
    ticker_atoms = [
        row
        for row in rows
        if row[3] == "note_topic_atom"
        and row[14] == "Mã chứng khoán"
        and row[7] == case["expected_ticker"]
    ]

    assert len(duration_atoms) == 1
    assert "Phần mềm máy vi tính" in duration_atoms[0][21]
    assert duration_atoms[0][25] == "Phần mềm máy vi tính"
    assert "Phần mềm máy vi tính" in duration_atoms[0][7]
    assert len(ticker_atoms) == 1
    assert ticker_atoms[0][16] == "identifier"
    assert "b) Niêm yết và mã chứng khoán" in ticker_atoms[0][21]
    assert any(row[14] == "Điều kiện ghi nhận" for row in rows)
    assert any(row[14] == "Điều kiện vốn hóa" for row in rows)
    original_paragraphs = [
        row
        for row in rows
        if row[3] == "note_text"
        and case["expected_duration_text"] in row[7]
    ]
    assert original_paragraphs
    assert original_paragraphs[0][16] == "text"
    assert set(case["expected_note_refs"]).issubset(
        {row[4] for row in rows if row[4]}
    )


def test_tab_separated_policy_rows_become_canonical_cells():
    cases = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "topic_atomization_cases.json"
        ).read_text(encoding="utf-8")
    )
    case = next(case for case in cases if case["name"].startswith("apec_"))
    rows = build_note_rows(
        case["markdown"],
        "Công ty kiểm thử",
        case["source_format"],
        case["fiscal_year"],
        include_table_rows=False,
    )
    policy_rows = [row for row in rows if row[3] == "note_policy_cell"]
    narrative_method = next(
        row
        for row in rows
        if row[3] == "note_policy_atom"
        and row[14] == "Phương pháp khấu hao"
    )

    assert len(policy_rows) == len(case["expected_cells"])
    assert narrative_method[25] == "Tài sản cố định hữu hình"
    for expected in case["expected_cells"]:
        matches = [
            row
            for row in policy_rows
            if row[14] == expected["row_label"]
            and row[15] == expected["column_label"]
            and row[7] == expected["value"]
        ]
        assert len(matches) == 1
        assert matches[0][16] == "count"
        assert matches[0][13] == "năm"
        assert matches[0][20] == "component"
        assert "Khấu hao theo nhóm tài sản" in matches[0][21]


def test_matrix_dividers_are_dynamic_when_first_header_is_blank():
    markdown = """--------Page 0
# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 9. Tài sản cố định hữu hình

|  | Nhà cửa | Cộng |
| --- | ---: | ---: |
| **Nguyên giá:** |  |  |
| Số dư đầu năm | 10.000 | 10.000 |
| Số dư cuối năm | 12.000 | 12.000 |
| **Giá trị hao mòn luỹ kế:** |  |  |
| Số dư cuối năm | 3.000 | 3.000 |
"""

    rows = _facts(markdown)
    by_value = {}
    for row in rows:
        by_value.setdefault(row[7], []).append(row)

    cost = by_value["12.000"][0]
    depreciation = by_value["3.000"][0]
    assert cost[12] == "nguyên giá"
    assert depreciation[12] == "hao mòn"
    assert "Nguyên giá" in cost[5]
    assert "Giá trị hao mòn lũy kế" in depreciation[5]
    assert cost[14] == "Số dư cuối năm"
    assert cost[15] in {"Nhà cửa", "Cộng"}
    assert cost[16] == "amount"
    assert cost[17] == "12000"
    assert cost[22].startswith("table-")
    assert cost[23] == "1"


def test_blank_terminal_total_gets_semantic_group_name_and_typed_slots():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 5. Chi phí tài chính

| Chỉ tiêu | Năm 2025 | Năm 2024 |
| --- | ---: | ---: |
| Lãi vay | 40 | 30 |
| Lỗ tỷ giá | 60 | 20 |
|  | 100 | 50 |
"""

    rows = _facts(markdown)
    totals = [row for row in rows if row[20] == "total"]

    assert len(totals) == 2
    assert {row[14] for row in totals} == {"Tổng Chi phí tài chính"}
    assert {row[17] for row in totals} == {"100", "50"}
    assert {row[19] for row in totals} == {"current", "previous"}
    assert all("Chi phí tài chính" in row[21] for row in totals)


def test_explicit_bare_total_gets_semantic_group_name_and_typed_slots():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 5. Chi phí tài chính

| Chỉ tiêu | Năm 2025 | Năm 2024 |
| --- | ---: | ---: |
| Lãi vay | 40 | 30 |
| Lỗ tỷ giá | 60 | 20 |
| Cộng | 100 | 50 |
"""

    totals = [row for row in _facts(markdown) if row[20] == "total"]

    assert len(totals) == 2
    assert {row[14] for row in totals} == {"Tổng Chi phí tài chính"}
    assert {row[6].split(" | ", 1)[0] for row in totals} == {
        "Tổng Chi phí tài chính"
    }
    assert {row[24] for row in totals} == {"Tổng Chi phí tài chính"}
    assert {row[17] for row in totals} == {"100", "50"}


def test_schedule_title_row_is_typed_as_total_without_bare_total_word():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 18a. Vay ngắn hạn

| Khoản mục | Số cuối kỳ Giá trị | Trong kỳ Tăng | Trong kỳ Giảm | Số đầu kỳ Giá trị |
| --- | ---: | ---: | ---: | ---: |
| a) Vay ngắn hạn | 150 | 50 | 80 | 180 |
| Vay ngân hàng | 130 | 30 | 50 | 150 |
| Vay các cá nhân | 20 | 20 | 30 | 30 |
"""

    rows = _facts(markdown)
    totals = [row for row in rows if row[20] == "total"]

    assert len(totals) == 4
    assert {row[14] for row in totals} == {"Tổng Vay ngắn hạn"}
    assert {row[24] for row in totals} == {"Tổng Vay ngắn hạn"}
    assert {row[17] for row in totals} == {"150", "50", "80", "180"}
    assert all(row[28] == "borrowing" for row in totals)


def test_cumulative_flow_columns_preserve_current_and_previous_roles():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 5. Chi phí tài chính

| Chỉ tiêu | Luỹ kế từ đầu năm đến cuối kỳ này Năm nay | Luỹ kế từ đầu năm đến cuối kỳ này Năm trước |
| --- | ---: | ---: |
| Tổng | 100 | 80 |
"""

    totals = [row for row in _facts(markdown) if row[20] == "total"]

    assert len(totals) == 2
    assert {row[19] for row in totals} == {"current", "previous"}
    assert {
        (row[17], row[19])
        for row in totals
    } == {("100", "current"), ("80", "previous")}


def test_blank_nonterminal_nonarithmetic_row_is_not_invented_as_total():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 5. Chi phí tài chính

| Chỉ tiêu | Năm 2025 |
| --- | ---: |
| Lãi vay | 40 |
|  | 999 |
| Lỗ tỷ giá | 60 |
"""

    rows = _facts(markdown)

    assert not [row for row in rows if row[7] == "999"]
    assert not [row for row in rows if row[20] == "total"]


def test_value_kind_covers_percent_date_and_identifier_without_losing_id_zeros():
    markdown = """# THÔNG TIN CÔNG TY

| Chỉ tiêu | Giá trị |
| --- | ---: |
| Tỷ lệ sở hữu | 10,5% |
| Ngày phát hành báo cáo | 31/03/2026 |
| Số báo cáo | 00123 |
"""

    rows = _facts(markdown)
    by_label = {row[14]: row for row in rows}

    assert by_label["Tỷ lệ sở hữu"][16:18] == ("percent", "10.5")
    assert by_label["Ngày phát hành báo cáo"][16:18] == ("date", "31/03/2026")
    assert by_label["Số báo cáo"][16:18] == ("identifier", "00123")


def test_explicit_continuation_inherits_only_compatible_header():
    markdown = """--------Page 0
# BÁO CÁO LƯU CHUYỂN TIỀN TỆ

| Chỉ tiêu | Năm 2025 | Năm 2024 |
| --- | ---: | ---: |
| Tiền thu từ bán hàng | 10 | 8 |

--------Page 1
# BÁO CÁO LƯU CHUYỂN TIỀN TỆ (tiếp theo)

| Tiền chi cho người lao động | (4) | (3) |
| --- | ---: | ---: |
| Lưu chuyển tiền thuần | 6 | 5 |
"""

    blocks = attach_context(markdown)
    warning_kinds = {
        warning["kind"]
        for warning in blocks[1]["parser_warnings"]
    }
    rows = build_fact_rows(
        blocks,
        company="Công ty A",
        source="fixture.md",
        fiscal_year=2025,
    )

    assert "continuation_header_inherited" in warning_kinds
    assert any(row[14] == "Tiền chi cho người lao động" and row[7] == "-4" for row in rows)
    assert any(row[14] == "Lưu chuyển tiền thuần" and row[7] == "6" for row in rows)
    assert {row[23] for row in rows if row[14] == "Tiền chi cho người lao động"} == {"2"}


def test_incompatible_continuation_fails_closed_with_warning():
    markdown = """--------Page 0
# BÁO CÁO LƯU CHUYỂN TIỀN TỆ

| Chỉ tiêu | Năm 2025 | Năm 2024 |
| --- | ---: | ---: |
| Tiền thu | 10 | 8 |

--------Page 1
# BÁO CÁO LƯU CHUYỂN TIỀN TỆ (tiếp theo)

| Tiền chi | (4) |
| --- | ---: |
| Tiền thuần | 6 |
"""

    blocks = attach_context(markdown)

    assert blocks[1]["table"][0].startswith("| Tiền chi")
    assert {
        warning["kind"]
        for warning in blocks[1]["parser_warnings"]
    } == {"continuation_header_not_inherited"}


def test_pipeline_mode_keeps_note_narrative_without_duplicate_table_blobs():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 5. Chi phí tài chính

Chi phí tài chính chủ yếu là lãi vay.

| Chỉ tiêu | Năm 2025 |
| --- | ---: |
| Lãi vay | 40 |
"""

    narrative_rows = build_note_rows(
        markdown,
        "Công ty A",
        "fixture.md",
        2025,
        include_table_rows=False,
    )

    assert narrative_rows
    assert all(row[3] != "note_table" for row in narrative_rows)
    assert any("chủ yếu là lãi vay" in row[7].lower() for row in narrative_rows)
    assert all(len(row) == 32 for row in narrative_rows)


def test_pipeline_mode_keeps_front_narrative_without_duplicate_table_blobs():
    markdown = """## BÁO CÁO CỦA BAN GIÁM ĐỐC

Ban Giám đốc trình bày thông tin quản trị.

| Họ và tên | Chức vụ |
| --- | --- |
| Nguyễn Văn A | Tổng Giám đốc |

# BẢNG CÂN ĐỐI KẾ TOÁN
"""

    narrative_rows = build_frontmatter_rows(
        markdown,
        "Công ty A",
        "fixture.md",
        2025,
        include_table_rows=False,
    )

    assert narrative_rows
    assert all(row[3] != "report_section_table" for row in narrative_rows)
    assert any("thông tin quản trị" in row[7].lower() for row in narrative_rows)


def test_sqlite_and_vector_metadata_have_exact_typed_field_parity():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 19. Cổ phiếu

| Chỉ tiêu | 31/12/2025 | 01/01/2025 |
| --- | ---: | ---: |
| Số lượng cổ phiếu đang lưu hành | 1.200 | 1.000 |
"""
    rows = _facts(markdown)
    connection = init_db(":memory:")
    insert_financial_facts(connection, rows)

    assert SQLITE_SCHEMA_VERSION == 6
    dataframe = pd.read_sql_query("SELECT * FROM financial_facts ORDER BY rowid", connection)
    _documents, metadatas, _ids = build_documents_and_metadata(dataframe)
    validate_canonical_vector_metadata(metadatas)
    stable_ids = _stable_vector_ids(dataframe, _documents, metadatas)

    assert rows
    assert metadatas
    for field in TYPED_FIELDS:
        assert field in dataframe.columns
        assert field in metadatas[0]
        assert str(metadatas[0][field]) == str(dataframe.iloc[0][field])
    assert metadatas[0]["fact_id"] == dataframe.iloc[0]["fact_id"]
    assert metadatas[0]["fact_id"]
    assert metadatas[0]["value_kind"] == "count"
    assert metadatas[0]["parsed_value"] == "1200"
    assert metadatas[0]["period_role"] == "current"
    assert metadatas[0]["metric_label"] == "Số lượng cổ phiếu đang lưu hành"
    assert metadatas[0]["scope_label"] == "Cổ phiếu"
    assert stable_ids == dataframe["fact_id"].astype(str).tolist()
    assert "Chỉ tiêu ngữ nghĩa: Số lượng cổ phiếu đang lưu hành" in _documents[0]


def test_semantic_dimensions_preserve_metric_direction_entity_and_scope():
    cases = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "typed_fact_semantic_dimension_cases.json"
        ).read_text(encoding="utf-8")
    )

    assert {case["name"].split("_", 1)[0] for case in cases} == {
        "vnm",
        "apec",
        "cnc",
        "ger",
        "dpc",
    }
    for case in cases:
        if case.get("parser") == "note":
            rows = build_note_rows(
                case["markdown"],
                company="Công ty kiểm thử",
                source=case["source_format"],
                fiscal_year=case["fiscal_year"],
                include_table_rows=False,
            )
        else:
            rows = build_fact_rows(
                attach_context(case["markdown"]),
                company="Công ty kiểm thử",
                source=case["source_format"],
                fiscal_year=case["fiscal_year"],
            )
        selected = [
            row
            for row in rows
            if row[14] == case["select"]["row_label"]
            and (
                not case["select"].get("column_contains")
                or case["select"]["column_contains"] in row[15]
            )
            and (
                not case["select"].get("item_code")
                or case["select"]["item_code"] == row[3]
            )
        ]
        assert len(selected) == 1, case["name"]
        row = selected[0]
        assert row[24] == case["expected"]["metric_label"], case["name"]
        assert row[25] == case["expected"]["entity_label"], case["name"]
        assert row[26] == case["expected"]["scope_label"], case["name"]
        assert row[27] == case["expected"]["counterparty"], case["name"]
        assert row[28] == case["expected"]["transaction_type"], case["name"]
        assert row[29] == case["expected"]["movement_type"], case["name"]
        assert row[30] == case["expected"]["geography"], case["name"]
        assert row[31] == case["expected"]["policy_topic"], case["name"]
        assert row[22].startswith(
            "note-atom-" if case.get("parser") == "note" else "table-"
        )


def test_entity_transaction_matrix_propagates_bounded_row_context():
    case = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "typed_fact_row_context_matrix.json"
        ).read_text(encoding="utf-8")
    )
    rows = build_fact_rows(
        attach_context(case["markdown"]),
        company="Công ty kiểm thử",
        source="generic_related_party_fixture.md",
        fiscal_year=case["fiscal_year"],
    )
    numeric_rows = [row for row in rows if row[16] == "amount"]

    assert len(numeric_rows) == 12
    assert len({(row[22], row[14], row[15], row[17]) for row in numeric_rows}) == 12

    by_transaction: dict[str, list[tuple]] = {}
    for expected in case["expected_numeric_facts"]:
        selected = [
            row
            for row in numeric_rows
            if row[24] == expected["metric_label"]
            and row[27] == expected["counterparty"]
        ]
        assert len(selected) == 2, expected
        assert {row[17] for row in selected} == set(expected["values"])
        assert {row[14] for row in selected} == {expected["counterparty"]}
        assert {row[25] for row in selected} == {expected["counterparty"]}
        assert {row[28] for row in selected} == {
            expected["transaction_type"]
        }
        assert all(
            f"Mối quan hệ: {expected['relationship']}" in row[6]
            and expected["metric_label"] in row[6]
            for row in selected
        )
        if expected["counterparty"] == "Công ty Cổ phần Alpha":
            by_transaction[expected["transaction_type"]] = selected

    query_by_transaction = {
        "purchase": (
            "Mua hàng hóa và dịch vụ từ Công ty Cổ phần Alpha "
            "là bao nhiêu?"
        ),
        "sale": (
            "Bán tài sản cố định cho Công ty Cổ phần Alpha "
            "là bao nhiêu?"
        ),
        "dividend": (
            "Cổ tức được chia từ Công ty Cổ phần Alpha "
            "là bao nhiêu?"
        ),
        "other_income": (
            "Thu nhập khác từ Công ty Cổ phần Alpha "
            "là bao nhiêu?"
        ),
        "sales_support": (
            "Hỗ trợ bán hàng cho Công ty Cổ phần Alpha "
            "là bao nhiêu?"
        ),
    }
    alpha_rows = [
        row
        for row in numeric_rows
        if row[27] == "Công ty Cổ phần Alpha"
    ]
    for transaction_type, query in query_by_transaction.items():
        slots = parse_query_slots(query)
        matched = []
        for row in alpha_rows:
            meta = {
                "item_name": row[6],
                "period": row[11],
                "value_type": row[12],
                "aggregation_level": row[20],
                "row_label": row[14],
                "column_label": row[15],
                "metric_label": row[24],
                "entity_label": row[25],
                "scope_label": row[26],
                "counterparty": row[27],
                "transaction_type": row[28],
            }
            if fact_matches_required_slots(slots, meta):
                matched.append(row)
        assert matched == by_transaction[transaction_type]


def test_sales_expense_is_not_misclassified_as_sale_transaction():
    assert parse_query_slots(
        "Chi phí bán hàng năm hiện tại là bao nhiêu?"
    ).transaction_type == ""


def test_entity_transaction_context_resets_at_group_and_total_boundaries():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

### 30. Giao dịch với các bên liên quan

| Bên liên quan | Mối quan hệ | Loại giao dịch | 2025 VND |
| --- | --- | --- | ---: |
| Công ty Cổ phần Alpha | Công ty liên kết | Mua hàng hóa | 100 |
| Nhóm giao dịch khác |  |  |  |
|  |  | Bán tài sản cố định | 12 |
| Công ty TNHH Beta | Công ty con | Mua nguyên vật liệu | 20 |
| Tổng cộng |  | Cổ tức được chia | 7 |
"""
    numeric_rows = [
        row for row in _facts(markdown) if row[16] == "amount"
    ]
    by_value = {row[17]: row for row in numeric_rows}

    assert by_value["100"][27] == "Công ty Cổ phần Alpha"
    assert by_value["12"][24] == "Bán tài sản cố định"
    assert by_value["12"][25] == ""
    assert by_value["12"][27] == ""
    assert by_value["20"][27] == "Công ty TNHH Beta"
    assert by_value["7"][25] == ""
    assert by_value["7"][27] == ""


def test_multi_level_group_context_propagates_without_flattening_siblings():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

### 20. Phân tích doanh thu

| Chỉ tiêu | Năm 2025 | Năm 2024 |
| --- | ---: | ---: |
| 1. Doanh thu |  |  |
| 1.1 Theo khu vực |  |  |
| Sản phẩm sữa | 100 | 80 |
| 1.2 Theo kênh bán hàng |  |  |
| Siêu thị | 60 | 50 |
"""

    rows = _facts(markdown)
    product_rows = [row for row in rows if row[14] == "Sản phẩm sữa"]
    supermarket_rows = [row for row in rows if row[14] == "Siêu thị"]

    assert len(product_rows) == 2
    assert len(supermarket_rows) == 2
    assert all("Doanh thu > Theo khu vực" in row[21] for row in product_rows)
    assert all(
        "Doanh thu > Theo kênh bán hàng" in row[21]
        for row in supermarket_rows
    )
    assert not any("Theo khu vực" in row[21] for row in supermarket_rows)
    assert len({row[22] for row in product_rows + supermarket_rows}) == 1


def test_person_event_employee_and_policy_atoms_have_semantic_axes():
    cases = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "topic_atomization_cases.json"
        ).read_text(encoding="utf-8")
    )
    case = next(case for case in cases if case["name"].startswith("dpc_"))
    front_rows = build_frontmatter_rows(
        case["markdown"],
        "Công ty kiểm thử",
        case["source_format"],
        case["fiscal_year"],
        include_table_rows=False,
    )
    note_rows = build_note_rows(
        case["markdown"],
        "Công ty kiểm thử",
        case["source_format"],
        case["fiscal_year"],
        include_table_rows=False,
    )

    person_events = [
        row for row in front_rows if row[3] == "report_section_person_event"
    ]
    assert {
        (row[14], row[24], row[25])
        for row in person_events
    } == {
        (
            expected["topic"],
            expected["topic"],
            expected["entity_label"],
        )
        for expected in case["expected_person_events"]
    }
    assert any(
        row[24] == case["expected_person_metric"]
        for row in front_rows
    )
    person_event_dates = [
        row
        for row in front_rows
        if row[3] == "report_section_person_event_date"
    ]
    assert {
        (row[7], row[16], row[25])
        for row in person_event_dates
    } == {("15/03/2025", "date", "Nguyễn Thị Lan")}

    policy_rows = [row for row in note_rows if row[3] == "note_policy_atom"]
    assert set(case["expected_policy_topics"]).issubset(
        {row[24] for row in policy_rows}
    )
    assert all(row[26] == "Chính sách kế toán" for row in policy_rows)
    assert {row[25] for row in policy_rows} == {
        "Tài sản cố định",
        "Quyền sử dụng đất lâu dài",
    }


def test_registry_year_conflict_fails_before_build_and_metadata_is_fingerprinted():
    markdown = (
        "# BÁO CÁO TÀI CHÍNH\n"
        "Cho năm tài chính kết thúc ngày 31 tháng 12 năm 2025\n"
    )
    stale = SimpleNamespace(
        company="Công ty A",
        ticker="AAA",
        report_type="financial_statement",
        fiscal_year=2024,
        fiscal_quarter=None,
        scope="separate",
        audit_status="audited",
    )
    with pytest.raises(ValueError, match="fiscal_year conflicts"):
        _resolve_ingestion_metadata(stale, markdown)

    current = SimpleNamespace(**{**vars(stale), "fiscal_year": 2025})
    changed_company = SimpleNamespace(**{**vars(current), "company": "Công ty B"})
    first = _resolve_ingestion_metadata(current, markdown)
    second = _resolve_ingestion_metadata(changed_company, markdown)

    assert first["fiscal_year"] == "2025"
    assert first["source_fiscal_year"] == "2025"
    assert first["metadata_sha256"] != second["metadata_sha256"]


def test_source_metadata_is_canonicalized_and_registry_conflicts_fail_fast():
    markdown = (
        "# CÔNG TY CỔ PHẦN A\n"
        "# BÁO CÁO TÀI CHÍNH RIÊNG QUÝ IV/2025 (ĐÃ ĐƯỢC KIỂM TOÁN)\n"
        "**Mã chứng khoán**: AAA\n"
    )
    missing = SimpleNamespace(
        company="Công ty A",
        ticker="",
        report_type="financial_statement",
        fiscal_year=2025,
        fiscal_quarter=None,
        scope="unknown",
        audit_status="unknown",
    )
    resolved = _resolve_ingestion_metadata(missing, markdown)
    assert resolved["company"] == "Công ty cổ phần a"
    assert resolved["ticker"] == "AAA"
    assert resolved["fiscal_quarter"] == "4"
    assert resolved["scope"] == "separate"
    assert resolved["audit_status"] == "audited"

    wrong_scope = SimpleNamespace(**{**vars(missing), "scope": "consolidated"})
    with pytest.raises(ValueError, match="scope conflicts"):
        _resolve_ingestion_metadata(wrong_scope, markdown)

    wrong_audit = SimpleNamespace(**{**vars(missing), "audit_status": "reviewed"})
    with pytest.raises(ValueError, match="audit_status conflicts"):
        _resolve_ingestion_metadata(wrong_audit, markdown)

    wrong_company = SimpleNamespace(**{**vars(missing), "company": "Công ty B"})
    with pytest.raises(ValueError, match="company conflicts"):
        _resolve_ingestion_metadata(wrong_company, markdown)

    wrong_ticker = SimpleNamespace(**{**vars(missing), "ticker": "BBB"})
    with pytest.raises(ValueError, match="ticker conflicts"):
        _resolve_ingestion_metadata(wrong_ticker, markdown)


def test_invalid_typed_metadata_is_rejected_and_index_contract_is_versioned():
    row = list(
        _facts(
            """# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 5. Chi phí

| Chỉ tiêu | Năm 2025 |
| --- | ---: |
| Lãi vay | 40 |
"""
        )[0]
    )
    row[16] = "numeric-ish"

    with pytest.raises(ValueError, match="unsupported value_kind"):
        normalize_financial_fact_rows([tuple(row)])

    assert VECTOR_INDEX_SCHEMA_VERSION == 4
    assert (
        _collection_metadata()["vector_index_schema_version"]
        == VECTOR_INDEX_SCHEMA_VERSION
    )
