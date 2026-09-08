"""Contracts for conservative person-role and dated corporate-event atoms."""

from __future__ import annotations

import json
from pathlib import Path

from ingestion.frontmatter_parser import build_frontmatter_rows
from ingestion.note_parser import build_note_rows
from ingestion.topic_atoms import derive_topic_atoms
from tools.query_routing import (
    fact_matches_required_slots,
    parse_query_slots,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "person_role_atomization_cases.json"


def test_explicit_person_role_formats_emit_one_typed_atom_per_person():
    cases = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    for case in cases:
        atoms = [
            atom
            for atom in derive_topic_atoms(case["text"])
            if atom.atom_type == "person_role"
        ]
        assert [
            {
                "entity_label": atom.entity_label,
                "metric_label": atom.metric_label,
            }
            for atom in atoms
        ] == case["expected"], case["name"]
        assert all(atom.topic == "Vai trò nhân sự" for atom in atoms)

    prose_atoms = derive_topic_atoms(cases[1]["text"])
    appointment_events = [
        atom
        for atom in prose_atoms
        if atom.atom_type == "person_event" and atom.entity_label == "Nguyễn Thị Lan"
    ]
    assert len(appointment_events) == 1
    assert "15/03/2025" in appointment_events[0].value
    role_atoms = [atom for atom in prose_atoms if atom.atom_type == "person_role"]
    assert "15/03/2025" in role_atoms[0].value
    assert "01/04/2025" in role_atoms[1].value


def test_person_role_item_codes_scope_and_exact_metadata_match():
    markdown = """\
# CÔNG TY CỔ PHẦN KIỂM THỬ
# BÁO CÁO CỦA BAN TỔNG GIÁM ĐỐC

## Ban Điều hành
- Bà Mai Kiều Liên: Tổng Giám đốc
- Bà Bùi Thị Hương: Giám đốc Điều hành – Đối Ngoại - Truyền Thông và Hành chính Tổng hợp

# BÁO CÁO KIỂM TOÁN ĐỘC LẬP
## Kiểm toán viên
Ông Trần Văn Bình: Kiểm toán viên

# BẢNG CÂN ĐỐI KẾ TOÁN
"""
    rows = build_frontmatter_rows(
        markdown,
        "Công ty kiểm thử",
        "front.md",
        "2025",
        include_table_rows=False,
    )
    role_rows = [row for row in rows if row[3] == "report_section_person_role"]
    assert {(row[25], row[24]) for row in role_rows} == {
        ("Mai Kiều Liên", "Tổng Giám đốc"),
        (
            "Bùi Thị Hương",
            "Giám đốc Điều hành – Đối Ngoại - Truyền Thông và Hành chính Tổng hợp",
        ),
        ("Trần Văn Bình", "Kiểm toán viên"),
    }

    audit_role = next(row for row in role_rows if row[24] == "Kiểm toán viên")
    assert audit_role[26] == "Kiểm toán viên"
    assert "Kiểm toán viên" in audit_role[21]
    assert audit_role[26] != "Ban Điều hành"

    chief_executive = next(row for row in role_rows if row[24] == "Tổng Giám đốc")
    slots = parse_query_slots("Ai là Tổng Giám đốc?")
    assert slots.metric == "tổng giám đốc"
    assert slots.aggregation == ""
    assert fact_matches_required_slots(
        slots,
        {
            "item_name": chief_executive[6],
            "row_label": chief_executive[14],
            "column_label": chief_executive[15],
            "aggregation_level": chief_executive[20],
            "section_path": chief_executive[21],
            "metric_label": chief_executive[24],
            "entity_label": chief_executive[25],
            "scope_label": chief_executive[26],
        },
        chief_executive[7],
    )


def test_note_parser_wires_person_role_item_code():
    markdown = """\
# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 1. Thông tin chung

Ông Nguyễn Văn An giữ chức Tổng Giám đốc từ ngày 01/01/2025.
"""
    rows = build_note_rows(
        markdown,
        "Công ty kiểm thử",
        "note.md",
        "2025",
        include_table_rows=False,
    )
    role = next(row for row in rows if row[3] == "note_person_role")
    assert role[24] == "Tổng Giám đốc"
    assert role[25] == "Nguyễn Văn An"
    assert role[26] == "Thông tin chung"


def test_policy_atoms_bind_specific_asset_heading_and_are_self_contained():
    markdown = """\
# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 7. Tài sản cố định vô hình

**(a) Quyền sử dụng đất**

Quyền sử dụng đất lâu dài không tính khấu hao.

**(b) Phần mềm máy vi tính**

Phần mềm máy vi tính được tính khấu hao theo phương pháp đường thẳng trong thời gian từ 2 - 8 năm.

**(c) Nhãn hiệu**

Khấu hao được tính theo phương pháp đường thẳng trong vòng 3 năm.
"""
    rows = build_note_rows(
        markdown,
        "Công ty kiểm thử",
        "vnm-policy.md",
        "2025",
        include_table_rows=False,
    )
    policies = [row for row in rows if row[3] == "note_policy_atom"]

    land = next(row for row in policies if row[31] == "non_depreciation")
    assert land[25].casefold() == "quyền sử dụng đất lâu dài"
    assert "quyền sử dụng đất" in land[7].casefold()

    software = [
        row
        for row in policies
        if row[25].casefold() == "phần mềm máy vi tính"
    ]
    assert {row[31] for row in software} == {
        "depreciation_method",
        "depreciation_period",
    }
    assert all("phần mềm máy vi tính" in row[7].casefold() for row in software)

    trademark = [
        row for row in policies if row[25].casefold() == "nhãn hiệu"
    ]
    assert {row[31] for row in trademark} == {
        "depreciation_method",
        "depreciation_period",
    }
    assert all(row[7].casefold().startswith("nhãn hiệu.") for row in trademark)
    assert all("c) nhãn hiệu" in row[21].casefold() for row in trademark)


def test_land_use_right_policy_entity_preserves_term_modifier():
    common_policy = "Quyền sử dụng đất được ghi nhận theo giá gốc."
    long_term = next(
        atom
        for atom in derive_topic_atoms(
            common_policy,
            section="Tài sản cố định vô hình",
            subsection="Quyền sử dụng đất lâu dài",
        )
        if atom.atom_type == "policy"
    )
    fixed_term = next(
        atom
        for atom in derive_topic_atoms(
            common_policy,
            section="Tài sản cố định vô hình",
            subsection="Quyền sử dụng đất có thời hạn",
        )
        if atom.atom_type == "policy"
    )

    assert {
        long_term.entity_label.casefold(),
        fixed_term.entity_label.casefold(),
    } == {
        "quyền sử dụng đất lâu dài",
        "quyền sử dụng đất có thời hạn",
    }
    assert long_term.value.casefold().startswith("quyền sử dụng đất lâu dài.")
    assert fixed_term.value.casefold().startswith(
        "quyền sử dụng đất có thời hạn."
    )
    assert (long_term.topic, long_term.value, long_term.entity_label) != (
        fixed_term.topic,
        fixed_term.value,
        fixed_term.entity_label,
    )


def test_person_event_requires_bound_person_and_emits_typed_date():
    text = (
        "Ngày 15/03/2025, Hội đồng quản trị bổ nhiệm "
        "Bà Nguyễn Thị Lan làm Tổng Giám đốc. "
        "Công ty chấm dứt hợp đồng lao động theo chính sách chung."
    )
    atoms = derive_topic_atoms(text)
    events = [atom for atom in atoms if atom.atom_type == "person_event"]
    dates = [atom for atom in atoms if atom.atom_type == "person_event_date"]

    assert [(atom.topic, atom.entity_label) for atom in events] == [
        ("Bổ nhiệm nhân sự", "Nguyễn Thị Lan")
    ]
    assert [(atom.value, atom.entity_label) for atom in dates] == [
        ("15/03/2025", "Nguyễn Thị Lan")
    ]

    markdown = f"""\
# BÁO CÁO CỦA BAN TỔNG GIÁM ĐỐC

## Thay đổi nhân sự
{text}

# BẢNG CÂN ĐỐI KẾ TOÁN
"""
    rows = build_frontmatter_rows(
        markdown,
        "Công ty kiểm thử",
        "person-front.md",
        "2025",
        include_table_rows=False,
    )
    event_rows = [
        row for row in rows if row[3] == "report_section_person_event"
    ]
    date_rows = [
        row for row in rows if row[3] == "report_section_person_event_date"
    ]
    assert len(event_rows) == 1
    assert len(date_rows) == 1
    assert date_rows[0][16] == "date"
    assert date_rows[0][17] == "15/03/2025"
    assert date_rows[0][25] == "Nguyễn Thị Lan"
    assert date_rows[0][10] == "person-front.md"


def test_corporatization_atom_accepts_both_vietnamese_accent_orders():
    for spelling in ("cổ phần hóa", "cổ phần hoá", "cổ phần hoa"):
        atoms = [
            atom
            for atom in derive_topic_atoms(
                f"Ngày 01/10/2003: Công ty được {spelling} theo "
                "Quyết định 155/2003/QĐ-BCN."
            )
            if atom.atom_type == "corporate_event"
        ]

        assert [atom.metric_label for atom in atoms] == [
            "Cổ phần hóa"
        ], spelling


def test_dated_corporate_events_keep_date_and_identifier_and_parser_codes():
    listing = (
        "Cổ phiếu của Công ty chính thức niêm yết trên HOSE "
        "ngày 19/01/2006 theo Quyết định số 42/QĐ-SGDCK."
    )
    atoms = [
        atom
        for atom in derive_topic_atoms(listing)
        if atom.atom_type == "corporate_event"
    ]
    assert len(atoms) == 1
    assert atoms[0].metric_label == "Niêm yết"
    assert "19/01/2006" in atoms[0].value
    assert "42/QĐ-SGDCK" in atoms[0].value
    typed_dates = [
        atom
        for atom in derive_topic_atoms(listing)
        if atom.atom_type == "corporate_event_date"
    ]
    typed_identifiers = [
        atom
        for atom in derive_topic_atoms(listing)
        if atom.atom_type == "corporate_event_identifier"
    ]
    assert [(atom.topic, atom.value) for atom in typed_dates] == [
        ("Ngày niêm yết", "19/01/2006")
    ]
    assert [(atom.topic, atom.value) for atom in typed_identifiers] == [
        ("Số quyết định", "42/QĐ-SGDCK")
    ]

    front_markdown = f"""\
# BÁO CÁO CỦA BAN TỔNG GIÁM ĐỐC

## Lịch sử hoạt động
{listing}

# BẢNG CÂN ĐỐI KẾ TOÁN
"""
    front_rows = build_frontmatter_rows(
        front_markdown,
        "Công ty kiểm thử",
        "front.md",
        "2025",
        include_table_rows=False,
    )
    front_event = next(
        row for row in front_rows if row[3] == "report_section_corporate_event"
    )
    assert front_event[24] == "Niêm yết"
    front_date = next(
        row
        for row in front_rows
        if row[3] == "report_section_corporate_event_date"
    )
    front_identifier = next(
        row
        for row in front_rows
        if row[3] == "report_section_corporate_event_identifier"
    )
    assert (front_date[7], front_date[16], front_date[17]) == (
        "19/01/2006",
        "date",
        "19/01/2006",
    )
    assert (front_identifier[7], front_identifier[16], front_identifier[17]) == (
        "42/QĐ-SGDCK",
        "identifier",
        "42/QĐ-SGDCK",
    )
    assert front_event[10] == front_date[10] == front_identifier[10] == "front.md"

    note_markdown = """\
# THUYẾT MINH BÁO CÁO TÀI CHÍNH

## 1. Thông tin chung

Công ty được thành lập ngày 02/02/2002 theo Giấy phép số 10/GP.
"""
    note_rows = build_note_rows(
        note_markdown,
        "Công ty kiểm thử",
        "note.md",
        "2025",
        include_table_rows=False,
    )
    note_event = next(row for row in note_rows if row[3] == "note_corporate_event")
    assert note_event[24] == "Thành lập doanh nghiệp"
    assert "10/GP" in note_event[7]
    note_date = next(
        row for row in note_rows if row[3] == "note_corporate_event_date"
    )
    note_identifier = next(
        row
        for row in note_rows
        if row[3] == "note_corporate_event_identifier"
    )
    assert (note_date[7], note_date[16], note_date[17]) == (
        "02/02/2002",
        "date",
        "02/02/2002",
    )
    assert (note_identifier[7], note_identifier[16], note_identifier[17]) == (
        "10/GP",
        "identifier",
        "10/GP",
    )
    assert note_event[10] == note_date[10] == note_identifier[10] == "note.md"
