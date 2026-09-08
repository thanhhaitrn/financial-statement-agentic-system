"""Source boundaries shared by prompt projections and financial guardrails.

Legacy report facts may omit a content type. Explicit external/unknown origins
are never upgraded to report evidence, even when they contain numeric values.
"""

PROVENANCE_FIELDS = (
    "source_kind", "content_type", "source_url", "publisher", "published_at",
    "retrieved_at", "content_hash", "ticker", "query", "cache_status",
)
REPORT_CONTENT_TYPES = {"table_fact", "text_fact", "note_fact", "frontmatter_fact"}


def fact_provenance(fact: dict) -> dict:
    return {key: fact[key] for key in PROVENANCE_FIELDS if fact.get(key) not in (None, "")}


def is_report_fact(fact: dict) -> bool:
    kind = str(fact.get("source_kind") or "").strip().lower()
    content_type = str(fact.get("content_type") or "").strip().lower()
    if fact.get("source_url") or kind not in {"", "report"}:
        return False
    # Empty type is the pre-provider report contract; preserve factual recall.
    return not content_type or content_type in REPORT_CONTENT_TYPES
