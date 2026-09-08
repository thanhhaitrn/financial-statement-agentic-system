"""Shared evidence-pack helpers for deterministic retrieval and tool caching."""
# Code note: Evidence helpers keep retrieval cache keys and fact shaping consistent across graph nodes and tools.

from __future__ import annotations

import os
import re
from collections import OrderedDict
from copy import deepcopy
from itertools import zip_longest
from threading import RLock
from typing import Any

from agents.agent_registry import ANALYSIS_DEFAULT_TOOL
from evaluation.narrative_semantics import (
    narrative_concept_atoms,
    narrative_evidence_surface,
)
from schemas.requirements import (
    FACT_STATUS_AMBIGUOUS,
    FACT_STATUS_FOUND,
    FACT_STATUS_NOT_FOUND,
    REQUIREMENT_AMBIGUOUS,
    REQUIREMENT_EXHAUSTIVE_ABSENT,
    REQUIREMENT_MATCHED,
    REQUIREMENT_UNMATCHED_TOPK,
    normalize_requirement_text,
    normalize_fact_status,
    not_found_after_search_message,
    requirement_name_matches_fact,
    requirement_matches_fact,
)
from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    normalize_table_heading,
)
from tools.query_routing import (
    QuerySlots,
    RouteCandidate,
    fact_reporting_basis,
    parse_query_slots,
    route_candidates,
)


SCOPED_TOOL_TO_TABLE = {
    "get_balance_sheet_info": TABLE_BS,
    "get_income_statement_info": TABLE_IS,
    "get_cashflow_info": TABLE_CF,
    "get_note_info": TABLE_NOTE,
    "get_report_section_info": TABLE_REPORT_SECTION,
}

TABLE_TO_SCOPED_TOOL = {
    table: tool_name
    for tool_name, table in SCOPED_TOOL_TO_TABLE.items()
}

MAIN_REPORT_TABLES = {TABLE_BS, TABLE_IS, TABLE_CF}
# Legacy behavior (=1): one exact label match discards all other candidates.
_EXACT_MATCH_COLLAPSE = str(os.getenv("EXACT_MATCH_COLLAPSE", "0")).strip() == "1"
_NARRATIVE_EVIDENCE_ATOMS = frozenset(
    {
        "state_owned",
        "corporatization",
        "joint_stock_registration",
        "listing",
        "listing_license",
        "internal_control",
        "going_concern",
    }
)
_NARRATIVE_ATOM_RETRY_PHRASES = (
    ("state_owned", "doanh nghiệp nhà nước"),
    ("corporatization", "cổ phần hóa doanh nghiệp"),
    (
        "joint_stock_registration",
        "đăng ký doanh nghiệp công ty cổ phần",
    ),
    ("listing_license", "giấy phép niêm yết cổ phiếu"),
    (
        "listing",
        "cổ phiếu chính thức niêm yết sở giao dịch chứng khoán",
    ),
    ("internal_control", "hệ thống kiểm soát nội bộ"),
    ("going_concern", "hoạt động liên tục"),
)

_SPACE_RE = re.compile(r"\s+")
_RUNTIME_CACHE_LOCK = RLock()
_RUNTIME_CACHE_MAX_ITEMS = max(1, int(os.getenv("RUNTIME_EVIDENCE_CACHE_MAX_ITEMS", "256")))
_RUNTIME_EVIDENCE_CACHE: OrderedDict[str, dict] = OrderedDict()

_FACT_METADATA_FIELDS = (
    "company",
    "fiscal_year",
    "index_generation",
    "fact_id",
    "note_number",
    "note_ref",
    "note_title",
    "section_path",
    "block_id",
    "subheading",
    "item_code",
    "item_name",
    "row_label",
    "column_label",
    "time_hint",
    "period",
    "period_label",
    "period_role",
    "reporting_basis",
    "value_type",
    "aggregation_level",
    "unit",
    "value_kind",
    "raw_value",
    "normalized_value",
    "parsed_value",
    "source",
    "source_page",
    "metric_label",
    "entity_label",
    "scope_label",
    "counterparty",
    "transaction_type",
    "movement_type",
    "geography",
    "policy_topic",
    "section_key",
    "coverage_template",
    "required_legs",
    "coverage_complete_legs",
    "rerank_score",
    "similarity_score",
    "distance",
    "score_query",
    "score_intent",
    "retrieval_origin",
)


def collapse_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip().lower())


def normalize_evidence_table(value: Any) -> str:
    return normalize_table_heading(str(value or "").strip())


def normalize_evidence_query(value: Any, *, table: str = "") -> str:
    normalized = normalize_requirement_text(value, table=table)
    return normalized or collapse_text(value)


def _required_narrative_evidence_atoms(query: Any) -> set[str]:
    """Return event atoms only for an explicit narrative-premise query.

    The caller must opt in through ``semantic_requirement``.  Normal typed
    retrieval never sets that argument, so a price/policy lookup containing
    ``niêm yết`` is not redirected to the company's listing history.  Route
    labels are never inspected here; matching later uses only each fact's
    canonical payload.
    """

    return (
        narrative_concept_atoms(query, requirement=True)
        & _NARRATIVE_EVIDENCE_ATOMS
    )


def _narrative_fact_atoms(fact: dict) -> set[str]:
    return narrative_concept_atoms(narrative_evidence_surface(fact))


def _is_usable_narrative_fact(fact: dict) -> bool:
    if normalize_fact_status(fact.get("status")) != FACT_STATUS_FOUND:
        return False
    if not (
        str(fact.get("fact_id", "") or "").strip()
        or str(fact.get("source", "") or "").strip()
        or str(fact.get("source_page", "") or "").strip()
    ):
        return False
    return bool(narrative_evidence_surface(fact))


def narrative_requirement_atoms_covered(
    semantic_requirement: str,
    facts: list[dict],
) -> bool:
    """Whether actual fact payloads jointly cover a narrative premise."""

    required_atoms = _required_narrative_evidence_atoms(
        semantic_requirement
    )
    if not required_atoms:
        return False
    return not missing_narrative_requirement_atoms(
        semantic_requirement,
        facts,
    )


def missing_narrative_requirement_atoms(
    semantic_requirement: str,
    facts: list[dict],
) -> set[str]:
    """Return premise atoms not covered by usable retrieved facts."""

    required_atoms = _required_narrative_evidence_atoms(
        semantic_requirement
    )
    if not required_atoms:
        return set()
    covered_atoms: set[str] = set()
    for fact in facts or []:
        if not isinstance(fact, dict) or not _is_usable_narrative_fact(fact):
            continue
        covered_atoms.update(
            required_atoms.intersection(_narrative_fact_atoms(fact))
        )
    return required_atoms - covered_atoms


def narrative_atom_gap_retry_query(
    semantic_requirement: str,
    facts: list[dict],
) -> str:
    """Build one deterministic retry query from the uncovered premise atoms."""

    missing_atoms = missing_narrative_requirement_atoms(
        semantic_requirement,
        facts,
    )
    phrases = [
        phrase
        for atom, phrase in _NARRATIVE_ATOM_RETRY_PHRASES
        if atom in missing_atoms
    ]
    return " và ".join(phrases)


def narrative_requirement_evidence_state(
    semantic_requirement: str,
    facts: list[dict],
) -> str:
    """Evaluate an opted-in narrative premise without generic name matching.

    A multi-atom premise is matched iff usable, provenance-bearing facts
    jointly cover every atom.  Partial semantic hits stay ``unmatched_topk`` so
    the evidence graph performs its targeted retry instead of accepting an
    exact route label as proof.
    """

    required_atoms = _required_narrative_evidence_atoms(
        semantic_requirement
    )
    if not required_atoms:
        return ""
    if narrative_requirement_atoms_covered(semantic_requirement, facts):
        return REQUIREMENT_MATCHED

    has_ambiguous_contributor = False
    has_exhaustive_absence = False
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        fact_atoms = required_atoms.intersection(_narrative_fact_atoms(fact))
        if (
            fact_atoms
            and normalize_fact_status(fact.get("status"))
            == FACT_STATUS_AMBIGUOUS
        ):
            has_ambiguous_contributor = True
        if (
            str(fact.get("evidence_state", "") or "")
            == REQUIREMENT_EXHAUSTIVE_ABSENT
        ):
            has_exhaustive_absence = True

    if has_ambiguous_contributor:
        return REQUIREMENT_AMBIGUOUS
    if has_exhaustive_absence:
        return REQUIREMENT_EXHAUSTIVE_ABSENT
    return REQUIREMENT_UNMATCHED_TOPK


def evidence_cache_key(
    *,
    dataset_id: str = "",
    table: str = "",
    query: str = "",
    mode: str = "table",
    intent: str = "",
    generation: str = "",
) -> str:
    # intent participates in the key because retrieval results depend on the full
    # user question (intent lexical fold + slot matching) while the runtime cache
    # is process-global: a batch run asks many different questions whose scoped
    # queries can collide on the same (table, query) pair.
    table_name = normalize_evidence_table(table)
    query_text = collapse_text(query)
    return "|".join(
        [
            collapse_text(dataset_id) or "default_dataset",
            collapse_text(mode) or "table",
            table_name,
            query_text,
            collapse_text(intent),
            collapse_text(generation),
        ]
    )


def get_runtime_cache_item(cache_key: str) -> dict:
    key = str(cache_key or "").strip()
    if not key:
        return {}
    with _RUNTIME_CACHE_LOCK:
        item = _RUNTIME_EVIDENCE_CACHE.get(key)
        if not isinstance(item, dict):
            return {}
        _RUNTIME_EVIDENCE_CACHE.move_to_end(key)
        return deepcopy(item)


def set_runtime_cache_item(cache_key: str, item: dict) -> None:
    key = str(cache_key or "").strip()
    if not key or not isinstance(item, dict):
        return
    with _RUNTIME_CACHE_LOCK:
        _RUNTIME_EVIDENCE_CACHE[key] = deepcopy(item)
        _RUNTIME_EVIDENCE_CACHE.move_to_end(key)
        while len(_RUNTIME_EVIDENCE_CACHE) > _RUNTIME_CACHE_MAX_ITEMS:
            _RUNTIME_EVIDENCE_CACHE.popitem(last=False)


def clear_runtime_evidence_cache() -> None:
    with _RUNTIME_CACHE_LOCK:
        _RUNTIME_EVIDENCE_CACHE.clear()


def scoped_tool_name_for_query(query: str, *, agent_name: str = "") -> str:
    """Compatibility wrapper over the ordered, multi-label router."""

    candidates = route_candidates(query, agent_name=agent_name)
    if not candidates:
        return ANALYSIS_DEFAULT_TOOL.get(
            str(agent_name or "").strip(), "get_income_statement_info"
        )
    return scoped_tool_name_for_table(candidates[0].table)


def table_for_scoped_tool(tool_name: str) -> str:
    return SCOPED_TOOL_TO_TABLE.get(str(tool_name or "").strip(), "")


def scoped_tool_name_for_table(table: str) -> str:
    return TABLE_TO_SCOPED_TOOL.get(normalize_evidence_table(table), "")


def _extract_docs_and_metas(result: dict) -> tuple[list[str], list[dict]]:
    docs = result.get("documents", []) if isinstance(result, dict) else []
    metas = result.get("metadatas", []) if isinstance(result, dict) else []
    docs = docs if isinstance(docs, list) else []
    metas = metas if isinstance(metas, list) else []
    normalized_metas = [meta if isinstance(meta, dict) else {} for meta in metas]
    return [str(doc or "") for doc in docs], normalized_metas


def _first_context_lines(context: str, *, limit: int = 5) -> list[str]:
    lines = []
    for line in str(context or "").splitlines():
        text = line.strip()
        if not text:
            continue
        lines.append(text)
        if len(lines) >= limit:
            break
    return lines


def _fact_key(fact: dict) -> tuple[str, str, str, str, str, str]:
    return (
        collapse_text(fact.get("table", "")),
        collapse_text(fact.get("item_name", "")),
        collapse_text(fact.get("note_ref", "")),
        collapse_text(fact.get("time_hint", "")),
        collapse_text(fact.get("value", "")),
        collapse_text(fact.get("source", "")),
    )


def _coerce_text_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []

    output = []
    seen = set()
    for item in values:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _has_metadata_value(value: Any) -> bool:
    return value not in ("", None, [], {})


def _merge_fact_routing_metadata(existing: dict, incoming: dict) -> dict:
    merged = dict(existing or {})

    needby = _coerce_text_list(merged.get("needby")) + _coerce_text_list(incoming.get("needby"))
    if needby:
        merged["needby"] = _coerce_text_list(needby)

    evidence_queries = (
        _coerce_text_list(merged.get("evidence_queries"))
        + _coerce_text_list(merged.get("evidence_query"))
        + _coerce_text_list(incoming.get("evidence_queries"))
        + _coerce_text_list(incoming.get("evidence_query"))
    )
    evidence_queries = _coerce_text_list(evidence_queries)
    if evidence_queries:
        merged["evidence_query"] = evidence_queries[0]
        if len(evidence_queries) > 1:
            merged["evidence_queries"] = evidence_queries

    for key in (
        "source_table",
        "source_item",
        "evidence_role",
        "linked_parent_fact_id",
        "linked_parent_item",
        "linked_parent_value",
        "linked_parent_period_label",
        "linked_parent_aggregation_level",
        "message",
        "evidence_text",
        *_FACT_METADATA_FIELDS,
    ):
        if not _has_metadata_value(merged.get(key)) and _has_metadata_value(
            incoming.get(key)
        ):
            merged[key] = incoming.get(key)

    existing_status = normalize_fact_status(merged.get("status"))
    incoming_status = normalize_fact_status(incoming.get("status"))
    if existing_status != FACT_STATUS_FOUND and incoming_status == FACT_STATUS_FOUND:
        merged["status"] = FACT_STATUS_FOUND
    elif not str(merged.get("status", "") or "").strip() and str(incoming.get("status", "") or "").strip():
        merged["status"] = incoming_status

    state_order = {
        REQUIREMENT_MATCHED: 4,
        REQUIREMENT_AMBIGUOUS: 3,
        REQUIREMENT_EXHAUSTIVE_ABSENT: 2,
        REQUIREMENT_UNMATCHED_TOPK: 1,
    }
    existing_state = str(merged.get("evidence_state", "") or "").strip()
    incoming_state = str(incoming.get("evidence_state", "") or "").strip()
    if state_order.get(incoming_state, -1) > state_order.get(existing_state, -1):
        merged["evidence_state"] = incoming_state
    if bool(incoming.get("search_exhaustive")):
        merged["search_exhaustive"] = True

    return merged


def _raw_item_label(value: Any) -> str:
    text = str(value or "").split("|", 1)[0]
    return collapse_text(text)


def _fact_label_exactly_matches(query: str, fact: dict, *, table: str = "") -> bool:
    if not isinstance(fact, dict):
        return False
    table_name = normalize_evidence_table(fact.get("table", "") or table)
    query_text = collapse_text(normalize_evidence_query(query, table=table_name))
    item_label = _raw_item_label(fact.get("item_name", ""))
    return bool(query_text and item_label and item_label == query_text)


def dedupe_facts(facts: list[dict]) -> list[dict]:
    output = []
    seen = {}
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        key = _fact_key(fact)
        if key in seen:
            output[seen[key]] = _merge_fact_routing_metadata(output[seen[key]], fact)
            continue
        output.append(fact)
        seen[key] = len(output) - 1
    return output


def not_found_fact(table: str, query: str, *, source: str = "") -> dict:
    table_name = normalize_evidence_table(table)
    item_name = normalize_evidence_query(query, table=table_name)
    return {
        "content_type": "table_fact",
        "item_name": item_name,
        "time_hint": "",
        "value": "",
        "source": str(source or "").strip(),
        "table": table_name,
        "status": FACT_STATUS_NOT_FOUND,
        "evidence_state": REQUIREMENT_UNMATCHED_TOPK,
        "message": not_found_after_search_message(item_name, table_name),
    }


def filter_facts_for_query(
    facts: list[dict],
    *,
    table: str,
    query: str,
    source: str = "",
    semantic_requirement: str = "",
) -> list[dict]:
    table_name = normalize_evidence_table(table)
    query_text = normalize_evidence_query(query, table=table_name)
    matching_query = str(query or "").strip() or query_text
    required_narrative_atoms = _required_narrative_evidence_atoms(
        semantic_requirement
    )
    candidates = []
    clean_facts = []
    exact_facts = []

    for fact in dedupe_facts(facts or []):
        if not isinstance(fact, dict):
            continue
        fact_payload = dict(fact)
        fact_table = normalize_evidence_table(fact_payload.get("table", "")) or table_name
        if fact_table:
            fact_payload["table"] = fact_table
        candidates.append(fact_payload)

        if required_narrative_atoms:
            # A narrative premise is a union requirement: licensing and the
            # actual listing, or corporatization and registration, may be
            # separate facts. Keep every actual payload that contributes an
            # atom. Do not fall through to exact-label matching, which can
            # promote a valuation policy merely because its route label repeats
            # "niêm yết".
            fact_atoms = narrative_concept_atoms(
                narrative_evidence_surface(fact_payload)
            )
            if required_narrative_atoms.intersection(fact_atoms):
                clean_facts.append(fact_payload)
            continue

        fact_scope = fact_table or table_name
        if requirement_name_matches_fact(
            matching_query,
            fact_payload,
            table=fact_scope,
        ):
            clean_facts.append(fact_payload)
            if (
                requirement_matches_fact(
                    matching_query,
                    fact_payload,
                    table=fact_scope,
                )
                and _fact_label_exactly_matches(
                    query_text, fact_payload, table=fact_scope
                )
            ):
                exact_facts.append(fact_payload)

    multi_fact_markers = (
        " so sánh ", " và ", "liệt kê", "danh sách", "các khoản",
        "những", "nào", "mỗi", "từng", "biến động",
    )
    padded_query = f" {collapse_text(query)} "
    query_slots = parse_query_slots(query)
    is_multi_fact_query = (
        query_slots.operation
        in {"compare", "delta", "percent_change", "ratio", "multiple", "share", "list"}
        or query_slots.period == "both"
        or query_slots.period_role == "both"
        or len(query_slots.value_type) > 1
        or any(marker in padded_query for marker in multi_fact_markers)
    )
    has_explicit_slots = bool(
        query_slots.metric
        or query_slots.entity
        or query_slots.period in {"cuối", "đầu", "both"}
        or query_slots.period_role in {"current", "previous", "both"}
        or query_slots.period_labels
        or query_slots.value_type
        or query_slots.aggregation in {"total", "component"}
        or query_slots.coverage_template
        or query_slots.counterparty
        or query_slots.transaction_type
        or query_slots.movement_type
        or query_slots.geography
        or query_slots.policy_topic
    )

    # Once an answer-bearing row is identified, coverage/comparison questions
    # may retain only siblings from the same parsed table block.  This supplies
    # the opposite period or declared schedule legs without opening a whole
    # note merely because it shares ``note_ref``.
    if clean_facts and (is_multi_fact_query or query_slots.coverage_template):
        anchor_blocks = {
            str(fact.get("block_id", "") or "").strip()
            for fact in clean_facts
            if str(fact.get("block_id", "") or "").strip()
        }
        if anchor_blocks:
            clean_keys = {_fact_key(fact) for fact in clean_facts}
            clean_facts.extend(
                fact
                for fact in candidates
                if str(fact.get("block_id", "") or "").strip() in anchor_blocks
                and _fact_key(fact) not in clean_keys
            )

    if exact_facts and (_EXACT_MATCH_COLLAPSE or not is_multi_fact_query):
        clean_facts = exact_facts
    elif exact_facts:
        # Exact-first ordering instead of collapse: an exact label match on the
        # aggregate row must not discard the reranker-vetted siblings (per-class
        # V.9 rows, opposite-period rows) that comparison/breakdown questions
        # need. Exact facts keep rank 1; the per-table fact cap bounds the tail.
        exact_keys = {_fact_key(fact) for fact in exact_facts}
        clean_facts = exact_facts + [
            fact for fact in clean_facts if _fact_key(fact) not in exact_keys
        ]

    # Main statements are always exact-line-item scoped.  NOTE/front prose is
    # kept for genuinely unstructured prompts, but an explicit typed query must
    # not treat unrelated top-k rows as evidence.
    enforce_match = (
        bool(required_narrative_atoms)
        or table_name in MAIN_REPORT_TABLES
        or has_explicit_slots
    )
    if enforce_match and query_text and not clean_facts:
        return [not_found_fact(table_name, query_text, source=source)]

    return dedupe_facts(clean_facts if enforce_match else candidates)


def result_to_facts(
    result: dict,
    *,
    table: str,
    query: str,
    limit: int = 5,
    semantic_requirement: str = "",
) -> list[dict]:
    table_name = normalize_evidence_table(table)
    docs, metas = _extract_docs_and_metas(result or {})
    facts = []

    for doc, meta in list(zip_longest(docs, metas, fillvalue=None))[:limit]:
        if doc is None:
            continue
        metadata_missing = not isinstance(meta, dict) or not meta
        meta = meta if isinstance(meta, dict) else {}
        evidence_text = doc.strip()
        heading = normalize_evidence_table(meta.get("heading", "")) or table_name
        item_name = str(meta.get("item_name", "") or "").strip() or normalize_evidence_query(query, table=heading)
        raw_value = str(meta.get("raw_value", "") or "").strip()
        normalized_value = str(meta.get("normalized_value", "") or "").strip()
        value = (
            raw_value
            or normalized_value
            or str(meta.get("value", "") or "").strip()
        )
        fact_id = str(meta.get("fact_id", "") or "").strip()
        canonical_value_missing = not value
        unstable_placeholder = canonical_value_missing and not fact_id
        source = str(meta.get("source", "") or result.get("source", "") or "").strip()
        explicit_status = str(meta.get("status", "") or "").strip()
        status = (
            FACT_STATUS_AMBIGUOUS
            if metadata_missing
            else FACT_STATUS_NOT_FOUND
            if canonical_value_missing
            else normalize_fact_status(explicit_status)
            if explicit_status
            else FACT_STATUS_FOUND
            if value
            else FACT_STATUS_NOT_FOUND
        )
        # A top-k document without a canonical fact value is not evidence that
        # the requirement is absent, even when its lexical text looks relevant.
        search_exhaustive = bool(
            (result or {}).get("search_exhaustive")
            and not canonical_value_missing
        )
        if status == FACT_STATUS_FOUND and value:
            evidence_state = REQUIREMENT_MATCHED
        elif status == FACT_STATUS_AMBIGUOUS:
            evidence_state = REQUIREMENT_AMBIGUOUS
        elif search_exhaustive:
            evidence_state = REQUIREMENT_EXHAUSTIVE_ABSENT
        else:
            evidence_state = REQUIREMENT_UNMATCHED_TOPK

        fact = {
            "content_type": "table_fact",
            "company": str(meta.get("company", "") or "").strip(),
            "fiscal_year": str(meta.get("fiscal_year", "") or "").strip(),
            "index_generation": str(
                meta.get("index_generation", "")
                or (result or {}).get("index_generation", "")
                or ""
            ).strip(),
            "item_name": item_name,
            "time_hint": str(
                meta.get("time_hint", "") or meta.get("period", "") or ""
            ).strip(),
            "value": value,
            "raw_value": raw_value,
            "normalized_value": normalized_value,
            "source": source,
            "table": heading,
            "heading": heading,
            "item_code": str(meta.get("item_code", "") or "").strip(),
            "note_ref": str(meta.get("note_ref", "") or "").strip(),
            "reference": str(
                meta.get("note_ref", "") or meta.get("heading", "") or heading
            ).strip(),
            "subheading": str(meta.get("subheading", "") or "").strip(),
            "reporting_basis": fact_reporting_basis(meta, evidence_text),
            "value_type": str(meta.get("value_type", "") or "").strip(),
            "unit": str(meta.get("unit", "") or "").strip(),
            "status": status,
            "evidence_state": evidence_state,
            "search_exhaustive": search_exhaustive,
            "evidence_text": evidence_text,
            **(
                {"message": "Retrieved document is missing required metadata."}
                if metadata_missing
                else {
                    "message": (
                        "Retrieved placeholder has no stable fact ID or "
                        "canonical value."
                    )
                }
                if unstable_placeholder
                else {
                    "message": "Retrieved fact is missing a canonical value."
                }
                if canonical_value_missing
                else {}
            ),
        }
        for field in _FACT_METADATA_FIELDS:
            if field in fact or not _has_metadata_value(meta.get(field)):
                continue
            fact[field] = meta.get(field)
        facts.append(fact)

    if not facts:
        item_name = normalize_evidence_query(query, table=table_name)
        facts.append(
            {
                "content_type": "table_fact",
                "item_name": item_name,
                "time_hint": "",
                "value": "",
                "source": "",
                "table": table_name,
                "status": FACT_STATUS_NOT_FOUND,
                "evidence_state": REQUIREMENT_UNMATCHED_TOPK,
                "search_exhaustive": False,
                "message": not_found_after_search_message(item_name, table_name),
            }
        )

    return filter_facts_for_query(
        facts,
        table=table_name,
        query=query,
        source=str((result or {}).get("source", "") or "").strip(),
        semantic_requirement=semantic_requirement,
    )


def cache_item_from_result(
    result: dict,
    *,
    table: str,
    query: str,
    tool: str,
    facts: list[dict] | None = None,
) -> dict:
    payload = dict(result or {})
    table_name = normalize_evidence_table(table)
    query_text = str(query or "").strip()
    facts_payload = dedupe_facts(facts if facts is not None else result_to_facts(payload, table=table_name, query=query_text))
    return {
        "tool": str(tool or "").strip(),
        "table": table_name,
        "query": query_text,
        "canonical_query": normalize_evidence_query(query_text, table=table_name),
        "context": str(payload.get("context", "") or ""),
        "source": str(payload.get("source", "") or ""),
        "documents": payload.get("documents", []) if isinstance(payload.get("documents", []), list) else [],
        "metadatas": payload.get("metadatas", []) if isinstance(payload.get("metadatas", []), list) else [],
        "facts": facts_payload,
    }


def observation_text(tool_name: str, item: dict) -> str:
    facts = (item or {}).get("facts", [])
    fact_lines = []
    if isinstance(facts, list):
        for fact in facts[:5]:
            if not isinstance(fact, dict):
                continue
            item_name = str(fact.get("item_name", "") or "").strip()
            subheading = str(fact.get("subheading", "") or "").strip()
            value = str(fact.get("value", "") or "").strip()
            status = normalize_fact_status(fact.get("status", FACT_STATUS_FOUND))
            if status == FACT_STATUS_NOT_FOUND:
                message = str(fact.get("message", "") or "").strip()
                fact_lines.append(message or f"Không tìm thấy {item_name}.")
            elif item_name or value:
                # Prefix the parent/section context so the agent can tell apart
                # flattened labels (e.g. "Số cuối kỳ | Cộng") across sections.
                label = f"{subheading} — {item_name}" if subheading else item_name
                fact_lines.append(f"{label}: {value}".strip(": "))

    if fact_lines:
        context = "\n".join(fact_lines)
    else:
        context = str((item or {}).get("context", "") or "")

    return (
        f"[{tool_name} source={(item or {}).get('source', '')} "
        f"table={(item or {}).get('table', '')} query={(item or {}).get('query', '')}]\n"
        f"{context[:1200] if context else '<EMPTY_CONTEXT>'}"
    )


def merge_worker_fact_payload(previous: dict, current: dict) -> dict:
    prev = previous if isinstance(previous, dict) else {}
    curr = current if isinstance(current, dict) else {}
    table = str(curr.get("table", "") or prev.get("table", "") or "").strip()
    return {
        "table": table,
        "facts": dedupe_facts(list(prev.get("facts", []) or []) + list(curr.get("facts", []) or [])),
    }
