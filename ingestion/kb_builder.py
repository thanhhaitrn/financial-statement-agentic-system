"""Parse financial-statement markdown tables into normalized SQLite fact rows."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

import pandas as pd
from ingestion.table_parser import markdown_table_to_df
from ingestion.period_normalize import (
    canonical_period,
    canonical_value_type,
    column_period,
    parse_unit,
    period_phrase_alias,
    section_total_alias,
)
import re
from schemas.table_names import normalize_table_heading, TABLE_NOTE

LABEL_PREFIX = re.compile(
    r"""
    ^\s*(
        \d+(\.\d+)*\.?      |   # 1, 1.1, 1.2.3
        [IVXLC]+(\.)?   |   # I, II, III.
        [A-Z]\.         |   # A.
    )\s+
    """,
    re.VERBOSE | re.IGNORECASE
)
_MARKDOWN_EMPHASIS_RE = re.compile(r"(\\\*\\\*|\\\*|\*\*|\*|__|_)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Section labels used inside accounting matrices (e.g. the fixed-asset schedule
# splits into Nguyên giá / Giá trị hao mòn / Giá trị còn lại). They appear either
# as the first-column header or as in-table divider rows.
_SECTION_LABELS = {
    "nguyên giá",
    "giá trị hao mòn",
    "giá trị hao mòn lũy kế",
    "giá trị còn lại",
}
# Un-numbered breakdown rows that belong to the most recent numbered parent line
# (e.g. "Nguyên giá"/"Giá trị hao mòn lũy kế" under "1. Tài sản cố định hữu hình").
_BREAKDOWN_SUBLABELS = _SECTION_LABELS | {"dự phòng"}
# Period divider rows in two-period reconciliation tables (e.g. the equity
# movement schedule "19a"): they carry no values and split the table into a
# prior-period and current-period block. Folding them into the subheading keeps
# the otherwise-identical rows ("Số dư cuối kỳ | Cộng") unambiguous.
_PERIOD_DIVIDER_LABELS = {
    "kỳ trước",
    "kỳ này",
    "kỳ hiện tại",
    "năm trước",
    "năm nay",
    "năm hiện tại",
}


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_inline_formatting(text).lower()).strip()


def _row_has_value(row, columns) -> bool:
    """True if any non-ignored cell in the row looks like a numeric value."""
    for col_name, cell in zip(columns, row.values):
        if _is_ignored_column(col_name):
            continue
        if cell and looks_like_value(cell):
            return True
    return False


def _strip_inline_formatting(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""

    value = _HTML_TAG_RE.sub(" ", value)
    value = _MARKDOWN_EMPHASIS_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_value_text(text: str) -> str:
    value = _strip_inline_formatting(text)
    if not value:
        return ""

    compact = value.replace(",", "").replace(".", "").replace(" ", "")
    if compact.startswith("(") and compact.endswith(")") and compact[1:-1].isdigit():
        inner = value[1:-1].strip()
        if inner:
            return f"-{inner}"

    return value


def _normalize_column_name(text: str) -> str:
    value = _strip_inline_formatting(text).lower()
    return re.sub(r"\s+", " ", value).strip()


def _display_column_name(text: str) -> str:
    # Join <br/>-wrapped header parts with a space instead of dropping everything
    # after the first <br/>. Note tables encode the value-type on a second line,
    # e.g. "Số cuối kỳ<br/>Giá gốc" / "Số cuối kỳ<br/>Dự phòng" — truncating loses
    # the giá gốc vs dự phòng distinction (both collapse to "Số cuối kỳ").
    raw = str(text or "").strip()
    joined = re.sub(r"<br\s*/?>", " ", raw, flags=re.IGNORECASE)
    return _strip_inline_formatting(joined)


def _is_item_code_column(col_name: str) -> bool:
    normalized = _normalize_column_name(col_name)
    return "mã số" in normalized or "ma so" in normalized


def _is_note_column(col_name: str) -> bool:
    normalized = _normalize_column_name(col_name)
    return "thuyết minh" in normalized or "thuyet minh" in normalized


def _is_ignored_column(col_name: str) -> bool:
    return _is_item_code_column(col_name) or _is_note_column(col_name)


def _item_code_for_row(row, columns) -> str | None:
    for col_name in columns:
        if not _is_item_code_column(col_name):
            continue
        value = str(row.get(col_name, "") or "").strip()
        return value or None
    return None


def _note_ref_for_row(row, columns) -> str | None:
    for col_name in columns:
        if not _is_note_column(col_name):
            continue
        value = _strip_inline_formatting(str(row.get(col_name, "") or ""))
        return value or None
    return None


def looks_like_value(x: str) -> bool:
    x = _normalize_value_text(x)

    if not x:
        return False

    # If it starts like "1. Tiền", "2.1 Nợ", etc → LABEL
    if LABEL_PREFIX.match(x):
        return False

    # Remove common formatting
    cleaned = x.replace(",", "").replace(".", "").replace(" ", "")

    # Accounting negative: (12345)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]

    if cleaned.startswith("-"):
        cleaned = cleaned[1:]

    # Pure number
    if cleaned.isdigit():
        return True

    # Date-like
    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", x):
        return False

    return False

def clean_label(text: str) -> str:
    return _strip_inline_formatting(LABEL_PREFIX.sub("", text)).strip()

def _resolve_heading(heading, section) -> tuple[str, str]:
    """Resolve a table's heading + subheading, folding note-section schedules.

    Sub-tables under "Thuyết minh báo cáo tài chính" carry ad-hoc titles (e.g.
    "18a. Vay ngắn hạn") as their heading, which makes them unreachable by the
    note-scoped retrieval tool. When the table is inside the notes section we
    canonicalise the heading to TABLE_NOTE and keep the schedule title as the
    subheading so it stays searchable and is correctly scoped to notes.
    """
    original_heading = _strip_inline_formatting(heading)
    local = normalize_table_heading(clean_label(heading))
    if normalize_table_heading(section) == TABLE_NOTE and local != TABLE_NOTE:
        return TABLE_NOTE, original_heading
    return local, ""


def _compose_subheading(*parts) -> str:
    return " — ".join(part for part in parts if str(part or "").strip())


def df_to_facts(df, heading, company, source, fiscal_year=None, section="", section_note_ref="", section_note_title=""):
    facts = []
    fact_heading, base_subheading = _resolve_heading(heading, section)

    # A descriptive schedule sub-heading ("Là chương trình phần mềm, chi tiết
    # như sau:") replaces the numbered note title, and with it the line-item
    # tokens retrieval matches on ("tài sản cố định vô hình"). Re-anchor the
    # block to its schedule by prefixing the numbered title — but only for
    # continuation-style headings (": " tail, "Là …", "Tại ngày …"); a bare
    # line-item heading names its own schedule and must not inherit a stale one.
    note_title = str(section_note_title or "").strip()
    subheading_text = str(base_subheading or "").strip()
    is_continuation = subheading_text.endswith(":") or subheading_text.lower().startswith(
        ("là ", "tại ngày ")
    )
    if (
        fact_heading == TABLE_NOTE
        and note_title
        and is_continuation
        and note_title.lower() not in subheading_text.lower()
    ):
        base_subheading = _compose_subheading(note_title, base_subheading)

    df = df.map(lambda x: "" if pd.isna(x) else str(x).strip())
    columns = [str(c).strip() for c in df.columns]

    # Value columns (exclude Mã số / Thuyết minh) carry the period & unit; resolve
    # each column's canonical period once with full-table context (year ties).
    value_cols = [c for c in columns if not _is_ignored_column(c)]
    col_period = {c: column_period(c, value_cols) for c in value_cols}
    col_unit = {c: parse_unit(c) for c in value_cols}

    # Row-grouped accounting matrix: the first column *header* is a section label
    # (e.g. "Nguyên giá") and later sections appear as divider rows. The column
    # name carries the section for the first block.
    matrix_mode = bool(columns) and _norm_label(columns[0]) in _SECTION_LABELS
    current_section = columns[0].strip() if matrix_mode else ""
    current_parent = ""
    current_period = ""

    for _, row in df.iterrows():
        row_label = None
        raw_label_cell = ""
        item_code = _item_code_for_row(row, columns)
        note_ref = _note_ref_for_row(row, columns)
        # Note schedules carry no per-row note_ref column; inherit the schedule's
        # 'V.<n>' ref (from its numbered heading) so the row links back to the
        # primary-statement line that references it.
        if not note_ref and fact_heading == TABLE_NOTE:
            note_ref = section_note_ref

        # find row label
        for col_name, cell in zip(columns, row.values):
            if _is_ignored_column(col_name):
                continue
            if cell and not looks_like_value(cell):
                raw_label_cell = cell
                row_label = clean_label(cell)
                break

        if not row_label:
            continue

        row_norm = _norm_label(row_label)

        # In a matrix, a section label row (e.g. "Giá trị hao mòn") switches the
        # active section; such divider rows carry no values and yield no facts.
        if matrix_mode and row_norm in _SECTION_LABELS:
            current_section = row_label
            continue

        # Period divider row (e.g. "Kỳ trước"/"Kỳ này") in a two-period table:
        # no values, splits prior- vs current-period blocks. Latch it so the
        # otherwise-identical rows below get distinct subheadings; emit no fact.
        if row_norm in _PERIOD_DIVIDER_LABELS and not _row_has_value(row, columns):
            current_period = row_label
            continue

        # Restore hierarchical context into the subheading so flattened cells
        # (e.g. "Số cuối kỳ | Cộng", "Nguyên giá | Số đầu năm") are unambiguous.
        if matrix_mode:
            row_subheading = _compose_subheading(base_subheading, current_section)
        elif LABEL_PREFIX.match(_strip_inline_formatting(raw_label_cell)):
            current_parent = row_label
            row_subheading = base_subheading
        elif current_parent and row_norm in _BREAKDOWN_SUBLABELS:
            row_subheading = _compose_subheading(base_subheading, current_parent)
        else:
            row_subheading = base_subheading

        # Fold the active period into the subheading so duplicate line items
        # across the two periods (prior/current) stay distinguishable.
        if current_period:
            row_subheading = _compose_subheading(row_subheading, current_period)

        # Section totals are labelled "A - TÀI SẢN NGẮN HẠN" / "C - NỢ PHẢI TRẢ" /
        # "I. Nợ ngắn hạn"…; fold the readable "Tổng …" alias into the subheading so
        # dense + lexical retrieval and the answer model see them as the "tổng …"
        # the question asks for.
        sec_alias = section_total_alias(row_label)
        if sec_alias:
            row_subheading = _compose_subheading(row_subheading, sec_alias)

        # "trong năm" line items ≡ "trong kỳ" in a periodic report; fold the
        # "trong kỳ" variant into the subheading so questions phrased "trong kỳ"
        # match the indexed "trong năm" label (e.g. lưu chuyển tiền thuần).
        ky_alias = period_phrase_alias(row_label)
        if ky_alias:
            row_subheading = _compose_subheading(row_subheading, ky_alias)

        # create facts
        for col_name, cell in zip(columns, row.values):
                if _is_ignored_column(col_name):
                    continue
                if not cell:
                    continue
                if cell == row_label:
                    continue
                if not looks_like_value(cell):
                    continue

                raw_value = _strip_inline_formatting(cell)
                normalized_value = _normalize_value_text(cell)

                facts.append({
                    "company": company,
                    "fiscal_year": "" if fiscal_year is None else str(fiscal_year),
                    "heading": fact_heading,
                    "item_code": item_code,
                    "note_ref": note_ref,
                    "subheading": row_subheading,
                    "item_name": f"{row_label} | {_display_column_name(col_name)}",
                    # First-class disambiguators for value-lookup rows (else they
                    # only differ inside item_name/subheading strings).
                    "period": col_period.get(col_name, "") or canonical_period(current_period),
                    # Value type sits in the matrix section header, the row label
                    # ("Nguyên giá | Số cuối năm"), OR the note column ("Số cuối kỳ
                    # <br/>Giá gốc"/"...Dự phòng"); try all three.
                    "value_type": (
                        canonical_value_type(current_section)
                        or canonical_value_type(row_label)
                        or canonical_value_type(_display_column_name(col_name))
                    ),
                    "unit": col_unit.get(col_name, ""),
                    "value": normalized_value,
                    "raw_value": raw_value,
                    "normalized_value": normalized_value,
                    "source": source
                })

    return facts

def build_fact_rows(tables_with_context, company, source, fiscal_year=None):
    rows = []

    for block in tables_with_context:
        df = markdown_table_to_df(block["table"])

        if df is None or df.empty:
            continue

        facts = df_to_facts(
            df,
            heading=block["heading"],
            company=company,
            source=source,
            fiscal_year=fiscal_year,
            section=block.get("section", ""),
            section_note_ref=block.get("note_ref", ""),
            section_note_title=block.get("note_title", ""),
        )

        for f in facts:
            if not f.get("item_name") or not f.get("value"):
                continue

            # 14-tuple in the canonical column order (legacy first 11 + the slot
            # fields period/value_type/unit), matching _normalize_fact_row.
            rows.append((
                f["company"],
                f.get("fiscal_year", ""),
                f["heading"],
                f.get("item_code"),
                f.get("note_ref", ""),
                f.get("subheading", ""),
                f["item_name"],
                f["value"],
                f.get("raw_value", ""),
                f.get("normalized_value", ""),
                f["source"],
                f.get("period", ""),
                f.get("value_type", ""),
                f.get("unit", ""),
            ))

    return rows

    




    
