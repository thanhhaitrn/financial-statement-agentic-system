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
_PAGE_MARKER_RE = re.compile(r"^-+\s*Page\s+(\d+)\s*$", flags=re.IGNORECASE)
_NOTE_CHAPTER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?P<chapter>[IVXLCDM]+)\.\s+",
    flags=re.IGNORECASE,
)
_CONTINUATION_RE = re.compile(
    r"(?:\(|\b)(?:tiếp\s+theo|tiep\s+theo)(?:\)|\b)",
    flags=re.IGNORECASE,
)
_LIST_MARKER_RE = re.compile(
    r"^(?:\\?[*+]|[-–—]|[•·▪◦‣])?$",
    flags=re.IGNORECASE,
)
_DATA_SCALAR_RE = re.compile(
    r"""
    ^\s*
    \(?
    [+-]?\d[\d.,\s]*
    \)?
    (?:
        \s*(?:[-–—]|đến|to)\s*
        \(?[+-]?\d[\d.,\s]*\)?
    )?
    \s*(?:%|phần\s+trăm|percent|năm|tháng|ngày|years?|months?|days?)?
    \s*$
    """,
    flags=re.IGNORECASE | re.VERBOSE,
)
_DATA_DATE_RE = re.compile(
    r"^(?:\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}|"
    r"(?:ngày\s+)?\d{1,2}\s+tháng\s+\d{1,2}\s+năm\s+(?:19|20)\d{2})$",
    flags=re.IGNORECASE,
)
_DURATION_VALUE_RE = re.compile(
    r"\b(?:năm|tháng|years?|months?)\s*$",
    flags=re.IGNORECASE,
)
_PERCENT_VALUE_RE = re.compile(
    r"(?:%|\bphần\s+trăm\b|\bpercent\b)",
    flags=re.IGNORECASE,
)
_USEFUL_LIFE_CONTEXT_RE = re.compile(
    r"(?:"
    r"\bkhấu\s+hao\b.{0,240}\bthời\s+gian\s+(?:sử\s+dụng\s+)?hữu\s+"
    r"(?:dụng|ích)\b|"
    r"\bthời\s+gian\s+(?:sử\s+dụng\s+)?hữu\s+(?:dụng|ích)\b"
    r".{0,240}\bkhấu\s+hao\b"
    r")",
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


def _is_primary_statement_start(raw_line: str, canonical: str) -> bool:
    """Distinguish a statement title from prose that merely names one.

    Note captions frequently end with phrases such as ``báo cáo tình hình tài
    chính riêng``.  The broad heading normalizer intentionally recognizes that
    phrase for retrieval, but it must not terminate the active notes section.
    """

    heading = _normalize_heading_line(raw_line).casefold()
    prefixes = {
        TABLE_BS: (
            "bảng cân đối kế toán",
            "báo cáo tình hình tài chính",
        ),
        TABLE_IS: (
            "báo cáo kết quả hoạt động kinh doanh",
            "kết quả hoạt động kinh doanh",
        ),
        TABLE_CF: (
            "báo cáo lưu chuyển tiền tệ",
            "lưu chuyển tiền tệ",
        ),
    }
    return any(
        heading.startswith(prefix)
        for prefix in prefixes.get(canonical, ())
    )


def _context_path(*parts: str) -> str:
    """Build a stable human-readable hierarchy without repeating ancestors."""

    result: list[str] = []
    seen: set[str] = set()
    for raw_part in parts:
        part = _normalize_heading_line(raw_part)
        if not part:
            continue
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
    return " > ".join(result)


def _table_width(table_lines: list[str]) -> int:
    if not table_lines:
        return 0
    cells, _, _ = _split_markdown_row(table_lines[0])
    return len(cells)


def _strip_cell_formatting(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^(?:\\?\*{1,2}|__)", "", text)
    text = re.sub(r"(?:\\?\*{1,2}|__)$", "", text)
    return _SPACE_RE.sub(" ", text).strip()


def _is_descriptive_cell(value: str) -> bool:
    text = _strip_cell_formatting(value)
    if not text or _DATA_SCALAR_RE.fullmatch(text) or _DATA_DATE_RE.fullmatch(text):
        return False
    return sum(character.isalpha() for character in text) >= 3


def _is_scalar_cell(value: str) -> bool:
    text = _strip_cell_formatting(value)
    return bool(
        text
        and (
            _DATA_SCALAR_RE.fullmatch(text)
            or _DATA_DATE_RE.fullmatch(text)
        )
    )


def _headerless_data_signature(cells: list[str]) -> tuple[str, str] | None:
    """Recognize a conservative OCR table row: marker, entity label, scalar.

    Converted policy schedules occasionally omit their real header.  Markdown
    then promotes the first bullet row to the header and silently loses it.
    Requiring exactly one marker axis, one descriptive axis and one scalar axis
    keeps this repair away from ordinary financial-statement matrices.
    """

    if len(cells) != 3:
        return None
    marker, description, scalar = cells
    if not _LIST_MARKER_RE.fullmatch(str(marker or "").strip()):
        return None
    if not _is_descriptive_cell(description) or not _is_scalar_cell(scalar):
        return None

    scalar_text = _strip_cell_formatting(scalar)
    if _DURATION_VALUE_RE.search(scalar_text):
        return "duration", scalar_text
    if _PERCENT_VALUE_RE.search(scalar_text):
        return "percent", scalar_text
    if _DATA_DATE_RE.fullmatch(scalar_text):
        return "date", scalar_text
    return "scalar", scalar_text


def _synthesized_header(
    *,
    value_family: str,
    context: str = "",
) -> list[str]:
    value_header = {
        "percent": "Tỷ lệ",
        "date": "Ngày",
        "scalar": "Giá trị",
    }.get(value_family, "Thời gian")
    if (
        value_family in {"duration", "scalar"}
        and _USEFUL_LIFE_CONTEXT_RE.search(context)
    ):
        value_header = "Thời gian hữu dụng"
    return ["Marker", "Đối tượng", value_header]


def _recover_headerless_data_header(
    table_lines: list[str],
    *,
    context: str = "",
) -> tuple[list[str], dict[str, object] | None]:
    """Prepend safe generic headers when Markdown promoted row one to a header."""

    if not table_lines:
        return list(table_lines or []), None
    first_cells, _, _ = _split_markdown_row(table_lines[0])
    signature = _headerless_data_signature(first_cells)
    if signature is None:
        return list(table_lines), None

    value_family, _scalar = signature
    compatible_rows = 0
    for raw_line in table_lines[1:]:
        cells, _, _ = _split_markdown_row(raw_line)
        if _is_alignment_row(cells):
            continue
        candidate = _headerless_data_signature(cells)
        if candidate is None or candidate[0] != value_family:
            continue
        compatible_rows += 1
    if compatible_rows < 1:
        return list(table_lines), None

    headers = _synthesized_header(
        value_family=value_family,
        context=str(context or ""),
    )
    synthetic_line = "| " + " | ".join(headers) + " |"
    return (
        [synthetic_line, *table_lines],
        {
            "kind": "headerless_data_header_recovered",
            "value_family": value_family,
            "headers": headers,
            "recovered_rows": compatible_rows + 1,
        },
    )


def _recover_headerless_block_headers(blocks: list[dict]) -> list[dict]:
    """Use the latched note/heading context before continuation inheritance."""

    for block in blocks:
        context = _context_path(
            block.get("section", ""),
            block.get("note_title", ""),
            block.get("heading", ""),
            block.get("section_path", ""),
        )
        repaired, warning = _recover_headerless_data_header(
            list(block.get("table") or []),
            context=context,
        )
        if warning is None:
            continue
        block["table"] = repaired
        warnings = list(block.get("parser_warnings") or [])
        warnings.append(warning)
        block["parser_warnings"] = warnings
    return blocks


def _header_is_semantic(table_lines: list[str]) -> bool:
    """True when the first row looks like a header rather than a lost data row."""

    if not table_lines:
        return False
    cells, _, _ = _split_markdown_row(table_lines[0])
    meaningful = [str(cell or "").strip() for cell in cells]
    if not any(meaningful):
        return False
    # A usable header normally carries a label or a period/value-type caption.
    # A continuation whose first row is a data row instead contains at least one
    # amount and no header vocabulary.
    header_text = " ".join(meaningful).lower()
    semantic_markers = (
        "chỉ tiêu",
        "khoản mục",
        "mã số",
        "thuyết minh",
        "đối tượng",
        "marker",
        "thời gian hữu dụng",
        "tỷ lệ",
        "năm ",
        "đầu kỳ",
        "cuối kỳ",
        "đầu năm",
        "cuối năm",
        "ngày ",
        "31/",
        "01/",
        "giá trị",
        "nguyên giá",
        "dự phòng",
        "tổng",
        "cộng",
        "vnd",
        "đồng",
    )
    return any(marker in header_text for marker in semantic_markers)


def _header_looks_like_data(table_lines: list[str]) -> bool:
    if not table_lines:
        return False
    cells, _, _ = _split_markdown_row(table_lines[0])
    if len(cells) < 2:
        return False
    numeric_cells = 0
    for cell in cells[1:]:
        compact = (
            str(cell or "")
            .strip()
            .replace("\\*", "")
            .replace("*", "")
            .replace("(", "")
            .replace(")", "")
            .replace("-", "")
            .replace(".", "")
            .replace(",", "")
            .replace(" ", "")
            .replace("%", "")
        )
        if compact.isdigit():
            numeric_cells += 1
    return numeric_cells > 0 and not _header_is_semantic(table_lines)


def _same_continuation_context(previous: dict, current: dict) -> bool:
    if str(previous.get("section", "") or "") != str(current.get("section", "") or ""):
        return False
    if str(previous.get("note_ref", "") or "") != str(current.get("note_ref", "") or ""):
        return False

    previous_page = previous.get("source_page")
    current_page = current.get("source_page")
    if previous_page is not None and current_page is not None:
        if int(current_page) not in {int(previous_page), int(previous_page) + 1}:
            return False

    heading = str(current.get("heading", "") or "")
    title = str(current.get("note_title", "") or "")
    return bool(
        current.get("is_continuation")
        or _CONTINUATION_RE.search(f"{heading} {title}")
    )


def _inherit_compatible_continuation_headers(blocks: list[dict]) -> list[dict]:
    """Repair only explicit, context-compatible continuation tables.

    Header inheritance is intentionally fail-closed.  The current table must
    explicitly say "(tiếp theo)", start with a data-like row, have the exact same
    width, and remain in the same statement/note and adjacent page.  Otherwise a
    diagnostic warning is attached and the source table is left untouched.
    """

    previous_by_context: dict[tuple[str, str], dict] = {}
    for block in blocks:
        key = (
            str(block.get("section", "") or ""),
            str(block.get("note_ref", "") or ""),
        )
        previous = previous_by_context.get(key)
        table = list(block.get("table") or [])
        warnings = list(block.get("parser_warnings") or [])

        continuation_marked = bool(
            block.get("is_continuation")
            or _CONTINUATION_RE.search(
                f"{block.get('heading', '')} {block.get('note_title', '')}"
            )
        )
        if continuation_marked and _header_looks_like_data(table):
            compatible = bool(
                previous
                and _same_continuation_context(previous, block)
                and _header_is_semantic(previous.get("table") or [])
                and _table_width(previous.get("table") or []) == _table_width(table)
            )
            if compatible:
                inherited_header = str(previous["table"][0])
                block["table"] = [inherited_header, *table]
                warnings.append(
                    {
                        "kind": "continuation_header_inherited",
                        "from_block_id": previous.get("block_id", ""),
                    }
                )
            else:
                warnings.append({"kind": "continuation_header_not_inherited"})

        block["parser_warnings"] = warnings
        if _header_is_semantic(block.get("table") or []):
            previous_by_context[key] = block
    return blocks


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
    current_note_chapter = "V"
    current_unit = ""
    current_page = None
    current_page_continuation = False
    block_counter = 0

    def flush_table() -> None:
        nonlocal current_table, block_counter
        if not current_table:
            return
        block_counter += 1
        tables_with_context.append(
            {
                "heading": current_heading,
                "section": current_section,
                "note_ref": current_note_ref,
                "note_title": current_note_title,
                "unit": current_unit,
                "section_path": _context_path(
                    current_section,
                    current_note_title,
                    current_heading,
                ),
                "block_id": f"table-{block_counter:06d}",
                "source_page": current_page,
                "is_continuation": current_page_continuation,
                "parser_warnings": [],
                "table": current_table,
            }
        )
        current_table = []

    for line in md_text.splitlines():
        if not line.strip().startswith("|") and current_table:
            flush_table()

        page_match = _PAGE_MARKER_RE.match(str(line or "").strip())
        if page_match:
            # Source converters use zero-based page markers; the rest of the
            # ingestion stack exposes one-based printed pages.
            current_page = int(page_match.group(1)) + 1
            current_page_continuation = False

        normalized_line = _normalize_heading_line(line)
        if _CONTINUATION_RE.search(normalized_line):
            current_page_continuation = True
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
            chapter_match = _NOTE_CHAPTER_RE.match(str(line or ""))
            if in_note_section and chapter_match:
                # A chapter title such as "V. ... BẢNG CÂN ĐỐI KẾ TOÁN" names
                # the family of notes, not a return to the primary statement.
                current_section = TABLE_NOTE
                current_note_chapter = str(
                    chapter_match.group("chapter") or "V"
                ).upper()
            elif canonical in _SECTION_HEADINGS:
                if canonical == TABLE_NOTE:
                    if _is_note_section_start(line, current_heading):
                        current_section = TABLE_NOTE
                        in_note_section = True
                        chapter_match = _NOTE_CHAPTER_RE.match(str(line or ""))
                        if chapter_match:
                            current_note_chapter = str(
                                chapter_match.group("chapter") or "V"
                            ).upper()
                elif (
                    in_note_section
                    and canonical in {TABLE_BS, TABLE_IS, TABLE_CF}
                    and not _is_primary_statement_start(line, canonical)
                ):
                    # This is a note caption mentioning the related primary
                    # statement, not a transition back into that statement.
                    current_section = TABLE_NOTE
                else:
                    current_section = canonical
                    in_note_section = False
                    current_note_ref = ""
                    current_note_title = ""
            # Inside the notes, a numbered schedule heading refreshes the link;
            # descriptive headings return "" and keep the previous schedule ref.
            if in_note_section:
                if chapter_match:
                    current_note_chapter = str(
                        chapter_match.group("chapter") or "V"
                    ).upper()
                ref = note_schedule_ref(
                    current_heading,
                    chapter=current_note_chapter,
                )
                if ref:
                    current_note_ref = ref
                    current_note_title = note_schedule_title(current_heading)
        elif in_note_section and not line.strip().startswith("|") and len(line.strip()) <= 100:
            # Schedule titles can also appear as plain/bold lines that is_heading
            # misses ("**10. Tài sản cố định vô hình**"); without this latch the
            # following descriptive heading ("Là chương trình phần mềm…:") keeps
            # the PREVIOUS schedule's ref/title (V.9 leaking onto the intangibles
            # matrix). Length-guarded so numbered prose sentences don't match.
            ref = note_schedule_ref(line, chapter=current_note_chapter)
            if ref:
                current_note_ref = ref
                current_note_title = note_schedule_title(line)

        if line.strip().startswith("|"):
            current_table.append(line)

    if current_table:
        flush_table()

    # Repair self-contained OCR/headerless schedules first.  This prevents a
    # page-level "(tiếp theo)" marker from making such a schedule inherit the
    # unrelated header of an earlier table in the same note.
    return _inherit_compatible_continuation_headers(
        _recover_headerless_block_headers(tables_with_context)
    )


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

    repaired_lines, recovery_warning = _recover_headerless_data_header(
        list(table_lines or [])
    )
    logical_rows = _logical_table_rows(repaired_lines)
    if not logical_rows:
        return pd.DataFrame()

    header, _, _ = logical_rows[0]
    columns = _unique_columns(header)
    expected_width = len(columns)
    values: list[list[object]] = []
    parser_warnings: list[dict[str, object]] = []
    if recovery_warning is not None:
        parser_warnings.append(recovery_warning)

    for cells, start_line, end_line in logical_rows[1:]:
        if _is_alignment_row(cells):
            continue
        if (
            cells
            and columns
            and str(columns[0] or "").strip().casefold() == "marker"
            and _LIST_MARKER_RE.fullmatch(str(cells[0] or "").strip())
        ):
            # The leading glyph is layout, not a searchable fact.  Keep its
            # physical column for positional stability but blank its values.
            cells = ["", *cells[1:]]
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
    df.attrs["markdown_original_columns"] = list(header)
    df.attrs["markdown_parser_warnings"] = parser_warnings
    return df
