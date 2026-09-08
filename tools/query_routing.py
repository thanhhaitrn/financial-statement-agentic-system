"""Central query intent, routing, and typed-slot helpers.

The router deliberately returns more than one candidate for concepts that can
legitimately live in both the notes and the report front matter.  Callers that
still require a single table can consume the first candidate, while evidence
execution can retain the bounded candidate set.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
import re
import unicodedata
from typing import Any

from config.allowed_keywords import (
    iter_keyword_table_pairs,
    keyword_vocabulary_version,
    normalize_keyword_synonyms,
)
from ingestion.period_normalize import (
    canonical_period,
    canonical_value_type,
    is_comparison_query,
    query_section_total_key,
    query_value_types,
    section_total_key,
)
from ingestion.semantic_dimensions import (
    derive_query_semantic_dimensions,
    derive_semantic_fact_dimensions,
)
from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
)


MAIN_REPORT_TABLES = (TABLE_BS, TABLE_IS, TABLE_CF)


@dataclass(frozen=True)
class RouteCandidate:
    """One deterministic evidence route, ordered by descending confidence."""

    table: str
    confidence: float
    reason: str


@dataclass(frozen=True)
class QueryOperand:
    """One explicit semantic leg of a ratio/share calculation."""

    role: str
    query: str
    metric: str
    entity: str = ""
    period: str = ""
    period_role: str = ""
    reporting_basis: str = ""
    period_label: str = ""
    value_type: str = ""
    aggregation: str = ""
    scope_label: str = ""
    counterparty: str = ""
    transaction_type: str = ""
    movement_type: str = ""
    geography: str = ""
    policy_topic: str = ""
    section_key: str = ""


@dataclass(frozen=True)
class QuerySlots:
    """Typed retrieval intent extracted conservatively from a user question."""

    metric: str = ""
    entity: str = ""
    period: str = ""
    period_role: str = ""
    reporting_basis: str = ""
    value_type: tuple[str, ...] = ()
    aggregation: str = ""
    scope_label: str = ""
    operation: str = "lookup"
    operands: tuple[QueryOperand, ...] = ()
    coverage_template: str = ""
    required_legs: tuple[str, ...] = ()
    counterparty: str = ""
    transaction_type: str = ""
    movement_type: str = ""
    geography: str = ""
    policy_topic: str = ""
    period_labels: tuple[str, ...] = ()
    section_key: str = ""


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PERIOD_PART_RE = re.compile(
    r"\b(?:so du\s+)?(?:dau|cuoi)\s+(?:ky|nam|quy|thang)\b"
    r"|\bnam\s+(?:nay|hien tai|truoc)\b"
    r"|\bky\s+(?:nay|truoc)\b"
    # Parsed statement headers commonly attach the unit without whitespace
    # (``2025VND`` / ``31/12/2025VND``). A trailing word boundary does not
    # exist between a digit and ``V``, so accept an attached canonical unit as
    # a period terminator when building logical-row sibling keys.
    r"|\b\d{1,2}/\d{1,2}/\d{4}(?=\b|vnd\b|dong\b)"
    r"|\b(?:19|20)\d{2}(?=\b|vnd\b|dong\b)"
)
_UNIT_RE = re.compile(
    r"(?<![a-z])(?:nghin dong|trieu dong|ty dong|vnd|dong)\b"
)
_ABSOLUTE_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-]"
    r"(?P<year>(?:19|20)\d{2})(?!\d)"
)
_ABSOLUTE_TEXT_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})\s*thang\s*(?P<month>\d{1,2})"
    r"\s*nam\s*(?P<year>(?:19|20)\d{2})\b"
)
_ABSOLUTE_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_RATIO_PREFIX_RE = re.compile(
    r"^\s*(?:tính|tinh)?\s*"
    r"(?:tỷ\s+lệ|tỉ\s+lệ|ty\s+le|ti\s+le|tỷ\s+trọng|tỉ\s+trọng"
    r"|ty\s+trong|ti\s+trong|hệ\s+số|he\s+so|phần\s+trăm|phan\s+tram)"
    r"\s+",
    flags=re.IGNORECASE,
)
_RATIO_SUFFIX_RE = re.compile(
    r"\s+(?:là|la|bằng|bang|chiếm|chiem)\s+bao\s+nhiêu"
    r"(?:\s+(?:phần\s+trăm|phan\s+tram|lần|lan))?\s*$"
    r"|\s+bao\s+nhiêu\s+(?:phần\s+trăm|phan\s+tram|lần|lan)\s*$",
    flags=re.IGNORECASE,
)
_RATIO_SEPARATORS = (
    re.compile(r"\s+(?:trên|tren)\s+", flags=re.IGNORECASE),
    re.compile(r"\s+(?:chia\s+cho)\s+", flags=re.IGNORECASE),
    re.compile(r"(?<!\d)\s*/\s*(?!\d)"),
)
_SHARE_SEPARATOR = re.compile(r"\s+(?:trong)\s+", flags=re.IGNORECASE)
_DEBT_EQUITY_RATIO_RE = re.compile(
    r"\bno(?:\s+phai\s+tra)?\s+(?:tren|chia\s+cho)\s+"
    r"(?:tong\s+)?von\s+chu\s+so\s+huu\b"
    r"|\bd\s*/\s*e\b",
    flags=re.IGNORECASE,
)
_DEBT_EQUITY_FORMULA_RE = re.compile(
    r"\s*\(?\s*d\s*/\s*e\s*\)?",
    flags=re.IGNORECASE,
)
_VALUE_TYPE_ONLY_METRICS = {
    "nguyen gia",
    "gia goc",
    "hao mon",
    "khau hao",
    "gia tri con lai",
    "du phong",
    "gia tri hop ly",
}

_COVERAGE_MATERIALITY_MARKERS = (
    "muc do trong yeu",
    "tinh trong yeu",
    "trong yeu",
    "tam quan trong",
    "muc do dang ke",
)
_COVERAGE_CAUSAL_MARKERS = (
    "nguyen nhan",
    "do dau",
    "chu yeu do",
    "dieu gi khien",
    "yeu to nao khien",
    "dong luc",
)
_COVERAGE_ROLL_FORWARD_MARKERS = (
    "roll forward",
    "roll-forward",
    "tinh hinh tang giam",
    "bien dong trong nam",
    "duoc trich lap va su dung",
    "trich lap va su dung",
    "vay them va hoan tra",
    "phat sinh va da nop",
    "dau tu xay dung co ban",
)
_COVERAGE_COMPOSITION_MARKERS = (
    "phan tich co cau",
    "co cau cua",
    "co cau va muc dich",
    "bao gom nhung",
    "cac thanh phan",
    "khoan chi phi lon nhat",
)


def _ascii_text(value: Any) -> str:
    text = str(value or "").replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return _SPACE_RE.sub(" ", text.strip().lower())


def _tokens(value: Any) -> set[str]:
    return set(_TOKEN_RE.findall(_ascii_text(value)))


def _explicit_period_labels(value: Any) -> tuple[str, ...]:
    """Return ordered absolute dates/years without conflating their roles."""

    text = _ascii_text(value)
    labels: list[str] = []
    date_spans: list[tuple[int, int]] = []
    for pattern in (_ABSOLUTE_DATE_RE, _ABSOLUTE_TEXT_DATE_RE):
        for match in pattern.finditer(text):
            labels.append(
                f"{int(match.group('day')):02d}/"
                f"{int(match.group('month')):02d}/"
                f"{match.group('year')}"
            )
            date_spans.append(match.span())

    masked = list(text)
    for start, end in date_spans:
        masked[start:end] = " " * (end - start)
    labels.extend(_ABSOLUTE_YEAR_RE.findall("".join(masked)))
    return tuple(dict.fromkeys(labels))


def _fact_period_labels(meta: dict, doc: str = "") -> set[str]:
    """Read the most specific available source-period label for one fact."""

    for key in ("period_label", "column_label", "item_name", "period"):
        labels = _explicit_period_labels(meta.get(key, ""))
        if labels:
            output = set(labels)
            # A year-only question may legitimately match a dated column from
            # that year, but a date question still requires the exact date.
            output.update(
                match.group("year")
                for value in labels
                for match in [_ABSOLUTE_DATE_RE.fullmatch(value)]
                if match
            )
            return output

    labels = _explicit_period_labels(doc)
    if labels:
        output = set(labels)
        output.update(
            match.group("year")
            for value in labels
            for match in [_ABSOLUTE_DATE_RE.fullmatch(value)]
            if match
        )
        return output
    return set(_explicit_period_labels(meta.get("fiscal_year", "")))


def fact_period_labels(meta: dict | None, doc: str = "") -> frozenset[str]:
    """Public read-only view of the explicit date/year labels on one fact."""

    return frozenset(
        _fact_period_labels(meta if isinstance(meta, dict) else {}, doc)
    )


def query_reporting_basis(value: Any) -> str:
    """Classify the flow horizon explicitly requested by a query.

    ``current``/``previous`` alone cannot distinguish a quarter column from a
    cumulative or annual column.  The distinction is material in quarterly
    filings, where both pairs coexist under the same metric and year roles.
    """

    text = _ascii_text(value)
    if not text:
        return ""
    # ``lũy kế`` is also part of balance-sheet value types such as accumulated
    # depreciation.  Remove those phrases before interpreting it as a YTD flow
    # horizon; otherwise a stock ratio is incorrectly forced onto cumulative
    # income/cash-flow facts.
    basis_text = re.sub(
        r"\b(?:hao mon|khau hao)\s+luy ke\b",
        " ",
        text,
    )
    if _contains_any(
        basis_text,
        (
            "luy ke",
            "tu dau nam",
            "year to date",
            "year-to-date",
            "ytd",
        ),
    ):
        return "cumulative"
    if re.search(r"\b(?:quy|quarter)\s*(?:i{1,3}|iv|[1-4])?\b", basis_text):
        return "quarter"
    if _contains_any(
        basis_text,
        (
            "nam nay",
            "nam hien tai",
            "nam truoc",
            "ca nam",
            "trong nam",
            "current year",
            "previous year",
            "prior year",
        ),
    ):
        return "full_period"
    return ""


def fact_reporting_basis(meta: dict | None, doc: str = "") -> str:
    """Return ``quarter``/``cumulative``/``annual``/``point_in_time``.

    This is derived from source labels at runtime, so existing indexes gain the
    guard without a schema migration.  The original label remains available for
    display and audit.
    """

    meta = meta if isinstance(meta, dict) else {}
    if _fact_has_stock_position(meta, doc):
        return "point_in_time"
    surface = _ascii_text(
        " ".join(
            str(meta.get(key, "") or "")
            for key in (
                "period_label",
                "column_label",
                "item_name",
                "time_hint",
            )
        )
        + f" {doc}"
    )
    if _contains_any(
        surface,
        ("luy ke", "tu dau nam", "year to date", "year-to-date", "ytd"),
    ):
        return "cumulative"
    if re.search(r"\b(?:quy|quarter)\s*(?:i{1,3}|iv|[1-4])?\b", surface):
        return "quarter"

    heading = _ascii_text(
        meta.get("heading", "")
        or meta.get("table", "")
        or meta.get("section_path", "")
    )
    if heading in {
        _ascii_text(TABLE_IS),
        _ascii_text(TABLE_CF),
    } and (
        _ABSOLUTE_YEAR_RE.search(surface)
        or _contains_any(
            surface,
            ("nam nay", "nam truoc", "ky nay", "ky truoc"),
        )
    ):
        return "annual"
    return ""


def reporting_basis_compatible(requested: str, actual: str) -> bool:
    requested_text = str(requested or "").strip()
    actual_text = str(actual or "").strip()
    if not requested_text:
        return True
    if requested_text == "full_period":
        return actual_text in {"annual", "cumulative"}
    return requested_text == actual_text


def _absolute_period_compatible(
    slots: QuerySlots,
    meta: dict,
    doc: str = "",
) -> bool:
    required = set(slots.period_labels)
    if not required:
        return True
    actual = _fact_period_labels(meta, doc)
    return bool(actual and required.intersection(actual))


def _fact_has_stock_position(meta: dict | None, doc: str = "") -> bool:
    """Return whether a fact is explicitly a point-in-time opening/closing row.

    ``period_role=current`` historically accompanied both balance rows and
    current-year flow rows.  The source wording is therefore authoritative:
    ``Số cuối kỳ``/``Số đầu năm`` and balance-sheet dates are stock positions,
    while ``Năm nay``/``Năm trước`` and ``Lũy kế ...`` are reporting-period
    roles.  Keeping those axes separate prevents a revenue comparison from
    binding to an accrued-interest balance merely because both say "current".
    """

    meta = meta if isinstance(meta, dict) else {}
    labels = " | ".join(
        str(meta.get(key, "") or "")
        for key in ("period_label", "column_label", "item_name")
    )
    text = _ascii_text(labels)
    if re.search(
        r"(?:^|\|)\s*(?:so du\s+)?(?:so\s+)?(?:dau|cuoi)\s+"
        r"(?:ky|nam|quy|thang)\b",
        text,
    ):
        return True
    if re.search(r"\btai\s+ngay\s+\d{1,2}/\d{1,2}/(?:19|20)\d{2}\b", text):
        return True
    heading = _ascii_text(meta.get("heading", ""))
    return bool(
        heading == _ascii_text(TABLE_BS)
        and str(meta.get("period", "") or "").strip() in {"cuối", "đầu"}
    )


def fact_period_role(meta: dict | None, doc: str = "") -> str:
    """Return ``current``/``previous`` for a reporting-period flow fact.

    The helper deliberately does not translate a stock ``cuối``/``đầu`` row
    into a flow role.  Sparse/legacy facts may omit ``period_role``; relative
    labels and an absolute year compared with ``fiscal_year`` recover it
    deterministically.
    """

    meta = meta if isinstance(meta, dict) else {}
    labels = " ".join(
        str(meta.get(key, "") or "")
        for key in (
            "period_label",
            "column_label",
            "item_name",
            "period_role",
        )
    )
    labels = f"{labels} {doc}".strip()
    text = _ascii_text(labels)
    # Check the explicit prior-year marker first: a header such as
    # ``Lũy kế ... cuối kỳ này Năm trước`` contains the incidental phrase
    # ``kỳ này`` but unambiguously belongs to the previous reporting year.
    if _contains_any(
        text,
        ("nam truoc", "ky truoc", "previous year", "prior year", "prior period"),
    ):
        return "previous"
    if _contains_any(
        text,
        ("nam nay", "nam hien tai", "ky nay", "current year", "current period"),
    ):
        return "current"

    fiscal_year_match = _ABSOLUTE_YEAR_RE.search(
        str(meta.get("fiscal_year", "") or "")
    )
    label_years = [
        int(value)
        for value in _ABSOLUTE_YEAR_RE.findall(labels)
    ]
    if fiscal_year_match and label_years and not _fact_has_stock_position(meta, doc):
        fiscal_year = int(fiscal_year_match.group(0))
        if fiscal_year in label_years:
            return "current"
        if max(label_years) < fiscal_year:
            return "previous"

    explicit = _ascii_text(meta.get("period_role", ""))
    if explicit in {"current", "previous"} and not _fact_has_stock_position(meta, doc):
        return explicit
    return ""


def _semantic_directions(value: Any) -> dict[str, str]:
    """Extract only mutually exclusive directions used by financial slots."""

    text = _ascii_text(value)
    lending = any(
        marker in text
        for marker in ("cho vay", "phai thu ve cho vay", "lai cho vay")
    )
    borrowing = bool(re.search(r"\bvay\b", text)) and not lending
    directions: dict[str, str] = {}
    if lending:
        directions["loan"] = "lending"
    elif borrowing:
        directions["loan"] = "borrowing"

    if "phai thu" in text:
        directions["balance"] = "receivable"
    elif "phai tra" in text:
        directions["balance"] = "payable"

    if any(marker in text for marker in ("tien thu", "thu tu", "thu hoi")):
        directions["cash"] = "inflow"
    elif any(marker in text for marker in ("tien chi", "chi tra", "da tra")):
        directions["cash"] = "outflow"

    if any(marker in text for marker in ("mua hang", "mua dich vu", "giao dich mua")):
        directions["transaction"] = "purchase"
    elif any(marker in text for marker in ("ban hang", "giao dich ban", "doanh thu voi")):
        directions["transaction"] = "sale"

    if "loi nhuan sau thue chua phan phoi" in text:
        directions["profit_level"] = "retained_earnings"
    elif "loi nhuan sau thue" in text:
        directions["profit_level"] = "after_tax"
    elif any(
        marker in text
        for marker in ("loi nhuan truoc thue", "loi nhuan ke toan truoc thue")
    ):
        directions["profit_level"] = "before_tax"
    elif any(
        marker in text
        for marker in (
            "loi nhuan thuan tu hoat dong kinh doanh",
            "loi nhuan tu hoat dong kinh doanh",
        )
    ):
        directions["profit_level"] = "operating"
    elif "loi nhuan gop" in text:
        directions["profit_level"] = "gross"

    # Specific statement sections must win over the broad substring
    # "tổng tài sản" (for example "tổng tài sản ngắn hạn").
    if "tai san ngan han" in text:
        directions["asset_scope"] = "current"
    elif "tai san dai han" in text:
        directions["asset_scope"] = "noncurrent"
    elif any(marker in text for marker in ("tong tai san", "tong cong tai san")):
        directions["asset_scope"] = "total"
    return directions


def _semantic_direction_conflict(required: Any, actual: Any) -> bool:
    required_directions = _semantic_directions(required)
    actual_directions = _semantic_directions(actual)
    return any(
        key in actual_directions and actual_directions[key] != value
        for key, value in required_directions.items()
    )


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _coverage_contract(query: str) -> tuple[str, tuple[str, ...]]:
    """Return the closed evidence shape required by common analytical prompts.

    The contract is routing metadata, not an instruction to manufacture a
    total.  Retrieval may bind only facts that exist in one compatible scope.
    """

    text = _ascii_text(query)
    if _contains_any(text, _COVERAGE_MATERIALITY_MARKERS):
        return "materiality", ("company_total", "transaction_categories")
    if _contains_any(text, _COVERAGE_CAUSAL_MARKERS):
        return "causal", ("target_current", "target_previous", "declared_drivers")
    if _contains_any(text, _COVERAGE_ROLL_FORWARD_MARKERS):
        return "roll_forward", ("opening", "additions", "reductions", "closing")
    if _contains_any(text, _COVERAGE_COMPOSITION_MARKERS):
        return "composition", ("total", "components_closed")
    return "", ()


_NOTE_EXCLUSIVE_MARKERS = (
    "thuyet minh",
    "chinh sach ke toan",
    "phuong phap ke toan",
    "ghi nhan",
    "hach toan",
    "dieu kien ghi nhan",
    "ben lien quan",
    "giao dich voi ben lien quan",
    "cam ket",
    "nghia vu tiem tang",
    "rui ro tai chinh",
    "su kien sau ngay",
    "ky han vay",
    "tai san bao dam",
    "tai san dam bao",
    "co cau no",
    "chi tiet khoan muc",
    "thoi gian khau hao",
    "thoi gian phan bo",
    "phuong phap khau hao",
    "phuong phap phan bo",
    "chinh sach khong khau hao",
    "lai cho vay",
    "lai tien gui ngan hang",
    "bao cao bo phan",
    "bo phan theo khu vuc",
    "giao dich noi bo",
    "tro cap thoi viec",
)

_POLICY_DUAL_MARKERS = (
    "chuan muc ke toan",
    "che do ke toan",
    "tuyen bo tuan thu",
)

_CASHFLOW_MARKERS = (
    "bao cao luu chuyen tien",
    "luu chuyen tien",
    "dong tien",
    "tien thu tu",
    "tien chi cho",
    "tien chi de",
    "tien chi tra",
    "tien thu hoi",
    "tra no goc vay",
    "luu chuyen tien thuan",
)

_REPORT_EXCLUSIVE_MARKERS = (
    "thong tin cong ty",
    "khai quat ve cong ty",
    "thong tin khai quat",
    "kiem soat noi bo",
    "bao cao kiem toan",
    "bao cao soat xet",
    "so bao cao kiem toan",
    "so hieu bao cao kiem toan",
    "y kien kiem toan",
    "y kien ngoai tru",
    "co so y kien",
    "ket luan cua kiem toan vien",
    "ket luan soat xet",
    "van de can nhan manh",
    "kiem toan vien",
    "cong ty kiem toan",
    "don vi kiem toan",
    "hang kiem toan",
    "hoi dong quan tri",
    "ban tong giam doc",
    "ban giam doc",
    "ban dieu hanh",
    "ban kiem soat",
    "ke toan truong",
    "nguoi dai dien theo phap luat",
    "giay chung nhan dang ky doanh nghiep",
    "dang ky doanh nghiep",
    "trach nhiem cua ban",
    "nguoi ky bao cao",
    "ngay ky bao cao",
    "ngay lap bao cao",
    "dia chi tru so",
    "dia chi cong ty",
    "tru so chinh",
    "bo nhiem",
    "mien nhiem",
    "tu nhiem",
    "bai nhiem",
)

# Bare governance titles occur both in the front-section personnel list and in
# note schedules for remuneration, balances, dividends, and related-party
# transactions.  They are therefore contextual signals, never unconditional
# front-only markers.
_GOVERNANCE_ROLE_MARKERS = (
    "tong giam doc",
    "giam doc dieu hanh",
    "giam doc tai chinh",
    "pho tong giam doc",
    "chu tich",
)
_FRONT_ROLE_CONTEXT_MARKERS = (
    "la ai",
    "ho ten",
    "danh sach",
    "gom nhung ai",
    "chuc vu",
    "thanh vien",
    "bo nhiem",
    "mien nhiem",
    "tu nhiem",
    "bai nhiem",
    "nguoi ky",
)
_NOTE_ROLE_FINANCIAL_MARKERS = (
    "thu lao",
    "tien luong",
    "luong",
    "thuong",
    "thu nhap",
    "phai tra",
    "so du",
    "giao dich",
    "co tuc",
    "tam ung",
)
_HEADQUARTERS_MARKERS = (
    "tru so",
    "dia chi",
)
_HEADQUARTERS_NOTE_SCOPE_MARKERS = (
    "cong ty con",
    "cong ty lien ket",
    "cong ty lien doanh",
    "chi nhanh",
    "don vi truc thuoc",
)
_ASSET_DETAIL_MARKERS = (
    "khau hao",
    "hao mon",
    "nhan hieu",
    "thuong hieu",
    "quyen su dung dat",
)
_ASSET_DETAIL_CONTEXT_MARKERS = (
    "nguyen gia",
    "gia goc",
    "gia tri con lai",
    "gia tri hao mon",
    "hao mon luy ke",
    "khau hao trong",
    "phuong phap",
    "thoi gian",
    "thoi gian huu dung",
    "khong trich khau hao",
    "tai san co dinh",
    "bat dong san dau tu",
)
_CORPORATE_ACTIVITY_MARKERS = (
    "nganh nghe",
    "hoat dong chinh",
    "hoat dong kinh doanh",
    "linh vuc kinh doanh",
)
_RELATED_TRANSACTION_MARKERS = (
    "giao dich mua",
    "mua hang hoa",
    "mua dich vu",
    "co tuc chi tra cho",
    "co tuc tra cho",
    "gop von vao",
    "loi nhuan duoc chia",
)
_NOTE_DETAIL_DUAL_MARKERS = (
    "thue phai nop",
    "ngan sach nha nuoc",
    "chi phi phai tra",
    "vay ngan han",
    "quy khen thuong va phuc loi",
    "co tuc tuyen bo",
    "chinh sach co tuc",
)
_SEGMENT_NOTE_MARKERS = (
    "thi truong nuoc ngoai",
    "thi truong trong nuoc",
    "loi nhuan gop nuoc ngoai",
    "loi nhuan gop trong nuoc",
)

_REPORT_HISTORY_MARKERS = (
    "hinh thuc so huu",
    "duoc thanh lap",
    "thanh lap cong ty",
    "co phan hoa",
    "niem yet",
    "ma co phieu",
    "ma chung khoan",
    "so giao dich chung khoan",
    "tien than",
    "ngay thanh lap",
)

# These concepts are not exclusive to front matter.  Corporate structure can be
# described in company information, note I, an investment schedule, or a related
# party note.  Both candidates must survive instead of a hard front-only route.
_AMBIGUOUS_ORG_MARKERS = (
    "cong ty con",
    "cong ty lien ket",
    "cong ty lien doanh",
    "lien doanh",
    "don vi truc thuoc",
    "chi nhanh",
    "nha may",
    "kho van",
    "hoat dong chinh",
    "hoat dong kinh doanh chinh",
    "nganh nghe kinh doanh",
    "linh vuc kinh doanh",
    "cau truc tap doan",
    "so luong nhan vien",
    "tong so nhan vien",
)

_GOING_CONCERN_MARKERS = (
    "hoat dong lien tuc",
    "going concern",
)

_METRIC_HINTS = (
    "gia tri con lai",
    "nguyen gia",
    "gia goc",
    "hao mon",
    "khau hao",
    "du phong",
    "lai cho vay",
    "cam ket thue",
    "giao dich ben lien quan",
    "thoi gian khau hao",
    "nhan hieu",
    "thuong hieu",
    "quyen su dung dat",
    "mua hang hoa va dich vu",
    "thu lao",
    "tien luong",
    "vay ngan han",
    "quy khen thuong va phuc loi",
    "thue phai nop ngan sach nha nuoc",
    "chi phi phai tra",
    "doanh thu ban thanh pham",
    "doanh thu",
    "tong doanh thu",
    "loi nhuan sau thue",
    "loi nhuan truoc thue",
    "loi nhuan thuan tu hoat dong kinh doanh",
    "loi nhuan gop",
    "tai san ngan han",
    "tai san dai han",
    "tong tai san",
    "co tuc",
    "cam ket thue",
)

_ENTITY_TYPE_RE = re.compile(
    r"\b(?:ctcp|tong cong ty|cong ty\s+(?:co phan|tnhh|con|lien ket|lien doanh)"
    r"|tap doan|ngan hang|du an|nha may|chi nhanh|don vi)\b[^?.,;:]*"
)
_ENTITY_STOP_RE = re.compile(
    r"\s+\b(?:tai|vao|giua|cuoi|dau|"
    r"nam(?=\s+(?:nay|truoc|hien tai|(?:19|20)\d{2})\b)|"
    r"ky|trong|theo|dua tren|doi voi"
    r"|so voi|la bao nhieu|bao nhieu|la bao lau|bao lau|trong bao lau"
    r"|nhu the nao|la gi|o dau|nao|gi|ai|co nhung|co gia tri"
    r"|co so du|thay doi|chenh lech|tang|giam)\b.*$"
)
_DIMENSION_ENTITY_RE = re.compile(
    r"\b(?:thi truong|khu vuc)\s+(?P<entity>nuoc ngoai|trong nuoc|noi dia)\b"
)
_GENERIC_ENTITY_MARKERS = (
    "giao dich",
    "hoat dong cua cong ty",
    "cong ty trong",
    "cong ty dua tren",
    "cong ty theo",
)
_POLICY_ENTITY_METRICS = (
    "thoi gian khau hao",
    "thoi gian huu dung",
    "phuong phap khau hao",
    "thoi gian phan bo",
    "phuong phap phan bo",
)


def _policy_query_contract(query: str) -> tuple[str, str]:
    """Return an explicit policy topic and its canonical metric label."""

    text = _ascii_text(query)
    if not _contains_any(text, ("khau hao", "hao mon", "phan bo")):
        return "", ""
    amortization = "phan bo" in text and not _contains_any(
        text, ("khau hao", "hao mon")
    )
    if re.search(
        r"\bco\s+(?:duoc\s+)?(?:khau hao|hao mon|phan bo)\s+khong\b"
        r"|\bkhong\s+(?:khau hao|hao mon|phan bo)\b",
        text,
    ):
        return "non_depreciation", "chính sách không khấu hao"
    if _contains_any(text, ("bao lau", "may nam", "thoi gian khau hao", "thoi gian phan bo")):
        return (
            ("amortization_period", "thời gian phân bổ")
            if amortization
            else ("depreciation_period", "thời gian khấu hao")
        )
    if _contains_any(
        text,
        (
            "nhu the nao",
            "phuong phap khau hao",
            "phuong phap hao mon",
            "phuong phap phan bo",
            "cach khau hao",
            "cach phan bo",
        ),
    ):
        return (
            ("amortization_method", "phương pháp phân bổ")
            if amortization
            else ("depreciation_method", "phương pháp khấu hao")
        )
    return "", ""


def _policy_entity_from_query(query: str, policy_metric: str) -> str:
    """Bind the named asset for metric-first and asset-first policy questions."""

    text = _ascii_text(query)
    dataset_entities = _dataset_slot_matches(query, "entity")
    if dataset_entities:
        # Canonical typed entities outrank prose wrappers introduced by a
        # planner ("bất động sản đầu tư - quyền sử dụng đất lâu dài") and
        # qualifiers such as "ước tính của".
        return dataset_entities[0]
    action = re.search(
        r"\b(?:duoc\s+)?(?:khau hao|hao mon|phan bo)\b",
        text,
    )
    if not action:
        return _entity_from_query(query, policy_metric)

    prefix = text[: action.start()].strip(" -")
    prefix = re.sub(
        r"\b(?:duoc\s+xu\s+ly|co|se)\s*$",
        "",
        prefix,
    ).strip(" -")
    prefix_is_policy_label = bool(
        re.fullmatch(
            r"(?:thoi gian|phuong phap|cach|chinh sach)(?:\s+duoc)?",
            prefix,
        )
    )
    candidate = (
        text[action.end() :] if prefix_is_policy_label else prefix
    ).strip(" -")
    if prefix_is_policy_label:
        candidate = re.sub(
            r"^(?:(?:uoc tinh|du kien)\s+)?"
            r"(?:cua|doi voi|ap dung cho)\s+",
            "",
            candidate,
        )
        if re.match(
            r"^(?:la|bao|trong|nhu|co|duoc)\b",
            candidate,
        ):
            return ""
    if " - " in candidate:
        candidate = candidate.rsplit(" - ", 1)[-1].strip()
    candidate = re.split(
        r"\s+va\s+(?=(?:thoi gian|phuong phap|cach|chinh sach)"
        r"\s+(?:khau hao|hao mon|phan bo)\b)",
        candidate,
        maxsplit=1,
    )[0].strip()
    candidate = _ENTITY_STOP_RE.sub("", candidate).strip(" -")
    candidate = re.sub(
        r"\b(?:duoc|co|se)\s*$",
        "",
        candidate,
    ).strip(" -")
    if not candidate or candidate in {"tai san", "khoan muc"}:
        return _entity_from_query(query, policy_metric)
    return candidate

# Query-slot vocabulary for the active dataset.  Unlike routing keywords, these
# phrases come only from canonical typed axes in SQLite and are used to bind
# exact metric/entity slots that a static vocabulary cannot anticipate.
_DATASET_SLOT_LEXICON: dict[str, tuple[tuple[str, str], ...]] = {
    "metric": (),
    "entity": (),
}
_DATASET_SLOT_LEXICON_VERSION = 0
_ACTIVE_SLOT_LEXICON_DATASET = ""


def _slot_phrase_norm(value: Any) -> str:
    """Normalize a phrase to a punctuation-insensitive token sequence."""

    return " ".join(_TOKEN_RE.findall(_ascii_text(value)))


def set_dataset_slot_lexicon(
    mapping: Mapping[str, Iterable[str]] | None,
    *,
    dataset_id: str = "",
) -> None:
    """Replace the query-slot lexicon for the active dataset.

    Every replacement advances a monotonic generation so cached parses cannot
    leak metric/entity slots when one process switches between datasets.
    """

    global _ACTIVE_SLOT_LEXICON_DATASET, _DATASET_SLOT_LEXICON_VERSION

    prepared: dict[str, tuple[tuple[str, str], ...]] = {}
    for axis in ("metric", "entity"):
        by_normalized: dict[str, str] = {}
        for raw_phrase in (mapping or {}).get(axis, ()):
            phrase = _SPACE_RE.sub(" ", str(raw_phrase or "").strip()).lower()
            normalized = _slot_phrase_norm(phrase)
            if not normalized:
                continue
            previous = by_normalized.get(normalized)
            if previous is None or (phrase.casefold(), phrase) < (
                previous.casefold(),
                previous,
            ):
                by_normalized[normalized] = phrase
        prepared[axis] = tuple(
            sorted(
                by_normalized.items(),
                key=lambda item: (
                    -len(item[0].split()),
                    -len(item[0]),
                    item[0],
                    item[1],
                ),
            )
        )

    _DATASET_SLOT_LEXICON.clear()
    _DATASET_SLOT_LEXICON.update(prepared)
    _ACTIVE_SLOT_LEXICON_DATASET = str(dataset_id or "").strip()
    _DATASET_SLOT_LEXICON_VERSION += 1


def dataset_slot_lexicon_version() -> int:
    """Return the active lexicon generation used in parse-cache keys."""

    return _DATASET_SLOT_LEXICON_VERSION


@lru_cache(maxsize=1024)
def _dataset_slot_matches_cached(
    query_norm: str,
    axis: str,
    _lexicon_version: int,
) -> tuple[str, ...]:
    padded_query = f" {query_norm} "
    exact = tuple(
        phrase
        for normalized, phrase in _DATASET_SLOT_LEXICON.get(axis, ())
        if f" {normalized} " in padded_query
    )
    if axis != "metric":
        return exact
    exact_specific = tuple(
        phrase
        for normalized, phrase in _DATASET_SLOT_LEXICON.get(axis, ())
        if (
            f" {normalized} " in padded_query
            and len(normalized.split()) >= 3
        )
    )
    if exact_specific:
        return exact

    # A report-specific metric may be paraphrased with modifiers inserted
    # between its canonical tokens ("doanh thu gộp từ bất động sản giữ để
    # bán" vs "doanh thu bán bất động sản"). Accept only a full token-subset
    # match of a sufficiently specific canonical metric; generic two-word
    # labels such as "doanh thu" remain fail-closed.
    query_tokens = set(query_norm.split())
    subset_matches = tuple(
        phrase
        for normalized, phrase in _DATASET_SLOT_LEXICON.get(axis, ())
        if (
            len(normalized.split()) >= 3
            and set(normalized.split()).issubset(query_tokens)
        )
    )
    return subset_matches or exact


def _dataset_slot_matches(query: str, axis: str) -> tuple[str, ...]:
    return _dataset_slot_matches_cached(
        _slot_phrase_norm(query),
        axis,
        dataset_slot_lexicon_version(),
    )


@lru_cache(maxsize=512)
def _keyword_matches_cached(
    query_norm: str,
    _vocabulary_version: int,
) -> tuple[tuple[str, str], ...]:
    matches: list[tuple[str, str]] = []
    for keyword, table in iter_keyword_table_pairs():
        keyword_norm = _ascii_text(keyword)
        if keyword_norm and keyword_norm in query_norm:
            matches.append((keyword, table))
    return tuple(matches)


def _keyword_matches(query: str) -> list[tuple[str, str]]:
    """Return exact vocabulary phrases contained in the normalized question."""

    query_norm = _ascii_text(normalize_keyword_synonyms(query))
    return list(_keyword_matches_cached(query_norm, keyword_vocabulary_version()))


def _metric_from_query(query: str) -> str:
    dataset_matches = _dataset_slot_matches(query, "metric")
    matches = _keyword_matches(query)
    text = _ascii_text(query)
    candidates = list(dataset_matches)
    candidates.extend(keyword for keyword, _table in matches)
    candidates.extend(marker for marker in _METRIC_HINTS if marker in text)
    if not candidates:
        return ""
    # Compare dataset-derived phrases with the canonical vocabulary instead of
    # returning the first dataset hit.  A broad learned phrase such as "tài
    # sản" must not erase a specific canonical total explicitly present in the
    # question ("tổng cộng tài sản").
    return max(
        candidates,
        key=lambda item: (len(_tokens(item)), len(_ascii_text(item))),
    )


_SECTION_METRIC_BY_KEY = {
    "ts_ngan_han": "tổng tài sản ngắn hạn",
    "ts_dai_han": "tổng tài sản dài hạn",
    "tong_tai_san": "tổng cộng tài sản",
    "no_phai_tra": "tổng nợ phải trả",
    "no_ngan_han": "tổng nợ ngắn hạn",
    "no_dai_han": "tổng nợ dài hạn",
    "von_chu": "tổng vốn chủ sở hữu",
    "nguon_von": "tổng nguồn vốn",
}


def _section_metric_from_query(query: str) -> str:
    """Preserve a specific requested statement total before broad synonyms."""

    return _SECTION_METRIC_BY_KEY.get(query_section_total_key(query), "")


def _entity_from_query(query: str, metric: str) -> str:
    text = _ascii_text(query)
    metric_norm = _ascii_text(metric)
    dataset_matches = _dataset_slot_matches(query, "entity")
    for candidate in dataset_matches:
        if _ascii_text(candidate) != metric_norm:
            return candidate

    dimension_match = _DIMENSION_ENTITY_RE.search(text)
    if dimension_match:
        return dimension_match.group("entity").strip()

    match = _ENTITY_TYPE_RE.search(text)
    entity = match.group(0).strip() if match else ""
    policy_metric_tail = False

    # A conservative "X của Y" fallback recovers asset classes and named
    # counterparties without treating the whole question as an entity.
    if not entity:
        possessive = re.search(r"\bcua\s+([^?.,;:]+)", text)
        if possessive:
            entity = possessive.group(1).strip()
            policy_metric_tail = metric_norm in _POLICY_ENTITY_METRICS

    # OCR policy schedules expose the asset class as an entity axis, while
    # questions often omit "của": "thời gian khấu hao máy móc ...".  Recover
    # only the bounded tail of an explicit policy metric and let the ordinary
    # question-suffix stop words close it.
    if (
        not entity
        and metric_norm in _POLICY_ENTITY_METRICS
        and metric_norm in text
    ):
        entity = text.split(metric_norm, 1)[1].strip()
        entity = re.sub(
            r"^(?:cua|doi voi|ap dung cho)\s+",
            "",
            entity,
        ).strip()
        policy_metric_tail = bool(entity)

    entity = _ENTITY_STOP_RE.sub("", entity).strip(" -")
    if (
        not entity
        or (" va " in entity and not policy_metric_tail)
        or entity in _AMBIGUOUS_ORG_MARKERS
        or entity == metric_norm
        or entity in metric_norm
        or (metric_norm and metric_norm in entity)
        or _contains_any(entity, _GENERIC_ENTITY_MARKERS)
    ):
        return ""
    # A bare legal/entity type is too weak to be a strict retrieval slot.
    if entity in {
        "cong ty",
        "doanh nghiep",
        "ctcp",
        "ngan hang",
        "du an",
        "don vi",
        "cac giao dich voi ben lien quan",
    }:
        return ""
    return entity


def _operation_from_query(query: str) -> str:
    text = _ascii_text(query)
    if _contains_any(text, ("bao nhieu lan", "gap bao nhieu", "gap may lan")):
        return "multiple"
    if _DEBT_EQUITY_RATIO_RE.search(text):
        return "ratio"
    raw_lower = _SPACE_RE.sub(" ", str(query or "").strip().lower())
    has_share_phrase = bool(
        re.search(r"\b(?:tỷ|tỉ)\s+trọng\b", raw_lower)
        or re.search(r"(?<!cong )\bty trong\b", text)
    )
    if has_share_phrase:
        numerator, denominator = _ratio_segments(query, "share")
        if numerator and denominator:
            return "share"
    if _contains_any(
        text,
        (
            "phan tram thay doi",
            "ty le thay doi",
            "tang bao nhieu phan tram",
            "giam bao nhieu phan tram",
            "ty le tang",
            "ty le giam",
        ),
    ):
        return "percent_change"
    if re.search(r"\s/\s", str(query or "")) or (
        "/" in str(query or "") and len(query_value_types(query)) >= 2
    ):
        return "ratio"
    if _contains_any(text, ("ty le", "phan tram", "he so")):
        numerator, denominator = _ratio_segments(query, "ratio")
        if numerator and denominator:
            return "ratio"
    calculation_context = _contains_any(
        text,
        (
            "bao nhieu",
            "so sanh",
            "so voi",
            "giua dau",
            "giua cuoi",
            "nam nay",
            "nam truoc",
            "ky nay",
            "ky truoc",
        ),
    )
    if _contains_any(
        text,
        (
            "muc thay doi",
            "muc do thay doi",
            "thay doi bao nhieu",
            "bien dong",
            "tang bao nhieu",
            "giam bao nhieu",
            "khong con so du",
        ),
    ) or ("chenh lech" in text and calculation_context):
        return "delta"
    if _contains_any(text, ("so sanh", "so voi")) or (
        calculation_context and is_comparison_query(query)
    ):
        return "compare"
    if _contains_any(
        text,
        (
            "liet ke",
            "danh sach",
            "nhung khoan",
            "cac khoan nao",
            "cong ty nao",
            "cong ty con nao",
            "nhung cong ty con",
            "cac cong ty con",
            "don vi nao",
            "du an nao",
            "nha may nao",
            "nhung nha may",
            "cac nha may",
            "chi nhanh nao",
            "nhung chi nhanh",
            "cac chi nhanh",
            "lon nhat",
            "cao nhat",
            "thap nhat",
        ),
    ):
        return "list"
    return "lookup"


def _meaningful_metric(query: str) -> str:
    metric = _metric_from_query(query)
    if _ascii_text(metric) in _VALUE_TYPE_ONLY_METRICS:
        return ""
    return metric


def _ratio_segments(query: str, operation: str) -> tuple[str, str]:
    """Split only explicit directional ratio syntax.

    ``so với`` and a bare ``và`` are deliberately excluded: both are also
    ordinary comparison syntax, so assigning numerator/denominator there would
    silently invent arithmetic direction.
    """

    raw = _SPACE_RE.sub(" ", str(query or "").strip()).strip(" ?.")
    if not raw:
        return "", ""
    cleaned = _RATIO_PREFIX_RE.sub("", raw, count=1).strip()
    separators = list(_RATIO_SEPARATORS)
    if operation == "share":
        separators.append(_SHARE_SEPARATOR)

    for separator in separators:
        parts = separator.split(cleaned, maxsplit=1)
        if len(parts) != 2:
            continue
        numerator = parts[0].strip(" -:;,")
        denominator = _RATIO_SUFFIX_RE.sub("", parts[1]).strip(" -:;,?.")
        if _DEBT_EQUITY_RATIO_RE.search(_ascii_text(query)):
            denominator = _DEBT_EQUITY_FORMULA_RE.sub(" ", denominator)
            denominator = _SPACE_RE.sub(" ", denominator).strip(" -:;,?.")
        if numerator and denominator:
            return numerator, denominator
    return "", ""


def _operand_query_with_shared_slots(
    fragment: str,
    *,
    shared_metric: str,
    shared_period: str,
    shared_period_role: str = "",
    shared_period_labels: tuple[str, ...] = (),
) -> str:
    query = _SPACE_RE.sub(" ", str(fragment or "").strip()).strip(" ?.")
    fragment_metric = _meaningful_metric(query)
    fragment_types = query_value_types(query)
    if not fragment_metric and fragment_types and shared_metric:
        query = f"{query} {shared_metric}".strip()
    if shared_period in {"cuối", "đầu"} and not canonical_period(query):
        query = f"{query} {'cuối kỳ' if shared_period == 'cuối' else 'đầu kỳ'}"
    query_norm = _ascii_text(query)
    has_relative_role = _contains_any(
        query_norm,
        (
            "nam nay",
            "nam hien tai",
            "ky nay",
            "nam truoc",
            "ky truoc",
            "current year",
            "previous year",
            "prior year",
        ),
    )
    if not has_relative_role:
        if shared_period_role == "current":
            query = f"{query} năm nay"
        elif shared_period_role == "previous":
            query = f"{query} năm trước"
    query_norm = _ascii_text(query)
    for period_label in shared_period_labels:
        label = str(period_label or "").strip()
        if label and _ascii_text(label) not in query_norm:
            query = f"{query} {label}"
            query_norm = _ascii_text(query)
    return _SPACE_RE.sub(" ", query).strip()


def _operand_aggregation(query: str, entity: str) -> str:
    text = _ascii_text(query)
    if (
        re.search(r"\b(?:tong|toan bo|total)\b", text)
        or query_section_total_key(query)
    ):
        return "total"
    return "component" if entity else ""


def _query_scope_label(query: str, semantic_dimensions: Any) -> str:
    """Return only an explicitly named, reusable semantic schedule scope."""

    text = _ascii_text(query)
    if getattr(semantic_dimensions, "counterparty", "") or "ben lien quan" in text:
        return "bên liên quan"
    if _contains_any(
        text,
        (
            "khu vuc dia ly",
            "bao cao bo phan theo khu vuc",
            "bo phan theo khu vuc",
        ),
    ):
        return "khu vực địa lý"
    if getattr(semantic_dimensions, "policy_topic", "") and _contains_any(
        text,
        ("chinh sach ke toan", "phuong phap", "dieu kien ghi nhan"),
    ):
        return "chính sách kế toán"
    return ""


def _ratio_operands(
    query: str,
    *,
    operation: str,
    period: str,
    period_role: str = "",
    period_labels: tuple[str, ...] = (),
) -> tuple[QueryOperand, ...]:
    if (
        operation not in {"ratio", "share"}
        or period == "both"
        or period_role == "both"
        or len(period_labels) > 1
    ):
        return ()

    numerator_raw, denominator_raw = _ratio_segments(query, operation)
    if not numerator_raw or not denominator_raw:
        return ()

    shared_metric = _meaningful_metric(query)
    shared_entity = _entity_from_query(query, shared_metric)
    shared_semantic_dimensions = derive_query_semantic_dimensions(
        query,
        entity=shared_entity,
    )
    shared_scope_label = _query_scope_label(query, shared_semantic_dimensions)
    debt_equity_ratio = bool(_DEBT_EQUITY_RATIO_RE.search(_ascii_text(query)))
    operand_specs = []
    for role, fragment in (
        ("numerator", numerator_raw),
        ("denominator", denominator_raw),
    ):
        if (
            debt_equity_ratio
            and role == "numerator"
            and _ascii_text(fragment).strip() == "no"
        ):
            # "Nợ" is a safe alias for total liabilities only inside an
            # explicitly directional D/E expression.  It must remain ambiguous
            # in ordinary loan/debt questions.
            fragment = "nợ phải trả"
        operand_query = _operand_query_with_shared_slots(
            fragment,
            shared_metric=shared_metric,
            shared_period=period,
            shared_period_role=period_role,
            shared_period_labels=period_labels,
        )
        metric = _meaningful_metric(operand_query)
        detected_types = query_value_types(operand_query)
        value_types = tuple(
            value_type
            for value_type in (
                "nguyên giá",
                "hao mòn",
                "giá trị còn lại",
                "dự phòng",
                "giá trị hợp lý",
            )
            if value_type in detected_types
        )
        # A value-type-only leg is safe only when the complete expression names
        # the shared schedule metric (e.g. hao mòn / nguyên giá của TSCĐ).
        if not metric and value_types and shared_metric:
            metric = shared_metric
            operand_query = _operand_query_with_shared_slots(
                operand_query,
                shared_metric=shared_metric,
                shared_period=period,
                shared_period_role=period_role,
                shared_period_labels=period_labels,
            )
        if not metric:
            return ()
        entity = _entity_from_query(operand_query, metric)
        leg_dimensions = derive_query_semantic_dimensions(
            operand_query,
            entity=entity,
        )
        full_query_dimensions = derive_query_semantic_dimensions(
            query,
            entity=entity,
        )

        def semantic_value(field: str) -> str:
            # Transaction, movement, geography, and policy meanings belong to
            # the individual arithmetic leg.  Only counterparty context may be
            # shared by the full expression (for example a trailing "trong
            # giao dịch với bên liên quan"), while the named entity remains
            # specific to this leg.
            fallback = (
                getattr(full_query_dimensions, field, "")
                if field == "counterparty"
                else ""
            )
            return str(
                getattr(leg_dimensions, field, "")
                or fallback
                or ""
            ).strip()

        operand_aggregation = (
            "total"
            if debt_equity_ratio
            else _operand_aggregation(operand_query, entity)
        )
        operand_section_key = (
            {
                "no phai tra": "no_phai_tra",
                "von chu so huu": "von_chu",
            }.get(_ascii_text(metric), "")
            if debt_equity_ratio
            else query_section_total_key(operand_query)
        )
        if (
            operand_aggregation == "total"
            and operand_section_key
            and query_section_total_key(operand_query) != operand_section_key
        ):
            # Every independently executed operand query must carry its own
            # aggregate slot.  Keeping ``aggregation=total`` only in side
            # metadata is insufficient because retrieval reparses the query:
            # a bare "vốn chủ sở hữu" then exact-matches the component row
            # (for example statement code 410) before the canonical total row.
            # Prefixing the generic accounting concept makes structured lookup,
            # targeted retry, cache replay, and result filtering agree on the
            # same primary-statement section without relying on a company/code.
            operand_query = f"tổng {operand_query}".strip()

        operand_specs.append(
            QueryOperand(
                role=role,
                query=operand_query,
                metric=metric,
                entity=entity,
                period=canonical_period(operand_query) or period,
                period_role=(
                    "current"
                    if _contains_any(
                        _ascii_text(operand_query),
                        ("nam nay", "nam hien tai", "ky nay", "current year"),
                    )
                    else "previous"
                    if _contains_any(
                        _ascii_text(operand_query),
                        ("nam truoc", "ky truoc", "previous year", "prior year"),
                    )
                    else period_role
                ),
                reporting_basis=(
                    query_reporting_basis(operand_query)
                    or query_reporting_basis(query)
                ),
                period_label=(
                    period_labels[0] if len(period_labels) == 1 else ""
                ),
                value_type=value_types[0] if len(value_types) == 1 else "",
                aggregation=operand_aggregation,
                scope_label=(
                    _query_scope_label(operand_query, leg_dimensions)
                    or shared_scope_label
                ),
                counterparty=semantic_value("counterparty"),
                transaction_type=semantic_value("transaction_type"),
                movement_type=semantic_value("movement_type"),
                geography=semantic_value("geography"),
                policy_topic=semantic_value("policy_topic"),
                section_key=operand_section_key,
            )
        )

    numerator, denominator = operand_specs
    if (
        _ascii_text(numerator.metric) == _ascii_text(denominator.metric)
        and numerator.value_type
        and denominator.value_type
        and numerator.value_type != denominator.value_type
        and not numerator.entity
        and not denominator.entity
    ):
        # A ratio between two value axes of the same schedule, without a named
        # row/entity, asks for the schedule total.  Carry that slot in each
        # self-contained retrieval query so exact structured lookup can filter
        # out asset-class/component distractors.
        operand_specs = [
            QueryOperand(
                role=operand.role,
                query=(
                    operand.query
                    if _operand_aggregation(operand.query, operand.entity) == "total"
                    else f"tổng {operand.query}"
                ),
                metric=operand.metric,
                entity=operand.entity,
                period=operand.period,
                period_role=operand.period_role,
                reporting_basis=operand.reporting_basis,
                period_label=operand.period_label,
                value_type=operand.value_type,
                aggregation="total",
                scope_label=operand.scope_label,
                counterparty=operand.counterparty,
                transaction_type=operand.transaction_type,
                movement_type=operand.movement_type,
                geography=operand.geography,
                policy_topic=operand.policy_topic,
                section_key=operand.section_key,
            )
            for operand in operand_specs
        ]
        numerator, denominator = operand_specs

    numerator_key = (
        _ascii_text(numerator.metric),
        _ascii_text(numerator.value_type),
        _ascii_text(numerator.entity),
        numerator.aggregation,
        _ascii_text(numerator.scope_label),
        _ascii_text(numerator.counterparty),
        _ascii_text(numerator.transaction_type),
        _ascii_text(numerator.movement_type),
        _ascii_text(numerator.geography),
        _ascii_text(numerator.policy_topic),
    )
    denominator_key = (
        _ascii_text(denominator.metric),
        _ascii_text(denominator.value_type),
        _ascii_text(denominator.entity),
        denominator.aggregation,
        _ascii_text(denominator.scope_label),
        _ascii_text(denominator.counterparty),
        _ascii_text(denominator.transaction_type),
        _ascii_text(denominator.movement_type),
        _ascii_text(denominator.geography),
        _ascii_text(denominator.policy_topic),
    )
    if numerator_key == denominator_key:
        return ()
    return tuple(operand_specs)


def parse_query_slots(query: str) -> QuerySlots:
    """Parse only slots with sufficiently explicit lexical evidence."""

    return _parse_query_slots_cached(
        str(query or ""),
        keyword_vocabulary_version(),
        dataset_slot_lexicon_version(),
    )


def targeted_retry_query(query: str) -> str:
    """Build one compact retry query that preserves every explicit typed slot."""

    raw = str(query or "").strip()
    slots = parse_query_slots(raw)
    transaction_phrases = {
        "purchase": "giao dịch mua",
        "sale": "giao dịch bán",
        "lending": "cho vay",
        "borrowing": "khoản vay",
        "capital_contribution": "góp vốn",
        "dividend": "cổ tức",
    }
    movement_phrases = {
        "opening_balance": "số dư đầu kỳ",
        "closing_balance": "số dư cuối kỳ",
        "reclassification": "phân loại lại",
        "disposal": "thanh lý nhượng bán",
        "depreciation_charge": "khấu hao trong kỳ",
        "amortization_charge": "phân bổ trong kỳ",
        "provision_charge": "trích lập dự phòng",
        "reversal": "hoàn nhập",
        "repayment": "hoàn trả",
        "utilization": "sử dụng trong kỳ",
        "transfer": "kết chuyển điều chuyển",
        "addition": "tăng trong kỳ",
        "reduction": "giảm trong kỳ",
    }
    policy_phrases = {
        "depreciation_period": "thời gian khấu hao",
        "amortization_period": "thời gian phân bổ",
        "depreciation_method": "phương pháp khấu hao",
        "amortization_method": "phương pháp phân bổ",
        "recognition_criteria": "điều kiện ghi nhận",
        "capitalization_criteria": "điều kiện vốn hóa",
        "measurement_basis": "cơ sở đo lường",
        "non_depreciation": "chính sách không khấu hao",
        "revenue_recognition": "ghi nhận doanh thu",
        "inventory_policy": "chính sách hàng tồn kho",
    }
    operation_phrases = {
        "compare": "so sánh",
        "delta": "thay đổi bao nhiêu",
        "percent_change": "phần trăm thay đổi",
        "multiple": "bao nhiêu lần",
        "list": "liệt kê",
    }
    coverage_phrases = {
        "materiality": "mức độ trọng yếu",
        "causal": "nguyên nhân",
        "roll_forward": "biến động trong năm",
        "composition": "phân tích cơ cấu",
    }

    # A directional ratio/share is defined by its ordered operands. Rebuild
    # that expression instead of reducing it to a generic metric, which would
    # silently turn the retry into a lookup and lose arithmetic direction.
    if slots.operation in {"ratio", "share"} and len(slots.operands) == 2:
        operand_texts = []
        for operand in slots.operands:
            operand_parts = [
                operand.query,
                transaction_phrases.get(operand.transaction_type, ""),
                movement_phrases.get(operand.movement_type, ""),
                policy_phrases.get(operand.policy_topic, ""),
            ]
            if operand.counterparty:
                operand_parts.append(f"bên liên quan {operand.counterparty}")
            if operand.geography:
                operand_parts.append(f"khu vực {operand.geography}")
            if operand.scope_label:
                operand_parts.append(operand.scope_label)
            if slots.coverage_template:
                operand_parts.append(
                    coverage_phrases.get(slots.coverage_template, "")
                )
            operand_texts.append(
                " ".join(
                    dict.fromkeys(
                        text
                        for text in (
                            str(part or "").strip()
                            for part in operand_parts
                        )
                        if text
                    )
                )
            )
        prefix = "tỷ trọng" if slots.operation == "share" else "hệ số"
        return f"{prefix} {operand_texts[0]} trên {operand_texts[1]}"

    parts = [
        operation_phrases.get(slots.operation, ""),
        coverage_phrases.get(slots.coverage_template, ""),
        # Put canonical human phrases first so ASCII-normalized metric aliases
        # do not win de-duplication and erase Vietnamese policy semantics.
        policy_phrases.get(slots.policy_topic, ""),
        transaction_phrases.get(slots.transaction_type, ""),
        movement_phrases.get(slots.movement_type, ""),
        slots.metric,
    ]
    if slots.counterparty:
        parts.append(f"bên liên quan {slots.counterparty}")
    if slots.geography:
        parts.append(f"khu vực {slots.geography}")
    entity_key = _ascii_text(slots.entity)
    if (
        slots.entity
        and entity_key
        not in {
            _ascii_text(slots.counterparty),
            _ascii_text(slots.geography),
        }
    ):
        parts.append(slots.entity)
    parts.extend(slots.period_labels)
    if slots.reporting_basis == "cumulative":
        parts.append("lũy kế từ đầu năm")
    elif slots.reporting_basis == "quarter":
        parts.append("trong quý")
    elif slots.reporting_basis == "full_period":
        parts.append("cả năm hoặc lũy kế năm")
    if slots.period_role == "current":
        parts.append("năm nay")
    elif slots.period_role == "previous":
        parts.append("năm trước")
    elif slots.period_role == "both":
        parts.extend(("năm nay", "năm trước"))
    if slots.period == "cuối":
        parts.append("số cuối kỳ")
    elif slots.period == "đầu":
        parts.append("số đầu kỳ")
    elif slots.period == "both":
        parts.extend(("số cuối kỳ", "số đầu kỳ"))
    parts.extend(slots.value_type)
    if slots.aggregation == "total":
        parts.append("tổng")

    compact = []
    seen = set()
    for part in parts:
        text = str(part or "").strip()
        key = _ascii_text(text)
        if not text or not key or key in seen:
            continue
        compact.append(text)
        seen.add(key)
    return " ".join(compact) or normalize_keyword_synonyms(raw) or raw


def coverage_legs_for_fact(
    slots: QuerySlots,
    meta: dict | None,
    doc: str = "",
) -> frozenset[str]:
    """Classify declared coverage legs carried by one typed fact.

    This shared classifier is used both while selecting block siblings and when
    deciding whether a requirement may transition to ``matched``.  Keeping the
    two stages on one contract prevents retrieval from injecting four
    roll-forward rows while lifecycle code closes the requirement on only the
    first row.
    """

    meta = meta if isinstance(meta, dict) else {}
    text = _ascii_text(
        " ".join(
            str(meta.get(key, "") or "")
            for key in (
                "item_name",
                "row_label",
                "column_label",
                "metric_label",
                "entity_label",
                "scope_label",
                "counterparty",
                "transaction_type",
                "movement_type",
                "geography",
                "policy_topic",
            )
        )
        + f" {doc}"
    )
    period = str(meta.get("period", "") or "").strip()
    if not period:
        period = canonical_period(
            f"{meta.get('period_label', '')} "
            f"{meta.get('period_role', '')} "
            f"{meta.get('item_name', '')} {doc}"
        )
    period_role_value = fact_period_role(meta, doc)
    aggregation = _ascii_text(meta.get("aggregation_level", ""))
    if aggregation not in {"total", "component"}:
        label = str(meta.get("item_name", "") or text)
        normalized_parts = {
            _ascii_text(part).strip()
            for part in label.split("|")
            if str(part).strip()
        }
        aggregation = (
            "total"
            if normalized_parts & {"tong", "tong cong", "cong", "total"}
            or section_total_key(label)
            else "component"
        )

    legs: set[str] = set()
    if (
        period == "đầu"
        or _contains_any(text, ("so dau", "dau ky", "dau nam"))
    ):
        legs.add("opening")
    if (
        period == "cuối"
        or _contains_any(text, ("so cuoi", "cuoi ky", "cuoi nam"))
    ):
        legs.add("closing")
    if period_role_value == "previous":
        legs.add("target_previous")
    if period_role_value == "current":
        legs.add("target_current")

    movement = _ascii_text(meta.get("movement_type", ""))
    if movement in {
        "addition",
        "provision_charge",
        "borrowing",
        "accrual",
    } or _contains_any(
        text,
        (
            "tang trong",
            "mua trong",
            "phat sinh",
            "vay them",
            "trich lap",
            "bo sung",
        ),
    ):
        legs.add("additions")
    if movement in {
        "reduction",
        "disposal",
        "repayment",
        "utilization",
        "reversal",
    } or _contains_any(
        text,
        (
            "giam trong",
            "thanh ly",
            "nhuong ban",
            "hoan tra",
            "da nop",
            "su dung",
            "hoan nhap",
        ),
    ):
        legs.add("reductions")
    if _contains_any(
        text,
        (
            "nguyen nhan",
            "chu yeu do",
            "do bien dong",
            "yeu to",
            "driver",
        ),
    ):
        legs.add("declared_drivers")
    if aggregation == "total":
        legs.update(("total", "company_total"))
    elif aggregation == "component":
        legs.update(("components_closed", "transaction_categories"))
    return frozenset(legs)


@lru_cache(maxsize=512)
def _parse_query_slots_cached(
    query: str,
    _vocabulary_version: int,
    _slot_lexicon_version: int,
) -> QuerySlots:
    raw = str(query or "")
    text = _ascii_text(raw)
    operation = _operation_from_query(raw)
    policy_topic_override, policy_metric = _policy_query_contract(raw)
    metric = (
        policy_metric
        or (
            _section_metric_from_query(raw)
            if operation not in {"ratio", "share"}
            else ""
        )
        or _metric_from_query(raw)
    )
    entity = (
        _policy_entity_from_query(raw, metric)
        if policy_topic_override
        else _entity_from_query(raw, metric)
    )
    semantic_dimensions = derive_query_semantic_dimensions(raw, entity=entity)
    policy_topic = (
        policy_topic_override
        or semantic_dimensions.policy_topic
    )
    scope_label = (
        "chính sách kế toán"
        if policy_topic_override
        else _query_scope_label(raw, semantic_dimensions)
    )

    period = canonical_period(raw)
    period_role = ""
    reporting_basis = query_reporting_basis(raw)
    period_labels = _explicit_period_labels(raw)
    explicit_dates = tuple(
        value for value in period_labels if "/" in value
    )
    explicit_years = tuple(
        value for value in period_labels if "/" not in value
    )
    has_current_role = _contains_any(
        text,
        ("nam nay", "nam hien tai", "ky nay", "current year", "current period"),
    )
    has_previous_role = _contains_any(
        text,
        ("nam truoc", "ky truoc", "previous year", "prior year", "prior period"),
    )
    has_closing_position = bool(
        re.search(r"\bcuoi\s+(?:ky|nam|quy|thang)\b", text)
    )
    has_opening_position = bool(
        re.search(r"\bdau\s+(?:ky|nam|quy|thang)\b", text)
    )
    has_explicit_period_pair = (
        len(explicit_dates) >= 2
        or (
            len(explicit_years) >= 2
            and operation in {"compare", "delta", "percent_change", "multiple"}
        )
    )
    if (
        has_closing_position and has_opening_position
    ):
        period = "both"
    elif has_closing_position:
        period = "cuối"
    elif has_opening_position:
        period = "đầu"
    elif has_explicit_period_pair:
        # Exact years/dates are their own temporal axis.  They are retained in
        # ``period_labels`` and must not be rewritten to opening/closing.
        period = ""

    if has_current_role and has_previous_role:
        period_role = "both"
    elif has_current_role:
        period_role = "current"
    elif has_previous_role:
        period_role = "previous"
    if (
        period in {"cuối", "đầu"}
        and period_role in {"current", "previous"}
    ):
        # Opening/closing is the temporal axis for a stock fact. Ingestion
        # intentionally does not reinterpret it as a flow current/previous
        # role, so a redundant phrase such as "cuối năm hiện tại" must not
        # exclude the correctly typed closing balance.
        period_role = ""
    if period in {"cuối", "đầu", "both"}:
        # Opening/closing is a stock-position axis, not a flow horizon.  A
        # phrase such as "cuối quý" must not force a quarterly-flow match.
        reporting_basis = ""

    detected_value_types = query_value_types(raw)
    if policy_topic in {
        "depreciation_period",
        "depreciation_method",
        "amortization_period",
        "amortization_method",
        "non_depreciation",
    }:
        # Here "khấu hao" names a policy topic, not the accumulated-
        # depreciation axis of a numeric fixed-asset matrix.
        detected_value_types.discard("hao mòn")
    value_types = tuple(
        value_type
        for value_type in (
            "nguyên giá",
            "hao mòn",
            "giá trị còn lại",
            "dự phòng",
            "giá trị hợp lý",
        )
        if value_type in detected_value_types
    )

    aggregation_text = text
    # "Tổng" is part of an officer title in "Tổng Giám đốc", not a request
    # for an aggregate fact.  Remove known governance titles before applying
    # the otherwise useful bare-total heuristic.
    for governance_role in _GOVERNANCE_ROLE_MARKERS:
        aggregation_text = aggregation_text.replace(governance_role, " ")
    if re.search(r"\b(?:tong|toan bo|total)\b", aggregation_text):
        aggregation = "total"
    elif operation == "list":
        aggregation = "list"
    elif entity:
        aggregation = "component"
    else:
        aggregation = ""

    operands = _ratio_operands(
        raw,
        operation=operation,
        period=period,
        period_role=period_role,
        period_labels=period_labels,
    )
    coverage_template, required_legs = _coverage_contract(raw)
    return QuerySlots(
        metric=metric,
        entity=entity,
        period=period,
        period_role=period_role,
        reporting_basis=reporting_basis,
        value_type=value_types,
        aggregation=aggregation,
        scope_label=scope_label,
        operation=operation,
        operands=operands,
        coverage_template=coverage_template,
        required_legs=required_legs,
        counterparty=semantic_dimensions.counterparty,
        transaction_type=semantic_dimensions.transaction_type,
        movement_type=semantic_dimensions.movement_type,
        geography=semantic_dimensions.geography,
        policy_topic=policy_topic,
        period_labels=period_labels,
        section_key=(
            ""
            if operation in {"ratio", "share"}
            else query_section_total_key(raw)
        ),
    )


def route_candidates(query: str, *, agent_name: str = "") -> list[RouteCandidate]:
    """Return a stable, bounded set of deterministic evidence-table candidates."""

    text = _ascii_text(query)
    by_table: dict[str, RouteCandidate] = {}

    def add(table: str, confidence: float, reason: str) -> None:
        candidate = RouteCandidate(table=table, confidence=confidence, reason=reason)
        previous = by_table.get(table)
        if previous is None or confidence > previous.confidence:
            by_table[table] = candidate

    if not text:
        default_table = {
            "agent_profitability": TABLE_IS,
            "agent_liquidity_solvency": TABLE_BS,
            "agent_cashflow_analysis": TABLE_CF,
            "agent_efficiency": TABLE_IS,
        }.get(str(agent_name or "").strip(), TABLE_IS)
        return [RouteCandidate(default_table, 0.40, "analysis_default")]

    has_cashflow_semantics = _contains_any(text, _CASHFLOW_MARKERS)
    has_governance_role = _contains_any(text, _GOVERNANCE_ROLE_MARKERS)
    has_role_financial_context = (
        has_governance_role
        and _contains_any(text, _NOTE_ROLE_FINANCIAL_MARKERS)
    )
    has_role_front_context = (
        has_governance_role
        and _contains_any(text, _FRONT_ROLE_CONTEXT_MARKERS)
    )
    has_headquarters = _contains_any(text, _HEADQUARTERS_MARKERS)
    has_headquarters_note_scope = _contains_any(
        text, _HEADQUARTERS_NOTE_SCOPE_MARKERS
    )
    has_asset_detail = _contains_any(text, _ASSET_DETAIL_MARKERS)
    has_corporate_activity = _contains_any(text, _CORPORATE_ACTIVITY_MARKERS)

    if _contains_any(text, _NOTE_EXCLUSIVE_MARKERS):
        add(TABLE_NOTE, 0.98, "explicit_note_semantics")
    if has_role_financial_context:
        add(TABLE_NOTE, 0.99, "management_compensation_note")
        add(TABLE_REPORT_SECTION, 0.72, "management_identity_may_be_front_matter")
    elif has_role_front_context:
        add(TABLE_REPORT_SECTION, 0.96, "governance_identity_front")
    elif has_governance_role:
        add(TABLE_REPORT_SECTION, 0.78, "ambiguous_governance_front")
        add(TABLE_NOTE, 0.72, "ambiguous_governance_note")

    if has_headquarters:
        if has_headquarters_note_scope:
            add(TABLE_NOTE, 0.88, "subsidiary_location_note")
            add(TABLE_REPORT_SECTION, 0.64, "location_may_be_front_matter")
        elif _contains_any(text, ("o dau", "dia chi", "tru so cong ty", "tru so chinh")):
            add(TABLE_REPORT_SECTION, 0.94, "company_headquarters_front")
        else:
            add(TABLE_REPORT_SECTION, 0.76, "ambiguous_headquarters_front")
            add(TABLE_NOTE, 0.70, "ambiguous_headquarters_note")

    if has_asset_detail:
        if has_corporate_activity and "quyen su dung dat" in text:
            add(TABLE_REPORT_SECTION, 0.88, "land_use_business_activity_front")
            add(TABLE_NOTE, 0.68, "land_use_may_be_note_asset")
        elif not has_cashflow_semantics and (
            _contains_any(text, _ASSET_DETAIL_CONTEXT_MARKERS)
            or _contains_any(text, ("nhan hieu", "thuong hieu", "quyen su dung dat"))
        ):
            add(TABLE_NOTE, 0.96, "note_asset_detail")
        elif not has_cashflow_semantics:
            add(TABLE_NOTE, 0.90, "note_asset_metric")

    named_related_transaction = (
        _contains_any(text, _RELATED_TRANSACTION_MARKERS)
        or (
            "co tuc" in text
            and _contains_any(text, ("chi tra", "tra cho"))
        )
    ) and _ENTITY_TYPE_RE.search(text)
    if named_related_transaction:
        add(TABLE_NOTE, 0.96, "named_related_transaction_note")

    if _contains_any(text, _NOTE_DETAIL_DUAL_MARKERS):
        add(TABLE_NOTE, 0.96, "note_schedule_detail")
        # A statement total may coexist with the detailed note schedule.  Keep
        # it as a bounded secondary candidate rather than forcing note-only.
        if _contains_any(
            text,
            (
                "thue phai nop",
                "chi phi phai tra",
                "vay ngan han",
                "quy khen thuong",
                "co tuc",
            ),
        ):
            add(TABLE_BS, 0.86, "statement_total_may_exist")

    if _contains_any(text, _SEGMENT_NOTE_MARKERS):
        add(TABLE_NOTE, 0.94, "geographic_segment_note")
    if _contains_any(text, _POLICY_DUAL_MARKERS):
        add(TABLE_NOTE, 0.95, "accounting_policy")
        add(TABLE_REPORT_SECTION, 0.68, "policy_may_be_in_compliance_front_matter")
    if has_cashflow_semantics:
        add(TABLE_CF, 0.98, "explicit_cashflow_semantics")
    if _contains_any(text, _REPORT_EXCLUSIVE_MARKERS):
        add(TABLE_REPORT_SECTION, 0.98, "exclusive_front_matter_semantics")
    if _contains_any(text, _REPORT_HISTORY_MARKERS):
        add(TABLE_REPORT_SECTION, 0.88, "company_history_or_listing")
        add(TABLE_NOTE, 0.62, "company_history_may_be_in_note_i")
    if _contains_any(text, _GOING_CONCERN_MARKERS):
        add(TABLE_REPORT_SECTION, 0.78, "going_concern_front_matter")
        add(TABLE_NOTE, 0.78, "going_concern_note")
    if _contains_any(text, _AMBIGUOUS_ORG_MARKERS):
        add(TABLE_NOTE, 0.68, "ambiguous_corporate_structure_note")
        add(TABLE_REPORT_SECTION, 0.64, "ambiguous_corporate_structure_front_matter")

    matches = _keyword_matches(query)
    matches_by_table: dict[str, list[str]] = {}
    for keyword, table in matches:
        matches_by_table.setdefault(table, []).append(keyword)
    main_matched_keywords = {
        _ascii_text(keyword)
        for table, keywords in matches_by_table.items()
        if table in MAIN_REPORT_TABLES
        for keyword in keywords
    }
    for table, keywords in matches_by_table.items():
        specificity = min(max(len(_ascii_text(item)) for item in keywords) / 200.0, 0.05)
        if table in MAIN_REPORT_TABLES:
            add(table, 0.89 + specificity, f"financial_metric:{max(keywords, key=len)}")
            continue
        if table == TABLE_NOTE:
            note_only = [
                keyword
                for keyword in keywords
                if _ascii_text(keyword) not in main_matched_keywords
            ]
            if note_only:
                # A shorter note topic embedded inside an exact main-statement
                # metric must not steal the route.  Example:
                # "lợi nhuận sau thuế thu nhập doanh nghiệp" contains the note
                # topic "thuế thu nhập doanh nghiệp", but its answer leg is the
                # income-statement profit line.
                note_confidence = (
                    0.86
                    if main_matched_keywords
                    else 0.90 + specificity
                )
                add(
                    TABLE_NOTE,
                    note_confidence,
                    f"note_metric:{max(note_only, key=len)}",
                )

    if not by_table:
        default_table = {
            "agent_profitability": TABLE_IS,
            "agent_liquidity_solvency": TABLE_BS,
            "agent_cashflow_analysis": TABLE_CF,
            "agent_efficiency": TABLE_IS,
        }.get(str(agent_name or "").strip(), TABLE_IS)
        add(default_table, 0.40, "analysis_default")

    priority = {
        TABLE_NOTE: 0,
        TABLE_CF: 1,
        TABLE_BS: 2,
        TABLE_IS: 3,
        TABLE_REPORT_SECTION: 4,
    }
    return sorted(
        by_table.values(),
        key=lambda item: (-item.confidence, priority.get(item.table, 99), item.table),
    )


def _fact_text(meta: dict, doc: str = "") -> str:
    return " ".join(
        str(value or "")
        for value in (
            meta.get("company", ""),
            meta.get("item_name", ""),
            meta.get("subheading", ""),
            meta.get("row_label", ""),
            meta.get("column_label", ""),
            meta.get("metric_label", ""),
            meta.get("entity_label", ""),
            meta.get("scope_label", ""),
            meta.get("counterparty", ""),
            meta.get("transaction_type", ""),
            meta.get("movement_type", ""),
            meta.get("geography", ""),
            meta.get("policy_topic", ""),
            meta.get("fiscal_year", ""),
            doc,
        )
    )


def canonical_metric_slot(value: Any) -> str:
    """Canonical comparison form for one accounting metric label.

    Synonyms are applied symmetrically to the query and the indexed fact.  This
    lets a schedule label such as ``Doanh thu thuần`` match the primary-statement
    wording ``Doanh thu thuần về bán hàng và cung cấp dịch vụ`` without making
    the broader ``Tổng doanh thu`` equivalent to net revenue.
    """

    return _ascii_text(normalize_keyword_synonyms(value))


def fact_metric_slot(meta: dict | None, doc: str = "") -> str:
    """Return the narrowest parser-owned metric text for exact slot matching.

    Rendered vector documents contain display labels such as ``Phạm vi bảng`` and
    company names.  Treating those words as part of the metric can fabricate a
    match (for example ``thành`` + ``phẩm`` from unrelated prose).  First-class
    typed metadata therefore wins; the document is only a legacy fallback.
    """

    meta = meta if isinstance(meta, dict) else {}
    candidates = (
        meta.get("metric_label", ""),
        meta.get("row_label", ""),
        str(meta.get("item_name", "") or "").split("|", 1)[0],
        meta.get("subheading", ""),
    )
    generic_aggregate = {"tong", "tong cong", "cong", "total"}
    for index, value in enumerate(candidates):
        text = str(value or "").strip()
        if text and (
            index == 0
            or _ascii_text(text) not in generic_aggregate
        ):
            return text
    return str(doc or "").strip()


def metric_slot_compatibility(
    required: Any,
    actual: Any,
) -> tuple[bool, bool, float]:
    """Return ``(compatible, canonical_exact, required_token_coverage)``."""

    required_norm = canonical_metric_slot(required)
    actual_norm = canonical_metric_slot(actual)
    if not required_norm:
        return True, False, 0.0
    if not actual_norm:
        return False, False, 0.0
    if _semantic_direction_conflict(required_norm, actual_norm):
        return False, False, 0.0
    if required_norm == actual_norm:
        return True, True, 1.0
    required_tokens = _tokens(required_norm)
    actual_tokens = _tokens(actual_norm)
    coverage = (
        len(required_tokens & actual_tokens) / len(required_tokens)
        if required_tokens
        else 0.0
    )
    return coverage >= 0.75, False, coverage


def _semantic_dimension_compatible(
    slots: QuerySlots,
    meta: dict,
    fact_text: str,
) -> tuple[bool, int]:
    """Match explicit query dimensions against canonical payload dimensions."""

    bonus = 0
    derived = derive_semantic_fact_dimensions(
        row_label=meta.get("row_label", "") or fact_text,
        column_label=meta.get("column_label", ""),
        metric_label=meta.get("metric_label", "") or fact_text,
        entity_label=meta.get("entity_label", ""),
        scope_label=meta.get("scope_label", "") or fact_text,
        section_path=meta.get("section_path", ""),
        item_name=meta.get("item_name", "") or fact_text,
        item_code=meta.get("item_code", ""),
        value=fact_text,
    )
    for field in (
        "counterparty",
        "transaction_type",
        "movement_type",
        "geography",
        "policy_topic",
    ):
        required = _ascii_text(getattr(slots, field, ""))
        if not required:
            continue
        actual = _ascii_text(
            meta.get(field, "") or getattr(derived, field, "")
        )
        if not actual:
            return False, bonus
        if field in {"counterparty", "geography"}:
            compatible = (
                required == actual
                or required in actual
                or actual in required
            )
        elif (
            field == "policy_topic"
            and required
            in {
                "depreciation_period",
                "depreciation_method",
                "amortization_period",
                "amortization_method",
            }
            and actual == "non_depreciation"
        ):
            # "Không khấu hao/phân bổ" is a valid terminal answer to a
            # how/how-long policy question.  The reverse is intentionally not
            # compatible: a method fact cannot prove non-depreciation.
            compatible = True
        else:
            compatible = required == actual
        if not compatible:
            return False, bonus
        bonus += 45
    return True, bonus


def _scope_label_compatible(slots: QuerySlots, meta: dict) -> bool:
    """Require an explicitly declared schedule scope to match first-class data."""

    required = _ascii_text(slots.scope_label)
    if not required:
        return True
    actual = _ascii_text(meta.get("scope_label", ""))
    if (
        required == "chinh sach ke toan"
        and slots.policy_topic
    ):
        # ``Chính sách kế toán`` names the broad report area, while canonical
        # policy atoms intentionally retain their specific subsection scope
        # (for example ``c) Nhãn hiệu``).  Keep requiring first-class scope
        # metadata, but do not mistake this broad routing label for an exact
        # schedule identity.
        return bool(actual)
    return bool(
        actual
        and (
            required == actual
            or required in actual
            or actual in required
        )
    )


def _policy_metric_alias_compatible(
    slots: QuerySlots,
    meta: dict,
    fact_text: str,
) -> bool:
    """Let an explicit policy topic bridge equivalent source/query labels.

    Reports commonly label a duration column ``Thời gian hữu dụng`` while a
    question calls it ``thời gian khấu hao``.  A matching typed policy topic is
    a safer bridge than fuzzy overlap and does not weaken ordinary amount
    matching.
    """

    required = _ascii_text(slots.policy_topic)
    if required not in {
        "depreciation_period",
        "depreciation_method",
        "amortization_period",
        "amortization_method",
        "non_depreciation",
    }:
        return False
    actual = _ascii_text(meta.get("policy_topic", ""))
    if not actual:
        derived = derive_semantic_fact_dimensions(
            row_label=meta.get("row_label", "") or fact_text,
            column_label=meta.get("column_label", ""),
            metric_label=meta.get("metric_label", "") or fact_text,
            entity_label=meta.get("entity_label", ""),
            scope_label=meta.get("scope_label", "") or fact_text,
            section_path=meta.get("section_path", ""),
            item_name=meta.get("item_name", "") or fact_text,
            item_code=meta.get("item_code", ""),
            value=fact_text,
        )
        actual = _ascii_text(derived.policy_topic)
    return bool(
        actual
        and (
            actual == required
            or (
                actual == "non_depreciation"
                and required
                in {
                    "depreciation_period",
                    "depreciation_method",
                    "amortization_period",
                    "amortization_method",
                }
            )
        )
    )


def _required_section_key(slots: QuerySlots) -> str:
    """Return the canonical balance-sheet aggregate requested by ``slots``.

    Query parsing may retain the explicit ``tổng`` token in the metric or carry
    it separately as ``aggregation=total``.  Normalize both representations so
    the stable statement section key, rather than a noisy OCR/header label,
    controls exact matching.
    """

    return str(getattr(slots, "section_key", "") or "").strip()


def fact_slot_score(slots: QuerySlots, meta: dict | None, doc: str = "") -> float:
    """Score explicit typed slots; unspecified slots are neutral."""

    meta = meta if isinstance(meta, dict) else {}
    fact_text = _fact_text(meta, doc)
    fact_norm = _ascii_text(fact_text)
    fact_tokens = _tokens(fact_text)
    score = 0.0
    required_section_key = _required_section_key(slots)
    fact_section_key = str(meta.get("section_key", "") or "").strip()
    section_key_match = bool(
        required_section_key and fact_section_key == required_section_key
    )
    if (
        required_section_key
        and fact_section_key
        and not section_key_match
    ):
        return -200.0

    if slots.metric:
        if section_key_match:
            # A canonical statement code/section is stronger than repeated or
            # contaminated column headers (for example code 270 under a
            # "Tài sản dài hạn" header).
            score += 120.0
        else:
            metric_text = fact_metric_slot(meta, fact_text)
            compatible, exact, coverage = metric_slot_compatibility(
                slots.metric,
                metric_text,
            )
            if _semantic_direction_conflict(slots.metric, metric_text):
                return -200.0
            if _policy_metric_alias_compatible(slots, meta, fact_text):
                score += 55.0
            elif exact:
                score += 75.0
            elif compatible:
                score += 55.0
            else:
                score += coverage * 35.0

    if slots.entity:
        entity_norm = _ascii_text(slots.entity)
        if entity_norm and entity_norm in fact_norm:
            score += 65.0
        else:
            score -= 45.0

    dimensions_compatible, dimension_bonus = _semantic_dimension_compatible(
        slots,
        meta,
        fact_text,
    )
    if not dimensions_compatible:
        return -200.0
    score += float(dimension_bonus)
    if slots.scope_label:
        if not _scope_label_compatible(slots, meta):
            return -200.0
        score += 55.0

    fact_period = str(meta.get("period", "") or "").strip() or canonical_period(fact_text)
    if slots.period in {"cuối", "đầu"} and fact_period:
        score += 45.0 if fact_period == slots.period else -40.0
    fact_role = fact_period_role(meta, doc)
    if slots.period_role in {"current", "previous"}:
        score += 55.0 if fact_role == slots.period_role else -80.0
    elif slots.period_role == "both" and fact_role:
        score += 25.0 if fact_role in {"current", "previous"} else -40.0
    if slots.period_labels:
        score += (
            60.0
            if _absolute_period_compatible(slots, meta, doc)
            else -100.0
        )
    if slots.reporting_basis:
        actual_basis = fact_reporting_basis(meta, doc)
        if actual_basis:
            score += (
                70.0
                if reporting_basis_compatible(slots.reporting_basis, actual_basis)
                else -120.0
            )

    fact_value_type = (
        str(meta.get("value_type", "") or "").strip()
        or canonical_value_type(fact_text)
    )
    if len(slots.value_type) == 1 and fact_value_type:
        score += 45.0 if fact_value_type == slots.value_type[0] else -45.0
    elif len(slots.value_type) > 1 and fact_value_type:
        score += 20.0 if fact_value_type in slots.value_type else -25.0

    fact_aggregation = _fact_aggregation(meta, fact_text)
    if slots.aggregation == "total":
        score += 45.0 if fact_aggregation == "total" else -30.0
    elif slots.aggregation == "component" and fact_aggregation == "total":
        score -= 30.0

    return score


def _fact_aggregation(meta: dict, fact_text: str) -> str:
    explicit = _ascii_text(meta.get("aggregation_level", ""))
    if explicit in {"total", "tong"}:
        return "total"
    if str(meta.get("section_key", "") or "").strip():
        return "total"
    item_name = str(meta.get("item_name", "") or fact_text)
    parts = [part.strip() for part in item_name.split("|") if part.strip()]
    row_parts = [_ascii_text(part) for part in parts]
    if any(part in {"tong", "tong cong", "cong", "total"} for part in row_parts):
        return "total"
    if section_total_key(item_name):
        return "total"
    return "component" if parts else ""


def fact_matches_required_slots(
    slots: QuerySlots,
    meta: dict | None,
    doc: str = "",
) -> bool:
    """True only for candidates satisfying every explicitly required slot.

    This predicate is used as an exact-first ordering guard only when at least
    one candidate satisfies it, so legacy facts with sparse metadata retain the
    normal lexical/semantic fallback.
    """

    meta = meta if isinstance(meta, dict) else {}
    fact_text = _fact_text(meta, doc)
    fact_norm = _ascii_text(fact_text)
    fact_tokens = _tokens(fact_text)
    required_section_key = _required_section_key(slots)
    fact_section_key = str(meta.get("section_key", "") or "").strip()
    section_key_match = bool(
        required_section_key and fact_section_key == required_section_key
    )
    if (
        required_section_key
        and fact_section_key
        and not section_key_match
    ):
        return False

    if slots.metric:
        if not section_key_match:
            metric_text = fact_metric_slot(meta, fact_text)
            compatible, _exact, _coverage = metric_slot_compatibility(
                slots.metric,
                metric_text,
            )
            if (
                not _policy_metric_alias_compatible(slots, meta, fact_text)
                and not compatible
            ):
                return False
    if slots.entity and _ascii_text(slots.entity) not in fact_norm:
        return False
    dimensions_compatible, _dimension_bonus = _semantic_dimension_compatible(
        slots,
        meta,
        fact_text,
    )
    if not dimensions_compatible:
        return False
    if not _scope_label_compatible(slots, meta):
        return False

    fact_period = str(meta.get("period", "") or "").strip() or canonical_period(fact_text)
    if slots.period in {"cuối", "đầu"} and fact_period != slots.period:
        return False
    fact_role = fact_period_role(meta, doc)
    if (
        slots.period_role in {"current", "previous"}
        and fact_role != slots.period_role
    ):
        return False
    if slots.period_role == "both" and fact_role not in {"current", "previous"}:
        return False
    if not _absolute_period_compatible(slots, meta, doc):
        return False
    if slots.reporting_basis:
        actual_basis = fact_reporting_basis(meta, doc)
        if actual_basis and not reporting_basis_compatible(
            slots.reporting_basis,
            actual_basis,
        ):
            return False

    fact_value_type = (
        str(meta.get("value_type", "") or "").strip()
        or canonical_value_type(fact_text)
    )
    if len(slots.value_type) == 1 and fact_value_type != slots.value_type[0]:
        return False
    if len(slots.value_type) > 1 and fact_value_type not in slots.value_type:
        return False

    if slots.aggregation == "total" and _fact_aggregation(meta, fact_text) != "total":
        return False
    if slots.aggregation == "component" and _fact_aggregation(meta, fact_text) == "total":
        return False
    return True


def fact_sibling_group_key(
    meta: dict | None,
    *,
    ignore_value_type: bool = False,
) -> tuple[str, ...]:
    """Stable logical-row key for period/value-type sibling pairing.

    It handles both statement rows (``metric | date``) and transposed note rows
    (``period row | entity column``) without widening to every row in a note.
    """

    meta = meta if isinstance(meta, dict) else {}

    def normalize_group_part(value: Any) -> str:
        normalized = _ascii_text(value)
        normalized = _PERIOD_PART_RE.sub(" ", normalized)
        normalized = _UNIT_RE.sub(" ", normalized)
        if ignore_value_type:
            normalized = re.sub(
                r"\bgia tri\s+(?:hao mon(?: luy ke)?|con lai|hop ly)\b"
                r"|\b(?:nguyen gia|gia goc|hao mon(?: luy ke)?|khau hao"
                r"|du phong)\b",
                " ",
                normalized,
            )
            normalized = re.sub(r"\btrong (?:ky|nam)\b", " ", normalized)
        return _SPACE_RE.sub(" ", normalized).strip(" -–—|")

    item_name = str(meta.get("item_name", "") or "")
    parts = []
    for part in item_name.split("|"):
        normalized = _ascii_text(part)
        if not normalized:
            continue
        if canonical_period(part) or _PERIOD_PART_RE.search(normalized):
            normalized = _PERIOD_PART_RE.sub(" ", normalized)
        normalized = normalize_group_part(normalized)
        if normalized:
            parts.append(normalized)

    value_type = "" if ignore_value_type else (
        str(meta.get("value_type", "") or "").strip()
        or canonical_value_type(item_name)
    )
    return (
        _ascii_text(meta.get("company", "")),
        _ascii_text(meta.get("fiscal_year", "")),
        _ascii_text(meta.get("index_generation", "")),
        _ascii_text(meta.get("heading", "")),
        _ascii_text(meta.get("note_ref", "")),
        _ascii_text(meta.get("block_id", "")),
        normalize_group_part(meta.get("subheading", "")),
        normalize_group_part(meta.get("scope_label", "")),
        normalize_group_part(meta.get("metric_label", "")),
        normalize_group_part(meta.get("entity_label", "")),
        normalize_group_part(meta.get("counterparty", "")),
        _ascii_text(meta.get("transaction_type", "")),
        _ascii_text(meta.get("movement_type", "")),
        normalize_group_part(meta.get("geography", "")),
        _ascii_text(meta.get("policy_topic", "")),
        _ascii_text(meta.get("section_key", "")),
        _ascii_text(meta.get("item_code", "")),
        " | ".join(parts),
        _ascii_text(value_type),
    )
