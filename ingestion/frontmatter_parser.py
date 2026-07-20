"""Extract front sections before the primary financial statements."""
# Code note: Front-matter rows cover management/audit/review narrative outside the notes.

import re
from typing import Iterable

import pandas as pd

from ingestion.kb_builder import _strip_inline_formatting
from ingestion.note_parser import infer_company, infer_fiscal_year
from ingestion.table_parser import markdown_table_to_df
from schemas.table_names import TABLE_REPORT_SECTION


_PAGE_MARKER_RE = re.compile(r"(?m)^-+\s*Page\s+(\d+)\s*$")
_MAIN_STATEMENT_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?"
    r"(?:"
    r"bảng\s+cân\s+đối\s+kế\s+toán|"
    r"báo\s+cáo\s+tình\s+hình\s+tài\s+chính|"
    r"báo\s+cáo\s+kết\s+quả\s+hoạt\s+động\s+kinh\s+doanh|"
    r"báo\s+cáo\s+lưu\s+chuyển\s+tiền\s+tệ|"
    r"thuyết\s+minh\s+báo\s+cáo\s+tài\s+chính"
    r")\b",
    flags=re.IGNORECASE,
)
_FRONT_SECTION_RE = re.compile(
    r"^(?:"
    r"mục\s+lục|"
    r"báo\s+cáo\s+của\s+ban\s+(?:tổng\s+)?giám\s+đốc|"
    r"báo\s+cáo\s+kiểm\s+toán(?:\s+độc\s+lập)?|"
    r"báo\s+cáo\s+soát\s+xét(?:\s+báo\s+cáo\s+tài\s+chính.*)?"
    r")$",
    flags=re.IGNORECASE,
)
_SKIP_SECTION_RE = re.compile(
    r"^báo\s+cáo\s+tài\s+chính(?:\s+riêng|\s+hợp\s+nhất|\s+giữa\s+niên\s+độ|\s+đã\s+được.*)?$",
    flags=re.IGNORECASE,
)
_REPORT_HEADER_PREFIXES = (
    "địa chỉ:",
    "khu công nghiệp",
    "báo cáo tài chính",
    "cho năm tài chính",
    "cho kỳ hoạt động",
)
_UNIT_ONLY_CELLS = {"vnd", "vnđ", "đồng", "dong"}
_SUPPLEMENTAL_REPORT_TOPIC_RE = re.compile(
    r"(chuẩn\s+mực\s+kế\s+toán|chế\s+độ\s+kế\s+toán|"
    r"tuyên\s+bố\s+.*tuân\s+thủ\s+chuẩn\s+mực|"
    r"công\s+ty\s+kiểm\s+toán|hãng\s+kiểm\s+toán|đơn\s+vị\s+kiểm\s+toán|"
    r"thực\s+hiện\s+kiểm\s+toán)",
    flags=re.IGNORECASE,
)
# Digital-signature / PDF-scanner artifacts emitted on the cover page. These leak
# into facts as garbage headings and values (e.g. the cert DN/OID line becomes a
# subheading, the signing timestamp and reader version become a fact value), so
# they must be dropped before any line becomes a heading or paragraph.
_SIGNATURE_NOISE_RE = re.compile(
    r"^(?:"
    r"digitally\s+signed\s+by\b"
    r"|dn:\s*c=.*\boid\."  # certificate distinguished name
    r"|.*\boid\.\d"  # any line carrying an OID dotted number
    r"|reason:\s*$"  # empty signature fields
    r"|location:\s*$"
    r"|date:\s*\d{4}\.\d{2}\.\d{2}\s+\d{1,2}:\d{2}:\d{2}"  # signing timestamp
    r"|.*\bpdf\s+reader\s+version\b"
    r"|.*\bfoxit\b"
    r")",
    flags=re.IGNORECASE,
)


def _clean_line(line: str) -> str:
    return _strip_inline_formatting(str(line or "")).strip()


def _is_signature_noise_line(line: str) -> bool:
    return bool(_SIGNATURE_NOISE_RE.match(_clean_line(line)))


def _is_all_caps_heading(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    if len(letters) < 4:
        return False
    upper_count = sum(1 for char in letters if char.isupper())
    lower_count = sum(1 for char in letters if char.islower())
    return upper_count > 0 and upper_count >= lower_count


def _readable_heading(text: str) -> str:
    cleaned = _clean_line(text).strip("#").strip()
    if not cleaned:
        return ""
    if not _is_all_caps_heading(cleaned):
        return cleaned
    lowered = cleaned.lower()
    return lowered[:1].upper() + lowered[1:]


def _is_report_header_line(line: str) -> bool:
    if _is_signature_noise_line(line):
        return True
    # Strip leading markdown heading markers so bare cover-page titles
    # ("# CÔNG TY ...", "# BÁO CÁO TÀI CHÍNH ...") are recognised as headers
    # instead of leaking through as paragraph values.
    cleaned = _clean_line(line).lstrip("#").strip()
    lowered = cleaned.lower()
    if not cleaned:
        return False
    if cleaned.isdigit():
        return True
    if lowered.startswith("công ty ") and (
        "báo cáo tài chính" in lowered or len(cleaned) <= 60 or _is_all_caps_heading(cleaned)
    ):
        return True
    if lowered.startswith("no:"):
        return True
    if lowered.startswith("t:") and "aasc" in lowered:
        return True
    return any(lowered.startswith(prefix) for prefix in _REPORT_HEADER_PREFIXES)


def _split_marked_pages(md_text: str) -> list[dict]:
    text = str(md_text or "")
    matches = list(_PAGE_MARKER_RE.finditer(text))
    if not matches:
        return [{"printed_page": None, "content": text}]

    pages = []
    preamble = text[: matches[0].start()]
    if preamble.strip():
        first_marker_page = int(matches[0].group(1))
        pages.append(
            {
                "printed_page": first_marker_page if first_marker_page > 0 else None,
                "content": preamble,
            }
        )

    for index, match in enumerate(matches):
        marker_page = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages.append(
            {
                "printed_page": marker_page + 1,
                "content": text[start:end],
            }
        )
    return pages


def _front_slice_pages(md_text: str) -> list[dict]:
    pages = []
    reached_main_statement = False

    for page in _split_marked_pages(md_text):
        content_lines = []
        for line in str(page.get("content", "") or "").splitlines():
            if _MAIN_STATEMENT_HEADING_RE.match(_clean_line(line).strip("#").strip()):
                reached_main_statement = True
                break
            content_lines.append(line)

        if content_lines and not reached_main_statement:
            pages.append({**page, "content": "\n".join(content_lines)})
        if reached_main_statement:
            break

    return pages


def _section_heading_from_line(line: str) -> str:
    cleaned = _clean_line(line).strip("#").strip()
    if not cleaned or _is_report_header_line(cleaned):
        return ""

    readable = _readable_heading(cleaned)
    if not readable:
        return ""

    if _FRONT_SECTION_RE.match(cleaned):
        return readable
    if str(line or "").lstrip().startswith("#") and _SKIP_SECTION_RE.match(cleaned):
        return ""
    if str(line or "").lstrip().startswith("#") and len(cleaned) <= 140:
        return readable

    return ""


def _subsection_heading_from_line(line: str) -> str:
    cleaned = _clean_line(line).strip("#").strip()
    if not cleaned or _is_report_header_line(cleaned):
        return ""
    if _FRONT_SECTION_RE.match(cleaned) or _SKIP_SECTION_RE.match(cleaned):
        return ""
    if str(line or "").lstrip().startswith("#") and len(cleaned) <= 140:
        return _readable_heading(cleaned)
    if _is_all_caps_heading(cleaned) and len(cleaned) <= 140:
        return _readable_heading(cleaned)
    return ""


def _source_with_page(source: str, page: int | None) -> str:
    if page is None:
        return source
    return f"{source}#page={page}"


def _supplemental_topic_title(text: str) -> str:
    lowered = str(text or "").lower()
    if "chuẩn mực kế toán" in lowered or "chế độ kế toán" in lowered:
        return "Chuẩn mực và chế độ kế toán"
    if "kiểm toán" in lowered:
        return "Đơn vị kiểm toán"
    return "Thông tin báo cáo tài chính"


def _row_tuple(
    *,
    company: str,
    fiscal_year: str,
    item_code: str,
    subheading: str,
    item_name: str,
    value: str,
    source: str,
):
    text = _strip_inline_formatting(value)
    if not text:
        return None
    return (
        company,
        fiscal_year,
        TABLE_REPORT_SECTION,
        item_code,
        "",
        subheading,
        item_name,
        text,
        text,
        text,
        source,
    )


def _item_name(section: str, subsection: str, label: str = "") -> str:
    parts = [
        part
        for part in (
            section or "Phần đầu báo cáo tài chính",
            subsection,
            label,
        )
        if str(part or "").strip()
    ]
    return " | ".join(parts)


def _looks_like_numeric_amount(value: str) -> bool:
    text = _clean_line(value)
    if not text:
        return False
    if text in {"-", "–"}:
        return True
    compact = text.replace(".", "").replace(",", "").replace(" ", "")
    return compact.isdigit()


def _table_row_label(cells: list[str]) -> str:
    if not cells:
        return ""
    first_cell = str(cells[0] or "").strip()
    value_cells = [cell for cell in cells[1:] if str(cell or "").strip()]
    if not first_cell and value_cells and all(_looks_like_numeric_amount(cell) for cell in value_cells):
        return "Tổng"
    return next((cell for cell in cells if cell), "")


def _iter_table_rows(df: pd.DataFrame) -> Iterable[tuple[str, str]]:
    df = df.map(lambda value: "" if pd.isna(value) else _strip_inline_formatting(value))
    columns = [_strip_inline_formatting(column) for column in df.columns]

    for _, row in df.iterrows():
        cells = [str(row.get(column, "") or "").strip() for column in df.columns]
        if not any(cells):
            continue
        if all(_clean_line(cell).lower() in _UNIT_ONLY_CELLS for cell in cells if cell):
            continue

        label = _table_row_label(cells)
        pairs = [
            f"{column}: {cell}"
            for column, cell in zip(columns, cells)
            if column and cell
        ]
        yield label, " | ".join(pairs)


def _front_table_rows(
    table_lines: list[str],
    *,
    company: str,
    fiscal_year: str,
    section: str,
    subsection: str,
    source: str,
    page: int | None,
) -> list[tuple]:
    try:
        df = markdown_table_to_df(table_lines)
    except Exception:
        return []

    if df is None or df.empty:
        return []

    rows = []
    for label, value in _iter_table_rows(df):
        row = _row_tuple(
            company=company,
            fiscal_year=fiscal_year,
            item_code="report_section_table",
            subheading=subsection,
            item_name=_item_name(section, subsection, label),
            value=value,
            source=_source_with_page(source, page),
        )
        if row is not None:
            rows.append(row)
    return rows


def build_frontmatter_rows(md_text: str, company: str, source: str, fiscal_year=None) -> list[tuple]:
    rows = []
    year = str(fiscal_year if fiscal_year is not None else infer_fiscal_year(md_text) or "").strip()
    company_name = str(company or "").strip() or infer_company(md_text)
    current_section = "Phần đầu báo cáo tài chính"
    current_subsection = ""
    paragraph_lines: list[str] = []
    table_lines: list[str] = []

    def flush_paragraph(page: int | None):
        nonlocal paragraph_lines
        paragraph = " ".join(_clean_line(line) for line in paragraph_lines if _clean_line(line))
        paragraph_lines = []
        if not paragraph:
            return
        row = _row_tuple(
            company=company_name,
            fiscal_year=year,
            item_code="report_section_text",
            subheading=current_subsection,
            item_name=_item_name(current_section, current_subsection),
            value=paragraph,
            source=_source_with_page(source, page),
        )
        if row is not None:
            rows.append(row)

    def flush_table(page: int | None):
        nonlocal table_lines
        if not table_lines:
            return
        rows.extend(
            _front_table_rows(
                table_lines,
                company=company_name,
                fiscal_year=year,
                section=current_section,
                subsection=current_subsection,
                source=source,
                page=page,
            )
        )
        table_lines = []

    for page in _front_slice_pages(md_text):
        page_number = page.get("printed_page")
        for line in str(page.get("content", "") or "").splitlines():
            stripped = line.strip()

            if stripped.startswith("|"):
                flush_paragraph(page_number)
                table_lines.append(line)
                continue

            flush_table(page_number)

            section = _section_heading_from_line(line)
            if section:
                flush_paragraph(page_number)
                current_section = section
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

    seen_supplemental = {
        (
            str(row[6] or "").strip().lower(),
            str(row[7] or "").strip().lower(),
        )
        for row in rows
    }
    for page in _split_marked_pages(md_text):
        page_number = page.get("printed_page")
        paragraph_lines = []

        def flush_supplemental():
            nonlocal paragraph_lines
            paragraph = " ".join(_clean_line(line) for line in paragraph_lines if _clean_line(line))
            paragraph_lines = []
            if not paragraph or not _SUPPLEMENTAL_REPORT_TOPIC_RE.search(paragraph):
                return
            topic = _supplemental_topic_title(paragraph)
            item_name = _item_name("Thông tin báo cáo tài chính", topic)
            key = (item_name.lower(), paragraph.lower())
            if key in seen_supplemental:
                return
            row = _row_tuple(
                company=company_name,
                fiscal_year=year,
                item_code="report_section_text",
                subheading=topic,
                item_name=item_name,
                value=paragraph,
                source=_source_with_page(source, page_number),
            )
            if row is not None:
                rows.append(row)
                seen_supplemental.add(key)

        for line in str(page.get("content", "") or "").splitlines():
            stripped = line.strip()
            if stripped.startswith("|"):
                flush_supplemental()
                continue
            if not stripped:
                flush_supplemental()
                continue
            if _is_report_header_line(line):
                continue
            paragraph_lines.append(line)
        flush_supplemental()

    return rows
