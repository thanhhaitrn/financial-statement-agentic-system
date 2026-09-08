"""Extract front sections before the primary financial statements."""
# Code note: Front-matter rows cover management/audit/review narrative outside the notes.

import re
from typing import Iterable

import pandas as pd

from ingestion.kb_builder import (
    _strip_inline_formatting,
    aggregation_level,
    compose_section_path,
    parsed_value,
    semantic_scope_label,
    value_kind,
)
from ingestion.note_parser import infer_company, infer_fiscal_year
from ingestion.semantic_dimensions import derive_semantic_fact_dimensions
from ingestion.table_parser import markdown_table_to_df
from ingestion.topic_atoms import derive_topic_atoms
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
    r")(?:\s*\((?:tiếp\s+theo|continued)\))?$",
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
_TOC_PAGE_SUFFIX_RE = re.compile(
    r"\s+\d{1,3}(?:\s*[-–—]\s*\d{1,3})?\s*$"
)
_NUMBERED_TOPIC_RE = re.compile(
    r"^\s*\d+(?:\.\d+)*[.)]?\s+\S.{1,138}$",
    flags=re.IGNORECASE,
)
_LETTER_SUBSECTION_RE = re.compile(
    r"^\s*(?:\((?P<paren>[a-zđ])\)|(?P<plain>[a-zđ])\))\s+(?P<title>.+?)\s*$",
    flags=re.IGNORECASE,
)
_BOLD_ONLY_RE = re.compile(
    r"^\s*(?:\*{2,3}|_{2,3})(?P<title>.+?)(?:\*{2,3}|_{2,3})\s*:?\s*$"
)
_LABELED_REPORT_ID_RE = re.compile(
    r"^(?P<label>"
    r"số(?:\s+(?:báo\s+cáo|tham\s+chiếu|kiểm\s+toán))?|"
    r"báo\s+cáo\s+kiểm\s+toán\s+số|"
    r"no\.?|reference(?:\s+no\.?)?"
    r")\s*:\s*(?P<value>\S(?:.*\S)?)\s*$",
    flags=re.IGNORECASE,
)
_ASSURANCE_HEADING_RE = re.compile(
    r"^báo\s+cáo\s+(?P<kind>kiểm\s+toán|soát\s+xét)\b",
    flags=re.IGNORECASE,
)
_AUDIT_ENTITY_RE = re.compile(
    r"^(?:"
    r"(?:chi\s+nhánh\s+)?công\s+ty\s+"
    r"(?:tnhh|trách\s+nhiệm\s+hữu\s+hạn|cổ\s+phần)\b"
    r"|.+\b(?:auditing|audit)\b.*\bcompany(?:\s+limited)?\b"
    r")",
    flags=re.IGNORECASE,
)
_STANDALONE_REPORT_DATE_RE = re.compile(
    r"^(?:[^,\n]{1,80},\s*)?"
    r"(?P<date>"
    r"ngày\s+\d{1,2}\s+tháng\s+\d{1,2}\s+năm\s+(?:19|20)\d{2}|"
    r"\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}"
    r")\s*[.]?$",
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


def _is_bold_only_line(line: str) -> bool:
    match = _BOLD_ONLY_RE.match(str(line or ""))
    if not match:
        return False
    title = _clean_line(match.group("title"))
    return bool(title and len(title) <= 140)


def _labeled_report_identifier(line: str) -> tuple[str, str] | None:
    match = _LABELED_REPORT_ID_RE.match(_clean_line(line))
    if not match:
        return None
    value = str(match.group("value") or "").strip()
    # A report reference is expected to contain a digit.  This rejects labels
    # whose OCR value is missing without guessing an identifier.
    if not value or not any(char.isdigit() for char in value):
        return None
    return str(match.group("label") or "").strip(), value


def _assurance_scope_from_text(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = _clean_line(line).strip("#").strip()
        if _TOC_PAGE_SUFFIX_RE.search(cleaned):
            continue
        if _labeled_report_identifier(cleaned):
            continue
        match = _ASSURANCE_HEADING_RE.match(cleaned)
        if not match:
            continue
        if str(match.group("kind") or "").lower().startswith("soát"):
            return "Báo cáo soát xét"
        return "Báo cáo kiểm toán độc lập"
    return ""


def _assurance_masthead_entity(
    text: str,
    *,
    company: str,
) -> str:
    """Return an audit/review firm's masthead only on an assurance page."""

    lines = str(text or "").splitlines()
    heading_index = next(
        (
            index
            for index, line in enumerate(lines)
            if (
                _ASSURANCE_HEADING_RE.match(_clean_line(line).strip("#").strip())
                and not _TOC_PAGE_SUFFIX_RE.search(
                    _clean_line(line).strip("#").strip()
                )
                and not _labeled_report_identifier(line)
            )
        ),
        None,
    )
    if heading_index is None:
        return ""

    company_key = re.sub(r"\W+", "", _clean_line(company).casefold())
    candidates = lines[:heading_index]
    for line in candidates:
        cleaned = _clean_line(line).strip("#").strip()
        if (
            not cleaned
            or _is_signature_noise_line(cleaned)
            or _labeled_report_identifier(cleaned)
            or not _AUDIT_ENTITY_RE.match(cleaned)
        ):
            continue
        candidate_key = re.sub(r"\W+", "", cleaned.casefold())
        if company_key and (
            candidate_key == company_key
            or candidate_key in company_key
            or company_key in candidate_key
        ):
            continue
        return cleaned
    return ""


def _standalone_report_date(line: str) -> str:
    match = _STANDALONE_REPORT_DATE_RE.match(_clean_line(line))
    if not match:
        return ""
    return str(match.group("date") or "").strip()


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
    in_toc = False

    for page in _split_marked_pages(md_text):
        content_lines = []
        for line in str(page.get("content", "") or "").splitlines():
            cleaned = _clean_line(line).strip("#").strip()
            if cleaned.casefold() == "mục lục":
                in_toc = True

            front_section = _FRONT_SECTION_RE.match(cleaned)
            if front_section and cleaned.casefold() != "mục lục":
                in_toc = False

            if _MAIN_STATEMENT_HEADING_RE.match(cleaned):
                toc_entry = bool(_TOC_PAGE_SUFFIX_RE.search(cleaned))
                if in_toc and not str(line or "").lstrip().startswith("#"):
                    toc_entry = True
                if not toc_entry:
                    reached_main_statement = True
                    break
            content_lines.append(line)

        # A page marker is optional, and front matter commonly shares a page
        # with the first statement heading. Preserve the prefix collected before
        # that heading instead of discarding the entire page.
        if content_lines:
            pages.append({**page, "content": "\n".join(content_lines)})
        if reached_main_statement:
            break

    return pages


def _section_heading_from_line(line: str) -> str:
    cleaned = _clean_line(line).strip("#").strip()
    if (
        not cleaned
        or _is_report_header_line(cleaned)
        or _labeled_report_identifier(cleaned)
    ):
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
    if _is_bold_only_line(line) and _NUMBERED_TOPIC_RE.match(cleaned):
        return readable

    return ""


def _subsection_heading_from_line(line: str) -> str:
    cleaned = _clean_line(line).strip("#").strip()
    if (
        not cleaned
        or _is_report_header_line(cleaned)
        or _labeled_report_identifier(cleaned)
    ):
        return ""
    if _FRONT_SECTION_RE.match(cleaned) or _SKIP_SECTION_RE.match(cleaned):
        return ""

    letter_match = _LETTER_SUBSECTION_RE.match(cleaned)
    if letter_match:
        label = str(
            letter_match.group("paren") or letter_match.group("plain") or ""
        ).lower()
        title = _readable_heading(str(letter_match.group("title") or ""))
        return f"{label}) {title}" if label and title else ""

    if str(line or "").lstrip().startswith("#") and len(cleaned) <= 140:
        return _readable_heading(cleaned)
    if _is_bold_only_line(line) and len(cleaned) <= 140:
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


def _report_identifier_label(scope: str, source_label: str) -> str:
    lowered = _clean_line(source_label).casefold()
    if "tham chiếu" in lowered or "reference" in lowered:
        return "Số tham chiếu"
    if "kiểm toán" in _clean_line(scope).casefold() or "kiểm toán" in lowered:
        return "Số báo cáo kiểm toán"
    if "soát xét" in _clean_line(scope).casefold():
        return "Số báo cáo soát xét"
    return "Số báo cáo"


def _row_tuple(
    *,
    company: str,
    fiscal_year: str,
    item_code: str,
    subheading: str,
    item_name: str,
    value: str,
    source: str,
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
    semantic_row_label = _clean_line(row_label or item_name)
    semantic_column_label = _clean_line(column_label)
    semantic_kind = value_kind_override or value_kind(
        text,
        row_label=semantic_row_label,
        column_label=semantic_column_label,
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
        "",
        "",
        "",
        semantic_row_label,
        semantic_column_label,
        semantic_kind,
        parsed_value(text, kind=semantic_kind),
        "",
        "",
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
        # Canonical cell parsing may recover this row when a group and terminal/
        # arithmetic evidence make it a safe total.  A row blob has insufficient
        # structure, so fail closed instead of inventing a generic "Tổng".
        return ""
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
        if not label:
            continue
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
    block_id: str = "",
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
            row_label=label,
            column_label="table_row",
            aggregation=aggregation_level(label),
            section_path=compose_section_path(
                TABLE_REPORT_SECTION,
                section,
                subsection,
            ),
            block_id=block_id,
            source_page=page,
            metric_label=label,
            scope_label=subsection or section,
        )
        if row is not None:
            rows.append(row)
    return rows


def build_frontmatter_rows(
    md_text: str,
    company: str,
    source: str,
    fiscal_year=None,
    *,
    include_table_rows: bool = True,
) -> list[tuple]:
    rows = []
    year = str(fiscal_year if fiscal_year is not None else infer_fiscal_year(md_text) or "").strip()
    company_name = str(company or "").strip() or infer_company(md_text)
    current_section = "Phần đầu báo cáo tài chính"
    current_subsection = ""
    paragraph_lines: list[str] = []
    table_lines: list[str] = []
    block_counter = 0
    active_assurance_scope = ""
    seen_structured: set[tuple[str, str, str, str, str]] = set()

    def next_block_id(kind: str) -> str:
        nonlocal block_counter
        block_counter += 1
        return f"front-{kind}-{block_counter:06d}"

    def add_structured_fact(
        *,
        item_code: str,
        section: str,
        subsection: str = "",
        topic: str,
        value: str,
        page: int | None,
        metric_label: str = "",
        entity_label: str = "",
        value_kind_override: str = "",
    ) -> None:
        cleaned_value = _clean_line(value)
        key = (
            _clean_line(section).casefold(),
            _clean_line(subsection).casefold(),
            topic.casefold(),
            cleaned_value.casefold(),
            _clean_line(entity_label).casefold(),
        )
        if not cleaned_value or key in seen_structured:
            return
        row = _row_tuple(
            company=company_name,
            fiscal_year=year,
            item_code=item_code,
            subheading=topic,
            item_name=_item_name(section, subsection, topic),
            value=cleaned_value,
            source=_source_with_page(source, page),
            row_label=topic,
            column_label=topic,
            section_path=compose_section_path(
                TABLE_REPORT_SECTION,
                section,
                subsection,
                topic,
            ),
            block_id=next_block_id("atom"),
            source_page=page,
            value_kind_override=value_kind_override,
            metric_label=metric_label or topic,
            entity_label=entity_label,
            scope_label=subsection or section,
        )
        if row is not None:
            rows.append(row)
            seen_structured.add(key)

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
            row_label=_item_name(current_section, current_subsection),
            section_path=compose_section_path(
                TABLE_REPORT_SECTION,
                current_section,
                current_subsection,
            ),
            block_id=next_block_id("text"),
            source_page=page,
            value_kind_override="text",
            scope_label=current_subsection or current_section,
        )
        if row is not None:
            rows.append(row)
        for atom in derive_topic_atoms(
            paragraph,
            section=current_section,
            subsection=current_subsection,
        ):
            item_code = {
                "person_event": "report_section_person_event",
                "person_event_date": "report_section_person_event_date",
                "person_role": "report_section_person_role",
                "corporate_event": "report_section_corporate_event",
                "corporate_event_date": "report_section_corporate_event_date",
                "corporate_event_identifier": (
                    "report_section_corporate_event_identifier"
                ),
                "policy": "report_section_policy_atom",
            }.get(atom.atom_type, "report_section_topic_atom")
            atom_value_kind = {
                "person_event_date": "date",
                "corporate_event_date": "date",
                "corporate_event_identifier": "identifier",
            }.get(atom.atom_type, "")
            add_structured_fact(
                item_code=item_code,
                section=current_section,
                subsection=current_subsection,
                topic=atom.topic,
                value=atom.value,
                page=page,
                metric_label=atom.metric_label,
                entity_label=atom.entity_label,
                value_kind_override=atom_value_kind,
            )

    def flush_table(page: int | None):
        nonlocal table_lines
        if not table_lines:
            return
        if not include_table_rows:
            # build_fact_rows owns canonical cells for every table, including
            # front matter.  The section parser keeps only narrative/topic
            # context in the production pipeline.
            table_lines = []
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
                block_id=next_block_id("table"),
            )
        )
        table_lines = []

    for page in _front_slice_pages(md_text):
        page_number = page.get("printed_page")
        page_content = str(page.get("content", "") or "")
        page_assurance_scope = _assurance_scope_from_text(page_content)
        assurance_active_before_page = active_assurance_scope
        assurance_heading_seen = False
        masthead_entity = _assurance_masthead_entity(
            page_content,
            company=company_name,
        )
        if page_assurance_scope and masthead_entity:
            add_structured_fact(
                item_code="report_section_entity",
                section=page_assurance_scope,
                topic="Đơn vị kiểm toán",
                value=masthead_entity,
                page=page_number,
                entity_label=masthead_entity,
            )

        for line in page_content.splitlines():
            stripped = line.strip()
            cleaned = _clean_line(line).strip("#").strip()

            if stripped.startswith("|"):
                flush_paragraph(page_number)
                table_lines.append(line)
                continue

            flush_table(page_number)

            identifier = _labeled_report_identifier(line)
            if identifier:
                flush_paragraph(page_number)
                identifier_scope = page_assurance_scope or active_assurance_scope
                if identifier_scope:
                    source_label, identifier_value = identifier
                    add_structured_fact(
                        item_code="report_section_identifier",
                        section=identifier_scope,
                        topic=_report_identifier_label(
                            identifier_scope,
                            source_label,
                        ),
                        value=identifier_value,
                        page=page_number,
                    )
                else:
                    # Outside an audit/review scope, preserve the source line as
                    # narrative rather than inventing a report-number meaning.
                    paragraph_lines.append(line)
                continue

            assurance_match = _ASSURANCE_HEADING_RE.match(cleaned)
            section = _section_heading_from_line(line)
            if section:
                flush_paragraph(page_number)
                current_section = section
                current_subsection = ""
                if assurance_match:
                    active_assurance_scope = (
                        "Báo cáo soát xét"
                        if str(assurance_match.group("kind") or "")
                        .lower()
                        .startswith("soát")
                        else "Báo cáo kiểm toán độc lập"
                    )
                    assurance_heading_seen = True
                elif _FRONT_SECTION_RE.match(cleaned):
                    active_assurance_scope = ""
                continue

            subsection = _subsection_heading_from_line(line)
            if subsection:
                flush_paragraph(page_number)
                current_subsection = subsection
                continue

            report_date = _standalone_report_date(line)
            date_is_assurance_scoped = bool(
                active_assurance_scope
                and (
                    assurance_heading_seen
                    or (
                        assurance_active_before_page
                        and not page_assurance_scope
                    )
                )
            )
            if report_date and date_is_assurance_scoped:
                flush_paragraph(page_number)
                add_structured_fact(
                    item_code="report_section_date",
                    section=active_assurance_scope,
                    topic="Ngày báo cáo",
                    value=report_date,
                    page=page_number,
                )
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
                row_label=item_name,
                section_path=compose_section_path(
                    TABLE_REPORT_SECTION,
                    "Thông tin báo cáo tài chính",
                    topic,
                ),
                block_id=next_block_id("supplemental"),
                source_page=page_number,
                value_kind_override="text",
                scope_label=topic,
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
