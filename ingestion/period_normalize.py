"""Canonicalize period / value-type / unit so value-lookup rows are distinguishable.

Financial statements bury the two disambiguators of a value-lookup question inside
strings:
- **Period** lives in the column header as a *date* ("31/12/2024" = closing,
  "01/01/2024" = opening) or a *year* ("Năm 2024" vs "Năm 2023"), while the query
  uses *relative* terms ("cuối kỳ", "đầu năm"). These must be mapped to a common
  canonical form ("cuối"/"đầu") so query intent can match a column.
- **Value type** in matrix note tables ("Nguyên giá" vs "Giá trị còn lại" vs
  "Hao mòn lũy kế") — near-identical wording across rows of the same line item.
- **Unit** is a header suffix ("...VND") or caption ("Đơn vị: nghìn đồng").

Used both at ingest (to set first-class metadata fields) and at query time (in the
heuristic reranker) so matching is slot-vs-slot, not fuzzy token overlap.
"""
from __future__ import annotations

import re

_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_TEXTUAL_DATE_RE = re.compile(
    r"\b(\d{1,2})\s*(?:tháng|thang)\s*(\d{1,2})"
    r"\s*(?:năm|nam)\s*(\d{4})\b"
)
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# "cuối"/"đầu" only count as a PERIOD when followed by a period unit (kỳ/năm/quý/
# tháng) — otherwise "đầu" false-matches "đầu tư" (investment), "ban đầu", etc.
_PERIOD_UNIT = r"(?:kỳ|kì|ky|ki|năm|nam|quý|quy|tháng|thang)"
_CUOI_RE = re.compile(r"(?:cuối|cuoi)\s*" + _PERIOD_UNIT)
_DAU_RE = re.compile(r"(?:đầu|dau)\s*" + _PERIOD_UNIT)

# Primary-statement section totals use stable Vietnamese accounting codes across
# issuers, even when OCR or page-continuation repair loses the visible row label.
# The code map is deliberately applied only inside balance-sheet scope; the same
# number may mean something unrelated in a note or another primary statement.
_BALANCE_SHEET_SCOPE_MARKERS = (
    "bảng cân đối kế toán",
    "bang can doi ke toan",
    "báo cáo tình hình tài chính",
    "bao cao tinh hinh tai chinh",
    "bcđkt",
    "bcdkt",
)
_BALANCE_SHEET_SECTION_BY_CODE = {
    "100": ("ts_ngan_han", "Tổng tài sản ngắn hạn"),
    "200": ("ts_dai_han", "Tổng tài sản dài hạn"),
    "270": ("tong_tai_san", "Tổng tài sản"),
    "300": ("no_phai_tra", "Tổng nợ phải trả"),
    "310": ("no_ngan_han", "Tổng nợ ngắn hạn"),
    "330": ("no_dai_han", "Tổng nợ dài hạn"),
    # Code 400 is the statement section total. Code 410 is intentionally not
    # included: many filings print both with the same value, and preferring 400
    # prevents two indistinguishable aggregate operands.
    "400": ("von_chu", "Tổng vốn chủ sở hữu"),
    "440": ("nguon_von", "Tổng nguồn vốn"),
}
_BALANCE_SHEET_FORMULA_CODE_RE = re.compile(
    r"(?:^|[\s(])(?P<code>100|200|270|300|310|330|400|440)"
    r"\s*=\s*\d{2,3}\b"
)
_BALANCE_SHEET_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})[/-](?P<month>\d{1,2})[/-](?P<year>\d{4})(?=$|\D)"
)
_QUERY_SECTION_PHRASES = (
    ("tong cong nguon von chu so huu", "von_chu"),
    ("tong nguon von chu so huu", "von_chu"),
    ("tong cong tai san ngan han", "ts_ngan_han"),
    ("tong cong tai san dai han", "ts_dai_han"),
    ("tong cong no phai tra", "no_phai_tra"),
    ("tong cong no ngan han", "no_ngan_han"),
    ("tong cong no dai han", "no_dai_han"),
    ("tong cong von chu so huu", "von_chu"),
    ("tong tai san ngan han", "ts_ngan_han"),
    ("tong tai san dai han", "ts_dai_han"),
    ("tong no phai tra", "no_phai_tra"),
    ("tong no ngan han", "no_ngan_han"),
    ("tong no dai han", "no_dai_han"),
    ("tong von chu so huu", "von_chu"),
    ("tong cong nguon von", "nguon_von"),
    ("tong nguon von", "nguon_von"),
    ("tong cong tai san", "tong_tai_san"),
    ("tong tai san", "tong_tai_san"),
)
_QUERY_SECTION_BOUNDARY_TOKENS = {
    "bao",
    "bang",
    "cho",
    "cuoi",
    "cua",
    "dau",
    "den",
    "duoc",
    "giam",
    "hien",
    "ky",
    "la",
    "ma",
    "nam",
    "ngay",
    "o",
    "quy",
    "so",
    "tai",
    "tang",
    "thang",
    "thay",
    "theo",
    "thoi",
    "trong",
    "truoc",
    "tu",
    "vao",
}


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _ascii_norm(text) -> str:
    value = str(text or "").replace("đ", "d").replace("Đ", "D")
    import unicodedata

    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", value).strip().lower()


def query_section_total_key(text) -> str:
    """Strict canonical section key for a user/runtime query.

    Ingestion deliberately accepts labels containing a canonical section phrase
    plus OCR noise.  Query matching must be stricter: ``tổng tài sản cố định``
    and ``tổng tài sản thuế hoãn lại`` are schedule/line-item totals, not code
    270.  A section phrase is therefore accepted only when the following token
    is a question/period/company modifier (or the phrase ends there).  Queries
    naming more than one section remain unbound for operand decomposition.
    """

    normalized = _ascii_norm(text)
    if not normalized:
        return ""
    matched_keys = set()
    for phrase, key in _QUERY_SECTION_PHRASES:
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", normalized):
            remainder = normalized[match.end():].lstrip(" \t\r\n-–—:;,?.()[]{}")
            if remainder:
                next_token = re.match(r"[a-z0-9]+", remainder)
                if next_token:
                    token = next_token.group(0)
                    allowed_co_question = bool(
                        token == "co"
                        and re.match(
                            r"co\s+(?:bao|gia|phai|thay)\b",
                            remainder,
                        )
                    )
                    if (
                        token not in _QUERY_SECTION_BOUNDARY_TOKENS
                        and not token.isdigit()
                        and not allowed_co_question
                    ):
                        continue
            matched_keys.add(key)
    return next(iter(matched_keys)) if len(matched_keys) == 1 else ""


def _is_balance_sheet_scope(table_scope) -> bool:
    scope = _norm(table_scope)
    return bool(
        scope
        and (
            scope == "tài sản"
            or scope == "nguồn vốn"
            or any(marker in scope for marker in _BALANCE_SHEET_SCOPE_MARKERS)
        )
    )


def _balance_sheet_item_code(item_code, text="") -> str:
    explicit = re.sub(r"\D", "", str(item_code or ""))
    if explicit in _BALANCE_SHEET_SECTION_BY_CODE:
        return explicit
    formula = _BALANCE_SHEET_FORMULA_CODE_RE.search(_norm(text))
    return formula.group("code") if formula else ""


def canonical_period(text) -> str:
    """Map a column header OR a query phrase to ``"cuối"`` / ``"đầu"`` / ``""``.

    A bare "đầu"/"cuối" does not count (avoids "đầu tư" → "đầu"); a period unit
    must follow. Comparison phrasing that names BOTH periods ("cuối kỳ với đầu
    năm") returns ``""`` so the reranker keeps both instead of penalizing one.
    Year-only columns ("Năm 2024") are ambiguous — resolve via ``column_period``.
    """
    t = _norm(text)
    if not t:
        return ""
    has_cuoi = bool(_CUOI_RE.search(t))
    has_dau = bool(_DAU_RE.search(t))
    if has_cuoi and has_dau:
        return ""  # comparison / both periods needed
    if has_cuoi:
        return "cuối"
    if has_dau:
        return "đầu"
    # Closing/opening balance dates (typically a balance-sheet column).
    m = _DATE_RE.search(t) or _TEXTUAL_DATE_RE.search(t)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        if day == 1 and month == 1:
            return "đầu"
        if month == 12 and day >= 28:
            return "cuối"
    return ""


# Change/comparison phrasings: the question is ABOUT movement between periods,
# so a row from the not-named period is evidence, not noise — the reranker keeps
# its same-period bonus but must not penalize the opposite-period sibling.
# Diacritics kept: callers lowercase but do not strip Vietnamese accents.
_COMPARISON_MARKERS = (
    "so sánh", "so với", "thay đổi", "biến động", "chênh lệch",
    "tăng", "giảm", "không còn",
)


def is_comparison_query(text) -> bool:
    """True when the phrase asks about change between periods (so sánh / biến
    động / tăng / giảm / "không còn số dư"...)."""
    t = _norm(text)
    return any(marker in t for marker in _COMPARISON_MARKERS)


def column_period(col_name, value_cols) -> str:
    """Canonical period of a value column, using sibling columns to break year ties.

    Falls back to year ranking ("Năm 2024" vs "Năm 2023" → later year = "cuối")
    when the column carries no closing/opening date.
    """
    direct = canonical_period(col_name)
    if direct:
        return direct
    years = {}
    for col in value_cols:
        m = _YEAR_RE.search(_norm(col))
        if m:
            years[col] = int(m.group(0))
    if col_name in years and len(set(years.values())) > 1:
        return "cuối" if years[col_name] == max(years.values()) else "đầu"
    return ""


def canonical_value_type(text) -> str:
    """Group accounting value types across statement + note tables.

    Buckets: nguyên giá (cost — TSCĐ "nguyên giá" ≡ investment "giá gốc"),
    dự phòng (provision), giá trị hợp lý (fair value), hao mòn (accumulated
    depreciation), giá trị còn lại (net book value). Order matters — check the
    specific multi-word types before the generic "giá trị". Note headers put the
    value type after <br/>, now preserved by _display_column_name.
    """
    t = _norm(text)
    if not t:
        return ""
    if "dự phòng" in t or "du phong" in t:
        return "dự phòng"
    if "hợp lý" in t or "hop ly" in t:
        return "giá trị hợp lý"
    if "hao mòn" in t or "hao mon" in t or "khấu hao" in t or "khau hao" in t:
        return "hao mòn"
    if "còn lại" in t or "con lai" in t:
        return "giá trị còn lại"
    # Cost basis: TSCĐ "nguyên giá" and investment "giá gốc" are the same slot,
    # so a question asking "nguyên giá [công ty con]" matches the "giá gốc" column.
    if "nguyên giá" in t or "nguyen gia" in t or "giá gốc" in t or "gia goc" in t:
        return "nguyên giá"
    return ""


def query_value_types(text) -> set:
    """All value types named in a query — to detect multi-type comparison questions.

    "thay đổi nguyên giá VÀ hao mòn" names two types; the reranker must not boost
    one and penalize the other (both are needed).
    """
    t = _norm(text)
    out = set()
    if "nguyên giá" in t or "nguyen gia" in t or "giá gốc" in t or "gia goc" in t:
        out.add("nguyên giá")
    if "còn lại" in t or "con lai" in t:
        out.add("giá trị còn lại")
    if "hao mòn" in t or "hao mon" in t or "khấu hao" in t or "khau hao" in t:
        out.add("hao mòn")
    if "dự phòng" in t or "du phong" in t:
        out.add("dự phòng")
    if "hợp lý" in t or "hop ly" in t:
        out.add("giá trị hợp lý")
    return out


def section_total_key(
    text,
    *,
    table_scope="",
    item_code="",
    equity_410_fallback=False,
) -> str:
    """Canonical key for a balance-sheet section TOTAL, mapping the question wording
    and the indexed label to the same bucket.

    The BS prints section totals with letter/roman/aggregate labels ("A - TÀI SẢN
    NGẮN HẠN", "C - NỢ PHẢI TRẢ", "I. Nợ ngắn hạn", "TỔNG CỘNG TÀI SẢN") while the
    question says "tổng tài sản ngắn hạn"… — without this they never match. Covers
    both asset and liability/equity sides. Order matters: specific sub-sections are
    checked before the broad "tổng tài sản"/"tổng nguồn vốn" buckets.

    During ingestion, ``table_scope`` and ``item_code`` make the mapping resilient
    to missing/OCR-contaminated labels. Code-based inference is never used outside
    balance-sheet scope. Query-time callers may continue passing only ``text``.
    """
    t = _norm(text)
    scoped_balance_sheet = _is_balance_sheet_scope(table_scope)
    if table_scope and not scoped_balance_sheet:
        # Textual aliases below intentionally support query-time calls with no
        # table scope. Once ingestion supplies an explicit non-BS scope, a note
        # row such as "Tổng tài sản thuế thu nhập hoãn lại" must not collapse to
        # the primary-statement total-assets section.
        return ""
    if scoped_balance_sheet:
        explicit_code = re.sub(r"\D", "", str(item_code or ""))
        if equity_410_fallback and explicit_code == "410":
            return "von_chu"
        code = _balance_sheet_item_code(item_code, t)
        if code:
            return _BALANCE_SHEET_SECTION_BY_CODE[code][0]
    if not t:
        return ""
    label_end = r"(?=\s*(?:\||\(|$))"
    if re.search(
        rf"(?:^|\|)\s*a\s*[-–.]\s*tài sản ngắn hạn{label_end}",
        t,
    ):
        return "ts_ngan_han"
    if re.search(
        rf"(?:^|\|)\s*b\s*[-–.]\s*tài sản dài hạn{label_end}",
        t,
    ):
        return "ts_dai_han"
    if re.search(
        rf"(?:^|\|)\s*i\s*[.]\s*nợ ngắn hạn{label_end}",
        t,
    ):
        return "no_ngan_han"
    if re.search(
        rf"(?:^|\|)\s*ii\s*[.]\s*nợ dài hạn{label_end}",
        t,
    ):
        return "no_dai_han"
    if re.search(
        rf"(?:^|\|)\s*c\s*[-–.]\s*nợ phải trả{label_end}",
        t,
    ):
        return "no_phai_tra"
    if re.search(
        rf"(?:^|\|)\s*d\s*[-–.]\s*(?:nguồn\s+)?vốn chủ sở hữu{label_end}",
        t,
    ):
        return "von_chu"
    return query_section_total_key(t)


def section_total_alias(
    text,
    *,
    table_scope="",
    item_code="",
    equity_410_fallback=False,
) -> str:
    """Readable "Tổng …" name for a balance-sheet section-total LABEL, else "".

    Injected into the fact at ingest so dense + lexical retrieval and the answer
    model recognise "A - TÀI SẢN NGẮN HẠN" / "C - NỢ PHẢI TRẢ" / "I. Nợ ngắn hạn"
    as the "tổng …" the question asks for — instead of a heuristic-only bridge.
    """
    if table_scope and not _is_balance_sheet_scope(table_scope):
        return ""
    key = section_total_key(
        text,
        table_scope=table_scope,
        item_code=item_code,
        equity_410_fallback=equity_410_fallback,
    )
    aliases = {
        spec_key: alias
        for spec_key, alias in _BALANCE_SHEET_SECTION_BY_CODE.values()
    }
    if key in aliases:
        return aliases[key]

    label = _norm(str(text or "").split("|")[0])
    if not label:
        return ""
    if re.match(r"a\s*[-–.]", label) and "tài sản ngắn hạn" in label:
        return "Tổng tài sản ngắn hạn"
    if re.match(r"b\s*[-–.]", label) and "tài sản dài hạn" in label:
        return "Tổng tài sản dài hạn"
    if re.match(r"c\s*[-–.]", label) and "nợ phải trả" in label:
        return "Tổng nợ phải trả"
    if re.match(r"d\s*[-–.]", label) and ("vốn chủ sở hữu" in label or "nguồn vốn" in label):
        return "Tổng vốn chủ sở hữu"
    if re.match(r"i\s*[.]", label) and "nợ ngắn hạn" in label:
        return "Tổng nợ ngắn hạn"
    if re.match(r"ii\s*[.]", label) and "nợ dài hạn" in label:
        return "Tổng nợ dài hạn"
    if "tổng cộng tài sản" in label:
        return "Tổng tài sản"
    if "tổng cộng nguồn vốn" in label:
        return "Tổng nguồn vốn"
    return ""


def canonical_balance_sheet_column_label(text, *, table_scope="") -> str:
    """Remove repeated row/header prose from a balance-sheet period column.

    PDF-to-markdown conversion sometimes yields headers such as
    ``31/12/2025 VND<br/>Tài sản dài hạn``. The trailing section title is not a
    semantic column dimension and can make a total-assets fact look like a
    long-term-assets fact. A dated balance-sheet column is canonically just its
    date plus unit; non-balance-sheet and non-dated headers remain untouched.
    """

    raw = re.sub(r"<br\s*/?>", " ", str(text or ""), flags=re.IGNORECASE)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw or not _is_balance_sheet_scope(table_scope):
        return raw
    match = _BALANCE_SHEET_DATE_RE.search(raw)
    if not match:
        return raw
    date_label = (
        f"{match.group('day')}/{match.group('month')}/{match.group('year')}"
    )
    unit = parse_unit(raw)
    return f"{date_label} {unit}".strip()


def period_phrase_alias(text) -> str:
    """For a periodic report, a line item saying "trong năm" is the same as "trong
    kỳ" (e.g. "Lưu chuyển tiền thuần trong năm" ≡ "... trong kỳ"). Returns the
    "trong kỳ" variant of such a label so both phrasings are searchable / visible
    to the answer model; "" when there is nothing to alias.
    """
    t = str(text or "")
    if re.search(r"trong năm", t, flags=re.IGNORECASE) and "trong kỳ" not in t.lower():
        return re.sub(r"trong năm", "trong kỳ", t, flags=re.IGNORECASE).strip()
    return ""


def parse_unit(text) -> str:
    """Extract the monetary unit from a column header / caption ("...VND")."""
    t = _norm(text)
    if not t:
        return ""
    if "triệu đồng" in t or "trieu dong" in t:
        return "triệu đồng"
    if "nghìn đồng" in t or "nghin dong" in t or "ngàn đồng" in t or "ngan dong" in t:
        return "nghìn đồng"
    if "vnd" in t or "đồng" in t or "dong" in t:
        return "VND"
    if "cổ phiếu" in t or "co phieu" in t:
        return "cổ phiếu"
    if "%" in t or "phần trăm" in t or "phan tram" in t:
        return "percent"
    return ""
