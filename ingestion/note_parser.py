"""Extract narrative and table facts from the notes-to-financial-statements section."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

import html
import re
from typing import Iterable

import pandas as pd

from ingestion.kb_builder import (
    _strip_inline_formatting,
    aggregation_level,
    compose_section_path,
    parsed_value,
    period_role,
    semantic_scope_label,
    value_kind,
)
from ingestion.period_normalize import parse_unit
from ingestion.semantic_dimensions import derive_semantic_fact_dimensions
from ingestion.table_parser import markdown_table_to_df
from ingestion.topic_atoms import derive_topic_atoms
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
    r"^\s*(?:#{1,6}\s*)?\d+(?:\.\d+)*[.)]?\s+.{2,}$",
    flags=re.IGNORECASE,
)
_NOTE_SECTION_RE = re.compile(
    r"^\s*(?:thuy[ếe]t\s+minh\s*)?"
    r"(?P<number>\d+(?:\.\d+)*)\s*[).:-]?\s+(?P<title>.+?)\s*$",
    flags=re.IGNORECASE,
)
_SUBSECTION_HEADING_RE = re.compile(
    r"^\s*(?:\((?P<paren>[a-zđ])\)|(?P<plain>[a-zđ])\))"
    r"\s+(?P<title>.+?)\s*$",
    flags=re.IGNORECASE,
)
_BOLD_ONLY_RE = re.compile(
    r"^\s*(?:\*{2,3}|_{2,3})(?P<title>.+?)(?:\*{2,3}|_{2,3})\s*:?\s*$"
)
_TAB_HEADER_TOPIC_RE = re.compile(
    r"\b(?:"
    r"chỉ\s+tiêu|khoản\s+mục|loại|tài\s+sản|"
    r"số\s+năm|thời\s+gian|năm\s+nay|năm\s+trước|"
    r"tỷ\s+lệ|giá\s+trị|đơn\s+vị|nội\s+dung"
    r")\b",
    flags=re.IGNORECASE,
)
_NUMERIC_RANGE_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*(?:[-–—]|đến)\s*\d+(?:[.,]\d+)?\s*$",
    flags=re.IGNORECASE,
)
# A note-schedule heading like "### 5. Phải thu về cho vay ngắn hạn", "2c. Đầu tư
# góp vốn vào đơn vị khác" or "17a. Phải trả ngắn hạn khác". The number carries an
# optional single-letter suffix (2c, 3a, 17a, 18b) that _NOTE_SECTION_RE misses.
# Used to backfill the 'V.<n>' note reference onto note rows so they link to the
# primary-statement line that references them (e.g. BS "Phải thu về cho vay" V.5).
_NOTE_SCHEDULE_RE = re.compile(
    r"^\s*(?:thuy[ếe]t\s+minh\s*)?"
    r"(?P<num>\d{1,3}(?:\.\d+)*(?:[a-zđ])?)\s*[).:-]\s+\S",
    flags=re.IGNORECASE,
)
_NOTE_CHAPTER_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?P<chapter>[IVXLCDM]+)\.\s+",
    flags=re.IGNORECASE,
)
_FISCAL_YEAR_RE = re.compile(
    r"(?:"
    r"năm\s+tài\s+chính|"
    r"kỳ\s+(?:hoạt\s+động|kế\s+toán|tài\s+chính)"
    r"(?:\s+\d+\s+tháng)?|"
    r"năm"
    r")"
    r".{0,120}?\bkết\s+thúc(?:\s+vào)?\s+ngày\b"
    r".{0,80}?(?P<year>(?:19|20)\d{2})",
    flags=re.IGNORECASE | re.DOTALL,
)
_REPORT_YEAR_RE = re.compile(
    r"báo\s+cáo\s+tài\s+chính.{0,100}?"
    r"(?:cho\s+)?(?:năm|kỳ).{0,40}?(?P<year>(?:19|20)\d{2})",
    flags=re.IGNORECASE | re.DOTALL,
)
_DATE_YEAR_RE = re.compile(r"\b(?:31|30)/12/(?P<year>(?:19|20)\d{2})\b")
_COMPANY_LINE_RE = re.compile(r"^\s*#{0,6}\s*(công\s+ty\b.+?)\s*$", flags=re.IGNORECASE)
_TICKER_SOURCE_RE = re.compile(
    r"\bmã\s+(?:chứng\s+khoán|cổ\s+phiếu)\b"
    r"(?:\s*(?:[:\-]|là)\s*)"
    r"(?P<ticker>[A-Z][A-Z0-9]{1,9})\b",
    flags=re.IGNORECASE,
)
_FISCAL_QUARTER_RE = re.compile(
    r"\bqu[ýy]\s*(?P<quarter>iv|iii|ii|i|[1-4])"
    r"(?:\s*[/\-]\s*(?:19|20)\d{2})?\b",
    flags=re.IGNORECASE,
)
# A company-name line in the cover/front matter is often the start of a prose
# sentence ("Công ty ... là Công ty cổ phần hoạt động theo Giấy chứng nhận...").
# Cut at the first clause boundary so only the name survives — otherwise the
# whole sentence becomes the "company" and gets prepended to every embedded
# document, drowning the discriminative text and polluting interpretation hints.
_COMPANY_CLAUSE_RE = re.compile(
    r"\s+(?:là|được|hoạt\s+động|thành\s+lập|gọi\s+tắt|có\s+trụ\s+sở|trình\s+bày)\b"
    r"|[,;:(]",
    flags=re.IGNORECASE,
)


def _trim_company_name(name: str) -> str:
    decoded = html.unescape(str(name or "")).replace("\xa0", " ")
    decoded = re.split(r"[\u2002\u2003]+|\s{4,}", decoded, maxsplit=1)[0]
    # A full stop can be part of the legal name (for example "Sông Đà 7.02"),
    # so it must not be treated as an unconditional clause boundary.
    return _COMPANY_CLAUSE_RE.split(decoded, maxsplit=1)[0].strip().rstrip(".")
_NUMERIC_AMOUNT_RE = re.compile(r"^\(?-?[\d.,\s]+%?\)?$")
_REPORT_HEADER_PREFIXES = (
    "địa chỉ:",
    "báo cáo tài chính",
    "bản thuyết minh báo cáo tài chính",
    "cho năm tài chính",
)
_UNIT_ONLY_CELLS = {"vnd", "vnđ", "đồng", "dong"}


def infer_fiscal_year(md_text: str) -> str:
    text = str(md_text or "")
    # Cover/header language is the authoritative reporting period. Restrict
    # broad fallbacks to the report prefix so a comparative date deep in the
    # notes cannot silently become the dataset year.
    report_prefix = text[:12000]
    match = _FISCAL_YEAR_RE.search(report_prefix)
    if match:
        return str(match.group("year"))

    match = _REPORT_YEAR_RE.search(report_prefix)
    if match:
        return str(match.group("year"))

    match = _DATE_YEAR_RE.search(report_prefix)
    if match:
        return str(match.group("year"))

    return ""


def infer_fiscal_quarter(md_text: str) -> int | None:
    """Infer an explicitly labelled reporting quarter from the cover only."""

    match = _FISCAL_QUARTER_RE.search(str(md_text or "")[:12000])
    if not match:
        return None
    raw_quarter = str(match.group("quarter") or "").strip().lower()
    roman_quarters = {"i": 1, "ii": 2, "iii": 3, "iv": 4}
    if raw_quarter in roman_quarters:
        return roman_quarters[raw_quarter]
    try:
        quarter = int(raw_quarter)
    except ValueError:
        return None
    return quarter if quarter in {1, 2, 3, 4} else None


def infer_ticker(md_text: str) -> str:
    """Return a ticker only when the source states an explicit ticker label."""

    match = _TICKER_SOURCE_RE.search(_strip_inline_formatting(str(md_text or "")))
    if not match:
        return ""
    return str(match.group("ticker") or "").strip().upper()


def infer_report_scope(md_text: str) -> str:
    """Infer the legal-entity scope from report-title language."""

    prefix = _clean_line(str(md_text or "")[:20000]).casefold()
    if re.search(r"báo\s+cáo\s+tài\s+chính\s+hợp\s+nhất", prefix):
        return "consolidated"
    if re.search(r"báo\s+cáo\s+tài\s+chính\s+tổng\s+hợp", prefix):
        return "combined"
    if re.search(r"báo\s+cáo\s+tài\s+chính\s+riêng", prefix):
        return "separate"
    # A generic financial statement headed by one legal entity is a separate
    # report unless the source positively labels it consolidated/combined.
    if "báo cáo tài chính" in prefix and infer_company(md_text):
        return "separate"
    return "unknown"


def infer_audit_status(md_text: str) -> str:
    """Infer assurance status conservatively from cover/report language."""

    prefix = _clean_line(str(md_text or "")[:30000]).casefold()
    # Review reports commonly explain that the work is not an audit, so review
    # markers must be checked before generic audit wording.
    if re.search(
        r"(?:đã\s+được\s+soát\s+xét|báo\s+cáo\s+soát\s+xét|"
        r"kết\s+luận\s+soát\s+xét)",
        prefix,
    ):
        return "reviewed"
    if re.search(
        r"(?:chưa\s+được\s+kiểm\s+toán|không\s+được\s+kiểm\s+toán)",
        prefix,
    ):
        return "unaudited"
    if re.search(
        r"(?:đã\s+được\s+kiểm\s+toán|báo\s+cáo\s+kiểm\s+toán\s+độc\s+lập|"
        r"ý\s+kiến\s+của\s+kiểm\s+toán\s+viên)",
        prefix,
    ):
        return "audited"
    # Quarterly reports without any assurance report are explicitly a
    # non-audited reporting form rather than an unknown annual audit.
    if infer_fiscal_quarter(md_text) is not None and "báo cáo tài chính" in prefix:
        return "unaudited"
    return "unknown"


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


def _is_bold_only_line(line: str) -> bool:
    match = _BOLD_ONLY_RE.match(str(line or ""))
    if not match:
        return False
    title = _clean_line(match.group("title"))
    return bool(title and len(title) <= 140)


def _is_report_header_line(line: str) -> bool:
    cleaned = _clean_line(line)
    lowered = cleaned.lower()
    if not cleaned:
        return False
    if cleaned.isdigit():
        return True
    # Do not drop every sentence that begins with the company name; only remove
    # repeated page headers or short standalone company-header lines.
    letters = [char for char in cleaned if char.isalpha()]
    is_all_caps = bool(letters) and sum(char.isupper() for char in letters) >= sum(
        char.islower() for char in letters
    )
    if lowered.startswith("công ty ") and (
        "báo cáo tài chính" in lowered
        or len(cleaned) <= 40
        or is_all_caps
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
    if match:
        label = str(match.group("paren") or match.group("plain") or "").strip().lower()
        title = _readable_note_title(str(match.group("title") or ""))
        if label and title:
            return f"{label}) {title}"

    if _is_bold_only_line(line):
        return _readable_note_title(cleaned)

    return ""


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


def note_schedule_ref(heading: str, *, chapter: str = "V") -> str:
    """Return the chapter-qualified note reference for a schedule heading.

    ``### 5. Phải thu về cho vay ngắn hạn`` under chapter V becomes ``V.5``;
    the same local number under chapter VI becomes ``VI.5``.  Preserving the
    Roman chapter is essential because balance-sheet and income-statement notes
    legitimately reuse the same local numbers.
    """
    cleaned = _clean_line(heading).strip("#").strip()
    if not cleaned or _NOTE_HEADING_RE.match(cleaned):
        return ""
    match = _NOTE_SCHEDULE_RE.match(cleaned)
    if not match:
        return ""
    prefix = str(chapter or "V").strip().upper().rstrip(".") or "V"
    return f"{prefix}." + str(match.group("num") or "").lower()


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
    row_label: str = "",
    column_label: str = "narrative",
    aggregation: str = "component",
    section_path: str = "",
    block_id: str = "",
    source_page: int | None = None,
    value_kind_override: str = "",
    metric_label: str = "",
    entity_label: str = "",
    scope_label: str = "",
    counterparty: str = "",
    transaction_type: str = "",
    movement_type: str = "",
    geography: str = "",
    policy_topic: str = "",
):
    text = _strip_inline_formatting(value)
    if not text:
        return None

    unit = parse_unit(text) or str(default_unit or "").strip()
    if "số lượng cổ phiếu" in _clean_line(f"{item_name} {text}").lower():
        unit = "cổ phiếu"

    semantic_row_label = _clean_line(row_label or item_name)
    semantic_column_label = _clean_line(column_label)
    semantic_kind = value_kind_override or value_kind(
        text,
        row_label=semantic_row_label,
        column_label=semantic_column_label,
        unit=unit,
    )
    cleaned_metric = _clean_line(metric_label)
    cleaned_entity = _clean_line(entity_label)
    cleaned_scope = semantic_scope_label(scope_label)
    dimensions = derive_semantic_fact_dimensions(
        row_label=semantic_row_label,
        column_label=semantic_column_label,
        metric_label=cleaned_metric,
        entity_label=cleaned_entity,
        scope_label=cleaned_scope,
        section_path=section_path,
        item_name=item_name,
        item_code=item_code,
        value=text,
    )
    # The legacy 14 fields remain first; typed-cell metadata is appended.
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
        semantic_row_label,
        semantic_column_label,
        semantic_kind,
        parsed_value(text, kind=semantic_kind),
        period,
        period_role(period),
        aggregation,
        section_path,
        block_id,
        "" if source_page is None else str(source_page),
        cleaned_metric,
        cleaned_entity,
        cleaned_scope,
        _clean_line(counterparty) or dimensions.counterparty,
        _clean_line(transaction_type) or dimensions.transaction_type,
        _clean_line(movement_type) or dimensions.movement_type,
        _clean_line(geography) or dimensions.geography,
        _clean_line(policy_topic) or dimensions.policy_topic,
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
    block_id: str = "",
    note_chapter: str = "V",
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
        note_ref=note_schedule_ref(section, chapter=note_chapter),
        period=fiscal_year,
        row_label=item_name,
        section_path=compose_section_path(TABLE_NOTE, section, subsection),
        block_id=block_id,
        source_page=page,
        value_kind_override="text",
        scope_label=subsection or section,
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
        if not label:
            continue
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
    block_id: str = "",
    note_chapter: str = "V",
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
            note_ref=note_schedule_ref(section, chapter=note_chapter),
            period=fiscal_year,
            default_unit=default_unit,
            row_label=label,
            column_label="table_row",
            aggregation=aggregation_level(label),
            section_path=compose_section_path(TABLE_NOTE, section, subsection),
            block_id=block_id,
            source_page=page,
            metric_label=label,
            scope_label=subsection or section,
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
        # The row-blob representation has no trustworthy group/arithmetic
        # context. Canonical cell parsing handles safe blank-label totals.
        return ""

    return next((cell for cell in cells if cell), "")


def _tab_cells(line: str) -> list[str]:
    if "\t" not in str(line or ""):
        return []
    cells = [_clean_line(cell) for cell in str(line or "").split("\t")]
    return cells if len(cells) >= 2 and sum(bool(cell) for cell in cells) >= 2 else []


def _is_tabular_policy_block(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False
    raw_headers = str(lines[0] or "").split("\t")
    headers = _tab_cells(lines[0])
    if not headers:
        return False

    bold_header = all(
        not raw_cell.strip()
        or bool(_BOLD_ONLY_RE.match(raw_cell.strip()))
        for raw_cell in raw_headers
    )
    semantic_header = any(_TAB_HEADER_TOPIC_RE.search(header) for header in headers)
    if not (bold_header or semantic_header):
        return False

    return any(
        len(_tab_cells(line)) == len(headers)
        and bool(_tab_cells(line)[0])
        and any(_tab_cells(line)[1:])
        for line in lines[1:]
    )


def _note_tab_policy_rows(
    lines: list[str],
    *,
    company: str,
    fiscal_year: str,
    section: str,
    subsection: str,
    source: str,
    page: int | None,
    default_unit: str = "",
    block_id: str = "",
    note_chapter: str = "V",
) -> list[tuple]:
    """Convert OCR tab-separated policy schedules into canonical cell facts."""

    if not _is_tabular_policy_block(lines):
        return []
    headers = _tab_cells(lines[0])
    rows: list[tuple] = []

    for line in lines[1:]:
        cells = _tab_cells(line)
        if len(cells) != len(headers):
            continue
        row_label = cells[0]
        if not row_label:
            continue

        for index, value in enumerate(cells[1:], start=1):
            column_label = headers[index]
            if not value or not column_label:
                continue
            column_unit = (
                "năm"
                if "năm" in _clean_line(column_label).casefold()
                else default_unit
            )
            item_name = _note_item_name_with_context(
                section,
                subsection,
                row_label,
                column_label,
            )
            row = _row_tuple(
                company=company,
                fiscal_year=fiscal_year,
                item_code="note_policy_cell",
                subheading=subsection,
                item_name=item_name,
                value=value,
                source=_page_source(source, page),
                note_ref=note_schedule_ref(section, chapter=note_chapter),
                period=fiscal_year,
                default_unit=column_unit,
                row_label=row_label,
                column_label=column_label,
                aggregation=aggregation_level(row_label),
                section_path=compose_section_path(
                    TABLE_NOTE,
                    section,
                    subsection,
                ),
                block_id=block_id,
                source_page=page,
                metric_label=(
                    "Thời gian khấu hao"
                    if (
                        "năm" in _clean_line(column_label).casefold()
                        and "khấu hao"
                        in _clean_line(f"{section} {subsection}").casefold()
                    )
                    else (
                        "Thời gian phân bổ"
                        if (
                            "năm" in _clean_line(column_label).casefold()
                            and "phân bổ"
                            in _clean_line(f"{section} {subsection}").casefold()
                        )
                        else column_label
                    )
                ),
                entity_label=row_label,
                scope_label=subsection or section,
            )
            if row is not None:
                rows.append(row)
    return rows


def build_note_rows(
    md_text: str,
    company: str,
    source: str,
    fiscal_year=None,
    *,
    include_table_rows: bool = True,
) -> list[tuple]:
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
    current_note_chapter = "V"
    paragraph_lines: list[str] = []
    table_lines: list[str] = []
    tab_lines: list[str] = []
    block_counter = 0
    seen_topic_atoms: set[tuple[str, str, str, str, str]] = set()

    def next_block_id(kind: str) -> str:
        nonlocal block_counter
        block_counter += 1
        return f"note-{kind}-{block_counter:06d}"

    def add_topic_atoms(paragraph: str, page: int | None) -> None:
        for atom in derive_topic_atoms(
            paragraph,
            section=current_section,
            subsection=current_subsection,
        ):
            key = (
                _clean_line(current_section).casefold(),
                _clean_line(current_subsection).casefold(),
                atom.topic.casefold(),
                atom.value.casefold(),
                atom.entity_label.casefold(),
            )
            if key in seen_topic_atoms:
                continue
            item_name = _note_item_name_with_context(
                current_section,
                current_subsection,
                atom.topic,
            )
            item_code = {
                "person_event": "note_person_event",
                "person_event_date": "note_person_event_date",
                "person_role": "note_person_role",
                "corporate_event": "note_corporate_event",
                "corporate_event_date": "note_corporate_event_date",
                "corporate_event_identifier": "note_corporate_event_identifier",
                "policy": "note_policy_atom",
            }.get(atom.atom_type, "note_topic_atom")
            atom_value_kind = {
                "person_event_date": "date",
                "corporate_event_date": "date",
                "corporate_event_identifier": "identifier",
            }.get(atom.atom_type, "")
            row = _row_tuple(
                company=company_name,
                fiscal_year=year,
                item_code=item_code,
                subheading=atom.topic,
                item_name=item_name,
                value=atom.value,
                source=_page_source(source, page),
                note_ref=note_schedule_ref(
                    current_section,
                    chapter=current_note_chapter,
                ),
                period=year,
                row_label=atom.topic,
                column_label=atom.topic,
                section_path=compose_section_path(
                    TABLE_NOTE,
                    current_section,
                    current_subsection,
                    atom.topic,
                ),
                block_id=next_block_id("atom"),
                source_page=page,
                value_kind_override=atom_value_kind,
                metric_label=atom.metric_label or atom.topic,
                entity_label=atom.entity_label,
                scope_label=current_subsection or current_section,
            )
            if row is not None:
                rows.append(row)
                seen_topic_atoms.add(key)

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
            next_block_id("text"),
            note_chapter=current_note_chapter,
        )
        if row is not None:
            rows.append(row)
        add_topic_atoms(paragraph, page)

    def flush_table(page: int | None):
        nonlocal table_lines
        if not table_lines:
            return
        if not include_table_rows:
            # Canonical cell facts are already emitted by build_fact_rows.  Keep
            # narrative note facts, but do not add a competing row-blob for the
            # same table into the full ingestion pipeline.
            table_lines = []
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
                block_id=next_block_id("table"),
                note_chapter=current_note_chapter,
            )
        )
        table_lines = []

    def flush_tabular(page: int | None):
        nonlocal tab_lines, paragraph_lines
        if not tab_lines:
            return
        block_rows = _note_tab_policy_rows(
            tab_lines,
            company=company_name,
            fiscal_year=year,
            section=current_section,
            subsection=current_subsection,
            source=source,
            page=page,
            default_unit=active_unit,
            block_id=next_block_id("tabular"),
            note_chapter=current_note_chapter,
        )
        if block_rows:
            rows.extend(block_rows)
        else:
            # Fail closed on an uncertain tab block while retaining its source
            # prose for retrieval.
            paragraph_lines.extend(tab_lines)
        tab_lines = []

    for page in extract_note_section_pages(md_text):
        page_number = page.get("printed_page")
        for line in str(page.get("content", "") or "").splitlines():
            stripped = line.strip()

            if _tab_cells(line):
                flush_table(page_number)
                flush_paragraph(page_number)
                tab_lines.append(line)
                continue

            flush_tabular(page_number)

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
                chapter_match = _NOTE_CHAPTER_RE.match(str(line or ""))
                if chapter_match:
                    current_note_chapter = str(
                        chapter_match.group("chapter") or "V"
                    ).upper()
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
        flush_tabular(page_number)
        flush_paragraph(page_number)

    return rows
