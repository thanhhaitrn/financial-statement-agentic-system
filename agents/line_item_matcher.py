"""Shared helpers for recognizing direct financial statement line-item queries."""
# Code note: Keep this deterministic so planner/router can avoid costly LLM routing for simple lookups.

import re
import unicodedata
from typing import Any, Iterable, Optional

from config.allowed_keywords import ALLOWED_KEYWORDS

DIRECT_LINE_ITEM_EVALUATIVE_PATTERNS = [
    r"\bđánh giá\b",
    r"\bdanh gia\b",
    r"\bnhận xét\b",
    r"\bnhan xet\b",
    r"\bgiải thích\b",
    r"\bgiai thich\b",
    r"\bxu hướng\b",
    r"\bxu huong\b",
    r"\bchất lượng\b",
    r"\bchat luong\b",
    r"\bbền vững\b",
    r"\bben vung\b",
    r"\brủi ro\b",
    r"\brui ro\b",
    r"\btốt không\b",
    r"\btot khong\b",
    r"\bmạnh không\b",
    r"\bmanh khong\b",
    r"\byếu không\b",
    r"\byeu khong\b",
    r"\bassess\b",
    r"\bevaluate\b",
    r"\bexplain\b",
    r"\btrend\b",
    r"\bquality\b",
    r"\bsustainable\b",
    r"\brisk\b",
    r"\bgood profit\b",
    r"\bgenerate(?:s|d)? good profit\b",
]
DIRECT_LINE_ITEM_CALCULATION_PATTERNS = [
    r"\btính\b",
    r"\btinh\b",
    r"\btỷ lệ\b",
    r"\bty le\b",
    r"\btỉ lệ\b",
    r"\bti le\b",
    r"\btỷ trọng\b",
    r"\bty trong\b",
    r"\bhệ số\b",
    r"\bhe so\b",
    r"\bvòng quay\b",
    r"\bvong quay\b",
    r"\bbiên\b",
    r"\bbien\b",
    r"\broa\b",
    r"\broe\b",
    r"\bmargin\b",
    r"\bso sánh\b",
    r"\bso sanh\b",
    r"\bchênh lệch\b",
    r"\bchenh lech\b",
    r"\btăng\b",
    r"\btang\b",
    r"\bgiảm\b",
    r"\bgiam\b",
    r"%",
]
DIRECT_LINE_ITEM_FILLER_TOKENS = {
    "bao",
    "bang",
    "biet",
    "cao",
    "can",
    "cho",
    "cong",
    "cua",
    "cuoi",
    "doanh",
    "doi",
    "dong",
    "du",
    "gia",
    "ke",
    "khoan",
    "ky",
    "la",
    "lay",
    "lieu",
    "muc",
    "nam",
    "nao",
    "ngay",
    "nghiep",
    "nhieu",
    "o",
    "q1",
    "q2",
    "q3",
    "q4",
    "quy",
    "so",
    "tai",
    "tap",
    "thong",
    "tim",
    "tin",
    "toan",
    "toi",
    "tren",
    "tri",
    "trich",
    "trong",
    "ty",
    "xem",
    "xuat",
}


def fold_diacritics(value: Any) -> str:
    text = str(value or "").replace("đ", "d").replace("Đ", "D")
    normalized = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")


def normalize_line_item_text(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    text = re.sub(r"[\"'“”‘’?!.,:;()\[\]{}]", " ", text)
    return " ".join(text.split())


def contains_intent(text: str, patterns: Iterable[str]) -> bool:
    normalized = normalize_line_item_text(text)
    folded = fold_diacritics(normalized)
    if not normalized:
        return False
    return any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        or re.search(pattern, folded, flags=re.IGNORECASE)
        for pattern in patterns
    )


def line_item_candidates(selected_tables: Iterable[str] | None = None) -> list[dict]:
    tables = list(selected_tables) if selected_tables is not None else list(ALLOWED_KEYWORDS.keys())
    canonical_tables = {}
    for table in tables:
        for keyword in ALLOWED_KEYWORDS.get(table, set()) or set():
            canonical_tables[normalize_line_item_text(keyword)] = table

    candidates = []
    seen = set()

    def add_candidate(match_text: str, canonical_text: str, table: str) -> None:
        match_norm = normalize_line_item_text(match_text)
        canonical_norm = normalize_line_item_text(canonical_text)
        if not match_norm or not canonical_norm:
            return
        key = (match_norm, canonical_norm, table)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            {
                "match_text": match_norm,
                "match_folded": fold_diacritics(match_norm),
                "canonical": canonical_norm,
                "table": table,
            }
        )

    for table in tables:
        for keyword in ALLOWED_KEYWORDS.get(table, set()) or set():
            add_candidate(keyword, keyword, table)

    candidates.sort(key=lambda item: len(item["match_text"]), reverse=True)
    return candidates


def _is_direct_line_item_remainder(value: str) -> bool:
    tokens = re.findall(r"\w+", fold_diacritics(value).lower())
    meaningful_tokens = [
        token
        for token in tokens
        if token and not re.fullmatch(r"(19|20)\d{2}|\d+", token)
    ]
    if all(token in DIRECT_LINE_ITEM_FILLER_TOKENS for token in meaningful_tokens):
        return True
    if "cua" in meaningful_tokens:
        company_marker_index = meaningful_tokens.index("cua")
        return all(
            token in DIRECT_LINE_ITEM_FILLER_TOKENS
            for token in meaningful_tokens[:company_marker_index]
        )
    return False


def direct_line_item_match(
    user_query: str,
    *,
    selected_tables: Iterable[str] | None = None,
    evaluative_patterns: Iterable[str] = DIRECT_LINE_ITEM_EVALUATIVE_PATTERNS,
    calculation_patterns: Iterable[str] = DIRECT_LINE_ITEM_CALCULATION_PATTERNS,
) -> Optional[dict]:
    query_norm = normalize_line_item_text(user_query)
    if not query_norm:
        return None
    if contains_intent(query_norm, evaluative_patterns):
        return None
    if contains_intent(query_norm, calculation_patterns):
        return None

    query_folded = fold_diacritics(query_norm)
    for candidate in line_item_candidates(selected_tables=selected_tables):
        match_text = candidate["match_text"]
        match_folded = candidate["match_folded"]
        if query_norm == match_text or query_folded == match_folded:
            return candidate

        if match_text in query_norm:
            remainder = query_norm.replace(match_text, " ", 1)
            if _is_direct_line_item_remainder(remainder):
                return candidate
        elif match_folded in query_folded:
            remainder = query_folded.replace(match_folded, " ", 1)
            if _is_direct_line_item_remainder(remainder):
                return candidate

    return None
