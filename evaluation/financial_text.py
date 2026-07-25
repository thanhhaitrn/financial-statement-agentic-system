"""Accent-stripping normalization of Vietnamese financial text for gate matching.

Extracted from ``eval_retrieval_recall``. These functions fold values, units,
periods and references into canonical, accent-free tokens so a contract fact can
be matched against a retrieved fact regardless of surface formatting.

Note the deliberate accent policy: this module strips diacritics (the gate
compares normalized tokens), which is the opposite of
``ingestion/period_normalize`` — that module keeps diacritics because the
retrieval reranker matches Vietnamese words directly. The two are intentionally
separate and must not be merged.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})(?:\s*(?:/|-)\s*|\s+thang\s+|\s+)"
    r"(?P<month>\d{1,2})(?:\s*(?:/|-)\s*|\s+nam\s+|\s+)"
    r"(?P<year>\d{4})\b"
)
_QUARTER_RE = re.compile(r"\b(?:quy|q)\s*([1-4])\s*(?:/|nam\s*)?(\d{4})?\b")
_YEAR_RE = re.compile(r"\b(?:nam\s*)?(20\d{2})\b")


def _plain_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("đ", "d")
    return " ".join(text.split()).strip()


def normalize_text(value: Any) -> str:
    text = _plain_text(value)
    text = re.sub(r"[^a-z0-9%]+", " ", text)
    return " ".join(text.split()).strip()


def normalize_value(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, float):
        return format(Decimal(str(value)).normalize(), "f")

    raw = _plain_text(value).strip()
    if not raw:
        return ""
    raw = re.sub(
        r"\s*(?:trieu|nghin|ty)?\s*(?:vnd|vnđ|dong|percent|phan tram|%)\s*$",
        "",
        raw,
    ).strip()
    negative = raw.startswith("(") and raw.endswith(")")
    raw = raw.strip("() ")
    compact = raw.replace(" ", "")
    if re.fullmatch(r"-?\d{1,3}(?:[.,]\d{3})+", compact):
        number = compact.replace(".", "").replace(",", "")
    elif re.fullmatch(r"-?\d+(?:[.,]\d+)?", compact):
        number = compact.replace(",", ".")
    else:
        return normalize_text(value)
    if negative and not number.startswith("-"):
        number = f"-{number}"
    try:
        return format(Decimal(number).normalize(), "f")
    except InvalidOperation:
        return number


def normalize_unit(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    if text in {"percent", "billion vnd", "million vnd", "thousand vnd", "vnd"}:
        return text.replace(" ", "_")
    if "%" in str(value) or "phan tram" in text or text == "percent":
        return "percent"
    if "ty" in text and ("vnd" in text or "dong" in text):
        return "billion_vnd"
    if "trieu" in text and ("vnd" in text or "dong" in text):
        return "million_vnd"
    if "nghin" in text and ("vnd" in text or "dong" in text):
        return "thousand_vnd"
    if text in {"vnd", "vnd dong", "dong", "vn d"} or "vnd" in text:
        return "vnd"
    return text.replace(" ", "_")


def normalize_period(value: Any) -> str:
    text = normalize_text(value)
    if not text:
        return ""
    date_match = _DATE_RE.search(text)
    if date_match and 1 <= int(date_match.group("day")) <= 31 and 1 <= int(date_match.group("month")) <= 12:
        return (
            f"{int(date_match.group('year')):04d}-"
            f"{int(date_match.group('month')):02d}-"
            f"{int(date_match.group('day')):02d}"
        )
    quarter_match = _QUARTER_RE.search(text)
    if quarter_match:
        year = quarter_match.group(2)
        return f"{year + '-' if year else ''}q{quarter_match.group(1)}"
    if any(marker in text for marker in ("nam hien tai", "nam nay", "current year")):
        return "current_year"
    if any(marker in text for marker in ("nam truoc", "previous year", "prior year")):
        return "prior_year"
    if any(marker in text for marker in ("cuoi ky", "cuoi nam", "so cuoi", "ending")) or text == "cuoi":
        year = _YEAR_RE.search(text)
        return f"{year.group(1)}-ending" if year else "ending"
    if any(marker in text for marker in ("dau ky", "dau nam", "so dau", "beginning")) or text == "dau":
        year = _YEAR_RE.search(text)
        return f"{year.group(1)}-beginning" if year else "beginning"
    year = _YEAR_RE.fullmatch(text)
    return year.group(1) if year else text


def normalize_reference(value: Any) -> str:
    text = normalize_text(value)
    text = re.sub(r"^(?:thuyet minh|note|tham chieu|ref(?:erence)?)\s*(?:so)?\s*", "", text)
    return text.replace(" ", "")


def _infer_unit(*values: Any) -> str:
    text = " ".join(str(value or "") for value in values)
    return normalize_unit(text) if re.search(r"(?i)(?:vnd|vnđ|đồng|dong|%|phần trăm|phan tram)", text) else ""


def normalize_fact(fact: dict[str, Any]) -> dict[str, str]:
    fact = fact if isinstance(fact, dict) else {}
    unit = fact.get("unit") or _infer_unit(fact.get("value"), fact.get("evidence_text"))
    return {
        "entity": normalize_text(fact.get("entity") or fact.get("company")),
        "metric": normalize_text(
            fact.get("metric")
            or fact.get("item_name")
            or fact.get("item")
            or fact.get("subheading")
        ),
        "period": normalize_period(fact.get("period") or fact.get("time_hint")),
        "value": normalize_value(fact.get("value")),
        "unit": normalize_unit(unit),
        "reference": normalize_reference(
            fact.get("reference") or fact.get("note_ref") or fact.get("note_number")
        ),
    }
