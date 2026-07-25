"""Exact parsing and formatting for financial numbers used in calculations."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any


_NUMBER_TOKEN_RE = re.compile(r"\(?[-+]?\d[\d\s.,]*\)?")


def parse_financial_decimal(value: Any) -> Decimal | None:
    """Parse common Vietnamese/international financial formats without floats.

    Dots or commas in repeated three-digit groups are treated as thousands
    separators. When both separators occur, the last one is the decimal mark.
    Parentheses represent a negative accounting value.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)

    match = _NUMBER_TOKEN_RE.search(str(value).replace("\u00a0", " "))
    if not match:
        return None

    token = match.group(0).strip().replace(" ", "")
    negative_parentheses = token.startswith("(") and token.endswith(")")
    token = token.strip("()")
    explicit_negative = token.startswith("-")
    token = token.lstrip("+-")
    if not token or not any(char.isdigit() for char in token):
        return None

    dot_positions = [index for index, char in enumerate(token) if char == "."]
    comma_positions = [index for index, char in enumerate(token) if char == ","]
    decimal_separator = ""

    if dot_positions and comma_positions:
        decimal_separator = "." if dot_positions[-1] > comma_positions[-1] else ","
    elif dot_positions or comma_positions:
        separator = "." if dot_positions else ","
        pieces = token.split(separator)
        # Financial statements overwhelmingly use grouped integer amounts. A
        # single non-three-digit tail is the unambiguous decimal case.
        if not (len(pieces) > 1 and all(len(piece) == 3 for piece in pieces[1:])):
            decimal_separator = separator

    if decimal_separator:
        thousands_separator = "," if decimal_separator == "." else "."
        normalized = token.replace(thousands_separator, "")
        if normalized.count(decimal_separator) > 1:
            head, tail = normalized.rsplit(decimal_separator, 1)
            head = head.replace(decimal_separator, "")
            normalized = f"{head}.{tail}"
        else:
            normalized = normalized.replace(decimal_separator, ".")
    else:
        normalized = token.replace(".", "").replace(",", "")

    try:
        parsed = Decimal(normalized)
    except InvalidOperation:
        return None
    if negative_parentheses or explicit_negative:
        parsed = -abs(parsed)
    return parsed if parsed.is_finite() else None


def format_decimal_vi(value: Decimal, *, max_decimal_places: int = 6) -> str:
    """Format an exact decimal with Vietnamese grouping and no fake zeros."""

    if not value.is_finite():
        raise ValueError("financial decimal must be finite")
    if max_decimal_places < 0:
        raise ValueError("max_decimal_places must be non-negative")

    quantum = Decimal(1).scaleb(-max_decimal_places)
    rounded = value.quantize(quantum) if value.as_tuple().exponent < -max_decimal_places else value
    sign = "-" if rounded < 0 else ""
    fixed = format(abs(rounded), "f")
    integer, dot, fraction = fixed.partition(".")
    grouped = f"{int(integer):,}".replace(",", ".")
    fraction = fraction.rstrip("0")
    return f"{sign}{grouped}{',' + fraction if dot and fraction else ''}"
