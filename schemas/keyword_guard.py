"""Validate and normalize retrieval keywords before they reach worker tools."""
# Code note: Schema modules normalize model/tool payloads; comments here clarify validation side effects.

from __future__ import annotations
from typing import List, Tuple, Dict, Optional
import re
from difflib import get_close_matches

from config.allowed_keywords import ALLOWED_KEYWORDS

_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)

def normalize_keyword(k: str) -> str:
    k = (k or "").strip().lower()
    k = _SPACE_RE.sub(" ", k)
    # normalize punctuation variants
    k = k.replace("–", "-").replace("—", "-")
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


def _best_effort_suggestion(table: str, keyword: str) -> Optional[str]:
    allowed = ALLOWED_KEYWORDS.get(table, set())
    nk = normalize_keyword(keyword)
    if not nk or not allowed:
        return None

    if nk in allowed:
        return nk

    tokens = set(_TOKEN_RE.findall(nk))
    if len(tokens) < 3:
        return None

    substring_matches = [
        candidate
        for candidate in allowed
        if nk in candidate or candidate in nk
    ]
    if substring_matches:
        substring_matches.sort(key=lambda item: (abs(len(item) - len(nk)), len(item), item))
        return substring_matches[0]

    loose_matches = get_close_matches(nk, list(allowed), n=1, cutoff=0.6)
    if loose_matches:
        return loose_matches[0]

    scored: List[Tuple[float, str]] = []
    for candidate in allowed:
        candidate_tokens = set(_TOKEN_RE.findall(candidate))
        if not candidate_tokens:
            continue
        overlap = len(tokens & candidate_tokens)
        if overlap == 0:
            continue
        score = overlap / max(len(tokens), len(candidate_tokens))
        scored.append((score, candidate))

    if not scored:
        return None

    scored.sort(key=lambda item: (-item[0], abs(len(item[1]) - len(nk)), item[1]))
    best_score, best_candidate = scored[0]
    if best_score >= 0.6:
        return best_candidate

    return None


def repair_keywords(table: str, keywords: List[str]) -> Tuple[List[str], List[Dict]]:
    repaired: List[str] = []
    details: List[Dict] = []
    seen = set()

    for raw in (keywords or []):
        suggestion = _best_effort_suggestion(table, raw)
        if not suggestion:
            continue
        if suggestion not in seen:
            repaired.append(suggestion)
            seen.add(suggestion)
        details.append(
            {
                "raw": raw,
                "normalized": normalize_keyword(raw),
                "suggested": suggestion,
            }
        )

    return repaired, details
