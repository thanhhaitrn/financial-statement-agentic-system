"""Extract narrative and table facts from the notes-to-financial-statements section."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

import re
from typing import Iterable

import pandas as pd

from ingestion.kb_builder import _strip_inline_formatting
from ingestion.period_normalize import parse_unit
from ingestion.table_parser import markdown_table_to_df
from schemas.table_names import TABLE_NOTE


_PAGE_MARKER_RE = re.compile(r"(?m)^-+\s*Page\s+(\d+)\s*$")
_NOTE_TOC_RE = re.compile(
    r"thuy[ếe]t\s+minh\s+b[áa]o\s+c[áa]o\s+t[àa]i\s+ch[íi]nh.*?"
    r"(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?",
    flags=re.IGNORECASE | re.DOTALL,
)
_NOTE_HEADING_RE = re.compile(
    r"(?im)^\s*#{0,6}\s*(?:b[ảa]n\s+)?"
    r"thuy[ếe]t\s+minh\s+b[áa]o\s+c[áa]o\s+t[àa]i\s+ch[íi]nh"
    r"(?:\s+(?:ri[êe]ng|h[ợo]p\s+nh[ấa]t|t[ổo]ng\s+h[ợo]p))?"
    r"(?:\s+cho\s+(?:n[ăa]m|k[ỳy]).*?)?"
    r"(?:\s*\(ti[ếe]p\s+theo\))?\s*$"
)
_NUMBERED_SECTION_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\d+(?:\.\d+)*\s+.{2,}$",
    flags=re.IGNORECASE,
)
_NOTE_SECTION_RE = re.compile(
    r"^\s*(?:thuy[ếe]t\s+minh\s*)?"
    r"(?P<number>\d+(?:\.\d+)*)\s*[).:-]?\s+(?P<title>.+?)\s*$",
    flags=re.IGNORECASE,
)
_SUBSECTION_HEADING_RE = re.compile(
    r"^\s*(?P<label>[a-zđ])\)\s+(?P<title>.+?)\s*$",
    flags=re.IGNORECASE,
)
# A note-schedule heading like "### 5. Phải thu về cho vay ngắn hạn", "2c. Đầu tư
# góp vốn vào đơn vị khác" or "17a. Phải trả ngắn hạn khác". The number carries an
# optional single-letter suffix (2c, 3a, 17a, 18b) that _NOTE_SECTION_RE misses.
# Used to backfill the 'V.<n>' note reference onto note rows so they link to the
# primary-statement line that references them (e.g. BS "Phải thu về cho vay" V.5).
_NOTE_SCHEDULE_RE = re.compile(
    r"^\s*(?:thuy[ếe]t\s+minh\s*)?(?P<num>\d{1,2}[a-zđ]?)\s*[.).:-]\s+\S",
    flags=re.IGNORECASE,
)
_FISCAL_YEAR_RE = re.compile(
    r"năm\s+tài\s+chính\s+kết\s+thúc\s+ngày.*?(?P<year>(?:19|20)\d{2})",
    flags=re.IGNORECASE | re.DOTALL,
)
_DATE_YEAR_RE = re.compile(r"\b(?:31|30)/12/(?P<year>(?:19|20)\d{2})\b")
_COMPANY_LINE_RE = re.compile(r"^\s*#{0,6}\s*(công\s+ty\b.+?)\s*$", flags=re.IGNORECASE)
# A company-name line in the cover/front matter is often the start of a prose
# sentence ("Công ty ... là Công ty cổ phần hoạt động theo Giấy chứng nhận...").
# Cut at the first clause boundary so only the name survives — otherwise the
# whole sentence becomes the "company" and gets prepended to every embedded
# document, drowning the discriminative text and polluting interpretation hints.
_COMPANY_CLAUSE_RE = re.compile(
    r"\s+(?:là|được|hoạt\s+động|thành\s+lập|gọi\s+tắt|có\s+trụ\s+sở|trình\s+bày)\b"
    r"|[.,;:(]",
    flags=re.IGNORECASE,
)


def _trim_company_name(name: str) -> str:
    return _COMPANY_CLAUSE_RE.split(str(name or ""), maxsplit=1)[0].strip()
_NUMERIC_AMOUNT_RE = re.compile(r"^\(?-?[\d.,\s]+%?\)?$")
_REPORT_HEADER_PREFIXES = (
    "địa chỉ:",
    "báo cáo tài chính",
    "cho năm tài chính",
)
_UNIT_ONLY_CELLS = {"vnd", "vnđ", "đồng", "dong"}


def infer_fiscal_year(md_text: str) -> str:
    text = str(md_text or "")
    match = _FISCAL_YEAR_RE.search(text)
    if match:
        return str(match.group("year"))

    match = _DATE_YEAR_RE.search(text)
    if match:
        return str(match.group("year"))

    return ""


def infer_company(md_text: str) -> str:
    heading_candidates = []
    for line in str(md_text or "").splitlines()[:120]:
        match = _COMPANY_LINE_RE.match(_strip_inline_formatting(line))
        if not match:
            continue

        company = _trim_company_name(_readable_note_title(str(match.group(1) or "").strip()))
        if len(company) <= 8:
            continue
        if not str(line or "").lstrip().startswith("#"):
            return company
        heading_candidates.append(company)

    return heading_candidates[0] if heading_candidates else ""


def _normalize_fiscal_year(value, md_text: str = "") -> str:
    if value is not None and str(value).strip():
        return str(value).strip()
    return infer_fiscal_year(md_text)


def _parse_note_page_range(md_text: str) -> tuple[int, int] | None:
    # The table of contents usually contains the authoritative printed page
    # range for "Thuyết minh báo cáo tài chính"; use it to keep retrieval scoped.
    match = _NOTE_TOC_RE.search(str(md_text or ""))
    if not match:
        return None

    start = int(match.group(1))
    end = int(match.group(2) or start)
    if end < start:
        start, end = end, start
    return start, end


def _split_marked_pages(md_text: str) -> list[dict]:
    text = str(md_text or "")
    matches = list(_PAGE_MARKER_RE.finditer(text))
    pages = []

    for index, match in enumerate(matches):
        marker_page = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        # The generated markdown page marker is zero-based relative to the
        # printed report page in this dataset.
        pages.append(
            {
                "marker_page": marker_page,
                "printed_page": marker_page + 1,
                "content": text[start:end],
            }
        )

    return pages


def _slice_from_note_heading(text: str) -> str:
    match = _NOTE_HEADING_RE.search(str(text or ""))
    if not match:
        return str(text or "")
    return str(text or "")[match.start():]


def extract_note_section_pages(md_text: str) -> list[dict]:
    # Never treat an arbitrary report body as notes. The old fallback returned
    # the complete document when no heading existed, causing primary statements
    # and front matter to be indexed as NOTE rows.
    if not _NOTE_HEADING_RE.search(str(md_text or "")):
        return []

    pages = _split_marked_pages(md_text)
    page_range = _parse_note_page_range(md_text)

    # Prefer page markers plus TOC range when available. This is stricter than
    # heading-only slicing and prevents later report sections from entering the
    # notes vector slice.
    if pages and page_range is not None:
        start_page, end_page = page_range
        selected = [
            page
            for page in pages
            if start_page <= int(page["printed_page"]) <= end_page
        ]
        # Only trust the TOC page range when the notes heading actually falls
        # inside the selected pages. Otherwise the TOC parse latched onto the
        # wrong number (e.g. a running footer) and we would slice a main
        # statement (the Balance Sheet) into the notes section.
        if selected and any(_NOTE_HEADING_RE.search(page["content"]) for page in selected):
            selected[0] = {
                **selected[0],
                "content": _slice_from_note_heading(selected[0]["content"]),
            }
            return selected

    sliced = _slice_from_note_heading(md_text)
    return [{"marker_page": None, "printed_page": None, "content": sliced}]


def _clean_line(line: str) -> str:
    return _strip_inline_formatting(str(line or "")).strip()


def _is_report_header_line(line: str) -> bool:
    cleaned = _clean_line(line)
    lowered = cleaned.lower()
    if not cleaned:
        return False
    if cleaned.isdigit():
        return True
    # Do not drop every sentence that begins with the company name; only remove
    # repeated page headers or short standalone company-header lines.
    if lowered.startswith("công ty ") and (
        "báo cáo tài chính" in lowered or len(cleaned) <= 40
    ):
        return True
    return any(lowered.startswith(prefix) for prefix in _REPORT_HEADER_PREFIXES)


def _section_heading_from_line(line: str) -> str:
    cleaned = _clean_line(line).strip("#").strip()
    if not cleaned or _is_report_header_line(cleaned):
        return ""

    if _NOTE_HEADING_RE.match(cleaned):
        return "Thuyết minh báo cáo tài chính"

    if str(line or "").lstrip().startswith("#"):
        return cleaned

    if _NUMBERED_SECTION_RE.match(cleaned) and len(cleaned) <= 160:
        return cleaned

    return ""


def _subsection_heading_from_line(line: str) -> str:
    cleaned = _clean_line(line)
    if not cleaned or _is_report_header_line(cleaned):
        return ""

    match = _SUBSECTION_HEADING_RE.match(cleaned)
    if not match:
        return ""

    label = str(match.group("label") or "").strip().lower()
    title = _readable_note_title(str(match.group("title") or ""))
    if not label or not title:
        return ""
    return f"{label}) {title}"


def _readable_note_title(title: str) -> str:
    cleaned = _clean_line(title)
    letters = [char for char in cleaned if char.isalpha()]
    if not letters:
        return cleaned

    upper_count = sum(1 for char in letters if char.isupper())
    lower_count = sum(1 for char in letters if char.islower())
    if upper_count <= lower_count:
        return cleaned

    lowered = cleaned.lower()
    return lowered[:1].upper() + lowered[1:]


def _parse_note_section(section: str) -> tuple[str, str]:
    cleaned = _clean_line(section).strip("#").strip()
    if not cleaned:
        return "", "Thuyết minh báo cáo tài chính"

    if _NOTE_HEADING_RE.match(cleaned):
        return "", "Thuyết minh báo cáo tài chính"

    match = _NOTE_SECTION_RE.match(cleaned)
    if not match:
        return "", cleaned

    return (
        str(match.group("number") or "").strip(),
        _readable_note_title(str(match.group("title") or "")),
    )


def note_schedule_ref(heading: str) -> str:
    """Return the 'V.<n>' note reference for a note-schedule heading, else "".

    "### 5. Phải thu về cho vay ngắn hạn" -> "V.5"; "2c. Đầu tư góp vốn vào đơn
    vị khác" -> "V.2c". This is the back-link that lets a note schedule be joined
    to the primary-statement row whose note_ref column points at it.
    """
    cleaned = _clean_line(heading).strip("#").strip()
    if not cleaned or _NOTE_HEADING_RE.match(cleaned):
        return ""
    match = _NOTE_SCHEDULE_RE.match(cleaned)
    if not match:
        return ""
    return "V." + str(match.group("num") or "").lower()


def note_schedule_title(heading: str) -> str:
    """The numbered schedule heading itself ("10. Tài sản cố định vô hình"), or "".

    Kept alongside note_schedule_ref so descriptive sub-headings that follow
    ("Là chương trình phần mềm, chi tiết như sau:") can be re-anchored to the
    schedule they belong to — otherwise those rows lose the line-item tokens
    the retrieval needs ("tài sản cố định vô hình").
    """
    cleaned = _clean_line(heading).strip("#").strip()
    if not cleaned or _NOTE_HEADING_RE.match(cleaned):
        return ""
    if not _NOTE_SCHEDULE_RE.match(cleaned):
        return ""
    return cleaned


def _note_item_name(section: str) -> str:
    note_number, note_title = _parse_note_section(section)
    if note_number and note_title:
        return f"Thuyết minh {note_number}: {note_title}"
    if note_title:
        return note_title
    return "Thuyết minh báo cáo tài chính"


def _note_item_name_with_context(section: str, *parts: str) -> str:
    item_name = _note_item_name(section)
    for part in parts:
        cleaned = _clean_line(part)
        if cleaned and cleaned not in item_name:
            item_name = f"{item_name} | {cleaned}"
    return item_name


def _row_tuple(
    *,
    company: str,
    fiscal_year: str,
    item_code: str,
    subheading: str,
    item_name: str,
    value: str,
    source: str,
    note_ref: str = "",
    period: str = "",
    default_unit: str = "",
):
    text = _strip_inline_formatting(value)
    if not text:
        return None

    unit = parse_unit(text) or str(default_unit or "").strip()
    if "số lượng cổ phiếu" in _clean_line(f"{item_name} {text}").lower():
        unit = "cổ phiếu"

    # 14-tuple in canonical order so provenance and calculation slots survive
    # the same typed SQLite contract as statement-table facts.
    return (
        company,
        fiscal_year,
        TABLE_NOTE,
        item_code,
        note_ref,
        subheading,
        item_name,
        text,
        text,
        text,
        source,
        period,
        "",
        unit,
    )


def _page_source(source: str, page: int | None) -> str:
    if page is None:
        return source
    return f"{source}#page={page}"


def _note_text_row(
    company: str,
    fiscal_year: str,
    section: str,
    subsection: str,
    paragraph: str,
    source: str,
    page: int | None,
):
    item_name = _note_item_name_with_context(section, subsection)
    value = paragraph
    if subsection and subsection not in value:
        value = f"{subsection}. {value}"
    return _row_tuple(
        company=company,
        fiscal_year=fiscal_year,
        item_code="note_text",
        subheading=subsection,
        item_name=item_name,
        value=value,
        source=_page_source(source, page),
        note_ref=note_schedule_ref(section),
        period=fiscal_year,
    )


def _iter_table_rows(df: pd.DataFrame) -> Iterable[tuple[str, str]]:
    df = df.map(lambda value: "" if pd.isna(value) else _strip_inline_formatting(value))
    columns = [_strip_inline_formatting(column) for column in df.columns]

    for _, row in df.iterrows():
        # Access by position, not column name: note tables often repeat header
        # labels (e.g. "Số cuối năm"), which makes row.get(name) return a Series.
        cells = [str(value or "").strip() for value in row.values]
        if not any(cells):
            continue
        if all(_clean_line(cell).lower() in _UNIT_ONLY_CELLS for cell in cells if cell):
            continue

        # Use the first non-empty cell as the row label, but preserve every
        # column/value pair in the searchable value text.
        label = _table_row_label(cells)
        pairs = [
            f"{column}: {cell}"
            for column, cell in zip(columns, cells)
            if column and cell
        ]
        yield label, " | ".join(pairs)


def _note_table_rows(
    table_lines: list[str],
    *,
    company: str,
    fiscal_year: str,
    section: str,
    subsection: str,
    source: str,
    page: int | None,
    default_unit: str = "",
) -> list[tuple]:
    try:
        df = markdown_table_to_df(table_lines)
    except Exception:
        return []

    if df is None or df.empty:
        return []

    rows = []
    for label, value in _iter_table_rows(df):
        item_name = _note_item_name_with_context(section, subsection, label)
        table_value = value
        if subsection and subsection not in table_value:
            table_value = f"{subsection}. {table_value}"
        row = _row_tuple(
            company=company,
            fiscal_year=fiscal_year,
            item_code="note_table",
            subheading=subsection,
            item_name=item_name,
            value=table_value,
            source=_page_source(source, page),
            note_ref=note_schedule_ref(section),
            period=fiscal_year,
            default_unit=default_unit,
        )
        if row is not None:
            rows.append(row)
    return rows


def _looks_like_numeric_amount(value: str) -> bool:
    text = _clean_line(value)
    if not text:
        return False
    if text in {"-", "–"}:
        return True
    return bool(_NUMERIC_AMOUNT_RE.match(text))


def _table_row_label(cells: list[str]) -> str:
    if not cells:
        return ""

    first_cell = str(cells[0] or "").strip()
    value_cells = [cell for cell in cells[1:] if str(cell or "").strip()]
    if not first_cell and value_cells and all(_looks_like_numeric_amount(cell) for cell in value_cells):
        return "Tổng"

    return next((cell for cell in cells if cell), "")


def build_note_rows(md_text: str, company: str, source: str, fiscal_year=None) -> list[tuple]:
    text = str(md_text or "")
    note_heading = _NOTE_HEADING_RE.search(text)
    if not note_heading:
        return []

    rows = []
    year = _normalize_fiscal_year(fiscal_year, md_text)
    company_name = str(company or "").strip() or infer_company(md_text)
    # A report-level caption before the notes is only a fallback.  Captions
    # encountered inside the notes override it for subsequent tables, avoiding
    # the old bug where the first unit anywhere in the report won forever.
    prefix_units = [
        parse_unit(line)
        for line in text[: note_heading.start()].splitlines()
        if "đơn vị tính" in _clean_line(line).lower() and parse_unit(line)
    ]
    active_unit = prefix_units[-1] if prefix_units else ""
    current_section = "Thuyết minh báo cáo tài chính"
    current_subsection = ""
    paragraph_lines: list[str] = []
    table_lines: list[str] = []

    def flush_paragraph(page: int | None):
        nonlocal paragraph_lines
        # Paragraphs are accumulated across wrapped markdown lines, then stored
        # as narrative note facts under the current numbered section heading.
        paragraph = " ".join(_clean_line(line) for line in paragraph_lines if _clean_line(line))
        paragraph_lines = []
        if not paragraph:
            return
        row = _note_text_row(
            company_name,
            year,
            current_section,
            current_subsection,
            paragraph,
            source,
            page,
        )
        if row is not None:
            rows.append(row)

    def flush_table(page: int | None):
        nonlocal table_lines
        if not table_lines:
            return
        # Markdown table blocks are converted row-by-row so vector retrieval can
        # return individual note facts instead of one large table blob.
        rows.extend(
            _note_table_rows(
                table_lines,
                company=company_name,
                fiscal_year=year,
                section=current_section,
                subsection=current_subsection,
                source=source,
                page=page,
                default_unit=active_unit,
            )
        )
        table_lines = []

    for page in extract_note_section_pages(md_text):
        page_number = page.get("printed_page")
        for line in str(page.get("content", "") or "").splitlines():
            stripped = line.strip()

            caption_unit = (
                parse_unit(line)
                if "đơn vị tính" in _clean_line(line).lower()
                else ""
            )
            if caption_unit and not stripped.startswith("|"):
                flush_table(page_number)
                flush_paragraph(page_number)
                active_unit = caption_unit
                continue

            if stripped.startswith("|"):
                flush_paragraph(page_number)
                table_lines.append(line)
                continue

            flush_table(page_number)

            heading = _section_heading_from_line(line)
            if heading:
                flush_paragraph(page_number)
                current_section = heading
                current_subsection = ""
                continue

            subsection = _subsection_heading_from_line(line)
            if subsection:
                flush_paragraph(page_number)
                current_subsection = subsection
                continue

            if not stripped:
                flush_paragraph(page_number)
                continue

            if _is_report_header_line(line):
                continue

            paragraph_lines.append(line)

        flush_table(page_number)
        flush_paragraph(page_number)

    return rows
