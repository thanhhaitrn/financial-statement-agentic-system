"""Shared helpers for normalizing retrieval requirements and fact status."""
# Code note: Schema modules normalize model/tool payloads; comments here clarify validation side effects.

from __future__ import annotations

import re
from typing import Any, Iterable

from config.allowed_keywords import ALIASES, ALLOWED_KEYWORDS


FACT_STATUS_FOUND = "found"
FACT_STATUS_NOT_FOUND = "not_found_after_search"
FACT_STATUS_AMBIGUOUS = "ambiguous"
VALID_FACT_STATUSES = {
    FACT_STATUS_FOUND,
    FACT_STATUS_NOT_FOUND,
    FACT_STATUS_AMBIGUOUS,
}
USABLE_FACT_STATUSES = {"", FACT_STATUS_FOUND}


def normalize_fact_status(value: Any) -> str:
    text = str(value or FACT_STATUS_FOUND).strip().lower()
    if text in VALID_FACT_STATUSES:
        return text
    return FACT_STATUS_FOUND


def is_usable_fact_status(value: Any) -> bool:
    return normalize_fact_status(value) in USABLE_FACT_STATUSES


def _collapse(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _strip_requirement_noise(text: str) -> str:
    cleaned = _collapse(text).strip(" .;,-:")
    if not cleaned:
        return ""

    cleaned = re.sub(
        r"^(cần|thiếu|bổ sung|lấy|truy xuất|tìm|kiểm tra)\s+"
        r"((dữ liệu|số liệu|thông tin|chi tiết|dòng|khoản mục)\s+)?"
        r"((về|cho|của|liên quan đến)\s+)?",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .;,-:")
    cleaned = re.sub(
        r"\s+để\s+(tính|đánh giá|phân tích|trả lời|xác định|kiểm tra)\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .;,-:")
    cleaned = re.sub(
        r"\b(cho năm|trong năm|tại năm|năm)\s+\d{4}\b",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip(" .;,-:")
    return " ".join(cleaned.split())


def _keyword_candidates(table: str = "") -> list[str]:
    table_name = str(table or "").strip()
    if table_name and table_name in ALLOWED_KEYWORDS:
        tables = [table_name]
    else:
        tables = list(ALLOWED_KEYWORDS.keys())

    candidates = []
    seen = set()
    for item in ALIASES.values():
        text = _collapse(item)
        if text and text not in seen:
            candidates.append(text)
            seen.add(text)
    for table_key in tables:
        for item in sorted(ALLOWED_KEYWORDS.get(table_key, set()) or set(), key=len, reverse=True):
            text = _collapse(item)
            if text and text not in seen:
                candidates.append(text)
                seen.add(text)
    return candidates


def normalize_requirement_text(value: Any, table: str = "") -> str:
    text = _strip_requirement_noise(str(value or ""))
    if not text:
        return ""

    for alias, canonical in ALIASES.items():
        alias_text = _collapse(alias)
        canonical_text = _collapse(canonical)
        if text in {alias_text, canonical_text}:
            return canonical_text

    candidates = []
    for keyword in _keyword_candidates(table):
        if text == keyword:
            return keyword
        if keyword in text or text in keyword:
            candidates.append(keyword)

    for alias, canonical in ALIASES.items():
        alias_text = _collapse(alias)
        canonical_text = _collapse(canonical)
        if alias_text and alias_text in text:
            candidates.append(canonical_text)
        elif canonical_text and canonical_text in text:
            candidates.append(canonical_text)

    candidates = _dedupe_keep_order(candidates)
    if len(candidates) == 1:
        return candidates[0]
    return text


def _dedupe_keep_order(items: Iterable[Any]) -> list[str]:
    seen = set()
    output = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def normalize_requirements_keep_order(
    items: Any,
    *,
    table: str = "",
    limit: int = 0,
) -> list[str]:
    if items is None:
        values = []
    elif isinstance(items, (list, tuple, set)):
        values = list(items)
    else:
        values = [items]

    normalized = _dedupe_keep_order(
        normalize_requirement_text(item, table=table)
        for item in values
    )
    if limit > 0:
        return normalized[:limit]
    return normalized


def extract_financial_statement_keywords(
    value: Any,
    *,
    table: str = "",
    limit: int = 3,
) -> list[str]:
    text = _strip_requirement_noise(str(value or ""))
    if not text:
        return []

    matches = []
    for keyword in _keyword_candidates(table):
        if keyword and (keyword in text or text in keyword):
            matches.append(keyword)

    for alias, canonical in ALIASES.items():
        alias_text = _collapse(alias)
        canonical_text = _collapse(canonical)
        if alias_text and alias_text in text:
            matches.append(canonical_text)
        elif canonical_text and canonical_text in text:
            matches.append(canonical_text)

    matches = _dedupe_keep_order(matches)
    if matches:
        return matches[:limit] if limit > 0 else matches

    normalized = normalize_requirement_text(text, table=table)
    if normalized and normalized != text:
        return [normalized]
    return []


def requirement_name_matches_fact(requirement: Any, fact: dict, *, table: str = "") -> bool:
    if not isinstance(fact, dict):
        return False

    fact_table = str(fact.get("table", "") or table or "").strip()
    requirement_text = normalize_requirement_text(requirement, table=fact_table or table)
    item_name = normalize_requirement_text(fact.get("item_name", ""), table=fact_table or table)
    if not requirement_text or not item_name:
        return False

    if requirement_text == item_name:
        return True
    if requirement_text in item_name or item_name in requirement_text:
        return True

    requirement_tokens = set(re.findall(r"\w+", requirement_text))
    item_tokens = set(re.findall(r"\w+", item_name))
    if not requirement_tokens or not item_tokens:
        return False
    return item_tokens.issubset(requirement_tokens) or requirement_tokens.issubset(item_tokens)


def requirement_matches_fact(requirement: Any, fact: dict, *, table: str = "") -> bool:
    if not requirement_name_matches_fact(requirement, fact, table=table):
        return False
    if not is_usable_fact_status(fact.get("status", FACT_STATUS_FOUND)):
        return False
    return fact.get("value", "") not in ("", None)


def not_found_after_search_message(item_name: Any, table: str = "") -> str:
    item = str(item_name or "").strip() or "khoản mục cần tìm"
    statement = str(table or "").strip() or "báo cáo tài chính"
    return (
        f"Không tìm thấy dòng {item} trong dữ liệu hiện có. "
        f"Có thể khoản này không phát sinh/không được trình bày riêng trong {statement}, "
        "nhưng cần xác nhận từ báo cáo gốc."
    )
