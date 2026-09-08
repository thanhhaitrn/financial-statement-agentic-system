"""Parse financial-statement markdown tables into normalized SQLite fact rows."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

from decimal import Decimal, InvalidOperation
import pandas as pd
from ingestion.table_parser import markdown_table_to_df
from ingestion.period_normalize import (
    canonical_balance_sheet_column_label,
    canonical_period,
    canonical_value_type,
    column_period,
    parse_unit,
    period_phrase_alias,
    section_total_alias,
    section_total_key,
)
from ingestion.semantic_dimensions import derive_semantic_fact_dimensions
import re
from schemas.table_names import (
    normalize_table_heading,
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
)

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
    "khấu hao lũy kế",
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
_DATE_VALUE_RE = re.compile(
    r"^(?:\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}|"
    r"(?:ngày\s+)?\d{1,2}\s+tháng\s+\d{1,2}\s+năm\s+(?:19|20)\d{2})$",
    flags=re.IGNORECASE,
)
_IDENTIFIER_LABEL_RE = re.compile(
    r"(?:"
    r"(?:mã|số)\s+(?:báo cáo|đăng ký|chứng nhận|giấy phép|hợp đồng|tham chiếu)|"
    r"mã\s+(?:chứng\s+khoán|cổ\s+phiếu)|"
    r"số\s+báo\s+cáo\s+kiểm\s+toán"
    r")",
    flags=re.IGNORECASE,
)
_COUNT_LABEL_RE = re.compile(
    r"(?:số lượng|số\s+năm|bao nhiêu|nhân sự|nhân viên|người|cổ phiếu)\b",
    flags=re.IGNORECASE,
)
_MULTIPLE_LABEL_RE = re.compile(
    r"(?:bao\s+nhiêu\s+lần|gấp\s+(?:bao\s+nhiêu\s+)?lần|số\s+lần)\b",
    flags=re.IGNORECASE,
)
_NUMERIC_RANGE_RE = re.compile(
    r"^\s*\d+(?:[.,]\d+)?\s*(?:[-–—]|đến)\s*\d+(?:[.,]\d+)?\s*$",
    flags=re.IGNORECASE,
)
_ENTITY_COLUMN_RE = re.compile(
    r"(?:"
    r"trụ\s+sở|địa\s+chỉ|địa\s+điểm|nơi\s+thành\s+lập|"
    r"họ\s+và\s+tên|tên(?:\s+công\s+ty|\s+đơn\s+vị)?|"
    r"đơn\s+vị\s+kiểm\s+toán|công\s+ty\s+kiểm\s+toán|"
    r"bên\s+liên\s+quan|mối\s+quan\s+hệ|chức\s+vụ"
    r")",
    flags=re.IGNORECASE,
)
_LABEL_COLUMN_RE = re.compile(
    r"^(?:"
    r"chỉ\s+tiêu|khoản\s+mục|diễn\s+giải|đối\s+tượng|"
    r"họ\s+và\s+tên|tên(?:\s+công\s+ty|\s+đơn\s+vị)?|"
    r"bên\s+liên\s+quan|loại\s+tài\s+sản|"
    r"tài\s+sản|nguồn\s+vốn"
    r")$",
    flags=re.IGNORECASE,
)
_TOTAL_LABEL_RE = re.compile(
    r"^(?:tổng(?:\s+cộng)?|cộng)\b",
    flags=re.IGNORECASE,
)
_TOTAL_COLUMN_RE = re.compile(
    r"^(?:tổng|cộng|tổng cộng|total)"
    r"(?:\s+(?:vnd|vnđ|đồng|dong|nghìn\s+đồng|triệu\s+đồng))?$",
    flags=re.IGNORECASE,
)
_BROAD_TOTAL_GROUPS = {
    "",
    "bảng cân đối kế toán",
    "báo cáo kết quả hoạt động kinh doanh",
    "báo cáo lưu chuyển tiền tệ",
    "thuyết minh báo cáo tài chính",
    "phần đầu báo cáo tài chính",
}
_TEXT_PLACEHOLDERS = {
    "",
    "-",
    "–",
    "—",
    "n/a",
    "na",
    "vnd",
    "vnđ",
    "đồng",
    "dong",
    "()",
    "( )",
    "empty",
    "null",
    "none",
}
_NON_DATA_TEXT_COLUMN_RE = re.compile(r"^(?:trang|page)$", flags=re.IGNORECASE)
_TEXT_ARTIFACT_RE = re.compile(
    r"^!?\s*\\?\[(?:image|signature|seal|logo)\b",
    flags=re.IGNORECASE,
)
_ENTITY_AXIS_HEADER_RE = re.compile(
    r"^(?:"
    r"tên(?:\s+(?:công\s+ty|đơn\s+vị|bên\s+liên\s+quan))?|"
    r"bên\s+liên\s+quan|đối\s+tượng|khách\s+hàng|nhà\s+cung\s+cấp|"
    r"ngân\s+hàng|đơn\s+vị|công\s+ty|khu\s+vực|bộ\s+phận"
    r")$",
    flags=re.IGNORECASE,
)
_METRIC_AXIS_HEADER_RE = re.compile(
    r"^(?:"
    r"chỉ\s+tiêu|khoản\s+mục|diễn\s+giải|nội\s+dung|"
    r"giao\s+dịch|loại\s+giao\s+dịch"
    r")$",
    flags=re.IGNORECASE,
)
_RELATIONSHIP_AXIS_HEADER_RE = re.compile(
    r"^(?:"
    r"mối\s+quan\s+hệ|quan\s+hệ|relationship"
    r")$",
    flags=re.IGNORECASE,
)
_TRANSACTION_DESCRIPTOR_AXIS_HEADER_RE = re.compile(
    r"^(?:"
    r"loại\s+giao\s+dịch|nội\s+dung\s+(?:giao\s+dịch|nghiệp\s+vụ)|"
    r"giao\s+dịch|transaction(?:\s+(?:type|nature))?|"
    r"nature\s+of\s+transaction"
    r")$",
    flags=re.IGNORECASE,
)
_STRONG_ENTITY_LABEL_RE = re.compile(
    r"^(?:"
    r"(?:công\s+ty|tổng\s+công\s+ty|ngân\s+hàng|tập\s+đoàn|"
    r"chi\s+nhánh|nhà\s+máy|ông|bà)\b|"
    r"(?:trong\s+nước|nước\s+ngoài|miền\s+bắc|miền\s+trung|miền\s+nam)|"
    r"(?:domestic|overseas|foreign)\b"
    r")",
    flags=re.IGNORECASE,
)
_METRIC_LABEL_RE = re.compile(
    r"\b(?:"
    r"doanh\s+thu|chi\s+phí|lợi\s+nhuận|lãi|lỗ|"
    r"mua|bán|vay|cho\s+vay|phải\s+thu|phải\s+trả|"
    r"số\s+dư|tăng|giảm|hoàn\s+trả|thu|chi|"
    r"nguyên\s+giá|hao\s+mòn|khấu\s+hao|dự\s+phòng|"
    r"giá\s+trị|số\s+lượng|tỷ\s+lệ|thời\s+gian|"
    r"hoạt\s+động\s+chính|trụ\s+sở|địa\s+chỉ"
    r")\b",
    flags=re.IGNORECASE,
)
_PERIOD_OR_UNIT_AXIS_RE = re.compile(
    r"(?:"
    r"(?:19|20)\d{2}|"
    r"\b(?:đầu|cuối)\s+(?:kỳ|năm|quý|tháng)\b|"
    r"\b(?:năm|kỳ|quý|tháng)\s+(?:19|20)?\d+\b|"
    r"\b\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}\b|"
    r"\b(?:vnd|vnđ|đồng|nghìn\s+đồng|triệu\s+đồng|percent)\b"
    r")",
    flags=re.IGNORECASE,
)
_SEMANTIC_AXIS_NOISE_RE = re.compile(
    r"(?:"
    r"\b\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}\s*"
    r"(?:vnd|vnđ|đồng|nghìn\s+đồng|triệu\s+đồng)\b|"
    r"\b(?:19|20)\d{2}\s*(?:vnd|vnđ|đồng|nghìn\s+đồng|triệu\s+đồng)\b|"
    r"\b(?:năm|kỳ)\s+(?:19|20)\d{2}\b|"
    r"\b(?:19|20)\d{2}\b|"
    r"\b\d{1,2}[/-]\d{1,2}[/-](?:19|20)\d{2}\b|"
    r"\b(?:vnd|vnđ|đồng|nghìn\s+đồng|triệu\s+đồng)\b"
    r")",
    flags=re.IGNORECASE,
)
_HIERARCHY_NUMERIC_RE = re.compile(
    r"^\s*(?P<number>\d+(?:\.\d+)*)[.)]?\s+\S",
    flags=re.IGNORECASE,
)
_HIERARCHY_ROMAN_RE = re.compile(
    r"^\s*(?P<number>[IVXLC]+)[.)]\s+\S",
    flags=re.IGNORECASE,
)
_HIERARCHY_LETTER_RE = re.compile(
    r"^\s*(?:\((?P<paren>[a-zđ])\)|(?P<plain>[A-ZĐa-zđ])[.)])\s+\S",
)
_REVENUE_CONTEXT_RE = re.compile(
    r"\b(?:tổng\s+)?doanh\s+thu\b",
    flags=re.IGNORECASE,
)
_REVENUE_COMPONENT_RE = re.compile(
    r"^(?:bán\b|cung\s+cấp\b|cho\s+thuê\b)",
    flags=re.IGNORECASE,
)


def _norm_label(text: str) -> str:
    return re.sub(r"\s+", " ", _strip_inline_formatting(text).lower()).strip()


def _canonical_section_label(text: str) -> str:
    normalized = _norm_label(text).strip(" :-–—")
    normalized = normalized.replace("luỹ", "lũy")
    normalized = re.sub(r"^-\s*", "", normalized)
    aliases = {
        "nguyên giá": "Nguyên giá",
        "giá trị hao mòn": "Giá trị hao mòn",
        "giá trị hao mòn lũy kế": "Giá trị hao mòn lũy kế",
        "hao mòn lũy kế": "Giá trị hao mòn lũy kế",
        "giá trị khấu hao lũy kế": "Giá trị hao mòn lũy kế",
        "khấu hao lũy kế": "Giá trị hao mòn lũy kế",
        "giá trị còn lại": "Giá trị còn lại",
    }
    return aliases.get(normalized, "")


def _parse_decimal_value(text: str) -> Decimal | None:
    """Parse common Vietnamese accounting numbers without guessing dates/codes."""

    raw = _strip_inline_formatting(text)
    if not raw or _DATE_VALUE_RE.match(raw):
        return None
    compact = raw.strip().replace("\xa0", "").replace(" ", "")
    negative = compact.startswith("(") and compact.endswith(")")
    if negative:
        compact = compact[1:-1]
    if compact.startswith("-"):
        negative = True
        compact = compact[1:]
    compact = compact.rstrip("%")
    if not compact or not re.fullmatch(r"\d[\d.,]*", compact):
        return None

    if "." in compact and "," in compact:
        if compact.rfind(",") > compact.rfind("."):
            compact = compact.replace(".", "").replace(",", ".")
        else:
            compact = compact.replace(",", "")
    elif "," in compact:
        parts = compact.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            compact = ".".join(parts)
        else:
            compact = "".join(parts)
    elif "." in compact:
        parts = compact.split(".")
        if len(parts) > 1 and all(len(part) == 3 for part in parts[1:]):
            compact = "".join(parts)
        elif len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            pass
        else:
            compact = "".join(parts)

    try:
        parsed = Decimal(compact)
    except InvalidOperation:
        return None
    return -parsed if negative else parsed


def parsed_value(text: str, *, kind: str = "") -> str:
    if kind in {"date", "identifier", "entity", "text"}:
        return _strip_inline_formatting(text)
    parsed = _parse_decimal_value(text)
    if parsed is None:
        return _strip_inline_formatting(text)
    canonical = format(parsed, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical or "0"


def value_kind(
    value: str,
    *,
    row_label: str = "",
    column_label: str = "",
    unit: str = "",
) -> str:
    text = _strip_inline_formatting(value)
    semantic_text = f"{row_label} {column_label} {unit}".strip()
    if _DATE_VALUE_RE.match(text):
        return "date"
    if _IDENTIFIER_LABEL_RE.search(semantic_text):
        return "identifier"
    if _ENTITY_COLUMN_RE.search(column_label):
        return "entity"
    if "%" in text or str(unit or "").lower() in {"percent", "%"} or "tỷ lệ" in semantic_text.lower():
        return "percent"
    if _NUMERIC_RANGE_RE.match(text):
        if _MULTIPLE_LABEL_RE.search(semantic_text):
            return "multiple"
        if _COUNT_LABEL_RE.search(semantic_text):
            return "count"
        return "amount"
    if _parse_decimal_value(text) is not None:
        if _MULTIPLE_LABEL_RE.search(semantic_text):
            return "multiple"
        if _COUNT_LABEL_RE.search(semantic_text):
            return "count"
        return "amount"
    return "text"


def period_role(period: str) -> str:
    normalized = str(period or "").strip().lower()
    # Check the explicit comparison-year suffix first: standard headers such
    # as "Luỹ kế ... kỳ này — Năm trước" contain both "kỳ này" and
    # "Năm trước", and the latter is the authoritative leg.
    if re.search(
        r"\b(?:năm\s+trước|kỳ\s+trước|previous\s+year|prior\s+year)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        return "previous"
    if re.search(
        r"\b(?:năm\s+nay|năm\s+hiện\s+tại|kỳ\s+này|current\s+year)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        return "current"
    canonical = normalized if normalized in {"cuối", "đầu"} else canonical_period(period)
    if canonical == "cuối":
        return "current"
    if canonical == "đầu":
        return "previous"
    return ""


def aggregation_level(
    row_label: str,
    *,
    inferred_total: bool = False,
    table_scope: str = "",
    item_code: str = "",
    equity_410_fallback: bool = False,
) -> str:
    label = _strip_inline_formatting(row_label)
    if (
        inferred_total
        or _TOTAL_LABEL_RE.match(label)
        or section_total_alias(
            label,
            table_scope=table_scope,
            item_code=item_code,
            equity_410_fallback=equity_410_fallback,
        )
    ):
        return "total"
    return "component"


def compose_section_path(*parts: str) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for raw_part in parts:
        part = _strip_inline_formatting(raw_part)
        if not part:
            continue
        key = part.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(part)
    return " > ".join(result)


def semantic_scope_label(*candidates: str) -> str:
    """Return the most specific stable schedule/topic label.

    A scope is deliberately free of page-continuation markers, numbering,
    periods and units.  It is a semantic schedule boundary, not a rendered
    heading.  Callers pass candidates from most to least specific.
    """

    fallback = ""
    for candidate in candidates:
        cleaned = _strip_inline_formatting(candidate).strip(" :-–—")
        cleaned = re.sub(
            r"^\s*\d+(?:\.\d+)*(?:[a-zđ])?\s*[.)-]?\s*",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"\s*\((?:tiếp\s+theo|tiep\s+theo|continued)\)\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip(" :-–—")
        cleaned = _SEMANTIC_AXIS_NOISE_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-–—")
        if not cleaned:
            continue
        if not fallback:
            fallback = cleaned
        if _norm_label(cleaned) not in _BROAD_TOTAL_GROUPS:
            return cleaned
    return fallback


def _semantic_axis_label(value: str) -> str:
    cleaned = _display_column_name(value).strip(" :-–—")
    cleaned = _SEMANTIC_AXIS_NOISE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :-–—")
    return cleaned


def _is_period_or_total_axis(value: str) -> bool:
    cleaned = _display_column_name(value)
    semantic_axis = _semantic_axis_label(cleaned)
    normalized_axis = _norm_label(semantic_axis)
    normalized = _norm_label(cleaned)
    # A compound header such as "Nước ngoài<br/>2025 VND" still carries a
    # semantic entity axis after period/unit noise is removed. It must not be
    # collapsed into a period-only column.
    if normalized_axis and not _TOTAL_COLUMN_RE.fullmatch(normalized_axis):
        return False
    return bool(
        not normalized
        or canonical_period(cleaned)
        or _PERIOD_OR_UNIT_AXIS_RE.search(cleaned)
        or _TOTAL_COLUMN_RE.fullmatch(normalized_axis)
        or normalized
        in {
            "giá trị",
            "số tiền",
            "amount",
            "value",
        }
    )


def _metric_with_schedule_context(
    metric_label: str,
    *,
    hierarchy_labels: tuple[str, ...] = (),
    scope_label: str = "",
) -> str:
    """Make a component metric self-contained when its parent supplies meaning.

    Revenue schedules often use a group row ``Tổng doanh thu`` followed by terse
    children such as ``- Bán thành phẩm``. Detached from the parent, that child
    is a generic sale description rather than the accounting metric the query
    asks for. Prefix only explicit sale/service/lease children under an explicit
    revenue scope; other schedules and already-qualified metrics are untouched.
    """

    metric = _strip_inline_formatting(metric_label).lstrip("-–— ").strip()
    if not metric or _REVENUE_CONTEXT_RE.search(metric):
        return metric
    context_parts = (*hierarchy_labels, scope_label)
    if not any(_REVENUE_CONTEXT_RE.search(part or "") for part in context_parts):
        return metric
    if not _REVENUE_COMPONENT_RE.search(metric):
        return metric
    return f"Doanh thu {metric[:1].lower()}{metric[1:]}"


def _hierarchy_depth(raw_label: str) -> int | None:
    """Map explicit outline labels to a stable depth; return None if uncertain."""

    raw = _strip_inline_formatting(raw_label)
    numeric = _HIERARCHY_NUMERIC_RE.match(raw)
    if numeric:
        return len(str(numeric.group("number") or "").split("."))
    if _HIERARCHY_ROMAN_RE.match(raw):
        return 1
    letter = _HIERARCHY_LETTER_RE.match(raw)
    if letter:
        marker = str(letter.group("paren") or letter.group("plain") or "")
        return 2 if marker.islower() else 1
    return None


def _update_hierarchy(
    hierarchy: list[tuple[int, str]],
    *,
    depth: int,
    label: str,
) -> None:
    cleaned = _strip_inline_formatting(label)
    if not cleaned:
        return
    hierarchy[:] = [
        (level, ancestor)
        for level, ancestor in hierarchy
        if level < depth
    ]
    hierarchy.append((depth, cleaned))


def _semantic_dimensions(
    *,
    row_label: str,
    column_label: str,
    label_axis_header: str,
    scope_candidates: tuple[str, ...],
    hierarchy_labels: tuple[str, ...] = (),
) -> tuple[str, str, str]:
    """Infer conservative metric/entity/scope axes for one canonical cell."""

    row_axis = _semantic_axis_label(row_label)
    column_axis = _semantic_axis_label(column_label)
    header_axis = _semantic_axis_label(label_axis_header)
    scope = semantic_scope_label(*scope_candidates)
    hierarchy_metric = next(
        (
            label
            for label in reversed(hierarchy_labels)
            if _METRIC_LABEL_RE.search(label)
        ),
        "",
    )

    row_is_entity = bool(
        _ENTITY_AXIS_HEADER_RE.fullmatch(header_axis)
        or _STRONG_ENTITY_LABEL_RE.search(row_axis)
    )
    row_is_metric = bool(
        _METRIC_AXIS_HEADER_RE.fullmatch(header_axis)
        or _METRIC_LABEL_RE.search(row_axis)
    )
    column_is_metric = bool(_METRIC_LABEL_RE.search(column_axis))
    column_is_period_or_total = (
        _is_period_or_total_axis(column_label)
        and not _METRIC_LABEL_RE.search(column_axis)
    )

    if column_is_period_or_total:
        if row_is_entity:
            return hierarchy_metric or scope, row_axis, scope
        return row_axis or hierarchy_metric or scope, "", scope

    if row_is_entity and column_axis:
        return column_axis or hierarchy_metric or scope, row_axis, scope

    if row_is_metric:
        entity = "" if _TOTAL_COLUMN_RE.fullmatch(_norm_label(column_axis)) else column_axis
        return row_axis, entity, scope

    if column_is_metric and row_axis:
        return column_axis, row_axis, scope

    # Ambiguous matrices still benefit from a deterministic row-metric /
    # column-entity orientation, while empty/generic axes remain unbound.
    entity = "" if _TOTAL_COLUMN_RE.fullmatch(_norm_label(column_axis)) else column_axis
    return row_axis or hierarchy_metric or scope, entity, scope


def _context_matrix_columns(
    original_columns: list[str],
    preferred_label_index: int | None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Find descriptor axes in an entity-by-transaction matrix.

    Forward filling is intentionally enabled only for tables whose primary
    label header is an explicit entity axis and which also expose a transaction
    descriptor column.  This avoids leaking a previous row label across generic
    statement schedules that merely happen to contain blank cells.
    """

    if preferred_label_index is None:
        return (), ()
    primary_header = _semantic_axis_label(
        original_columns[preferred_label_index]
    )
    if not _ENTITY_AXIS_HEADER_RE.fullmatch(primary_header):
        return (), ()

    relationship_indexes: list[int] = []
    transaction_indexes: list[int] = []
    for index, header in enumerate(original_columns):
        if index == preferred_label_index:
            continue
        semantic_header = _semantic_axis_label(header)
        if _RELATIONSHIP_AXIS_HEADER_RE.fullmatch(semantic_header):
            relationship_indexes.append(index)
        elif _TRANSACTION_DESCRIPTOR_AXIS_HEADER_RE.fullmatch(semantic_header):
            transaction_indexes.append(index)

    if not transaction_indexes:
        return (), ()
    return tuple(relationship_indexes), tuple(transaction_indexes)


def _context_values(row, indexes: tuple[int, ...]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for index in indexes:
        if index >= len(row.values):
            continue
        value = _strip_inline_formatting(row.values[index])
        key = _norm_label(value)
        if not value or not key or key in _TEXT_PLACEHOLDERS or key in seen:
            continue
        seen.add(key)
        values.append(value)
    return tuple(values)


def _contextual_item_name(
    row_label: str,
    column_label: str,
    *,
    relationship: str = "",
    descriptors: tuple[str, ...] = (),
) -> str:
    """Make inherited row semantics directly searchable on each scalar fact."""

    parts: list[str] = []
    seen: set[str] = set()
    for raw_part in (
        row_label,
        f"Mối quan hệ: {relationship}" if relationship else "",
        *descriptors,
        column_label,
    ):
        part = _strip_inline_formatting(raw_part)
        key = _norm_label(part)
        if not part or not key or key in seen:
            continue
        seen.add(key)
        parts.append(part)
    return " | ".join(parts)


def _is_text_payload(value: str) -> bool:
    text = _strip_inline_formatting(value)
    if not text or _norm_label(text) in _TEXT_PLACEHOLDERS:
        return False
    if _TEXT_ARTIFACT_RE.match(text):
        return False
    return not looks_like_value(text)


def _column_accepts_text(
    column_label: str,
    *,
    column_index: int,
    label_index: int | None,
) -> bool:
    if label_index is not None and column_index == label_index:
        return False
    cleaned = _display_column_name(column_label)
    if not cleaned or _is_ignored_column(cleaned):
        return False
    if _NON_DATA_TEXT_COLUMN_RE.fullmatch(_norm_label(cleaned)):
        return False
    return bool(re.search(r"[A-Za-zÀ-ỹĐđ]", cleaned))


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


def _canonical_item_code(value: str | None) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if digits else ""


def _table_item_codes(df, columns) -> set[str]:
    return {
        code
        for _, row in df.iterrows()
        if (code := _canonical_item_code(_item_code_for_row(row, columns)))
    }


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
    if _DATE_VALUE_RE.match(x):
        return True

    # If it starts like "1. Tiền", "2.1 Nợ", etc → LABEL
    if LABEL_PREFIX.match(x):
        return False

    return _parse_decimal_value(x) is not None

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
    canonical_section = normalize_table_heading(section)
    if canonical_section == TABLE_NOTE and local != TABLE_NOTE:
        return TABLE_NOTE, original_heading
    # Page headers and local captions often replace the Markdown heading on a
    # continued primary statement (for example just ``BÁO CÁO TÀI CHÍNH``).
    # Keep the latched statement as the authoritative table and retain the
    # local caption only as subheading context.  A real canonical statement
    # heading still wins normally.
    if (
        canonical_section in {TABLE_BS, TABLE_IS, TABLE_CF}
        and local not in {TABLE_BS, TABLE_IS, TABLE_CF}
    ):
        return canonical_section, original_heading
    return local, ""


def _compose_subheading(*parts) -> str:
    result: list[str] = []
    seen: set[str] = set()
    for raw_part in parts:
        part = str(raw_part or "").strip()
        if not part:
            continue
        key = _norm_label(part)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(part)
    return " — ".join(result)


def _infer_label_column_index(df, columns, original_columns) -> int | None:
    """Find the structural row-label column once per table.

    Prefer explicit label headers.  For blank/OCR headers, score columns by the
    amount of non-scalar text they carry; this selects the real item-name column
    instead of leading spacer/group-code columns.
    """

    usable_indexes = [
        index
        for index, column in enumerate(columns)
        if not _is_ignored_column(column)
    ]
    for index in usable_indexes:
        header = _norm_label(_display_column_name(original_columns[index]))
        if _LABEL_COLUMN_RE.fullmatch(header):
            return index

    scored: list[tuple[int, int, int]] = []
    for index in usable_indexes:
        text_count = 0
        text_weight = 0
        for row in df.itertuples(index=False, name=None):
            cell = str(row[index] or "").strip()
            if not _is_text_payload(cell):
                continue
            text_count += 1
            text_weight += min(len(_strip_inline_formatting(cell)), 120)
        if text_count:
            scored.append((text_count, text_weight, -index))
    if not scored:
        return usable_indexes[0] if usable_indexes else None
    best = max(scored)
    return -best[2]


def _row_label_details(
    row,
    columns,
    preferred_label_index: int | None = None,
) -> tuple[str, str, int | None]:
    """Find the semantic row label and retain its physical cell position."""

    candidates: list[tuple[str, str, int]] = []
    row_has_numeric = _row_has_value(row, columns)
    for index, (col_name, cell) in enumerate(zip(columns, row.values)):
        if _is_ignored_column(col_name):
            continue
        if not cell or looks_like_value(cell):
            continue
        cleaned = clean_label(cell)
        if cleaned:
            candidates.append((cleaned, str(cell), index))
    if not candidates:
        return "", "", None

    if preferred_label_index is not None:
        for candidate in candidates:
            if candidate[2] == preferred_label_index:
                return candidate

    if not row_has_numeric and len(candidates) > 1:
        # OCR-converted group rows often put a terse marker ("a)") in a leading
        # spacer column and the actual group name in the next cell.
        semantic = [
            candidate
            for candidate in candidates
            if not re.fullmatch(r"(?:[a-zđivxlc]+[).]?|\d+[).]?)", candidate[0], re.IGNORECASE)
            and not _DATE_VALUE_RE.match(candidate[0])
        ]
        if semantic:
            return semantic[-1]
    return candidates[0]


def _matrix_has_divider(
    df,
    columns,
    preferred_label_index: int | None = None,
) -> bool:
    """Recognize a matrix from divider rows even when its first header is blank."""

    for _, row in df.iterrows():
        row_label, _, _ = _row_label_details(
            row,
            columns,
            preferred_label_index,
        )
        if (
            _canonical_section_label(row_label)
            and not _row_has_value(row, columns)
        ):
            return True
    return False


def _row_has_data_value(
    row,
    columns,
    original_columns,
    label_index: int | None,
) -> bool:
    for cell_index, (col_name, cell) in enumerate(zip(columns, row.values)):
        if _is_ignored_column(col_name) or not cell:
            continue
        if label_index is not None and cell_index == label_index:
            continue
        if looks_like_value(cell):
            return True
        if (
            _is_text_payload(cell)
            and _column_accepts_text(
                original_columns[cell_index],
                column_index=cell_index,
                label_index=label_index,
            )
            and _norm_label(cell)
            != _norm_label(_display_column_name(original_columns[cell_index]))
        ):
            return True
    return False


def _meaningful_total_group(*candidates: str) -> str:
    for candidate in candidates:
        cleaned = clean_label(candidate).strip(" :-–—")
        cleaned = re.sub(
            r"^(?:(?:\d+(?:\.\d+)*[a-z]?|[a-z]|[ivxlcdm]+)"
            r"\s*[.)-]\s*)+",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        cleaned = re.sub(
            r"\s*\((?:tiếp\s+theo|tiep\s+theo)\)\s*$",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()
        letters = [character for character in cleaned if character.isalpha()]
        if (
            not cleaned
            or len(letters) < 3
            or _norm_label(cleaned) in _BROAD_TOTAL_GROUPS
        ):
            continue
        return cleaned
    return ""


def _semantic_total_label(group: str) -> str:
    cleaned = _strip_inline_formatting(group).strip()
    if _TOTAL_LABEL_RE.match(cleaned):
        return cleaned
    return f"Tổng {cleaned}"


def _row_numeric_values(row, columns) -> dict[str, Decimal]:
    values: dict[str, Decimal] = {}
    for col_name, cell in zip(columns, row.values):
        if _is_ignored_column(col_name) or not cell:
            continue
        parsed = _parse_decimal_value(cell)
        if parsed is not None:
            values[col_name] = parsed
    return values


def _matches_component_sum(
    current_values: dict[str, Decimal],
    component_sums: dict[str, Decimal],
    component_count: int,
) -> bool:
    if component_count < 2 or not current_values:
        return False
    comparable = 0
    for column, value in current_values.items():
        if column not in component_sums or component_sums[column] != value:
            return False
        comparable += 1
    return comparable > 0


def df_to_facts(
    df,
    heading,
    company,
    source,
    fiscal_year=None,
    section="",
    section_note_ref="",
    section_note_title="",
    default_unit="",
    section_path="",
    block_id="",
    source_page=None,
    balance_sheet_item_codes=None,
):
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

    original_columns = list(df.attrs.get("markdown_original_columns") or [])
    df = df.map(lambda x: "" if pd.isna(x) else str(x).strip())
    columns = [str(c).strip() for c in df.columns]
    if len(original_columns) != len(columns):
        original_columns = list(columns)
    available_balance_sheet_codes = {
        _canonical_item_code(code)
        for code in (
            balance_sheet_item_codes
            if balance_sheet_item_codes is not None
            else _table_item_codes(df, columns)
        )
        if _canonical_item_code(code)
    }
    preferred_label_index = _infer_label_column_index(
        df,
        columns,
        original_columns,
    )
    label_axis_header = (
        _display_column_name(original_columns[preferred_label_index])
        if preferred_label_index is not None
        else ""
    )
    (
        relationship_column_indexes,
        transaction_descriptor_column_indexes,
    ) = _context_matrix_columns(original_columns, preferred_label_index)
    context_matrix_mode = bool(transaction_descriptor_column_indexes)

    # Value columns (exclude Mã số / Thuyết minh) carry the period & unit; resolve
    # each column's canonical period once with full-table context (year ties).
    value_cols = [c for c in columns if not _is_ignored_column(c)]
    col_period = {c: column_period(c, value_cols) for c in value_cols}
    col_unit = {c: parse_unit(c) for c in value_cols}

    # A matrix may advertise its first value type in the first column header, or
    # start with a blank header followed by in-table dividers.  Inspect divider
    # rows as well so converter-specific blank headers do not erase value_type.
    header_section = _canonical_section_label(columns[0]) if columns else ""
    matrix_mode = bool(header_section) or _matrix_has_divider(
        df,
        columns,
        preferred_label_index,
    )
    current_section = header_section
    current_parent = ""
    current_group = ""
    current_period = ""
    hierarchy: list[tuple[int, str]] = []
    component_sums: dict[str, Decimal] = {}
    component_count = 0
    active_primary_entity = ""
    active_relationship = ""
    nonempty_indexes = [
        index
        for index, (_, row) in enumerate(df.iterrows())
        if any(str(cell or "").strip() for cell in row.values)
    ]
    last_nonempty_index = nonempty_indexes[-1] if nonempty_indexes else -1

    for row_index, (_, row) in enumerate(df.iterrows()):
        raw_cells = [
            _strip_inline_formatting(cell)
            for cell in row.values
        ]
        if context_matrix_mode and not any(raw_cells):
            active_primary_entity = ""
            active_relationship = ""
            continue

        item_code = _item_code_for_row(row, columns)
        equity_410_fallback = bool(
            fact_heading == TABLE_BS
            and _canonical_item_code(item_code) == "410"
            and "400" not in available_balance_sheet_codes
        )
        note_ref = _note_ref_for_row(row, columns)
        # Note schedules carry no per-row note_ref column; inherit the schedule's
        # 'V.<n>' ref (from its numbered heading) so the row links back to the
        # primary-statement line that references it.
        if not note_ref and fact_heading == TABLE_NOTE:
            note_ref = section_note_ref

        row_label, raw_label_cell, label_index = _row_label_details(
            row,
            columns,
            preferred_label_index,
        )
        row_context_entity = ""
        row_relationship = ""
        row_descriptors: tuple[str, ...] = ()
        context_boundary = False
        if context_matrix_mode and preferred_label_index is not None:
            primary_cell = _strip_inline_formatting(
                row.values[preferred_label_index]
            )
            explicit_primary = (
                clean_label(primary_cell)
                if primary_cell and not looks_like_value(primary_cell)
                else ""
            )
            row_descriptors = _context_values(
                row,
                transaction_descriptor_column_indexes,
            )
            relationship_values = _context_values(
                row,
                relationship_column_indexes,
            )
            explicit_relationship = " — ".join(relationship_values)
            has_numeric_value = bool(_row_numeric_values(row, columns))
            descriptor_is_total = any(
                _TOTAL_LABEL_RE.match(descriptor)
                for descriptor in row_descriptors
            )
            primary_is_total = bool(
                explicit_primary and _TOTAL_LABEL_RE.match(explicit_primary)
            )
            primary_is_group_boundary = bool(
                explicit_primary
                and not has_numeric_value
                and not row_descriptors
            )
            context_boundary = (
                primary_is_total
                or descriptor_is_total
                or primary_is_group_boundary
            )

            if context_boundary:
                active_primary_entity = ""
                active_relationship = ""
            elif explicit_primary:
                # A new explicit entity always starts a fresh contiguous run;
                # an absent relationship must not inherit from the prior one.
                active_primary_entity = explicit_primary
                active_relationship = explicit_relationship
                row_context_entity = explicit_primary
                row_relationship = explicit_relationship
            elif active_primary_entity:
                row_context_entity = active_primary_entity
                if explicit_relationship:
                    active_relationship = explicit_relationship
                row_relationship = explicit_relationship or active_relationship

            if row_context_entity:
                # Keep the physical primary entity column as the label axis even
                # when the source leaves it blank on a follow-up transaction.
                # This prevents "Thu nhập khác" / "Bán hàng" from becoming a
                # fabricated counterparty.
                row_label = row_context_entity
                raw_label_cell = primary_cell or row_context_entity
                label_index = preferred_label_index
            elif not explicit_primary and row_descriptors:
                # Orphan descriptors remain metrics with no entity binding.
                # Retaining a descriptor as row_label preserves the cell, while
                # the explicit entity override below remains fail-closed.
                row_label = row_descriptors[0]
                raw_label_cell = row_descriptors[0]
                label_index = transaction_descriptor_column_indexes[0]

        hierarchy_depth = _hierarchy_depth(raw_label_cell)
        row_has_data = _row_has_data_value(
            row,
            columns,
            original_columns,
            label_index,
        )
        inferred_total = False
        if not row_label:
            numeric_values = _row_numeric_values(row, columns)
            total_group = _meaningful_total_group(
                current_group,
                current_parent,
                current_section,
                base_subheading,
                section_note_title,
            )
            is_terminal_candidate = (
                row_index == last_nonempty_index and component_count >= 2
            )
            arithmetic_total = _matches_component_sum(
                numeric_values,
                component_sums,
                component_count,
            )
            if total_group and numeric_values and (
                is_terminal_candidate or arithmetic_total
            ):
                row_label = _semantic_total_label(total_group)
                inferred_total = True
            else:
                # A blank label is ambiguous (spacer, wrapped continuation, or
                # aggregate).  Never invent a generic "Tổng".
                continue

        row_norm = _norm_label(row_label)
        canonical_section = _canonical_section_label(row_label)

        # Many roll-forward schedules repeat their own title as the first
        # numeric row (for example "a) Vay ngắn hạn" under note
        # "18a. Vay ngắn hạn"), followed by lender/category components.  That
        # title row is the schedule aggregate even without the word "Tổng".
        root_group = _meaningful_total_group(
            base_subheading,
            section_note_title,
        )
        row_group = _meaningful_total_group(row_label)
        if (
            row_has_data
            and not inferred_total
            and root_group
            and row_group
            and _norm_label(root_group) == _norm_label(row_group)
        ):
            row_label = _semantic_total_label(root_group)
            raw_label_cell = row_label
            row_norm = _norm_label(row_label)
            canonical_section = _canonical_section_label(row_label)
            hierarchy_depth = _hierarchy_depth(raw_label_cell)
            inferred_total = True

        # An explicit bare "Cộng/Tổng" row is still not self-describing once
        # detached from its source table.  Bind it to the narrowest active
        # parser-owned group, just as we do for validated blank-label totals.
        # Already descriptive labels such as "Tổng tài sản" remain untouched.
        if row_has_data and row_norm in {"cộng", "tổng", "tổng cộng"}:
            hierarchy_groups = tuple(
                label for _, label in reversed(hierarchy)
            )
            total_group = _meaningful_total_group(
                current_section,
                *hierarchy_groups,
                current_group,
                current_parent,
                base_subheading,
                section_note_title,
            )
            if total_group:
                row_label = _semantic_total_label(total_group)
                raw_label_cell = row_label
                row_norm = _norm_label(row_label)
                canonical_section = _canonical_section_label(row_label)
                hierarchy_depth = _hierarchy_depth(raw_label_cell)
                inferred_total = True

        # In a matrix, a section label row (e.g. "Giá trị hao mòn") switches the
        # active section; such divider rows carry no values and yield no facts.
        if matrix_mode and canonical_section and not row_has_data:
            current_section = canonical_section
            current_group = canonical_section
            hierarchy = []
            component_sums = {}
            component_count = 0
            continue

        # Period divider row (e.g. "Kỳ trước"/"Kỳ này") in a two-period table:
        # no values, splits prior- vs current-period blocks. Latch it so the
        # otherwise-identical rows below get distinct subheadings; emit no fact.
        if row_norm in _PERIOD_DIVIDER_LABELS and not row_has_data:
            current_period = row_label
            component_sums = {}
            component_count = 0
            continue

        # A label-only row is a group header.  It provides the semantic name for
        # a later blank-label total but is not itself a fact.
        if not row_has_data:
            current_group = row_label
            if hierarchy_depth is not None:
                _update_hierarchy(
                    hierarchy,
                    depth=hierarchy_depth,
                    label=row_label,
                )
            else:
                implicit_depth = hierarchy[-1][0] + 1 if hierarchy else 1
                _update_hierarchy(
                    hierarchy,
                    depth=implicit_depth,
                    label=row_label,
                )
            component_sums = {}
            component_count = 0
            continue

        if hierarchy_depth is None:
            hierarchy_labels = tuple(label for _, label in hierarchy)
        else:
            hierarchy_labels = tuple(
                label
                for level, label in hierarchy
                if level < hierarchy_depth
            )

        # Restore hierarchical context into the subheading so flattened cells
        # (e.g. "Số cuối kỳ | Cộng", "Nguyên giá | Số đầu năm") are unambiguous.
        if matrix_mode:
            row_subheading = _compose_subheading(
                base_subheading,
                *hierarchy_labels,
                current_section,
            )
        elif LABEL_PREFIX.match(_strip_inline_formatting(raw_label_cell)):
            current_parent = row_label
            current_group = row_label
            row_subheading = _compose_subheading(
                base_subheading,
                *hierarchy_labels,
            )
        elif current_parent and (
            row_norm in _BREAKDOWN_SUBLABELS or bool(canonical_section)
        ):
            row_subheading = _compose_subheading(
                base_subheading,
                *hierarchy_labels,
                current_parent,
            )
        else:
            row_subheading = _compose_subheading(
                base_subheading,
                *hierarchy_labels,
            )

        # Fold the active period into the subheading so duplicate line items
        # across the two periods (prior/current) stay distinguishable.
        if current_period:
            row_subheading = _compose_subheading(row_subheading, current_period)

        # Section totals are labelled "A - TÀI SẢN NGẮN HẠN" / "C - NỢ PHẢI TRẢ" /
        # "I. Nợ ngắn hạn"…; fold the readable "Tổng …" alias into the subheading so
        # dense + lexical retrieval and the answer model see them as the "tổng …"
        # the question asks for.
        balance_section_key = section_total_key(
            row_label,
            table_scope=fact_heading,
            item_code=item_code or "",
            equity_410_fallback=equity_410_fallback,
        )
        sec_alias = section_total_alias(
            row_label,
            table_scope=fact_heading,
            item_code=item_code or "",
            equity_410_fallback=equity_410_fallback,
        )
        if sec_alias:
            row_subheading = _compose_subheading(row_subheading, sec_alias)

        # "trong năm" line items ≡ "trong kỳ" in a periodic report; fold the
        # "trong kỳ" variant into the subheading so questions phrased "trong kỳ"
        # match the indexed "trong năm" label (e.g. lưu chuyển tiền thuần).
        ky_alias = period_phrase_alias(row_label)
        if ky_alias:
            row_subheading = _compose_subheading(row_subheading, ky_alias)

        row_aggregation = aggregation_level(
            row_label,
            inferred_total=inferred_total,
            table_scope=fact_heading,
            item_code=item_code or "",
            equity_410_fallback=equity_410_fallback,
        )
        fact_section_path = compose_section_path(
            section_path,
            fact_heading,
            base_subheading,
            *hierarchy_labels,
            current_section,
            current_parent,
            current_period,
            sec_alias if balance_section_key else "",
        )
        row_numeric_values = _row_numeric_values(row, columns)

        # create one canonical fact per numeric cell
        for cell_index, (col_name, cell) in enumerate(zip(columns, row.values)):
            if _is_ignored_column(col_name):
                continue
            if not cell:
                continue
            if label_index is not None and cell_index == label_index:
                continue
            column_label = canonical_balance_sheet_column_label(
                _display_column_name(original_columns[cell_index]),
                table_scope=fact_heading,
            )
            scalar_value = looks_like_value(cell)
            text_value = (
                _is_text_payload(cell)
                and _column_accepts_text(
                    original_columns[cell_index],
                    column_index=cell_index,
                    label_index=label_index,
                )
                and _norm_label(cell) != _norm_label(column_label)
            )
            if not scalar_value and not text_value:
                continue

            raw_value = _strip_inline_formatting(cell)
            normalized_value = _normalize_value_text(cell)
            row_period = canonical_period(row_label)
            resolved_period = (
                col_period.get(col_name, "")
                or canonical_period(current_period)
                or row_period
            )
            resolved_unit = (
                "cổ phiếu"
                if "số lượng cổ phiếu" in _norm_label(row_label)
                else col_unit.get(col_name, "") or str(default_unit or "").strip()
            )
            resolved_kind = value_kind(
                raw_value,
                row_label=row_label,
                column_label=column_label,
                unit=resolved_unit,
            )
            if resolved_kind in {"entity", "identifier", "date", "text"}:
                # A report-level VND caption applies to numeric cells, not to an
                # address/activity/relationship cell in the same note table.
                resolved_unit = col_unit.get(col_name, "")
            total_eligible_kind = resolved_kind in {
                "amount",
                "count",
                "percent",
                "multiple",
                "text",
            }
            cell_aggregation = (
                "total"
                if (
                    total_eligible_kind
                    and (
                        row_aggregation == "total"
                        or bool(
                            _TOTAL_COLUMN_RE.fullmatch(
                                _norm_label(_semantic_axis_label(column_label))
                            )
                        )
                    )
                )
                else "component"
            )
            metric_label, entity_label, scope_label = _semantic_dimensions(
                row_label=sec_alias or row_label,
                column_label=column_label,
                label_axis_header=label_axis_header,
                scope_candidates=(
                    base_subheading,
                    section_note_title,
                    fact_heading,
                ),
                hierarchy_labels=hierarchy_labels,
            )
            if balance_section_key and sec_alias:
                # The accounting code/section label is authoritative. Do not
                # let a repeated page-header fragment ("Tài sản dài hạn") turn
                # total assets into a non-current-assets fact.
                metric_label = sec_alias
                entity_label = ""
                scope_label = sec_alias
                metric_was_contextualized = False
            else:
                contextual_metric_label = _metric_with_schedule_context(
                    metric_label,
                    hierarchy_labels=hierarchy_labels,
                    scope_label=scope_label,
                )
                metric_was_contextualized = (
                    contextual_metric_label != metric_label
                )
                metric_label = contextual_metric_label
            if scalar_value and context_matrix_mode:
                if row_descriptors:
                    metric_label = " — ".join(row_descriptors)
                # Entity binding in this matrix comes only from the explicit
                # primary axis (or its bounded forward-fill state).  Never let
                # a descriptor from a blank primary row become the entity.
                entity_label = "" if context_boundary else row_context_entity
            item_label = (
                sec_alias
                if balance_section_key and sec_alias
                else metric_label
                if metric_was_contextualized
                else row_label
            )
            cell_item_name = f"{item_label} | {column_label}"
            if scalar_value and context_matrix_mode:
                cell_item_name = _contextual_item_name(
                    row_label,
                    column_label,
                    relationship=row_relationship,
                    descriptors=row_descriptors,
                )
            semantic_dimensions = derive_semantic_fact_dimensions(
                row_label=sec_alias or row_label,
                column_label=column_label,
                metric_label=metric_label,
                entity_label=entity_label,
                scope_label=scope_label,
                section_path=fact_section_path,
                item_name=cell_item_name,
                item_code=item_code,
                value=raw_value,
            )

            facts.append({
                "company": company,
                "fiscal_year": "" if fiscal_year is None else str(fiscal_year),
                "heading": fact_heading,
                "item_code": item_code,
                "note_ref": note_ref,
                "subheading": row_subheading,
                "item_name": cell_item_name,
                # First-class disambiguators for value-lookup rows (else they
                # only differ inside item_name/subheading strings).
                "period": resolved_period,
                # Value type sits in the matrix section header, the row label
                # ("Nguyên giá | Số cuối năm"), OR the note column ("Số cuối kỳ
                # <br/>Giá gốc"/"...Dự phòng"); try all three.
                "value_type": (
                    canonical_value_type(current_section)
                    or canonical_value_type(row_label)
                    or canonical_value_type(_display_column_name(col_name))
                ),
                "unit": resolved_unit,
                "value": normalized_value,
                "raw_value": raw_value,
                "normalized_value": normalized_value,
                "source": source,
                "row_label": row_label,
                "column_label": column_label,
                "value_kind": resolved_kind,
                "parsed_value": parsed_value(raw_value, kind=resolved_kind),
                "period_label": (
                    _compose_subheading(
                        current_period or (row_label if row_period else ""),
                        column_label,
                    )
                    if current_period or row_period
                    else column_label
                ),
                "period_role": period_role(
                    current_period or resolved_period or column_label
                ),
                "aggregation_level": cell_aggregation,
                "section_path": fact_section_path,
                "block_id": str(block_id or ""),
                "source_page": "" if source_page is None else str(source_page),
                "metric_label": metric_label,
                "entity_label": entity_label,
                "scope_label": scope_label,
                "section_key": balance_section_key,
                **semantic_dimensions.as_dict(),
            })

        if hierarchy_depth is not None:
            _update_hierarchy(
                hierarchy,
                depth=hierarchy_depth,
                label=row_label,
            )

        if row_aggregation == "total":
            component_sums = {}
            component_count = 0
        elif row_numeric_values:
            for column, numeric_value in row_numeric_values.items():
                component_sums[column] = (
                    component_sums.get(column, Decimal("0")) + numeric_value
                )
            component_count += 1

    return facts

def build_fact_rows(tables_with_context, company, source, fiscal_year=None):
    rows = []
    parsed_blocks = []
    balance_sheet_item_codes: set[str] = set()

    # Inspect the complete report before scalarizing individual blocks. Equity
    # code 410 is a valid aggregate fallback only when canonical section code
    # 400 is absent from the report; a page boundary must not create a duplicate
    # total merely because 400 appeared in the preceding block.
    for block in tables_with_context:
        df = markdown_table_to_df(block["table"])

        if df is None or df.empty:
            continue
        parsed_blocks.append((block, df))
        block_heading, _ = _resolve_heading(
            block["heading"],
            block.get("section", ""),
        )
        if block_heading != TABLE_BS:
            continue
        scan_df = df.map(lambda x: "" if pd.isna(x) else str(x).strip())
        balance_sheet_item_codes.update(
            _table_item_codes(
                scan_df,
                [str(column).strip() for column in scan_df.columns],
            )
        )

    for block, df in parsed_blocks:
        facts = df_to_facts(
            df,
            heading=block["heading"],
            company=company,
            source=source,
            fiscal_year=fiscal_year,
            section=block.get("section", ""),
            section_note_ref=block.get("note_ref", ""),
            section_note_title=block.get("note_title", ""),
            default_unit=block.get("unit", ""),
            section_path=block.get("section_path", ""),
            block_id=block.get("block_id", ""),
            source_page=block.get("source_page"),
            balance_sheet_item_codes=balance_sheet_item_codes,
        )

        for f in facts:
            if not f.get("item_name") or not f.get("value"):
                continue

            # Canonical typed-fact tuple: the legacy 14 fields remain first so
            # callers using stable positional slots continue to work.
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
                f.get("row_label", ""),
                f.get("column_label", ""),
                f.get("value_kind", ""),
                f.get("parsed_value", ""),
                f.get("period_label", ""),
                f.get("period_role", ""),
                f.get("aggregation_level", ""),
                f.get("section_path", ""),
                f.get("block_id", ""),
                f.get("source_page", ""),
                f.get("metric_label", ""),
                f.get("entity_label", ""),
                f.get("scope_label", ""),
                f.get("counterparty", ""),
                f.get("transaction_type", ""),
                f.get("movement_type", ""),
                f.get("geography", ""),
                f.get("policy_topic", ""),
                f.get("section_key", ""),
            ))

    return rows

    




    
