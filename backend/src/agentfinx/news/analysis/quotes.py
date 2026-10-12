"""Quote verification: a supporting quote must be in the source verbatim.

Only Unicode normalization (NFC) and whitespace are forgiven; there is no
fuzzy matching. The article must also belong to the event the quote supports.
"""

from __future__ import annotations

import unicodedata

MIN_QUOTE_CHARS = 12
# Wrapping marks a model adds around a quote are not part of the source.
_WRAPPERS = "\"'“”‘’«»…. "


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split())


def clean_quote(text: str) -> str:
    return normalize(text).strip(_WRAPPERS).strip()


def quote_matches(quote: str, source: str) -> bool:
    needle = clean_quote(quote)
    if len(needle) < MIN_QUOTE_CHARS:
        return False
    return needle in normalize(source)
