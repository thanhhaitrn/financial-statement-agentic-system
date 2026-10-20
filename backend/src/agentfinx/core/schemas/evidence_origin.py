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


def provenance_allows(fact: dict, *, site: str) -> bool:
    """The report provenance gate at ``site``: whether ``fact`` may serve as
    report evidence there. Normally that is :func:`is_report_fact`; with the
    provenance gate ablated (security experiment only) every fact passes,
    unchanged. A fact the normal gate rejects is recorded for the active
    experiment run with both decisions."""

    from agentfinx.core import ablation, experiment

    normal = is_report_fact(fact)
    effective = normal or ablation.config().provenance_off
    if not normal:
        experiment.emit("provenance_decision", site=site, fact_id=str(fact.get("fact_id", "") or ""),
                        source_kind=str(fact.get("source_kind", "") or ""),
                        content_type=str(fact.get("content_type", "") or ""),
                        normal="blocked", effective="allowed" if effective else "blocked")
    return effective


def record_after_gate(fact: dict, *, site: str, outcome: str) -> None:
    """What a later check did with a fact the normal gate would have blocked
    (e.g. ``rejected:unparseable_number``), so a calculation refusing a value
    is not mistaken for the source gate blocking it."""

    if is_report_fact(fact):
        return
    from agentfinx.core import experiment

    experiment.emit("provenance_outcome", site=site, fact_id=str(fact.get("fact_id", "") or ""),
                    outcome=outcome)
