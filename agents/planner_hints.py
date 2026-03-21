from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, List, Optional

from config.allowed_keywords import ALIASES, ALLOWED_KEYWORDS
from schemas.keyword_guard import repair_keywords


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)


def _normalize_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = _SPACE_RE.sub(" ", text)
    return text


def _dedupe_keep_order(items: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items or []:
        text = str(item).strip()
        if not text or text in seen:
            continue
        out.append(text)
        seen.add(text)
    return out


def _text_tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(_normalize_text(text)))


def _relevant_axes(analysis_axes: list[dict], table: str) -> list[dict]:
    normalized_table = str(table or "").strip()
    relevant = []

    for axis in analysis_axes or []:
        if not isinstance(axis, dict):
            continue

        tables = [str(item).strip() for item in (axis.get("tables", []) or []) if str(item).strip()]
        if tables and normalized_table not in tables:
            continue
        relevant.append(axis)

    return relevant


def infer_table_query_hints(table: str, user_query: str, analysis_axes: list[dict]) -> List[str]:
    relevant_axes = _relevant_axes(analysis_axes, table)
    candidates: List[str] = []

    for axis in relevant_axes:
        objective = str(axis.get("objective", "") or "").strip()
        axis_name = str(axis.get("axis", "") or "").strip()
        if objective:
            candidates.append(objective)
        if objective and axis_name:
            candidates.append(f"{axis_name}: {objective}")
        elif axis_name:
            candidates.append(axis_name)

    if user_query:
        candidates.append(str(user_query).strip())

    return _dedupe_keep_order(candidates)


def infer_time_hint(
    user_query: str,
    *,
    dataset_fiscal_year: Optional[int] = None,
    dataset_fiscal_quarter: Optional[int] = None,
) -> str:
    text = _normalize_text(user_query)

    quarter_patterns = [
        re.compile(r"\bquý\s*([1-4])\s*(?:năm\s*)?(20\d{2})\b", flags=re.IGNORECASE),
        re.compile(r"\bq\s*([1-4])\s*[/\-]?\s*(20\d{2})\b", flags=re.IGNORECASE),
        re.compile(r"\b(20\d{2})\s*[/\-]?\s*q\s*([1-4])\b", flags=re.IGNORECASE),
    ]
    for pattern in quarter_patterns:
        match = pattern.search(text)
        if not match:
            continue
        if pattern.pattern.startswith("\\b(20"):
            year, quarter = match.group(1), match.group(2)
        else:
            quarter, year = match.group(1), match.group(2)
        return f"quý {int(quarter)}/{int(year)}"

    year_match = re.search(r"\b(20\d{2})\b", text)
    if year_match:
        return f"năm {int(year_match.group(1))}"

    if dataset_fiscal_quarter is not None and dataset_fiscal_year is not None:
        return f"quý {int(dataset_fiscal_quarter)}/{int(dataset_fiscal_year)}"
    if dataset_fiscal_year is not None:
        return f"năm {int(dataset_fiscal_year)}"

    return ""


def infer_table_keywords(table: str, user_query: str, analysis_axes: list[dict]) -> List[str]:
    allowed = ALLOWED_KEYWORDS.get(table, set())
    if not allowed:
        return []

    texts = infer_table_query_hints(table, user_query, analysis_axes)
    normalized_texts = [_normalize_text(text) for text in texts if str(text).strip()]
    normalized_corpus = " ".join(normalized_texts)
    corpus_tokens = _text_tokens(normalized_corpus)

    candidates: List[str] = []

    for alias, canonical in ALIASES.items():
        if canonical not in allowed:
            continue
        if _normalize_text(alias) in normalized_corpus:
            candidates.append(canonical)

    for keyword in sorted(allowed, key=len, reverse=True):
        if _normalize_text(keyword) in normalized_corpus:
            candidates.append(keyword)

    for text in texts:
        repaired, _details = repair_keywords(table, [text])
        candidates.extend(repaired)

    # If we already found an exact alias/canonical/repaired keyword, do not
    # expand with fuzzy semantic neighbors. This keeps planner hints focused
    # and avoids noisy follow-up lookups like "tong tai san" -> fixed assets.
    candidates = _dedupe_keep_order(candidates)
    if candidates:
        return candidates

    scored: List[tuple[float, str]] = []
    for keyword in allowed:
        keyword_norm = _normalize_text(keyword)
        keyword_tokens = _text_tokens(keyword_norm)
        if not keyword_tokens:
            continue

        best_score = 0.0

        if keyword_tokens <= corpus_tokens:
            best_score = max(best_score, 3.0 + (len(keyword_tokens) / 10.0))

        overlap = len(keyword_tokens & corpus_tokens)
        if overlap:
            coverage = overlap / len(keyword_tokens)
            best_score = max(best_score, coverage * 2.0)

        for text_norm in normalized_texts:
            if not text_norm:
                continue

            ratio = SequenceMatcher(None, keyword_norm, text_norm).ratio()
            best_score = max(best_score, ratio * 0.9)

            text_tokens = _text_tokens(text_norm)
            if text_tokens:
                token_overlap = len(keyword_tokens & text_tokens)
                if token_overlap:
                    score = (token_overlap / len(keyword_tokens)) * 2.2
                    best_score = max(best_score, score)

        if best_score >= 1.05:
            scored.append((best_score, keyword))

    scored.sort(key=lambda item: (-item[0], -len(item[1]), item[1]))
    candidates.extend([keyword for _score, keyword in scored[:4]])

    return _dedupe_keep_order(candidates)
