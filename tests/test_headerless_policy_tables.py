import json
from pathlib import Path

from ingestion.kb_builder import build_fact_rows
from ingestion.table_parser import attach_context, markdown_table_to_df
from schemas.table_names import TABLE_NOTE
from tools.query_routing import (
    fact_matches_required_slots,
    parse_query_slots,
    route_candidates,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "headerless_policy_table_cases.json"
)
FACT_FIELDS = (
    "company",
    "fiscal_year",
    "heading",
    "item_code",
    "note_ref",
    "subheading",
    "item_name",
    "value",
    "raw_value",
    "normalized_value",
    "source",
    "period",
    "value_type",
    "unit",
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
)


def _fixture():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]


def test_headerless_bullet_table_recovers_first_row_positionally():
    case = _fixture()
    raw_table = [
        line
        for line in case["markdown"].splitlines()
        if line.strip().startswith("|")
    ]
    direct = markdown_table_to_df(raw_table)
    assert len(direct) == 4
    assert direct.iloc[0]["Đối tượng"] == case["expected_entities"][0]
    assert direct.attrs["markdown_parser_warnings"][0]["kind"] == (
        "headerless_data_header_recovered"
    )

    block = attach_context(case["markdown"])[0]

    assert block["parser_warnings"][0]["kind"] == (
        "headerless_data_header_recovered"
    )
    assert block["parser_warnings"][0]["recovered_rows"] == 4

    dataframe = markdown_table_to_df(block["table"])
    assert list(dataframe.columns) == [
        "Marker",
        "Đối tượng",
        "Thời gian hữu dụng",
    ]
    assert len(dataframe) == 4
    assert dataframe["Marker"].tolist() == ["", "", "", ""]
    assert dataframe["Đối tượng"].tolist() == case["expected_entities"]
    assert dataframe.iloc[0]["Thời gian hữu dụng"] == "5 – 50 năm"


def test_recovered_useful_life_rows_have_typed_axes_and_policy_topic():
    case = _fixture()
    rows = build_fact_rows(
        attach_context(case["markdown"]),
        company="Công ty kiểm thử",
        source="ocr_headerless_policy.md",
        fiscal_year=2025,
    )

    assert len(rows) == 4
    assert {row[14] for row in rows} == set(case["expected_entities"])
    selected = next(
        row for row in rows if row[14] == case["selected_entity"]
    )
    assert selected[8] == case["selected_raw_value"]
    assert selected[15] == "Thời gian hữu dụng"
    assert selected[24] == "Thời gian hữu dụng"
    assert selected[25] == case["selected_entity"]
    assert selected[31] == "depreciation_period"


def test_policy_duration_query_routes_and_strictly_matches_only_named_entity():
    case = _fixture()
    rows = build_fact_rows(
        attach_context(case["markdown"]),
        company="Công ty kiểm thử",
        source="ocr_headerless_policy.md",
        fiscal_year=2025,
    )
    facts = [dict(zip(FACT_FIELDS, row)) for row in rows]

    query = "Thời gian khấu hao máy móc và thiết bị là bao nhiêu?"
    slots = parse_query_slots(query)
    assert slots.entity == "may moc va thiet bi"
    assert slots.policy_topic == "depreciation_period"
    assert slots.value_type == ()
    assert route_candidates(query)[0].table == TABLE_NOTE

    matches = [
        fact
        for fact in facts
        if fact_matches_required_slots(slots, fact, fact["value"])
    ]
    assert [fact["entity_label"] for fact in matches] == [
        case["selected_entity"]
    ]


def test_depreciation_policy_word_does_not_weaken_numeric_value_type():
    policy_slots = parse_query_slots(
        "Thời gian khấu hao máy móc là bao nhiêu?"
    )
    balance_slots = parse_query_slots(
        "Số dư khấu hao lũy kế cuối năm là bao nhiêu?"
    )
    balance_fact = {
        "metric_label": "Khấu hao lũy kế",
        "item_name": "Khấu hao lũy kế | Số dư cuối năm",
        "period": "cuối",
        "value_type": "hao mòn",
        "aggregation_level": "component",
    }

    assert policy_slots.value_type == ()
    assert balance_slots.value_type == ("hao mòn",)
    assert fact_matches_required_slots(balance_slots, balance_fact, "100")


def test_governance_title_total_is_not_aggregate_but_total_assets_is():
    role_slots = parse_query_slots("Ai là Tổng Giám đốc?")
    role_fact = {
        "metric_label": "Tổng Giám đốc",
        "row_label": "Tổng Giám đốc",
        "entity_label": "Nguyễn Văn A",
        "aggregation_level": "component",
    }
    asset_slots = parse_query_slots("Tổng tài sản là bao nhiêu?")

    assert role_slots.aggregation == ""
    assert fact_matches_required_slots(role_slots, role_fact, "Nguyễn Văn A")
    assert asset_slots.aggregation == "total"
