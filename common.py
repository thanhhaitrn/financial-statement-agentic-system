"""Small shared utilities used across the pipeline, agents and evaluation.

Kept as a top-level module (like the other root entry modules) so both the
root scripts and the sub-packages can import it without depending on the
optional installed ``agentfinx`` package.
"""

from __future__ import annotations

from typing import Any, Iterable

from evaluation.contracts import stable_json_fingerprint


def dedupe_keep_order(items: Iterable[Any] | None) -> list[str]:
    """Return the stripped, non-empty string items in first-seen order.

    Falsy items (``None``, ``0``, ``""``) collapse to an empty string and are
    dropped, so the result is always a list of distinct non-empty strings.
    """
    seen: set[str] = set()
    output: list[str] = []
    for item in items or []:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _record_id(item: dict, index: int) -> Any:
    """Return an explicit record id, falling back to the positional index."""
    value = item.get("id")
    return value if value not in ("", None) else index


def prediction_key(item: dict) -> str:
    """Stable identity for a seed/prediction record (id + question + reference).

    Used to align predictions with scores and seed records across resume runs,
    independent of list position.
    """
    if not isinstance(item, dict):
        return ""
    question = str(item.get("question", "") or "").strip()
    reference = str(item.get("ground_truth", "") or item.get("reference", "") or "").strip()
    record_id = str(item.get("id", "") or "").strip()
    if not (record_id or question):
        return ""
    fingerprint = stable_json_fingerprint(
        {"id": record_id, "question": question, "reference": reference}
    )[:16]
    return f"sample:{fingerprint}"
