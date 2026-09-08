"""Format workflow state into compact API-style response payloads."""
# Code note: Formatter code is the final presentation layer; it should not change computed facts.

import re

from common import dedupe_keep_order as _dedupe_keep_order


_LEGACY_FINAL_HEADER_RE = re.compile(
    r"^\s*=+\s*final\s+answer\s*=+\s*",
    flags=re.IGNORECASE,
)
_LEGACY_ANSWER_PREFIX_RE = re.compile(
    r"^\s*answer\s*:\s*",
    flags=re.IGNORECASE,
)
_LEADING_EVIDENCE_BOILERPLATE_RE = re.compile(
    r"^\s*(?:\*{1,2})?"
    r"(?:dựa\s+(?:trên|vào)\s+(?:các\s+)?số\s+liệu\s+hiện\s+có|"
    r"theo\s+(?:các\s+)?số\s+liệu\s+hiện\s+có)"
    r"(?:\*{1,2})?\s*(?:[:;,\.\-–—]+\s*)?",
    flags=re.IGNORECASE,
)


def _normalize_display_answer(answer: str) -> str:
    """Remove legacy transport labels and leading presentation boilerplate.

    Only prefixes are removed. The formatter never edits matching prose inside
    the answer body and never composes analysis content from worker outputs.
    """

    text = str(answer or "").strip()
    # Accept persisted output from either legacy ordering without leaking those
    # protocol labels into the public Markdown response.
    for _ in range(2):
        text = _LEGACY_FINAL_HEADER_RE.sub("", text, count=1)
        text = _LEGACY_ANSWER_PREFIX_RE.sub("", text, count=1)
    text = _LEADING_EVIDENCE_BOILERPLATE_RE.sub("", text, count=1)
    return text.strip()


def format_final_answer(state: dict) -> str:
    d = state.get("synth_decision", {}) or {}
    status = d.get("status", "answer")
    answer = _normalize_display_answer(d.get("answer", ""))
    if not answer:
        answer = "Chưa đủ dữ liệu để trả lời."

    # The synthesizer already incorporates successful analysis outputs.  Printing
    # worker sections again duplicates the answer and can expose stale rounds.
    # Worker-level ``not_found_after_search`` facts are retrieval diagnostics, not
    # user-facing follow-ups: appending all of them can leak unrelated searches
    # into an otherwise complete answer.  Only the synthesizer's explicit
    # ``need_more`` contract below is allowed to add missing requirements.
    lines = [answer]

    if status == "need_more":
        missing = list(d.get("missing", []) or [])
        for followup in (d.get("followups", []) or []):
            if not isinstance(followup, dict):
                continue
            missing.extend(followup.get("requirements", []) or [])

        missing = _dedupe_keep_order(missing)
        if missing:
            lines.append(
                "\n".join(
                    ["**Còn thiếu dữ liệu**", *[f"- {x}" for x in missing]]
                )
            )
        return "\n\n".join(lines)

    return "\n\n".join(lines)
