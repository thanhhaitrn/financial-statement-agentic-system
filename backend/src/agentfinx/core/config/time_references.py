"""Verbatim time references for provenance guards, not semantic routing rules.

The current/previous-period vocabulary is shared with report retrieval. Extra
surfaces preserve the old chat guard's coverage without changing CLI routing
or resolving relative periods to a date.
"""

from __future__ import annotations

import re
import unicodedata

from agentfinx.core.config.domain_catalog import CURRENT_PERIOD_MARKERS, PREVIOUS_PERIOD_MARKERS

RELATIVE_PERIOD_MARKERS = tuple(dict.fromkeys((
    *CURRENT_PERIOD_MARKERS, *PREVIOUS_PERIOD_MARKERS,
    "nam ngoai", "quy truoc", "thang truoc", "cung ky",
    "dau quy", "cuoi quy", "dau thang", "cuoi thang",
)))
_TIME_REFERENCE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(x) for x in sorted(RELATIVE_PERIOD_MARKERS, key=len, reverse=True))
    + r"|\d{1,2}[/-]\d{1,2}[/-]\d{4})(?!\w)"
)


def time_reference_spans(text: str) -> list[str]:
    """Find folded surfaces but return slices of ORIGINAL user text.

    Use an offset map: decomposed Vietnamese and whitespace are not 1:1.
    """
    folded: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        part = "".join(c for c in unicodedata.normalize("NFD", char.casefold())
                       if not unicodedata.combining(c)).replace("đ", "d")
        for item in part:
            if item.isspace():
                if not folded or folded[-1] == " ":
                    continue
                item = " "
            folded.append(item)
            offsets.append(index)
    result = []
    for match in _TIME_REFERENCE.finditer("".join(folded)):
        end = offsets[match.end() - 1] + 1
        while end < len(text) and unicodedata.combining(text[end]):
            end += 1
        result.append(text[offsets[match.start()]:end])
    return list(dict.fromkeys(result))
