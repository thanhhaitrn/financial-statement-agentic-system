"""Utilities for turning markdown tables into cleaned pandas DataFrames."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

import pandas as pd
import re

from ingestion.period_normalize import parse_unit
from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    normalize_table_heading,
)


_SPACE_RE = re.compile(r"\s+")
# The canonical statement sections; a heading that resolves to one of these
# starts a new section context for the tables that follow it.
_SECTION_HEADINGS = {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE, TABLE_REPORT_SECTION}
_NOTE_HEADING_PREFIXES = (
    "bản thuyết minh",
    "ban thuyet minh",
    "thuyết minh báo cáo tài chính",
    "thuyet minh bao cao tai chinh",
    "thuyết minh bctc",
    "thuyet minh bctc",
)
_SIGNATURE_HEADING_RE = re.compile(
    r"^(người lập|người soát xét|người duyệt)\s*:$",
    flags=re.IGNORECASE,
)
_ALIGNMENT_CELL_RE = re.compile(r"^:?-{1,}:?$")
_UNIT_CAPTION_RE = re.compile(
    r"^(?:đơn vị(?:\s+tính)?|don vi(?:\s+tinh)?|unit)\s*:",
    flags=re.IGNORECASE,
)


def _normalize_heading_line(line: str) -> str:
    text = str(line or "").strip().replace("#", "").replace("*", "")
    return _SPACE_RE.sub(" ", text).strip()


def is_heading(line: str) -> bool:
    raw_line = str(line or "").strip()
    normalized_line = _normalize_heading_line(line)
    lowered = normalized_line.lower()
    if not normalized_line:
        return False

    return (
        raw_line.startswith("#")
        or lowered.startswith("bảng")
        or lowered.startswith("báo cáo")
        or lowered.startswith(_NOTE_HEADING_PREFIXES)
        or (normalized_line.endswith(":") and not _SIGNATURE_HEADING_RE.match(lowered))
    )


def _is_note_section_start(raw_line: str, heading: str) -> bool:
    lowered = _normalize_heading_line(heading).lower()
    return (
        str(raw_line or "").strip().startswith("#")
        or lowered.startswith(_NOTE_HEADING_PREFIXES)
    )

def attach_context(md_text: str) -> list[dict]:
    # Local import avoids a module cycle (note_parser -> kb_builder -> table_parser).
    from ingestion.note_parser import note_schedule_ref, note_schedule_title

    tables_with_context = []
    current_table = []
    current_heading = None
    current_section = ""
    in_note_section = False
    # The 'V.<n>' reference and numbered title of the current note schedule,
    # latched from its numbered heading (e.g. "### 10. Tài sản cố định vô hình")
    # so descriptive sub-headings that follow (e.g. "Là chương trình phần mềm,
    # chi tiết như sau:") — which overwrite current_heading — do not lose the
    # schedule link nor the line-item tokens retrieval needs.
    current_note_ref = ""
    current_note_title = ""
    current_unit = ""

    for line in md_text.splitlines():
        if not line.strip().startswith("|") and current_table:
            tables_with_context.append({
                    "heading": current_heading,
                    "section": current_section,
                    "note_ref": current_note_ref,
                    "note_title": current_note_title,
                    "unit": current_unit,
                    "table": current_table
                })
            current_table = []

        normalized_line = _normalize_heading_line(line)
        if _UNIT_CAPTION_RE.match(normalized_line):
            parsed_unit = parse_unit(normalized_line)
            if parsed_unit:
                current_unit = parsed_unit

        if is_heading(line):
            current_heading = _normalize_heading_line(line)
            # Latch the active statement section so sub-tables that follow a
            # "Thuyết minh báo cáo tài chính" heading are tagged as notes even
            # when their own heading is an ad-hoc schedule title (e.g. "18a.
            # Vay ngắn hạn"). Orphan headings leave the section unchanged.
            canonical = normalize_table_heading(current_heading)
            if canonical in _SECTION_HEADINGS:
                if canonical == TABLE_NOTE and _is_note_section_start(line, current_heading):
                    current_section = TABLE_NOTE
                    in_note_section = True
                elif not in_note_section:
                    current_section = canonical
                    current_note_ref = ""
                    current_note_title = ""
            # Inside the notes, a numbered schedule heading refreshes the link;
            # descriptive headings return "" and keep the previous schedule ref.
            if in_note_section:
                ref = note_schedule_ref(current_heading)
                if ref:
                    current_note_ref = ref
                    current_note_title = note_schedule_title(current_heading)
        elif in_note_section and not line.strip().startswith("|") and len(line.strip()) <= 100:
            # Schedule titles can also appear as plain/bold lines that is_heading
            # misses ("**10. Tài sản cố định vô hình**"); without this latch the
            # following descriptive heading ("Là chương trình phần mềm…:") keeps
            # the PREVIOUS schedule's ref/title (V.9 leaking onto the intangibles
            # matrix). Length-guarded so numbered prose sentences don't match.
            ref = note_schedule_ref(line)
            if ref:
                current_note_ref = ref
                current_note_title = note_schedule_title(line)

        if line.strip().startswith("|"):
            current_table.append(line)

    if current_table:
        tables_with_context.append({
            "heading": current_heading,
            "section": current_section,
            "note_ref": current_note_ref,
            "note_title": current_note_title,
            "unit": current_unit,
            "table": current_table
        })

    return tables_with_context


def _split_markdown_row(raw_line: str) -> tuple[list[str], bool, bool]:
    r"""Split one logical Markdown row without treating ``\|`` as a boundary.

    Markdown only gives the backslash special meaning when it precedes a pipe
    here.  Other escapes (``\*`` is common in converted reports) are retained
    verbatim.  An even run of backslashes does not escape the pipe; an odd run
    does, which avoids the usual ``str.split('|')`` ambiguity.
    """

    raw = str(raw_line or "").rstrip("\r\n")
    cells: list[str] = []
    current: list[str] = []
    delimiters: list[int] = []
    index = 0

    while index < len(raw):
        char = raw[index]
        if char == "\\":
            run_end = index
            while run_end < len(raw) and raw[run_end] == "\\":
                run_end += 1
            slash_count = run_end - index
            if run_end < len(raw) and raw[run_end] == "|":
                current.append("\\" * (slash_count // 2))
                if slash_count % 2:
                    current.append("|")
                else:
                    delimiters.append(run_end)
                    cells.append("".join(current))
                    current = []
                index = run_end + 1
                continue
            current.append("\\" * slash_count)
            index = run_end
            continue

        if char == "|":
            delimiters.append(index)
            cells.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1

    cells.append("".join(current))

    first_content = len(raw) - len(raw.lstrip())
    last_content = len(raw.rstrip()) - 1
    has_leading_boundary = bool(delimiters and delimiters[0] == first_content)
    has_trailing_boundary = bool(delimiters and delimiters[-1] == last_content)
    if has_leading_boundary:
        cells = cells[1:]
    if has_trailing_boundary:
        cells = cells[:-1]

    return [cell.strip() for cell in cells], has_leading_boundary, has_trailing_boundary


def _is_alignment_row(cells: list[str]) -> bool:
    meaningful = [str(cell or "").strip() for cell in cells]
    return bool(meaningful) and all(
        cell and _ALIGNMENT_CELL_RE.fullmatch(cell)
        for cell in meaningful
    )


def _unique_columns(columns: list[str]) -> list[str]:
    """Return deterministic column labels while preserving column positions."""

    used: set[str] = set()
    result: list[str] = []
    for raw_column in columns:
        column = str(raw_column or "").strip()
        candidate = column
        suffix = 1
        while candidate in used:
            candidate = f"{column}.{suffix}" if column else f".{suffix}"
            suffix += 1
        used.add(candidate)
        result.append(candidate)
    return result


def _physical_table_lines(table_lines: list[str]) -> list[tuple[int, str]]:
    """Expand entries containing embedded newlines and retain source positions."""

    physical: list[tuple[int, str]] = []
    line_number = 0
    for entry in table_lines or []:
        expanded = str(entry or "").splitlines() or [""]
        for line in expanded:
            line_number += 1
            physical.append((line_number, line))
    return physical


def _logical_table_rows(table_lines: list[str]) -> list[tuple[list[str], int, int]]:
    """Parse physical lines, joining only structurally incomplete wrapped rows."""

    physical = _physical_table_lines(table_lines)
    if not physical:
        return []

    rows: list[tuple[list[str], int, int]] = []
    header_line, header_raw = physical[0]
    header_cells, _, _ = _split_markdown_row(header_raw)
    if not header_cells or not any(header_cells):
        return []
    rows.append((header_cells, header_line, header_line))
    expected_width = len(header_cells)

    pending_raw = ""
    pending_start = 0
    pending_end = 0

    def flush_pending() -> None:
        nonlocal pending_raw, pending_start, pending_end
        if pending_raw:
            cells, _, _ = _split_markdown_row(pending_raw)
            rows.append((cells, pending_start, pending_end))
        pending_raw = ""
        pending_start = 0
        pending_end = 0

    for line_number, raw_line in physical[1:]:
        if not raw_line.strip():
            if pending_raw:
                pending_raw += "\n"
                pending_end = line_number
            continue

        cells, has_leading_boundary, has_trailing_boundary = _split_markdown_row(raw_line)

        if pending_raw:
            # A leading boundary unambiguously opens a new row.  Finalize the
            # incomplete row as ragged instead of accidentally consuming it.
            if has_leading_boundary:
                flush_pending()
            else:
                pending_raw += f"\n{raw_line}"
                pending_end = line_number
                joined_cells, _, joined_trailing = _split_markdown_row(pending_raw)
                if len(joined_cells) >= expected_width or joined_trailing:
                    flush_pending()
                continue

        # A short row without a closing boundary can be an OCR/text-conversion
        # wrap.  Delay it until a non-leading continuation supplies the missing
        # separators.  Complete rows and explicitly closed ragged rows are final.
        if len(cells) < expected_width and not has_trailing_boundary:
            pending_raw = raw_line
            pending_start = line_number
            pending_end = line_number
        else:
            rows.append((cells, line_number, line_number))

    flush_pending()
    return rows


def markdown_table_to_df(table_lines: list[str]) -> pd.DataFrame:
    """Convert a Markdown table into a positionally stable DataFrame.

    Short rows are padded on the right, so a missing trailing value cannot move
    another value into a different semantic column.  For over-wide rows, the
    overflow is retained in the final cell rather than discarded or shifted.
    Repairs are exposed through ``df.attrs['markdown_parser_warnings']`` for
    ingestion diagnostics without making imperfect source reports unreadable.
    """

    logical_rows = _logical_table_rows(table_lines)
    if not logical_rows:
        return pd.DataFrame()

    header, _, _ = logical_rows[0]
    columns = _unique_columns(header)
    expected_width = len(columns)
    values: list[list[object]] = []
    parser_warnings: list[dict[str, object]] = []

    for cells, start_line, end_line in logical_rows[1:]:
        if _is_alignment_row(cells):
            continue
        actual_width = len(cells)
        if actual_width < expected_width:
            parser_warnings.append({
                "kind": "short_row_padded",
                "start_line": start_line,
                "end_line": end_line,
                "expected_cells": expected_width,
                "actual_cells": actual_width,
            })
            row: list[object] = [*cells, *([None] * (expected_width - actual_width))]
        elif actual_width > expected_width:
            parser_warnings.append({
                "kind": "wide_row_merged",
                "start_line": start_line,
                "end_line": end_line,
                "expected_cells": expected_width,
                "actual_cells": actual_width,
            })
            row = [*cells[: expected_width - 1], " | ".join(cells[expected_width - 1 :])]
        else:
            row = list(cells)
        values.append(row)

    df = pd.DataFrame(values, columns=columns)
    df = df.map(lambda value: value.strip() if isinstance(value, str) else value)
    df.attrs["markdown_parser_warnings"] = parser_warnings
    return df
