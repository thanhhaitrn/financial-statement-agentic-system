from __future__ import annotations
from typing import List, Tuple, Dict
import re
from difflib import get_close_matches

from config.allowed_keywords import ALLOWED_KEYWORDS, ALIASES

_SPACE_RE = re.compile(r"\s+")

def normalize_keyword(k: str) -> str:
    k = (k or "").strip().lower()
    k = _SPACE_RE.sub(" ", k)
    # normalize punctuation variants
    k = k.replace("–", "-").replace("—", "-")
    # alias mapping
    if k in ALIASES:
        k = ALIASES[k]
    return k

def validate_keywords(
    table: str,
    keywords: List[str],
    *,
    fuzzy: bool = True,
    cutoff: float = 0.88,
) -> Tuple[List[str], List[Dict]]:
    """
    Returns:
      - valid_keywords (canonical, deduped, original order)
      - invalid_details: [{"raw":..., "normalized":..., "suggested":...}, ...]
    """
    allowed = ALLOWED_KEYWORDS.get(table, set())
    if not allowed:
        # table unknown -> reject everything
        return [], [{"raw": k, "normalized": normalize_keyword(k), "suggested": None} for k in (keywords or [])]

    valid: List[str] = []
    seen = set()
    invalid_details: List[Dict] = []

    for raw in (keywords or []):
        nk = normalize_keyword(raw)
        if not nk:
            continue

        if nk in allowed:
            if nk not in seen:
                valid.append(nk)
                seen.add(nk)
            continue

        suggested = None
        if fuzzy:
            matches = get_close_matches(nk, list(allowed), n=1, cutoff=cutoff)
            if matches:
                suggested = matches[0]
                if suggested not in seen:
                    valid.append(suggested)
                    seen.add(suggested)
                invalid_details.append({"raw": raw, "normalized": nk, "suggested": suggested})
                continue

        invalid_details.append({"raw": raw, "normalized": nk, "suggested": None})

    return valid, invalid_details