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
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# "cuối"/"đầu" only count as a PERIOD when followed by a period unit (kỳ/năm/quý/
# tháng) — otherwise "đầu" false-matches "đầu tư" (investment), "ban đầu", etc.
_PERIOD_UNIT = r"(?:kỳ|kì|ky|ki|năm|nam|quý|quy|tháng|thang)"
_CUOI_RE = re.compile(r"(?:cuối|cuoi)\s*" + _PERIOD_UNIT)
_DAU_RE = re.compile(r"(?:đầu|dau)\s*" + _PERIOD_UNIT)


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


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
    m = _DATE_RE.search(t)
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


def section_total_key(text) -> str:
    """Canonical key for a balance-sheet section TOTAL, mapping the question wording
    and the indexed label to the same bucket.

    The BS prints section totals with letter/roman/aggregate labels ("A - TÀI SẢN
    NGẮN HẠN", "C - NỢ PHẢI TRẢ", "I. Nợ ngắn hạn", "TỔNG CỘNG TÀI SẢN") while the
    question says "tổng tài sản ngắn hạn"… — without this they never match. Covers
    both asset and liability/equity sides. Order matters: specific sub-sections are
    checked before the broad "tổng tài sản"/"tổng nguồn vốn" buckets.
    """
    t = _norm(text)
    if not t:
        return ""
    if re.search(r"\ba\s*[-–.]\s*tài sản ngắn hạn", t) or "tổng tài sản ngắn hạn" in t:
        return "ts_ngan_han"
    if re.search(r"\bb\s*[-–.]\s*tài sản dài hạn", t) or "tổng tài sản dài hạn" in t:
        return "ts_dai_han"
    if re.search(r"\bi\s*[.]\s*nợ ngắn hạn", t) or "tổng nợ ngắn hạn" in t:
        return "no_ngan_han"
    if re.search(r"\bii\s*[.]\s*nợ dài hạn", t) or "tổng nợ dài hạn" in t:
        return "no_dai_han"
    if re.search(r"\bc\s*[-–.]\s*nợ phải trả", t) or "tổng nợ phải trả" in t:
        return "no_phai_tra"
    if (
        re.search(r"\bd\s*[-–.]\s*nguồn vốn chủ sở hữu", t)
        or "tổng vốn chủ sở hữu" in t
        or "tổng nguồn vốn chủ sở hữu" in t
    ):
        return "von_chu"
    if "tổng cộng nguồn vốn" in t or "tổng nguồn vốn" in t:
        return "nguon_von"
    if "tổng cộng tài sản" in t or "tổng tài sản" in t:
        return "tong_tai_san"
    return ""


def section_total_alias(text) -> str:
    """Readable "Tổng …" name for a balance-sheet section-total LABEL, else "".

    Injected into the fact at ingest so dense + lexical retrieval and the answer
    model recognise "A - TÀI SẢN NGẮN HẠN" / "C - NỢ PHẢI TRẢ" / "I. Nợ ngắn hạn"
    as the "tổng …" the question asks for — instead of a heuristic-only bridge.
    """
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
