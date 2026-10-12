"""Conservative semantic dimensions shared by ingestion and typed retrieval.

The dimensions in this module describe meanings that are orthogonal to the
existing metric/entity/scope axes.  Empty strings are intentional: a parser
must not invent a counterparty, transaction direction, movement, geography, or
accounting-policy topic when the source does not state one clearly.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import re
import unicodedata
from typing import Any


SEMANTIC_FACT_DIMENSION_FIELDS = (
    "counterparty",
    "transaction_type",
    "movement_type",
    "geography",
    "policy_topic",
)


@dataclass(frozen=True)
class SemanticFactDimensions:
    counterparty: str = ""
    transaction_type: str = ""
    movement_type: str = ""
    geography: str = ""
    policy_topic: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


_SPACE_RE = re.compile(r"\s+")
_GENERIC_ENTITY_RE = re.compile(
    r"^(?:"
    r"bên\s+liên\s+quan|đối\s+tượng|đơn\s+vị|công\s+ty|"
    r"khu\s+vực|bộ\s+phận|chỉ\s+tiêu|tổng(?:\s+cộng)?|cộng"
    r")$",
    flags=re.IGNORECASE,
)
_COUNTERPARTY_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"bên\s+liên\s+quan|giao\s+dịch\s+(?:chủ\s+yếu\s+)?với\s+"
    r"(?:các\s+)?bên|related\s+part(?:y|ies)"
    r")\b",
    flags=re.IGNORECASE,
)
_GEOGRAPHIC_SCOPE_RE = re.compile(
    r"\b(?:"
    r"khu\s+vực\s+địa\s+lý|theo\s+(?:khu\s+vực|địa\s+lý)|"
    r"bộ\s+phận\s+theo\s+khu\s+vực|geograph(?:y|ic|ical)|region"
    r")\b",
    flags=re.IGNORECASE,
)
_EXPLICIT_GEOGRAPHY_RE = re.compile(
    r"^(?:"
    r"trong\s+nước|nội\s+địa|nước\s+ngoài|quốc\s+tế|"
    r"miền\s+(?:bắc|trung|nam|đông|tây)|"
    r"(?:khu\s+vực\s+)?(?:châu\s+á|châu\s+âu|châu\s+mỹ|"
    r"đông\s+nam\s+á|asia|europe|americas?)"
    r")$",
    flags=re.IGNORECASE,
)
_NON_GEOGRAPHY_RE = re.compile(
    r"^(?:tổng(?:\s+cộng)?|cộng|không\s+phân\s+bổ|loại\s+trừ)$",
    flags=re.IGNORECASE,
)


_TRANSACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "sales_support",
        re.compile(
            r"\b(?:hỗ\s+trợ|hoàn\s+trả|bù)\s+"
            r"(?:chi\s+phí\s+)?bán\s+hàng\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "other_income",
        re.compile(
            r"\b(?:thu\s+nhập|doanh\s+thu)\s+khác\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "purchase",
        re.compile(
            r"\b(?:"
            r"mua\s+(?:hàng|hàng\s+hóa|dịch\s+vụ|tài\s+sản|"
            r"nguyên\s+vật\s+liệu)|giao\s+dịch\s+mua|"
            r"hàng\s+hóa\s+và\s+dịch\s+vụ\s+mua"
            r")\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "sale",
        re.compile(
            r"\b(?:"
            r"bán\s+(?:hàng|hàng\s+hóa|dịch\s+vụ|tài\s+sản)|"
            r"giao\s+dịch\s+bán|doanh\s+thu\s+(?:bán|với|từ)\b|"
            r"cung\s+cấp\s+(?:hàng|hàng\s+hóa|dịch\s+vụ)\s+cho"
            r")\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "lending",
        re.compile(
            r"\b(?:cho\s+vay|phải\s+thu\s+về\s+cho\s+vay|lãi\s+cho\s+vay)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "borrowing",
        re.compile(
            r"\b(?:đi\s+vay|khoản\s+vay|vay\s+(?:ngắn|dài)\s+hạn|"
            r"nợ\s+vay)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "capital_contribution",
        re.compile(
            r"\b(?:góp\s+vốn|vốn\s+góp|đầu\s+tư\s+góp\s+vốn)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "dividend",
        re.compile(
            r"\b(?:cổ\s+tức|lợi\s+nhuận\s+được\s+chia)\b",
            flags=re.IGNORECASE,
        ),
    ),
)


_MOVEMENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "opening_balance",
        re.compile(
            r"^(?:số\s+dư\s+)?(?:đầu\s+kỳ|đầu\s+năm|tại\s+ngày\s+đầu)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "closing_balance",
        re.compile(
            r"^(?:số\s+dư\s+)?(?:cuối\s+kỳ|cuối\s+năm|tại\s+ngày\s+cuối)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "reclassification",
        re.compile(
            r"\b(?:phân\s+loại\s+lại|tái\s+phân\s+loại|reclassif)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "disposal",
        re.compile(
            r"\b(?:thanh\s+lý|nhượng\s+bán|loại\s+bỏ|xóa\s+sổ)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "depreciation_charge",
        re.compile(
            r"\b(?:khấu\s+hao|hao\s+mòn)\s+(?:trích\s+)?"
            r"(?:trong\s+)?(?:kỳ|năm)\b|\btrích\s+khấu\s+hao\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "amortization_charge",
        re.compile(
            r"\b(?:phân\s+bổ|amorti[sz])\s+(?:trong\s+)?(?:kỳ|năm)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "provision_charge",
        re.compile(
            r"\b(?:trích|lập)\s+(?:thêm\s+)?dự\s+phòng\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "reversal",
        re.compile(
            r"\b(?:hoàn\s+nhập|đảo\s+ngược)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "repayment",
        re.compile(
            r"\b(?:hoàn\s+trả|trả\s+nợ|đã\s+trả)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "utilization",
        re.compile(
            r"\b(?:sử\s+dụng\s+(?:trong\s+)?(?:kỳ|năm)|đã\s+sử\s+dụng)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "transfer",
        re.compile(
            r"\b(?:kết\s+chuyển|chuyển\s+sang|điều\s+chuyển)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "addition",
        re.compile(
            r"^(?:tăng|phát\s+sinh|mua\s+sắm|đầu\s+tư\s+thêm)"
            r"(?:\s+(?:trong\s+)?(?:kỳ|năm))?\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "reduction",
        re.compile(
            r"^(?:giảm)(?:\s+(?:trong\s+)?(?:kỳ|năm))?\b",
            flags=re.IGNORECASE,
        ),
    ),
)


_POLICY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "depreciation_period",
        re.compile(
            r"(?:"
            r"\bthời\s+gian\s+(?:trích\s+)?khấu\s+hao\b|"
            r"\b(?:được\s+)?khấu\s+hao\s+(?:trong\s+)?bao\s+lâu\b|"
            r"\bkhấu\s+hao\b.{0,240}\bthời\s+gian\s+"
            r"(?:sử\s+dụng\s+)?hữu\s+(?:dụng|ích)\b|"
            r"\bthời\s+gian\s+(?:sử\s+dụng\s+)?hữu\s+(?:dụng|ích)\b"
            r".{0,240}\bkhấu\s+hao\b"
            r")",
            re.IGNORECASE,
        ),
    ),
    (
        "amortization_period",
        re.compile(
            r"\bthời\s+gian\s+phân\s+bổ\b|"
            r"\b(?:được\s+)?phân\s+bổ\s+(?:trong\s+)?bao\s+lâu\b",
            re.IGNORECASE,
        ),
    ),
    (
        "depreciation_method",
        re.compile(
            r"\bphương\s+pháp\s+(?:khấu\s+hao|hao\s+mòn)\b|"
            r"\b(?:khấu\s+hao|hao\s+mòn)\s+như\s+thế\s+nào\b",
            re.IGNORECASE,
        ),
    ),
    (
        "amortization_method",
        re.compile(
            r"\bphương\s+pháp\s+phân\s+bổ\b|"
            r"\bphân\s+bổ\s+như\s+thế\s+nào\b",
            re.IGNORECASE,
        ),
    ),
    (
        "recognition_criteria",
        re.compile(r"\bđiều\s+kiện\s+ghi\s+nhận\b", re.IGNORECASE),
    ),
    (
        "capitalization_criteria",
        re.compile(r"\bđiều\s+kiện\s+vốn\s+h[oó]a\b", re.IGNORECASE),
    ),
    (
        "measurement_basis",
        re.compile(r"\bcơ\s+sở\s+đo\s+lường\b", re.IGNORECASE),
    ),
    (
        "non_depreciation",
        re.compile(
            r"\bchính\s+sách\s+không\s+khấu\s+hao\b|"
            r"\bcó\s+(?:được\s+)?(?:khấu\s+hao|phân\s+bổ)\s+không\b",
            re.IGNORECASE,
        ),
    ),
    (
        "revenue_recognition",
        re.compile(r"\bghi\s+nhận\s+doanh\s+thu\b", re.IGNORECASE),
    ),
    (
        "inventory_policy",
        re.compile(
            r"\b(?:chính\s+sách|phương\s+pháp).{0,80}\bhàng\s+tồn\s+kho\b",
            re.IGNORECASE,
        ),
    ),
)
_EXPLICIT_POLICY_SCOPE_RE = re.compile(
    r"(?:"
    r"\b(?:thời\s+gian|phương\s+pháp)\s+(?:trích\s+)?"
    r"(?:khấu\s+hao|phân\s+bổ)\b|"
    r"\bkhấu\s+hao\b.{0,240}\bthời\s+gian\s+"
    r"(?:sử\s+dụng\s+)?hữu\s+(?:dụng|ích)\b|"
    r"\bthời\s+gian\s+(?:sử\s+dụng\s+)?hữu\s+(?:dụng|ích)\b"
    r".{0,240}\bkhấu\s+hao\b|"
    r"\b(?:điều\s+kiện\s+(?:ghi\s+nhận|vốn\s+h[oó]a)|"
    r"chính\s+sách\s+không\s+khấu\s+hao)\b"
    r")",
    flags=re.IGNORECASE,
)


def _clean(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip(" \t\r\n|:;-–—")


def _ascii(value: Any) -> str:
    text = _clean(value).replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    return "".join(char for char in text if not unicodedata.combining(char)).casefold()


def _unique_match(
    text: str,
    patterns: tuple[tuple[str, re.Pattern[str]], ...],
) -> str:
    matches = [name for name, pattern in patterns if pattern.search(text)]
    return matches[0] if len(set(matches)) == 1 else ""


def _transaction_type(text: str) -> str:
    matches = {
        name
        for name, pattern in _TRANSACTION_PATTERNS
        if pattern.search(text)
    }
    # "Cho vay ngắn hạn" necessarily contains the bare borrowing phrase
    # "vay ngắn hạn"; the explicit "cho vay" direction is authoritative.
    if "lending" in matches:
        matches.discard("borrowing")
    # "Hỗ trợ bán hàng" is a distinct related-party transaction category,
    # not a sale merely because it contains the words "bán hàng".
    if "sales_support" in matches:
        matches.discard("sale")
    # In an income statement, "chi phí bán hàng" names an expense function;
    # it is not evidence of a sale transaction direction.
    if re.search(r"\bchi\s+phí\s+bán\s+hàng\b", text, flags=re.IGNORECASE):
        matches.discard("sale")
    return next(iter(matches)) if len(matches) == 1 else ""


def _counterparty(entity: str, context: str) -> str:
    cleaned = _clean(entity)
    if (
        not cleaned
        or _GENERIC_ENTITY_RE.fullmatch(cleaned)
        or not _COUNTERPARTY_CONTEXT_RE.search(context)
    ):
        return ""
    return cleaned


def _geography(entity: str, context: str) -> str:
    cleaned = _clean(entity)
    if not cleaned or _NON_GEOGRAPHY_RE.fullmatch(cleaned):
        return ""
    if _EXPLICIT_GEOGRAPHY_RE.fullmatch(cleaned) or _ascii(cleaned) in {
        "trong nuoc",
        "noi dia",
        "nuoc ngoai",
        "quoc te",
        "mien bac",
        "mien trung",
        "mien nam",
        "mien dong",
        "mien tay",
        "chau a",
        "chau au",
        "chau my",
        "dong nam a",
        "asia",
        "europe",
        "america",
        "americas",
    }:
        return cleaned
    if _GEOGRAPHIC_SCOPE_RE.search(context) and not _GENERIC_ENTITY_RE.fullmatch(cleaned):
        # Inside an explicitly geographic segment schedule, the entity axis may
        # be a country/province name outside any finite vocabulary.
        return cleaned
    return ""


def derive_semantic_fact_dimensions(
    *,
    row_label: Any = "",
    column_label: Any = "",
    metric_label: Any = "",
    entity_label: Any = "",
    scope_label: Any = "",
    section_path: Any = "",
    item_name: Any = "",
    item_code: Any = "",
    value: Any = "",
) -> SemanticFactDimensions:
    """Derive semantic dimensions only from explicit source labels/context."""

    entity = _clean(entity_label)
    metric_context = " | ".join(
        part
        for part in (
            _clean(metric_label),
            _clean(row_label),
            _clean(column_label),
        )
        if part
    )
    full_context = " | ".join(
        part
        for part in (
            metric_context,
            _clean(scope_label),
            _clean(section_path),
            _clean(item_name),
            _clean(value),
        )
        if part
    )

    transaction_type = _transaction_type(metric_context)
    movement_type = _unique_match(metric_context, _MOVEMENT_PATTERNS)
    geography = _geography(entity, full_context)
    counterparty = _counterparty(entity, full_context)
    if geography:
        # A geographic segment is not a transaction counterparty even if a
        # broad surrounding note happens to mention related parties.
        counterparty = ""

    policy_context = " | ".join(
        part
        for part in (
            _clean(metric_label),
            _clean(row_label),
            _clean(column_label),
            _clean(scope_label),
            _clean(section_path),
            _clean(item_name),
        )
        if part
    )
    item_code_norm = _ascii(item_code)
    has_policy_scope = bool(
        "policy" in item_code_norm
        or re.search(
            r"\b(?:chính\s+sách\s+kế\s+toán|các\s+chính\s+sách\s+"
            r"kế\s+toán|accounting\s+polic)",
            policy_context,
            flags=re.IGNORECASE,
        )
        or _EXPLICIT_POLICY_SCOPE_RE.search(policy_context)
    )
    policy_topic = (
        _unique_match(policy_context, _POLICY_PATTERNS)
        if has_policy_scope
        else ""
    )

    return SemanticFactDimensions(
        counterparty=counterparty,
        transaction_type=transaction_type,
        movement_type=movement_type,
        geography=geography,
        policy_topic=policy_topic,
    )


def derive_query_semantic_dimensions(
    query: Any,
    *,
    entity: Any = "",
) -> SemanticFactDimensions:
    """Extract only semantic dimensions explicitly named by a query."""

    text = _clean(query)
    return derive_semantic_fact_dimensions(
        row_label=text,
        metric_label=text,
        entity_label=entity,
        scope_label=text,
        section_path=text,
        item_name=text,
        item_code="policy_query",
        value=text,
    )
