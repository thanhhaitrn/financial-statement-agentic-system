"""Build a shared evidence pack from router evidence_plan before analysis runs."""
# Code note: Evidence executor replaces LLM retrieval workers with deterministic scoped retrieval and shared cache.

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import re
import time
from dataclasses import asdict, dataclass, field
from hashlib import sha256
from typing import Any, Callable

from agents.agent_registry import is_analysis_agent
from config.runtime_policy import DEFAULT_POLICY, active_policy
from evaluation.narrative_semantics import (
    narrative_concept_atoms,
    narrative_evidence_surface,
    normalize_narrative_text,
)
from graph.dispatch_nodes import prepare_analysis_dispatch_state
from graph.logger import make_debug_log, make_log
from schemas.requirements import (
    FACT_STATUS_FOUND,
    REQUIREMENT_EXHAUSTIVE_ABSENT,
    REQUIREMENT_MATCHED,
    REQUIREMENT_UNMATCHED_TOPK,
    requirement_name_matches_fact,
    requirement_evidence_state,
)
from schemas.web_evidence import WebEvidence, WebEvidenceRequest
from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
)
from tools.evidence import (
    cache_item_from_result,
    dedupe_facts,
    evidence_cache_key,
    get_runtime_cache_item,
    merge_worker_fact_payload,
    narrative_atom_gap_retry_query,
    narrative_requirement_evidence_state,
    normalize_evidence_table,
    normalize_evidence_query,
    not_found_fact,
    filter_facts_for_query,
    result_to_facts,
    set_runtime_cache_item,
)
from tools.query_routing import (
    fact_sibling_group_key,
    fact_slot_score,
    parse_query_slots,
    route_candidates,
    targeted_retry_query,
)
from tools.tool_runner import get_collection
from tools.tools import get_related_info, needs_full_schedule
from common import dedupe_keep_order as _dedupe_keep_order


# Defaults validated on batch apec q181-210 v3 (2026-07-19): raising 5->10/12
# lifted context_recall 0.545->0.609 AND context_precision 0.466->0.528 — the
# gold rows previously cut at rank 6-15 (entity/per-class note rows) carry both.
_EVIDENCE_POLICY = DEFAULT_POLICY.evidence
EVIDENCE_FACTS_LIMIT = _EVIDENCE_POLICY.default_facts_limit
HARD_ANALYSIS_MAIN_FACTS_LIMIT = _EVIDENCE_POLICY.hard_analysis_main_facts_limit
NOTE_FACTS_LIMIT = _EVIDENCE_POLICY.note_facts_limit
# Compatibility aliases share one canonical value; retrieval and LLM dispatch
# must never drift to independent 5/10-fact defaults again.
NOTE_EVIDENCE_FACTS_LIMIT = NOTE_FACTS_LIMIT
NOTE_LLM_FACTS_LIMIT = NOTE_FACTS_LIMIT
REPORT_SECTION_FACTS_LIMIT = _EVIDENCE_POLICY.report_section_facts_limit
# List / superlative / per-entity questions need EVERY row of a note schedule
# (per-project receivables, per-borrower loans, ~20 rows) in the evidence pack —
# the default 5-fact cap structurally zeroes their context_recall. Matches the
# retrieval-side _SCHEDULE_LIMIT in tools/tools.py.
SCHEDULE_FACTS_LIMIT = _EVIDENCE_POLICY.schedule_facts_limit
# Main-statement routes on schedule questions carry a cross-table note slice
# (e.g. the 4-class × 2-value V.9 block), not a whole 24-row schedule — a
# tighter cap keeps precision while the NOTE route holds the full schedule.
SCHEDULE_MAIN_FACTS_LIMIT = _EVIDENCE_POLICY.schedule_main_facts_limit
NOTE_REF_FACTS_SCAN_LIMIT = _EVIDENCE_POLICY.note_ref_scan_limit
NOTE_REF_ENRICHMENT_LIMIT = _EVIDENCE_POLICY.note_ref_enrichment_limit
EVIDENCE_VALUE_PREVIEW_LIMIT = DEFAULT_POLICY.observability.value_preview_limit
EVIDENCE_HINT_PREVIEW_LIMIT = DEFAULT_POLICY.observability.hint_preview_limit
WEB_RESULT_KEY = "WEB"
NOTE_REF_SCOPE = "note_ref"
FACT_ROUTE_METADATA_FIELDS = (
    "time_hint",
    "period",
    "period_label",
    "unit",
    "value_type",
    "source",
    "operation",
    "operand_role",
    "coverage_template",
    "required_legs",
)
QUERY_ROUTE_METADATA_FIELDS = (
    *FACT_ROUTE_METADATA_FIELDS,
    "web_intent",
    "operands",
    "period_role",
    "reporting_basis",
    "scope_label",
    "counterparty",
    "transaction_type",
    "movement_type",
    "geography",
    "policy_topic",
    "section_key",
)
WEB_UNSUPPORTED_MESSAGE = (
    "Web retrieval is not configured for this runtime; no external evidence was retrieved."
)
EVIDENCE_LEDGER_SCHEMA_VERSION = 1
# Retrieval is I/O-bound (embedding/Qdrant calls).  Keep this deliberately small:
# cloud embedding providers often queue aggressively, while four concurrent chains
# remove most of the serial latency without creating an unbounded request burst.
EVIDENCE_MAX_CONCURRENCY = _EVIDENCE_POLICY.max_concurrency


def _evidence_max_concurrency() -> int:
    """Honour the policy activated for this run, not the import-time default."""

    return active_policy().evidence.max_concurrency
_FACT_SCORE_FIELDS = (
    "rerank_score",
    "retrieval_score",
    "similarity_score",
    "score",
    "distance",
)


@dataclass
class _EvidenceChainResult:
    """All side effects produced by one retrieval chain, merged by the caller.

    Worker threads never mutate graph state, trace lists, cache dictionaries or
    ledgers.  Keeping every artifact local also lets the caller merge completed
    futures in plan order, independent of wall-clock completion order.
    """

    cache_updates: dict[str, dict] = field(default_factory=dict)
    evidence_items: list[dict] = field(default_factory=list)
    worker_results: dict[str, dict] = field(default_factory=dict)
    prompt_worker_results: dict[str, dict] = field(default_factory=dict)
    trace: list[dict] = field(default_factory=list)
    ledger_entries: list[dict[str, Any]] = field(default_factory=list)
    retrieval_calls: int = 0
    targeted_retries: int = 0
    cache_hits: int = 0
    web_unsupported: int = 0
    web_refreshes: int = 0
    web_stale: int = 0
    web_errors: int = 0


def _cache_log_id(cache_key: str) -> str:
    """Return a short, stable identifier without exposing the cache payload."""

    digest = sha256(str(cache_key or "").encode("utf-8")).hexdigest()[:12]
    return f"ec_{digest}"


def _table_chain_cache_mode(
    matching_query: str,
    *,
    search_query: str,
    structured_slots: bool,
) -> str:
    """Separate backend-identical searches with different fact requirements.

    Cached table results contain requirement-filtered facts, so search_query alone
    is not a safe identity: two requirements can deliberately share one broadened
    search phrase.  The short digest keeps cache keys compact while preserving exact
    duplicate-chain deduplication.
    """

    normalized_matching = normalize_evidence_query(matching_query)
    normalized_search = normalize_evidence_query(search_query)
    if structured_slots and normalized_matching == normalized_search:
        # Preserve evidence -> analysis-tool cache reuse for the ordinary scoped
        # path.  Only requirement-materialization variants need a distinct mode.
        return "table"

    identity = "|".join(
        [
            normalized_matching,
            normalized_search,
            f"structured_slots={int(bool(structured_slots))}",
        ]
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"table_chain:{digest}"


def _append_debug_log(trace: list[dict], state: dict, event: str, **data: Any) -> None:
    """Append verbose lifecycle details only for explicitly enabled debug traces."""

    entry = make_debug_log(state, event, **data)
    if entry is not None:
        trace.append(entry)


def _trace_details(state: dict, **data: Any) -> dict:
    """Expose retrieval payload details only in explicit debug traces."""

    if not bool((state or {}).get("debug_trace", False)):
        return {}
    return data


def _ledger_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _ledger_json_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_ledger_json_value(item) for item in value]
    return value


def _parsed_query_slots_payload(query: str) -> dict[str, Any]:
    """Return every typed slot in a JSON-ready, audit-stable shape."""

    return _ledger_json_value(asdict(parse_query_slots(str(query or ""))))


def _route_candidates_payload(
    query: str,
    *,
    selected_table: str,
) -> list[dict[str, Any]]:
    return [
        {
            "rank": rank,
            "table": candidate.table,
            "confidence": candidate.confidence,
            "reason": candidate.reason,
            "selected": candidate.table == selected_table,
        }
        for rank, candidate in enumerate(route_candidates(query), start=1)
    ]


def _selected_facts_payload(facts: list[dict]) -> list[dict[str, Any]]:
    """Persist final evidence order and only scores already carried by a fact."""

    selected = []
    for rank, fact in enumerate(facts or [], start=1):
        if not isinstance(fact, dict):
            continue
        fact_id = str(fact.get("fact_id", "") or "").strip()
        if not fact_id:
            continue
        item: dict[str, Any] = {
            "fact_id": fact_id,
            "rank": rank,
        }
        for key in ("status", "evidence_state", "source"):
            value = fact.get(key)
            if value not in ("", None):
                item[key] = value
        for key in _FACT_SCORE_FIELDS:
            value = fact.get(key)
            if value not in ("", None):
                item[key] = value
        for key in ("score_query", "score_intent", "retrieval_origin"):
            value = fact.get(key)
            if value not in ("", None):
                item[key] = value
        selected.append(item)
    return selected


def _retrieval_ledger_entry(
    *,
    requirement: str,
    table: str,
    route_query: str,
    matching_query: str,
    search_query: str,
    retrieval_intent: str,
    route_metadata: dict,
    state_before_retry: str,
    state_after_retry: str,
    retry_performed: bool,
    retry_query: str,
    cache_hit: bool,
    facts: list[dict],
) -> dict[str, Any]:
    candidates = _route_candidates_payload(
        route_query,
        selected_table=table,
    )
    selected_route: dict[str, Any] = {"table": table}
    matched_route = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("selected") is True
        ),
        None,
    )
    if matched_route:
        selected_route.update(
            {
                "confidence": matched_route["confidence"],
                "reason": matched_route["reason"],
            }
        )
    if route_metadata.get("route_confidence") not in ("", None):
        selected_route["confidence"] = route_metadata["route_confidence"]
    if route_metadata.get("route_reason") not in ("", None):
        selected_route["reason"] = route_metadata["route_reason"]

    return {
        "kind": "retrieval_requirement",
        "requirement": requirement,
        "table": table,
        "route_query": route_query,
        "route_candidates": candidates,
        "selected_route": selected_route,
        "parsed_query_slots": _parsed_query_slots_payload(
            retrieval_intent or matching_query
        ),
        "parsed_requirement_slots": _parsed_query_slots_payload(matching_query),
        "matching_query": matching_query,
        "search_query": search_query,
        "retrieval_intent": retrieval_intent,
        "requirement_state": {
            "before_retry": state_before_retry,
            "after_retry": state_after_retry,
        },
        "targeted_retry": {
            "performed": bool(retry_performed),
            "query": str(retry_query or "").strip(),
        },
        "cache_hit": bool(cache_hit),
        "selected_facts": _selected_facts_payload(facts),
    }


def _merge_evidence_ledger(
    existing: Any,
    retrieval_entries: list[dict[str, Any]],
) -> dict[str, Any]:
    existing_entries = (
        list(existing.get("entries", []) or [])
        if isinstance(existing, dict)
        and isinstance(existing.get("entries", []), list)
        else []
    )
    return {
        "schema_version": EVIDENCE_LEDGER_SCHEMA_VERSION,
        "entries": [*existing_entries, *retrieval_entries],
    }

def _needby_values(item: dict) -> list[str]:
    if not isinstance(item, dict):
        return []
    raw = item.get("needby")
    if raw is None:
        raw = item.get("needed_by")
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = []
    return _dedupe_keep_order(
        [
            str(agent).strip()
            for agent in values
            if is_analysis_agent(str(agent).strip())
        ]
    )


def _evidence_item_queries(item: dict) -> list[str]:
    if not isinstance(item, dict):
        return []

    queries = []
    query = str(item.get("query", "") or "").strip()
    if query:
        queries.append(query)

    value = item.get("queries")
    if isinstance(value, (list, tuple, set)):
        queries.extend(str(query).strip() for query in value if str(query).strip())
    elif str(value or "").strip():
        queries.append(str(value).strip())

    return _dedupe_keep_order(queries)


def _evidence_query_metadata(item: dict, map_key: str, scalar_key: str, query: str) -> str:
    values = item.get(map_key)
    if isinstance(values, dict):
        direct = str(values.get(query, "") or "").strip()
        if direct:
            return direct
    return str(item.get(scalar_key, "") or "").strip()


def _evidence_route_metadata(item: dict, query: str) -> dict:
    metadata = {}
    per_query = item.get("query_metadata", {}) if isinstance(item, dict) else {}
    if isinstance(per_query, dict) and isinstance(per_query.get(query), dict):
        for key in QUERY_ROUTE_METADATA_FIELDS:
            value = per_query[query].get(key)
            if value not in ("", None, [], {}):
                metadata[key] = value
    for key in QUERY_ROUTE_METADATA_FIELDS:
        value = item.get(key) if isinstance(item, dict) else None
        if metadata.get(key) in ("", None, [], {}) and value not in ("", None, [], {}):
            metadata[key] = value
    return metadata


def _compact_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _compact_fact_for_prompt(fact: dict) -> dict:
    if not isinstance(fact, dict):
        return {}

    compact = {}
    for key in (
        "content_type",
        "source_kind",
        "content_hash",
        "table",
        "fiscal_year",
        "index_generation",
        "fact_id",
        "section_path",
        "block_id",
        "item_name",
        "row_label",
        "column_label",
        "metric_label",
        "entity_label",
        "scope_label",
        "counterparty",
        "transaction_type",
        "movement_type",
        "geography",
        "policy_topic",
        "section_key",
        "time_hint",
        "period",
        "period_label",
        "period_role",
        "reporting_basis",
        "unit",
        "value_type",
        "aggregation_level",
        "value_kind",
        "value",
        "parsed_value",
        "source",
        "source_url",
        "publisher",
        "published_at",
        "retrieved_at",
        "source_page",
        "status",
        "evidence_state",
        "search_exhaustive",
        "message",
        "retrieval_status",
        "evidence_query",
        "evidence_queries",
        "operation",
        "operand_role",
        "coverage_template",
        "required_legs",
        "source_table",
        "source_item",
        "evidence_role",
        "linked_parent_fact_id",
        "linked_parent_item",
        "linked_parent_value",
        "linked_parent_period_label",
        "linked_parent_aggregation_level",
        "needby",
        "note_ref",
        "note_number",
        "note_title",
        "subheading",
    ):
        value = fact.get(key)
        if value in ("", None, [], {}):
            continue
        if key == "search_exhaustive" and not bool(value):
            continue
        if key in {"evidence_queries", "needby", "required_legs"}:
            values = _dedupe_keep_order(value if isinstance(value, (list, tuple, set)) else [value])
            if values:
                compact[key] = values
            continue
        limit = EVIDENCE_VALUE_PREVIEW_LIMIT if key == "value" else EVIDENCE_HINT_PREVIEW_LIMIT
        if key in {
            "item_name",
            "source",
            "source_url",
            "publisher",
            "published_at",
            "retrieved_at",
            "table",
            "time_hint",
            "period",
            "unit",
            "value_type",
            "status",
            "evidence_state",
            "retrieval_status",
            "content_type",
            "fact_id",
            "block_id",
            "row_label",
            "column_label",
            "metric_label",
            "entity_label",
            "scope_label",
            "counterparty",
            "transaction_type",
            "movement_type",
            "geography",
            "policy_topic",
            "section_key",
            "period_label",
            "period_role",
            "reporting_basis",
            "aggregation_level",
            "value_kind",
            "source_page",
            "evidence_role",
            "linked_parent_fact_id",
            "linked_parent_item",
            "linked_parent_period_label",
            "linked_parent_aggregation_level",
        }:
            limit = 120
        compact[key] = _compact_text(value, limit=limit)
    return compact


def _annotate_facts_for_route(
    facts: list[dict],
    *,
    needby: list[str] | None = None,
    evidence_query: str = "",
    source_table: str = "",
    source_item: str = "",
    route_metadata: dict | None = None,
) -> list[dict]:
    routed_facts = []
    needed_by = _dedupe_keep_order(needby or [])
    query_text = str(evidence_query or "").strip()
    source_table_text = str(source_table or "").strip()
    source_item_text = str(source_item or "").strip()
    metadata = route_metadata if isinstance(route_metadata, dict) else {}

    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        payload = dict(fact)
        table_name = normalize_evidence_table(payload.get("table", ""))
        if table_name in {TABLE_BS, TABLE_IS, TABLE_CF}:
            payload.setdefault("evidence_role", "primary_statement")
        if needed_by:
            payload["needby"] = _dedupe_keep_order(
                _needby_values(payload) + needed_by
            )
        if query_text:
            payload["evidence_query"] = query_text
        if source_table_text:
            payload["source_table"] = source_table_text
        if source_item_text:
            payload["source_item"] = source_item_text
        for key in FACT_ROUTE_METADATA_FIELDS:
            if payload.get(key) in ("", None, [], {}) and metadata.get(key) not in ("", None, [], {}):
                payload[key] = metadata.get(key)
        routed_facts.append(payload)

    return routed_facts


def _compact_facts_for_prompt(facts: Any) -> list[dict]:
    if not isinstance(facts, list):
        return []
    return [
        compact
        for compact in (_compact_fact_for_prompt(fact) for fact in facts)
        if compact
    ]


def _compact_worker_result_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        return payload
    if not isinstance(payload.get("facts"), list):
        return payload
    compact = {
        "table": _compact_text(payload.get("table", ""), limit=120),
        "facts": _compact_facts_for_prompt(payload.get("facts", [])),
    }
    for key in (
        "query",
        "canonical_query",
        "search_query",
        "evidence_query",
        "source",
        "status",
        "retrieval_status",
        "message",
        "time_hint",
        "period",
        "period_role",
        "unit",
        "value_type",
        "operation",
        "operand_role",
        "coverage_template",
        "required_legs",
    ):
        value = payload.get(key)
        if value not in ("", None, [], {}):
            if key == "required_legs":
                compact[key] = _dedupe_keep_order(
                    value if isinstance(value, (list, tuple, set)) else [value]
                )
            else:
                compact[key] = _compact_text(
                    value, limit=EVIDENCE_HINT_PREVIEW_LIMIT
                )
    return {key: value for key, value in compact.items() if value not in ("", None, [], {})}


def _compact_worker_results(results: dict) -> dict:
    output = {}
    for key, payload in (results or {}).items():
        output[key] = _compact_worker_result_payload(payload)
    return output


def _compact_evidence_targets(targets: list[dict]) -> list[dict]:
    compact_targets = []
    for target in targets or []:
        if not isinstance(target, dict):
            continue
        payload = {
            "mode": str(target.get("mode", "") or "").strip(),
            "table": str(target.get("table", "") or "").strip(),
            "requirements": _dedupe_keep_order(target.get("requirements", []) or [])[:8],
        }
        compact_targets.append(
            {
                key: value
                for key, value in payload.items()
                if value not in ("", None, [], {})
            }
        )
    return compact_targets


def _compact_evidence_item(item: dict) -> dict:
    payload = {
        "scope": str(item.get("scope", "") or "").strip(),
        "table": str(item.get("table", "") or "").strip(),
        "query": _compact_text(item.get("query", ""), limit=120),
        "note_ref": _compact_text(item.get("note_ref", ""), limit=40),
        "source_table": _compact_text(item.get("source_table", ""), limit=120),
        "source_item": _compact_text(item.get("source_item", ""), limit=120),
        "needby": _needby_values(item),
        "facts_n": int(item.get("facts_n", 0) or 0),
        "source": _compact_text(item.get("source", ""), limit=120),
        "status": _compact_text(item.get("status", ""), limit=80),
        "retrieval_status": _compact_text(item.get("retrieval_status", ""), limit=80),
        "message": _compact_text(item.get("message", ""), limit=EVIDENCE_HINT_PREVIEW_LIMIT),
        "time_hint": _compact_text(item.get("time_hint", ""), limit=120),
        "period": _compact_text(item.get("period", ""), limit=120),
        "unit": _compact_text(item.get("unit", ""), limit=80),
        "value_type": _compact_text(item.get("value_type", ""), limit=120),
        "operation": _compact_text(item.get("operation", ""), limit=40),
        "operand_role": _compact_text(item.get("operand_role", ""), limit=40),
        "coverage_template": _compact_text(
            item.get("coverage_template", ""), limit=40
        ),
        "required_legs": _dedupe_keep_order(
            item.get("required_legs", [])
            if isinstance(item.get("required_legs"), (list, tuple, set))
            else [item.get("required_legs")]
        ),
        "evidence_query": _compact_text(item.get("evidence_query", ""), limit=120),
        "cache_hit": bool(item.get("cache_hit", False)),
        "targeted_retry": bool(item.get("targeted_retry", False)),
        "facts_preview": _compact_facts_for_prompt(item.get("facts_preview", []) or []),
    }
    return {
        key: value
        for key, value in payload.items()
        if value not in ("", None, [], {})
    }


def _compact_evidence_items(items: list[dict]) -> list[dict]:
    return [
        compact
        for compact in (_compact_evidence_item(item) for item in items or [])
        if compact
    ]


def _normalized_retrieval_targets(worker_plan: dict) -> list[dict]:
    evidence_plan = worker_plan.get("evidence_plan", []) or []
    allow_web = bool(worker_plan.get("need_web", False))
    if evidence_plan:
        grouped: dict[tuple[str, str], dict] = {}
        order: list[tuple[str, str]] = []
        for item in evidence_plan:
            if not isinstance(item, dict):
                continue
            table = normalize_evidence_table(item.get("table", ""))
            mode = "table" if table else "web"
            if mode == "web" and not allow_web:
                continue
            key = (mode, table)
            if key not in grouped:
                grouped[key] = {
                    "mode": mode,
                    "table": table,
                    "requirements": [],
                    "needby_by_query": {},
                    "search_queries": {},
                    "canonical_queries": {},
                    "metadata_by_query": {},
                    "source": str(item.get("source", "") or "").strip(),
                    "evidence_items": [],
                }
                order.append(key)
            for raw_query in _evidence_item_queries(item):
                canonical_query = normalize_evidence_query(
                    _evidence_query_metadata(item, "canonical_queries", "canonical_query", raw_query)
                    or raw_query,
                    table=table,
                )
                query = raw_query or canonical_query
                if not query and mode != "web":
                    continue
                if query:
                    grouped[key]["requirements"].append(query)
                    grouped[key]["needby_by_query"][query] = _dedupe_keep_order(
                        list(grouped[key]["needby_by_query"].get(query, []) or [])
                        + _needby_values(item)
                    )
                    if canonical_query:
                        grouped[key]["canonical_queries"][query] = canonical_query
                    search_query = (
                        _evidence_query_metadata(item, "search_queries", "search_query", raw_query)
                        or str(
                            item.get("original_query")
                            or item.get("raw_query")
                            or raw_query
                            or ""
                        ).strip()
                    )
                    if search_query:
                        grouped[key]["search_queries"][query] = search_query
                    metadata = grouped[key]["metadata_by_query"].setdefault(query, {})
                    route_metadata = _evidence_route_metadata(item, raw_query)
                    for metadata_key in QUERY_ROUTE_METADATA_FIELDS:
                        metadata_value = route_metadata.get(metadata_key)
                        if (
                            metadata.get(metadata_key) in ("", None, [], {})
                            and metadata_value not in ("", None, [], {})
                        ):
                            metadata[metadata_key] = metadata_value
            grouped[key]["evidence_items"].append(dict(item))

        output = []
        for key in order:
            target = grouped[key]
            target["requirements"] = _dedupe_keep_order(target.get("requirements", []) or [])
            if target["requirements"] or target.get("mode") == "web":
                output.append(target)
        return output

    targets = []
    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue
        table = normalize_evidence_table(target.get("table", ""))
        mode = "table" if table else "web"
        if mode == "web" and not allow_web:
            continue
        requirements = _dedupe_keep_order(target.get("requirements", []) or [])
        if not requirements and mode != "web":
            continue
        targets.append(
            {
                "mode": mode,
                "table": table,
                "requirements": requirements,
                "source": str(target.get("source", "") or "").strip(),
                "metadata_by_query": {
                    query: {
                        key: target.get(key)
                        for key in QUERY_ROUTE_METADATA_FIELDS
                        if target.get(key) not in ("", None, [], {})
                    }
                    for query in requirements
                },
            }
        )
    return targets


def _planned_analysis_agents(worker_plan: dict) -> list[str]:
    agents = []
    for target in (worker_plan.get("analysis_plan", []) or []):
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if agent and is_analysis_agent(agent) and agent not in agents:
            agents.append(agent)

    for target in (worker_plan.get("targets", []) or []):
        if not isinstance(target, dict):
            continue
        agent = str(target.get("agent", "") or "").strip()
        if agent and is_analysis_agent(agent) and agent not in agents:
            agents.append(agent)
    return agents


def _target_metadata_for_query(target: dict, query: str) -> dict:
    metadata_by_query = target.get("metadata_by_query", {}) if isinstance(target, dict) else {}
    if not isinstance(metadata_by_query, dict):
        return {}
    direct = metadata_by_query.get(query)
    if isinstance(direct, dict):
        return dict(direct)

    merged = {}
    for metadata in metadata_by_query.values():
        if not isinstance(metadata, dict):
            continue
        for key in QUERY_ROUTE_METADATA_FIELDS:
            if merged.get(key) in ("", None, [], {}) and metadata.get(key) not in ("", None, [], {}):
                merged[key] = metadata.get(key)
    return merged


def _difficulty_level_from_state(state: dict, worker_plan: dict) -> str:
    for source in (state.get("planner_plan", {}), worker_plan):
        if not isinstance(source, dict):
            continue
        difficulty = str(source.get("difficulty_level", "") or "").strip().lower()
        if difficulty in {"easy", "medium", "hard"}:
            return difficulty
    return ""


def _facts_limit_for_table(state: dict, worker_plan: dict, table: str) -> int:
    table_name = normalize_evidence_table(table)
    # Report-section prose has its own contract. It must never inherit either
    # the wider NOTE cap or the schedule widening used by financial statements.
    if table_name == TABLE_REPORT_SECTION:
        return REPORT_SECTION_FACTS_LIMIT
    # Any-table, not just NOTE: get_related_info already widens its rerank cut to
    # _SCHEDULE_LIMIT for schedule questions on main-table routes, and a per-class
    # note schedule (V.9) answering a BS-routed question would otherwise be sliced
    # back to EVIDENCE_FACTS_LIMIT by result_to_facts.
    if needs_full_schedule(str((state or {}).get("user_query", "") or "")):
        if table_name == TABLE_NOTE:
            return SCHEDULE_FACTS_LIMIT
        return SCHEDULE_MAIN_FACTS_LIMIT
    if table_name == TABLE_NOTE:
        return NOTE_EVIDENCE_FACTS_LIMIT
    return EVIDENCE_FACTS_LIMIT


def _effective_analysis_agents(state: dict, worker_plan: dict, agents: list[str]) -> list[str]:
    difficulty = _difficulty_level_from_state(state, worker_plan)
    if difficulty in {"easy", "medium"}:
        return []
    return agents


def _is_statement_only_broad_profitability_run(
    state: dict,
    worker_plan: dict,
    planned_agents: list[str],
) -> bool:
    """Identify the deterministic main-statement profitability contract."""

    if "agent_profitability" not in set(planned_agents or []):
        return False
    query = normalize_narrative_text((state or {}).get("user_query", ""))
    if not any(
        marker in query
        for marker in (
            "kha nang sinh loi",
            "hieu qua sinh loi",
            "profitability",
        )
    ):
        return False
    response_mode = ""
    for source in ((state or {}).get("planner_plan", {}), worker_plan):
        if not isinstance(source, dict):
            continue
        value = str(source.get("response_mode", "") or "").strip().lower()
        if value:
            response_mode = value
            break
    if response_mode not in {"", "extractive"}:
        return False
    return not any(
        normalize_evidence_table(item.get("table", ""))
        in {TABLE_NOTE, TABLE_REPORT_SECTION}
        for item in (worker_plan.get("evidence_plan", []) or [])
        if isinstance(item, dict)
    )


def _should_fetch_note_ref_context(state: dict, worker_plan: dict, planned_agents: list[str]) -> bool:
    difficulty = _difficulty_level_from_state(state, worker_plan)
    if difficulty in {"easy", "medium"}:
        return False
    if (
        difficulty == "hard"
        and _is_statement_only_broad_profitability_run(
            state,
            worker_plan,
            planned_agents,
        )
    ):
        # Main-statement note_ref values are provenance for this deterministic
        # contract, not extra requirements. Expanding every retrieved distractor
        # previously added ten NOTE calls without supplying any ratio operand.
        return False
    if difficulty == "hard":
        return True
    return bool(planned_agents)


def _web_result_to_payload(result: dict, query: str) -> dict:
    context = str((result or {}).get("context", "") or "").strip()
    retrieval_status = str((result or {}).get("retrieval_status", "") or "").strip()
    if not retrieval_status:
        retrieval_status = "found" if context else "unsupported"
    message = str((result or {}).get("message", "") or "").strip()
    facts = []
    for raw_item in (result or {}).get("evidence", []) or []:
        try:
            item = raw_item if isinstance(raw_item, WebEvidence) else WebEvidence.model_validate(raw_item)
        except Exception:
            continue
        facts.append(item.as_fact())
    if not facts and context:
        facts.append(
            {
                "content_type": "web_fact",
                "item_name": query,
                "time_hint": "",
                "value": context,
                "source": str((result or {}).get("source", "") or "").strip(),
                "table": "",
                "status": "found",
                "retrieval_status": retrieval_status,
                "message": message,
                "evidence_text": context,
            }
        )
    if not facts and not message:
        message = WEB_UNSUPPORTED_MESSAGE
    if not facts:
        facts = [
            {
                "content_type": "web_fact",
                "item_name": query,
                "time_hint": "",
                "value": "",
                "source": str((result or {}).get("source", "") or "").strip(),
                "table": "",
                "status": "not_found_after_search",
                "retrieval_status": retrieval_status,
                "message": message,
                "evidence_text": "",
            }
        ]
    return {
        "table": "",
        "status": "found" if any(fact.get("value") for fact in facts) else "not_found_after_search",
        "retrieval_status": retrieval_status,
        "message": message,
        "facts": facts,
    }


def _compact_log_text(value: Any, *, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _limit_evidence_facts(facts: Any, *, limit: int = EVIDENCE_FACTS_LIMIT) -> list[dict]:
    if not isinstance(facts, list):
        return []
    return [
        fact
        for fact in facts
        if isinstance(fact, dict)
    ][:limit]


def _limit_evidence_facts_for_table(
    table: str,
    facts: Any,
    *,
    state: dict | None = None,
    worker_plan: dict | None = None,
) -> list[dict]:
    limit = _facts_limit_for_table(state or {}, worker_plan or {}, table)
    return _limit_evidence_facts(facts, limit=limit)


def _facts_log_preview(facts: Any, *, limit: int = EVIDENCE_FACTS_LIMIT) -> list[dict]:
    if not isinstance(facts, list):
        return []

    preview = []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        preview.append(
            {
                "table": _compact_log_text(fact.get("table", ""), limit=80),
                "item_name": _compact_log_text(fact.get("item_name", ""), limit=120),
                "note_ref": _compact_log_text(fact.get("note_ref", ""), limit=40),
                "time_hint": _compact_log_text(fact.get("time_hint", ""), limit=80),
                "value": _compact_log_text(fact.get("value", ""), limit=160),
                "status": _compact_log_text(fact.get("status", ""), limit=60),
                "source": _compact_log_text(fact.get("source", ""), limit=120),
            }
        )
        if len(preview) >= limit:
            break
    return preview


def _merge_worker_results(existing: dict, current: dict) -> dict:
    merged = dict(existing or {})
    for result_key, payload in (current or {}).items():
        merged[result_key] = merge_worker_fact_payload(merged.get(result_key, {}), payload)
    return merged


def _llm_facts_limit_for_table(state: dict, worker_plan: dict, table: str) -> int:
    table_name = normalize_evidence_table(table)
    if table_name == TABLE_REPORT_SECTION:
        return REPORT_SECTION_FACTS_LIMIT
    if needs_full_schedule(str((state or {}).get("user_query", "") or "")):
        # ragas_facts_by_table (RAGAS retrieved_contexts) flows through this cap
        # too, so schedule questions must keep the whole per-entity schedule.
        if table_name == TABLE_NOTE:
            return SCHEDULE_FACTS_LIMIT
        return SCHEDULE_MAIN_FACTS_LIMIT
    if table_name == TABLE_NOTE:
        return NOTE_LLM_FACTS_LIMIT
    if (
        table_name in {TABLE_BS, TABLE_IS, TABLE_CF}
        and _difficulty_level_from_state(state, worker_plan) == "hard"
    ):
        return HARD_ANALYSIS_MAIN_FACTS_LIMIT
    return EVIDENCE_FACTS_LIMIT


def _select_route_balanced_facts(facts: list[dict], *, limit: int) -> list[dict]:
    """Reserve prompt space across independent evidence routes.

    Routes are merged by table before dispatch.  A positional slice lets the
    first metric consume the table allowance, even though later routes may be
    equally important to another analysis axis.  Round-robin selection keeps
    one fact from every route first, then its comparison sibling, while the
    audit branch remains untouched.
    """

    candidates = [fact for fact in facts or [] if isinstance(fact, dict)]
    if limit <= 0 or len(candidates) <= limit:
        return candidates[:limit] if limit > 0 else []

    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for index, fact in enumerate(candidates):
        queries = _fact_evidence_queries(fact)
        key = normalize_evidence_query(queries[0]) if queries else f"__unscoped_{index}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(fact)

    selected: list[dict] = []
    offset = 0
    while len(selected) < limit:
        added = False
        for key in order:
            group = groups[key]
            if offset >= len(group):
                continue
            selected.append(group[offset])
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
        offset += 1
    return selected


_NARRATIVE_QUOTA_STOP_TOKENS = frozenset(
    {
        "bang",
        "bao",
        "cao",
        "cho",
        "cong",
        "cua",
        "duoc",
        "kien",
        "mot",
        "noi",
        "phan",
        "su",
        "thai",
        "theo",
        "thong",
        "trang",
        "truoc",
        "va",
    }
)


def _grounded_premises_for_evidence_cap(
    state: dict,
    worker_plan: dict,
) -> list[str]:
    sources = (
        (state or {}).get("planner_plan", {}),
        worker_plan,
    )
    if not any(
        isinstance(source, dict)
        and str(source.get("response_mode", "") or "").strip().lower()
        == "grounded_interpretation"
        for source in sources
    ):
        return []
    for source in sources:
        if not isinstance(source, dict):
            continue
        premises = _dedupe_keep_order(
            str(item).strip()
            for item in (source.get("premise_requirements", []) or [])
            if str(item).strip()
        )
        if premises:
            return premises
    return []


def _fact_evidence_queries(fact: dict) -> list[str]:
    values = []
    for key in ("evidence_query", "evidence_queries"):
        value = fact.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, (list, tuple, set)):
            values.extend(value)
    return _dedupe_keep_order(
        str(value).strip()
        for value in values
        if str(value).strip()
    )


def _narrative_quota_tokens(value: Any) -> set[str]:
    return {
        token
        for token in re.findall(
            r"[a-z0-9]+",
            normalize_narrative_text(value),
        )
        if len(token) > 2
        and token not in _NARRATIVE_QUOTA_STOP_TOKENS
        and not re.fullmatch(r"(?:19|20)\d{2}", token)
    }


def _is_core_prompt_requirement(
    state: dict,
    query: str,
    *,
    needby: list[str] | None = None,
    route_metadata: dict | None = None,
    premise_scoped: bool = False,
) -> bool:
    """Conservatively identify misses Synth still needs to know about.

    Successful retrievals are always retained. This predicate is only used for
    an unresolved route: analysis operands, grounded premises, and requirements
    that directly overlap the user query may keep one terse diagnostic. A
    speculative sibling route is not allowed to inject a missing-data claim.
    """

    if premise_scoped or _dedupe_keep_order(needby or []):
        return True

    metadata = route_metadata if isinstance(route_metadata, dict) else {}
    if str(metadata.get("operand_role", "") or "").strip() in {
        "numerator",
        "denominator",
    }:
        return True
    if str(metadata.get("operation", "") or "").strip() in {
        "ratio",
        "share",
        "delta",
        "percent_change",
        "compare",
    }:
        return True

    requirement = normalize_narrative_text(query)
    user_query = normalize_narrative_text((state or {}).get("user_query", ""))
    if not requirement or not user_query:
        return False
    if requirement in user_query or user_query in requirement:
        return True

    requirement_tokens = _narrative_quota_tokens(requirement)
    user_tokens = _narrative_quota_tokens(user_query)
    if not requirement_tokens or not user_tokens:
        return False
    overlap_n = len(requirement_tokens.intersection(user_tokens))
    return overlap_n >= min(2, len(requirement_tokens))


def _sanitize_prompt_fact(fact: dict) -> dict:
    """Remove retrieval diagnostics that are not financial evidence."""

    payload = dict(fact or {})
    payload.pop("message", None)
    payload.pop("evidence_text", None)
    return payload


def _complete_multi_period_route_facts(
    requirement: str,
    facts: list[dict],
) -> list[dict]:
    """Return one best complete sibling pair for a typed comparison route.

    Retrieval remains recall-first and the audit ledger keeps its full top-k.
    The analysis payload is capped per table, though, so allowing the first
    route's ten near-matches to consume that cap can evict every later core
    metric. Reserve the smallest complete logical-row group for each matched
    current/previous or opening/closing route before table-level merging.
    """

    requirement_text = str(requirement or "").strip()
    if not requirement_text or needs_full_schedule(requirement_text):
        return []
    slots = parse_query_slots(requirement_text)
    if slots.period != "both" and slots.period_role != "both":
        return []

    matched = [
        fact
        for fact in facts or []
        if isinstance(fact, dict)
        and requirement_name_matches_fact(
            requirement_text,
            fact,
            table=str(fact.get("table", "") or ""),
        )
    ]
    if not matched:
        return []

    groups: dict[tuple[str, ...], list[dict]] = {}
    group_order: list[tuple[str, ...]] = []
    for fact in matched:
        key = fact_sibling_group_key(fact)
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        groups[key].append(fact)

    complete_groups = [
        (index, groups[key])
        for index, key in enumerate(group_order)
        if requirement_evidence_state(
            requirement_text,
            groups[key],
            table=str(groups[key][0].get("table", "") or ""),
        )
        == REQUIREMENT_MATCHED
    ]
    if not complete_groups:
        return []

    _index, selected = max(
        complete_groups,
        key=lambda item: (
            max(
                fact_slot_score(
                    slots,
                    fact,
                    str(fact.get("evidence_text", "") or ""),
                )
                for fact in item[1]
            ),
            -item[0],
        ),
    )
    return selected


def _prompt_facts_for_route(
    facts: list[dict],
    *,
    requirement: str,
    requirement_state: str,
    core_requirement: bool,
) -> list[dict]:
    """Project one retrieval route into a fact-only Synth payload.

    The caller retains the unmodified facts in RAGAS state and the evidence
    ledger. Top-k misses are not evidence of absence. For a core requirement we
    preserve valid partial facts and, only when none exist, one message-free
    placeholder so Synth can fail closed. Non-core misses contribute nothing.
    """

    state_text = str(requirement_state or "").strip().lower()
    candidates = [fact for fact in facts or [] if isinstance(fact, dict)]
    usable = [
        _sanitize_prompt_fact(fact)
        for fact in candidates
        if str(fact.get("status", FACT_STATUS_FOUND) or FACT_STATUS_FOUND)
        .strip()
        .lower()
        == FACT_STATUS_FOUND
        and fact.get("value", "") not in ("", None)
    ]

    if state_text in {"", REQUIREMENT_MATCHED, "found", "completed"}:
        if core_requirement:
            complete_pair = _complete_multi_period_route_facts(
                requirement,
                usable,
            )
            if complete_pair:
                return complete_pair
        return usable

    if state_text == REQUIREMENT_UNMATCHED_TOPK:
        if not core_requirement:
            return []
        matched_partial = [
            fact
            for fact in usable
            if requirement_name_matches_fact(
                requirement,
                fact,
                table=str(fact.get("table", "") or ""),
            )
        ]
        if matched_partial:
            return matched_partial

    if not core_requirement:
        return []

    if state_text not in {
        REQUIREMENT_UNMATCHED_TOPK,
        REQUIREMENT_EXHAUSTIVE_ABSENT,
        "ambiguous",
        "unsupported",
    }:
        return usable

    diagnostic = next(
        (
            _sanitize_prompt_fact(fact)
            for fact in candidates
            if str(fact.get("status", "") or "").strip().lower()
            != FACT_STATUS_FOUND
        ),
        None,
    )
    return [diagnostic] if diagnostic else []


def _drop_redundant_prompt_diagnostics(results: dict) -> dict:
    """Suppress a table-level miss when another route already found the fact."""

    found_facts = [
        fact
        for payload in (results or {}).values()
        if isinstance(payload, dict)
        for fact in (payload.get("facts", []) or [])
        if isinstance(fact, dict)
        and str(fact.get("status", FACT_STATUS_FOUND) or FACT_STATUS_FOUND)
        .strip()
        .lower()
        == FACT_STATUS_FOUND
        and fact.get("value", "") not in ("", None)
    ]
    output = {}
    for result_key, payload in (results or {}).items():
        if not isinstance(payload, dict) or not isinstance(payload.get("facts"), list):
            output[result_key] = payload
            continue

        item = dict(payload)
        kept = []
        for fact in payload.get("facts", []) or []:
            if not isinstance(fact, dict):
                continue
            clean_fact = _sanitize_prompt_fact(fact)
            status = str(
                clean_fact.get("status", FACT_STATUS_FOUND) or FACT_STATUS_FOUND
            ).strip().lower()
            if status == FACT_STATUS_FOUND:
                kept.append(clean_fact)
                continue

            requirement = str(
                clean_fact.get("evidence_query", "")
                or clean_fact.get("item_name", "")
                or ""
            ).strip()
            redundant = bool(
                requirement
                and any(
                    requirement_name_matches_fact(
                        requirement,
                        found,
                        table=str(found.get("table", "") or ""),
                    )
                    for found in found_facts
                )
            )
            if not redundant:
                kept.append(clean_fact)
        if not kept and not is_analysis_agent(str(result_key or "").strip()):
            continue
        item["facts"] = kept
        output[result_key] = item
    return output


def _select_grounded_premise_facts(
    facts: list[dict],
    *,
    premises: list[str],
    limit: int,
) -> list[dict]:
    """Apply a semantic per-premise quota before the shared table cap.

    Retrieval is executed independently for each grounded premise, but the
    payloads are merged by table.  A positional slice after that merge lets the
    first query consume the whole NOTE/REPORT allowance.  Reserve the smallest
    useful set for every premise first, then fill the remaining slots in the
    original retrieval order.
    """

    candidates = [
        fact
        for fact in facts or []
        if isinstance(fact, dict)
    ]
    if limit <= 0 or not candidates:
        return []
    if len(candidates) <= limit or not premises:
        return candidates[:limit]

    normalized_premises = {
        premise: normalize_narrative_text(premise)
        for premise in premises
    }
    route_queries = [
        {
            normalize_narrative_text(query)
            for query in _fact_evidence_queries(fact)
        }
        for fact in candidates
    ]
    fact_surfaces = [
        narrative_evidence_surface(fact)
        for fact in candidates
    ]
    fact_atoms = [
        narrative_concept_atoms(surface)
        for surface in fact_surfaces
    ]
    fact_tokens = [
        _narrative_quota_tokens(surface)
        for surface in fact_surfaces
    ]

    reserved: list[int] = []
    reserved_set: set[int] = set()
    for premise in premises:
        premise_normalized = normalized_premises[premise]
        scoped = [
            index
            for index, queries in enumerate(route_queries)
            if premise_normalized in queries
        ]
        # Existing worker facts may predate route annotations.  Only in that
        # compatibility case consider the full table payload.
        pool = scoped or list(range(len(candidates)))
        required_atoms = narrative_concept_atoms(
            premise,
            requirement=True,
        )
        premise_tokens = _narrative_quota_tokens(premise)
        selected_for_premise: list[int] = []
        remaining_atoms = set(required_atoms)

        # Prefer facts returned for this exact premise. If those cover only
        # part of a multi-atom requirement, a sibling route may still carry a
        # full timeline or another actual event fact. Cross-query fallback is
        # semantic-only: an unrelated route label cannot supply the atom.
        semantic_pools = [
            pool,
            [
                index
                for index in range(len(candidates))
                if index not in pool
            ],
        ]
        for semantic_pool in semantic_pools:
            while remaining_atoms:
                ranked = sorted(
                    (
                        index
                        for index in semantic_pool
                        if remaining_atoms.intersection(fact_atoms[index])
                    ),
                    key=lambda index: (
                        len(
                            remaining_atoms.intersection(
                                fact_atoms[index]
                            )
                        ),
                        len(
                            required_atoms.intersection(
                                fact_atoms[index]
                            )
                        ),
                        len(
                            premise_tokens.intersection(
                                fact_tokens[index]
                            )
                        ),
                        bool(fact_surfaces[index]),
                        -index,
                    ),
                    reverse=True,
                )
                if not ranked:
                    break
                best = ranked[0]
                selected_for_premise.append(best)
                remaining_atoms.difference_update(fact_atoms[best])
            if not remaining_atoms:
                break

        if not selected_for_premise:
            # Narrative topics without a known event atom still receive one
            # scoped slot.  Lexical overlap chooses the most relevant fact;
            # route order is the final deterministic tie-breaker.
            best = max(
                pool,
                key=lambda index: (
                    len(premise_tokens.intersection(fact_tokens[index])),
                    bool(fact_surfaces[index]),
                    -index,
                ),
            )
            selected_for_premise.append(best)

        for index in selected_for_premise:
            if index in reserved_set:
                continue
            reserved.append(index)
            reserved_set.add(index)

    # Grounded plans contain at most a handful of premises, so their semantic
    # reserve fits under the normal 10/12-fact table caps.  Preserve premise
    # order for auditability, then retain the original retrieval ranking.
    selected_indices = [
        *reserved,
        *(
            index
            for index in range(len(candidates))
            if index not in reserved_set
        ),
    ][:limit]
    return [candidates[index] for index in selected_indices]


def _limit_note_facts_for_llm(results: dict, *, state: dict, worker_plan: dict) -> dict:
    output = {}
    grounded_premises = _grounded_premises_for_evidence_cap(
        state,
        worker_plan,
    )
    for result_key, payload in (results or {}).items():
        if not isinstance(payload, dict):
            output[result_key] = payload
            continue
        if is_analysis_agent(str(result_key or "").strip()) or not isinstance(payload.get("facts"), list):
            output[result_key] = payload
            continue

        item = dict(payload)
        table = _result_key_for_table(item.get("table", "") or result_key)
        facts = item.get("facts", [])
        limit = _llm_facts_limit_for_table(state, worker_plan, table)
        item["facts"] = (
            _select_grounded_premise_facts(
                facts,
                premises=grounded_premises,
                limit=limit,
            )
            if grounded_premises
            else _select_route_balanced_facts(facts, limit=limit)
        )
        output[result_key] = item
    return output


def _limit_note_item_previews_for_llm(items: list[dict], *, state: dict, worker_plan: dict) -> list[dict]:
    output = []
    grounded_premises = _grounded_premises_for_evidence_cap(
        state,
        worker_plan,
    )
    if grounded_premises:
        for item in items or []:
            output.append(dict(item) if isinstance(item, dict) else item)

        locations_by_table: dict[
            str,
            list[tuple[int, int, dict]],
        ] = {}
        for item_index, payload in enumerate(output):
            if not isinstance(payload, dict):
                continue
            table = normalize_evidence_table(payload.get("table", ""))
            previews = payload.get("facts_preview", [])
            if not table or not isinstance(previews, list):
                continue
            query = str(
                payload.get("evidence_query", "")
                or payload.get("query", "")
                or ""
            ).strip()
            for preview_index, preview in enumerate(previews):
                if not isinstance(preview, dict):
                    continue
                routed_preview = dict(preview)
                if query:
                    routed_preview["evidence_query"] = query
                locations_by_table.setdefault(table, []).append(
                    (item_index, preview_index, routed_preview)
                )

        selected_locations: set[tuple[int, int]] = set()
        for table, entries in locations_by_table.items():
            limit = _llm_facts_limit_for_table(
                state,
                worker_plan,
                table,
            )
            selected = _select_grounded_premise_facts(
                [entry[2] for entry in entries],
                premises=grounded_premises,
                limit=limit,
            )
            selected_object_ids = {id(fact) for fact in selected}
            selected_locations.update(
                (item_index, preview_index)
                for item_index, preview_index, routed_preview in entries
                if id(routed_preview) in selected_object_ids
            )

        for item_index, payload in enumerate(output):
            if not isinstance(payload, dict):
                continue
            table = normalize_evidence_table(payload.get("table", ""))
            if table not in locations_by_table:
                continue
            previews = payload.get("facts_preview", [])
            if not isinstance(previews, list):
                continue
            payload["facts_preview"] = [
                preview
                for preview_index, preview in enumerate(previews)
                if (item_index, preview_index) in selected_locations
            ]
        return output

    facts_seen_by_table: dict[str, int] = {}

    for item in items or []:
        if not isinstance(item, dict):
            continue

        payload = dict(item)
        table = normalize_evidence_table(payload.get("table", ""))
        previews = payload.get("facts_preview", [])
        if table and isinstance(previews, list):
            limit = _llm_facts_limit_for_table(state, worker_plan, table)
            seen = int(facts_seen_by_table.get(table, 0) or 0)
            remaining = max(limit - seen, 0)
            limited_previews = [
                fact
                for fact in previews
                if isinstance(fact, dict)
            ][:remaining]
            facts_seen_by_table[table] = seen + len(limited_previews)
            payload["facts_preview"] = limited_previews
        output.append(payload)

    return output


def _result_key_for_table(table: str = "", *, mode: str = "table") -> str:
    table_name = normalize_evidence_table(table)
    if str(mode or "").strip().lower() == "web" or not table_name:
        return WEB_RESULT_KEY
    return table_name


def _row_label_from_fact(fact: dict) -> str:
    item_name = str((fact or {}).get("item_name", "") or "").strip()
    return item_name.split("|", 1)[0].strip()


def _is_valid_note_ref(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text.strip(" -–—.;,") != ""


def _note_ref_queries_from_results(worker_results: dict) -> list[dict]:
    queries = []
    seen = set()

    for result_key, payload in (worker_results or {}).items():
        if not isinstance(payload, dict):
            continue
        table = _result_key_for_table(payload.get("table", "") or result_key)
        if not table or table == TABLE_NOTE or table == WEB_RESULT_KEY:
            continue

        for fact in payload.get("facts", []) or []:
            if not isinstance(fact, dict):
                continue
            note_ref = str(fact.get("note_ref", "") or "").strip()
            if not _is_valid_note_ref(note_ref):
                continue
            row_label = _row_label_from_fact(fact)
            if not row_label:
                continue

            key = (note_ref, row_label.lower())
            if key in seen:
                continue
            seen.add(key)
            queries.append(
                {
                    "query": f"thuyết minh {note_ref} {row_label}",
                    "note_ref": note_ref,
                    "source_table": table,
                    "source_item": row_label,
                    "source_query": str(fact.get("evidence_query", "") or "").strip(),
                    "source_fact_id": str(fact.get("fact_id", "") or "").strip(),
                    "source_value": str(fact.get("value", "") or "").strip(),
                    "source_period_label": str(
                        fact.get("period_label", "")
                        or fact.get("column_label", "")
                        or fact.get("time_hint", "")
                        or ""
                    ).strip(),
                    "source_aggregation_level": str(
                        fact.get("aggregation_level", "") or ""
                    ).strip(),
                    "needby": _needby_values(fact),
                }
            )

    priority_markers = (
        "phải thu",
        "hàng tồn kho",
        "vay",
        "nợ",
        "doanh thu",
        "giá vốn",
        "chi phí tài chính",
        "lãi vay",
    )
    ranked = sorted(
        enumerate(queries),
        key=lambda item: (
            0
            if any(
                marker in str(item[1].get("source_item", "") or "").lower()
                for marker in priority_markers
            )
            else 1,
            item[0],
        ),
    )
    return [query for _index, query in ranked[:NOTE_REF_ENRICHMENT_LIMIT]]


def _fact_matches_note_ref(fact: dict, note_ref: str) -> bool:
    ref = str(note_ref or "").strip()
    if not ref or not isinstance(fact, dict):
        return False
    text = " ".join(
        str(fact.get(key, "") or "")
        for key in ("item_name", "subheading")
    )
    return bool(
        re.search(
            rf"\bthuyết\s+minh\s+{re.escape(ref)}(?=\D|$)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _filter_note_ref_facts(facts: list[dict], note_ref: str) -> list[dict]:
    exact_facts = [
        fact
        for fact in facts or []
        if _fact_matches_note_ref(fact, note_ref)
    ]
    return exact_facts or list(facts or [])


def _coerce_existing_worker_results(existing: dict) -> dict:
    output = {}
    for key, payload in (existing or {}).items():
        key_text = str(key or "").strip()
        if not key_text:
            continue
        if is_analysis_agent(key_text):
            output[key_text] = payload
            continue
        output[key_text] = payload
    return output


def _ensure_report_section_target(targets: list[dict], user_query: str) -> list[dict]:
    """Add every high/medium-confidence deterministic route not already planned.

    The legacy name is kept for callers/tests.  Unlike the former hard-front
    guard, ambiguous corporate-structure questions retain both NOTE and REPORT
    candidates; exclusive audit/governance questions still rank REPORT first.
    """
    query = str(user_query or "").strip()
    if not query:
        return targets

    existing_tables = {
        normalize_evidence_table(target.get("table", ""))
        for target in targets
        if isinstance(target, dict)
    }
    candidates = route_candidates(query)
    # An explicit NOTE/CF/front intent is exclusive at execution time.  Lower
    # lexical metric candidates remain useful to the keyworder, but the graph
    # guard only fans out when the deterministic router itself is uncertain.
    semantic_reasons = {
        "explicit_note_semantics",
        "accounting_policy",
        "explicit_cashflow_semantics",
        "exclusive_front_matter_semantics",
    }
    semantic_candidates = [
        candidate
        for candidate in candidates
        if candidate.confidence >= 0.90 and candidate.reason in semantic_reasons
    ]
    runner_up = candidates[1] if len(candidates) > 1 else None
    top_is_clearly_exclusive = bool(
        candidates
        and candidates[0].confidence >= 0.90
        and len(semantic_candidates) <= 1
        and (
            candidates[0].reason in {"explicit_note_semantics", "accounting_policy"}
            or runner_up is None
            or runner_up.confidence < 0.80
        )
    )
    selected_candidates = candidates[:1] if top_is_clearly_exclusive else candidates
    additions = []
    for candidate in selected_candidates:
        if candidate.confidence < 0.60 or candidate.table in existing_tables:
            continue
        additions.append(
            {
                "mode": "table",
                "table": candidate.table,
                "requirements": [query],
                "needby_by_query": {},
                "search_queries": {},
                "canonical_queries": {},
                "metadata_by_query": {
                    query: {
                        "route_confidence": candidate.confidence,
                        "route_reason": candidate.reason,
                    }
                },
                "source": "deterministic_route_guard",
                "evidence_items": [{"table": candidate.table, "query": query}],
            }
        )
        existing_tables.add(candidate.table)
    return [*additions, *targets]


def _run_evidence_chains_in_plan_order(
    specs: list[dict],
    worker: Callable[[dict], _EvidenceChainResult],
) -> list[_EvidenceChainResult]:
    """Run independent chains concurrently and return results in input order."""

    if not specs:
        return []
    max_workers = min(_evidence_max_concurrency(), len(specs))
    if max_workers <= 1:
        return [worker(spec) for spec in specs]

    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="evidence",
    ) as executor:
        futures = [executor.submit(copy_context().run, worker, spec) for spec in specs]
        # Resolve in submission order.  Chains may finish out of order, but trace,
        # facts, cache output and ledger order remain stable across runs.
        return [future.result() for future in futures]


def _execute_web_evidence_chain(
    state: dict,
    spec: dict,
    *,
    web_provider: Any = None,
) -> _EvidenceChainResult:
    query = str(spec.get("query", "") or "").strip()
    cache_key = str(spec.get("cache_key", "") or "").strip()
    needby = list(spec.get("needby", []) or [])
    route_metadata = dict(spec.get("route_metadata", {}) or {})
    web_intent = str(
        route_metadata.get("web_intent", "")
        or (state.get("planner_plan", {}) or {}).get("web_intent", "")
        or "unsupported_external"
    ).strip()
    provider_name = str(getattr(web_provider, "provider_identity", "") or "").strip()
    call_started_at = time.perf_counter()
    if web_provider is None:
        result = {
            "tool": "web_search",
            "table": "",
            "query": query,
            "canonical_query": query,
            "context": "",
            "source": "",
            "status": "not_found_after_search",
            "retrieval_status": "unsupported",
            "message": WEB_UNSUPPORTED_MESSAGE,
            "cache_status": "disabled",
        }
        chain = _EvidenceChainResult(web_unsupported=1)
    else:
        try:
            request = WebEvidenceRequest(
                query=query,
                intent=web_intent,
                owner_id=str(state.get("owner_id", "") or ""),
                ticker=str(
                    state.get("dataset_ticker", "")
                    or (state.get("planner_plan", {}) or {}).get("ticker", "")
                    or ""
                ),
                company=str(
                    state.get("dataset_company", "")
                    or (state.get("planner_plan", {}) or {}).get("company", "")
                    or ""
                ),
                limit=active_policy().web.max_results,
                force_refresh=bool(state.get("force_web_refresh", False)),
            )
            batch = web_provider.search(request)
            result = batch.model_dump(mode="python")
            result.update(
                {
                    "tool": "web_search",
                    "table": "",
                    "query": query,
                    "canonical_query": query,
                    "source": provider_name,
                    "retrieval_status": batch.status,
                }
            )
            chain = _EvidenceChainResult(
                retrieval_calls=1,
                cache_hits=int(batch.cache_status in {"fresh", "stale"}),
                web_unsupported=int(batch.status == "unsupported"),
                web_refreshes=int(batch.refresh_scheduled),
                web_stale=int(batch.cache_status == "stale"),
                web_errors=int(batch.status == "error"),
            )
        except Exception as exc:
            result = {
                "tool": "web_search",
                "table": "",
                "query": query,
                "canonical_query": query,
                "context": "",
                "source": provider_name,
                "status": "not_found_after_search",
                "retrieval_status": "error",
                "message": f"Web evidence provider failed: {type(exc).__name__}",
                "cache_status": "miss",
            }
            chain = _EvidenceChainResult(retrieval_calls=1, web_errors=1)

    retrieval_status = str(result.get("retrieval_status", "") or "unsupported")
    event = "evidence_tool:done"
    if retrieval_status == "unsupported":
        event = "evidence_tool:unsupported"
    elif retrieval_status == "error":
        event = "evidence_tool:error"
    chain.trace.append(
        make_log(
            state,
            event,
            tool="web_search",
            provider=provider_name,
            provider_diagnostics=result.get("diagnostics", {}),
            scope="web",
            table="",
            query=query,
            cache_id=_cache_log_id(cache_key),
            cache_status=str(result.get("cache_status", "") or ""),
            status=str(result.get("status", "") or "not_found_after_search"),
            retrieval_status=retrieval_status,
            facts_n=len(result.get("evidence", []) or []),
            duration_ms=int((time.perf_counter() - call_started_at) * 1000),
            message=str(result.get("message", "") or ""),
        )
    )

    payload = _web_result_to_payload(result, query)
    payload["facts"] = _limit_evidence_facts(payload.get("facts", []))
    payload["facts"] = _annotate_facts_for_route(
        payload.get("facts", []),
        needby=needby,
        evidence_query=query,
        route_metadata=route_metadata,
    )
    result_key = _result_key_for_table("", mode="web")
    chain.worker_results[result_key] = payload
    prompt_facts = _prompt_facts_for_route(
        payload.get("facts", []),
        requirement=query,
        requirement_state=retrieval_status,
        core_requirement=_is_core_prompt_requirement(
            state,
            query,
            needby=needby,
            route_metadata=route_metadata,
        ),
    )
    if prompt_facts:
        chain.prompt_worker_results[result_key] = {
            "table": "",
            "facts": prompt_facts,
        }
    chain.evidence_items.append(
        {
            "key": cache_key,
            "scope": "web",
            "table": "",
            "query": query,
            "evidence_query": query,
            "needby": needby,
            **route_metadata,
            "cache_hit": str(result.get("cache_status", "") or "") in {"fresh", "stale"},
            "facts_n": len(payload.get("facts", []) or []),
            "facts_preview": _facts_log_preview(payload.get("facts", [])),
            "source": str(result.get("source", "") or ""),
            "status": payload.get("status", "not_found_after_search"),
            "retrieval_status": retrieval_status,
            "cache_status": str(result.get("cache_status", "") or ""),
            "message": str(result.get("message", "") or ""),
        }
    )
    return chain


def _execute_table_evidence_chain(
    state: dict,
    worker_plan: dict,
    collection: Any,
    spec: dict,
) -> _EvidenceChainResult:
    """Execute one table requirement, including its optional targeted retry."""

    table = str(spec.get("table", "") or "").strip()
    query = str(spec.get("query", "") or "").strip()
    route_metadata = dict(spec.get("route_metadata", {}) or {})
    needby = list(spec.get("needby", []) or [])
    canonical_query = str(spec.get("canonical_query", "") or "").strip()
    search_query = str(spec.get("search_query", "") or "").strip()
    premise_scoped = bool(spec.get("premise_scoped", False))
    semantic_requirement = str(
        spec.get("semantic_requirement", "") or ""
    ).strip()
    retrieval_intent = str(spec.get("retrieval_intent", "") or "").strip()
    matching_query = str(spec.get("matching_query", "") or "").strip()
    cache_key = str(spec.get("cache_key", "") or "").strip()
    retrieval_limit = int(spec.get("retrieval_limit", EVIDENCE_FACTS_LIMIT) or 0)
    chain = _EvidenceChainResult()

    call_started_at = time.perf_counter()
    cached = spec.get("cached_result")
    cache_hit = isinstance(cached, dict) and bool(cached)
    if cache_hit:
        result = cached
        reconstructed_cached_facts = (
            result_to_facts(
                result,
                table=table,
                query=matching_query,
                limit=retrieval_limit,
                semantic_requirement=semantic_requirement,
            )
            if (result.get("documents") or result.get("metadatas"))
            else []
        )
        cache_facts = filter_facts_for_query(
            dedupe_facts(
                [
                    *reconstructed_cached_facts,
                    *(result.get("facts", []) or []),
                ]
            ),
            table=table,
            query=matching_query,
            source=str(result.get("source", "") or ""),
            semantic_requirement=semantic_requirement,
        )
        facts = _limit_evidence_facts_for_table(
            table,
            cache_facts,
            state=state,
            worker_plan=worker_plan,
        )
        if table == TABLE_NOTE:
            result = dict(result)
            result["facts"] = cache_facts
        chain.cache_hits = 1
    else:
        _append_debug_log(
            chain.trace,
            state,
            "evidence_tool:start",
            tool="get_related_info",
            scope="table",
            table=table,
            query=query,
            canonical_query=canonical_query if canonical_query != query else "",
            search_query=search_query if search_query != query else "",
            cache_id=_cache_log_id(cache_key),
            strict_table=(table in {TABLE_NOTE, TABLE_REPORT_SECTION}),
        )
        raw_result = get_related_info(
            query=search_query,
            table=table,
            collection=collection,
            strict_table=(table in {TABLE_NOTE, TABLE_REPORT_SECTION}),
            limit=retrieval_limit,
            intent=retrieval_intent,
            structured_slots=not premise_scoped,
        )
        cache_facts = result_to_facts(
            raw_result,
            table=table,
            query=matching_query,
            limit=retrieval_limit,
            semantic_requirement=semantic_requirement,
        )
        facts = _limit_evidence_facts_for_table(
            table,
            cache_facts,
            state=state,
            worker_plan=worker_plan,
        )
        result = cache_item_from_result(
            raw_result,
            table=table,
            query=query,
            tool="get_related_info",
            facts=cache_facts,
        )
        result["canonical_query"] = canonical_query
        result["search_query"] = search_query
        chain.cache_updates[cache_key] = result
        chain.retrieval_calls = 1

    requirement_state = (
        narrative_requirement_evidence_state(
            semantic_requirement,
            facts,
        )
        or requirement_evidence_state(
            matching_query,
            facts,
            table=table,
        )
    )
    requirement_state_before_retry = requirement_state
    chain.trace.append(
        make_log(
            state,
            "evidence_tool:done",
            tool="get_related_info",
            scope="table",
            table=table,
            cache_id=_cache_log_id(cache_key),
            cache_hit=cache_hit,
            cache_stored=not cache_hit,
            context_len=len(str(result.get("context", "") or "")),
            facts_n=len(facts),
            retrieval_status=requirement_state_before_retry,
            duration_ms=int((time.perf_counter() - call_started_at) * 1000),
            **_trace_details(
                state,
                query=query,
                source=result.get("source", ""),
                facts_preview=_facts_log_preview(facts),
            ),
        )
    )

    retried = False
    retry_query = ""
    if (
        requirement_state == REQUIREMENT_UNMATCHED_TOPK
        and not bool((result or {}).get("targeted_retry_performed"))
    ):
        retry_query = (
            narrative_atom_gap_retry_query(
                semantic_requirement,
                facts,
            )
            if premise_scoped
            else ""
        ) or targeted_retry_query(query or search_query)
        retry_intent = (
            retry_query
            if premise_scoped
            else retrieval_intent or matching_query
        )
        retry_started_at = time.perf_counter()
        _append_debug_log(
            chain.trace,
            state,
            "evidence_tool:targeted_retry_start",
            tool="get_related_info",
            scope="targeted_retry",
            table=table,
            query=query,
            retry_query=retry_query,
            cache_id=_cache_log_id(cache_key),
            strict_table=True,
        )
        retry_raw_result = get_related_info(
            query=retry_query,
            table=table,
            collection=collection,
            strict_table=True,
            cross_table=False,
            limit=retrieval_limit,
            intent=retry_intent,
            structured_slots=not premise_scoped,
        )
        retry_facts = result_to_facts(
            retry_raw_result,
            table=table,
            query=matching_query,
            limit=retrieval_limit,
            semantic_requirement=semantic_requirement,
        )
        cache_facts = filter_facts_for_query(
            dedupe_facts([*retry_facts, *cache_facts]),
            table=table,
            query=matching_query,
            source=str(
                retry_raw_result.get("source", "")
                or result.get("source", "")
                or ""
            ),
            semantic_requirement=semantic_requirement,
        )
        facts = _limit_evidence_facts_for_table(
            table,
            cache_facts,
            state=state,
            worker_plan=worker_plan,
        )
        requirement_state = (
            narrative_requirement_evidence_state(
                semantic_requirement,
                facts,
            )
            or requirement_evidence_state(
                matching_query,
                facts,
                table=table,
            )
        )
        if (
            requirement_state == REQUIREMENT_UNMATCHED_TOPK
            and not any(
                str(fact.get("status", "") or "")
                == "not_found_after_search"
                for fact in cache_facts
                if isinstance(fact, dict)
            )
        ):
            cache_facts = [
                not_found_fact(
                    table,
                    matching_query,
                    source=str(
                        retry_raw_result.get("source", "")
                        or result.get("source", "")
                        or ""
                    ),
                ),
                *cache_facts,
            ]
            facts = _limit_evidence_facts_for_table(
                table,
                cache_facts,
                state=state,
                worker_plan=worker_plan,
            )
        result = dict(result or {})
        result["facts"] = cache_facts
        result["targeted_retry_performed"] = True
        result["targeted_retry_query"] = retry_query
        result["retrieval_status"] = requirement_state
        if not str(result.get("source", "") or "").strip():
            result["source"] = str(
                retry_raw_result.get("source", "") or ""
            ).strip()
        chain.cache_updates[cache_key] = result
        retried = True
        chain.targeted_retries = 1
        chain.retrieval_calls += 1
        chain.trace.append(
            make_log(
                state,
                "evidence_tool:targeted_retry_done",
                tool="get_related_info",
                scope="targeted_retry",
                table=table,
                cache_id=_cache_log_id(cache_key),
                cache_hit=False,
                retrieval_status=requirement_state,
                facts_n=len(facts),
                duration_ms=int(
                    (time.perf_counter() - retry_started_at) * 1000
                ),
                **_trace_details(
                    state,
                    query=query,
                    retry_query=retry_query,
                    source=result.get("source", ""),
                    facts_preview=_facts_log_preview(facts),
                ),
            )
        )

    chain.ledger_entries.append(
        _retrieval_ledger_entry(
            requirement=query,
            table=table,
            route_query=str(state.get("user_query", "") or "").strip()
            or query,
            matching_query=matching_query,
            search_query=search_query,
            retrieval_intent=retrieval_intent,
            route_metadata=route_metadata,
            state_before_retry=requirement_state_before_retry,
            state_after_retry=requirement_state,
            retry_performed=retried,
            retry_query=retry_query,
            cache_hit=cache_hit,
            facts=facts,
        )
    )
    routed_facts = _annotate_facts_for_route(
        facts,
        needby=needby,
        evidence_query=query,
        route_metadata=route_metadata,
    )
    result_key = _result_key_for_table(table, mode="table")
    chain.worker_results[result_key] = {
        "table": table,
        "facts": routed_facts,
    }
    prompt_facts = _prompt_facts_for_route(
        routed_facts,
        requirement=matching_query,
        requirement_state=requirement_state,
        core_requirement=_is_core_prompt_requirement(
            state,
            query,
            needby=needby,
            route_metadata=route_metadata,
            premise_scoped=premise_scoped,
        ),
    )
    if prompt_facts:
        chain.prompt_worker_results[result_key] = {
            "table": table,
            "facts": prompt_facts,
        }
    chain.evidence_items.append(
        {
            "key": cache_key,
            "scope": "table",
            "table": table,
            "query": query,
            "evidence_query": query,
            "needby": needby,
            **route_metadata,
            "cache_hit": cache_hit,
            "targeted_retry": retried,
            "retrieval_status": requirement_state,
            "facts_n": len(facts),
            "facts_preview": _facts_log_preview(facts),
            "source": result.get("source", ""),
        }
    )
    return chain


def _execute_note_ref_evidence_chain(
    state: dict,
    worker_plan: dict,
    collection: Any,
    spec: dict,
) -> _EvidenceChainResult:
    """Fetch one note-reference enrichment after the primary-stage barrier."""

    note_request = dict(spec.get("note_request", {}) or {})
    query = str(note_request.get("query", "") or "").strip()
    cache_key = str(spec.get("cache_key", "") or "").strip()
    chain = _EvidenceChainResult()
    call_started_at = time.perf_counter()
    cached = spec.get("cached_result")
    cache_hit = isinstance(cached, dict) and bool(cached)
    if cache_hit:
        result = cached
        cache_facts = _filter_note_ref_facts(
            filter_facts_for_query(
                dedupe_facts(result.get("facts", []) or []),
                table=TABLE_NOTE,
                query=query,
                source=str(result.get("source", "") or ""),
            ),
            str(note_request.get("note_ref", "") or ""),
        )
        facts = _limit_evidence_facts_for_table(
            TABLE_NOTE,
            cache_facts,
            state=state,
            worker_plan=worker_plan,
        )
        result = dict(result)
        result["facts"] = cache_facts
        chain.cache_hits = 1
    else:
        _append_debug_log(
            chain.trace,
            state,
            "evidence_tool:start",
            tool="get_related_info",
            scope=NOTE_REF_SCOPE,
            table=TABLE_NOTE,
            query=query,
            note_ref=note_request.get("note_ref", ""),
            source_table=note_request.get("source_table", ""),
            source_item=note_request.get("source_item", ""),
            cache_id=_cache_log_id(cache_key),
            strict_table=True,
        )
        raw_result = get_related_info(
            query=query,
            table=TABLE_NOTE,
            collection=collection,
            strict_table=True,
            limit=NOTE_REF_FACTS_SCAN_LIMIT,
            intent=str(state.get("user_query", "") or "").strip(),
        )
        cache_facts = _filter_note_ref_facts(
            result_to_facts(
                raw_result,
                table=TABLE_NOTE,
                query=query,
                limit=NOTE_REF_FACTS_SCAN_LIMIT,
            ),
            str(note_request.get("note_ref", "") or ""),
        )
        facts = _limit_evidence_facts_for_table(
            TABLE_NOTE,
            cache_facts,
            state=state,
            worker_plan=worker_plan,
        )
        result = cache_item_from_result(
            raw_result,
            table=TABLE_NOTE,
            query=query,
            tool="get_related_info",
            facts=cache_facts,
        )
        chain.cache_updates[cache_key] = result
        chain.retrieval_calls = 1

    chain.trace.append(
        make_log(
            state,
            "evidence_tool:done",
            tool="get_related_info",
            scope=NOTE_REF_SCOPE,
            table=TABLE_NOTE,
            cache_id=_cache_log_id(cache_key),
            cache_hit=cache_hit,
            cache_stored=not cache_hit,
            context_len=len(str(result.get("context", "") or "")),
            facts_n=len(facts),
            retrieval_status=str(
                result.get("retrieval_status", "")
                or result.get("status", "")
                or "completed"
            ),
            duration_ms=int((time.perf_counter() - call_started_at) * 1000),
            **_trace_details(
                state,
                query=query,
                source=result.get("source", ""),
                facts_preview=_facts_log_preview(facts),
            ),
        )
    )
    routed_facts = _annotate_facts_for_route(
        facts,
        needby=_needby_values(note_request),
        evidence_query=(
            str(note_request.get("source_query", "") or "").strip()
            or query
        ),
        source_table=str(note_request.get("source_table", "") or "").strip(),
        source_item=str(note_request.get("source_item", "") or "").strip(),
    )
    for fact in routed_facts:
        fact["evidence_role"] = "note_detail"
        fact["linked_parent_fact_id"] = str(
            note_request.get("source_fact_id", "") or ""
        ).strip()
        fact["linked_parent_item"] = str(
            note_request.get("source_item", "") or ""
        ).strip()
        fact["linked_parent_value"] = str(
            note_request.get("source_value", "") or ""
        ).strip()
        fact["linked_parent_period_label"] = str(
            note_request.get("source_period_label", "") or ""
        ).strip()
        fact["linked_parent_aggregation_level"] = str(
            note_request.get("source_aggregation_level", "") or ""
        ).strip()
    chain.worker_results[TABLE_NOTE] = {
        "table": TABLE_NOTE,
        "facts": routed_facts,
    }
    prompt_facts = _prompt_facts_for_route(
        routed_facts,
        requirement=query,
        requirement_state=requirement_evidence_state(
            query,
            facts,
            table=TABLE_NOTE,
        ),
        # Note-ref lookup is optional enrichment. Its miss must not create a new
        # user-facing requirement; matched facts are still retained.
        core_requirement=False,
    )
    if prompt_facts:
        chain.prompt_worker_results[TABLE_NOTE] = {
            "table": TABLE_NOTE,
            "facts": prompt_facts,
        }
    chain.evidence_items.append(
        {
            "key": cache_key,
            "scope": NOTE_REF_SCOPE,
            "table": TABLE_NOTE,
            "query": query,
            "note_ref": note_request.get("note_ref", ""),
            "source_table": note_request.get("source_table", ""),
            "source_item": note_request.get("source_item", ""),
            "needby": _needby_values(note_request),
            "cache_hit": cache_hit,
            "facts_n": len(facts),
            "facts_preview": _facts_log_preview(facts),
            "source": result.get("source", ""),
        }
    )
    return chain


def _merge_evidence_chain_results(
    chain_results: list[_EvidenceChainResult],
    *,
    evidence_cache_updates: dict,
    evidence_items: list[dict],
    current_worker_results: dict[str, dict],
    current_prompt_worker_results: dict[str, dict],
    trace: list[dict],
    retrieval_ledger_entries: list[dict[str, Any]],
) -> dict[str, int]:
    """Merge local worker artifacts in deterministic plan order."""

    counts = {
        "retrieval_calls": 0,
        "targeted_retries": 0,
        "cache_hits": 0,
        "web_unsupported": 0,
        "web_refreshes": 0,
        "web_stale": 0,
        "web_errors": 0,
    }
    for chain in chain_results:
        trace.extend(chain.trace)
        evidence_items.extend(chain.evidence_items)
        retrieval_ledger_entries.extend(chain.ledger_entries)
        for cache_key, result in chain.cache_updates.items():
            evidence_cache_updates[cache_key] = result
            # Runtime cache mutation happens only on the orchestrating thread.
            set_runtime_cache_item(cache_key, result)
        for result_key, payload in chain.worker_results.items():
            current_worker_results[result_key] = merge_worker_fact_payload(
                current_worker_results.get(result_key, {}),
                payload,
            )
        for result_key, payload in chain.prompt_worker_results.items():
            current_prompt_worker_results[result_key] = merge_worker_fact_payload(
                current_prompt_worker_results.get(result_key, {}),
                payload,
            )
        counts["retrieval_calls"] += chain.retrieval_calls
        counts["targeted_retries"] += chain.targeted_retries
        counts["cache_hits"] += chain.cache_hits
        counts["web_unsupported"] += chain.web_unsupported
        counts["web_refreshes"] += chain.web_refreshes
        counts["web_stale"] += chain.web_stale
        counts["web_errors"] += chain.web_errors
    return counts


def build_evidence_pack(state: dict, *, web_provider: Any = None) -> dict:
    started_at = time.perf_counter()
    worker_plan = state.get("worker_plan", {}) or {}
    collection = get_collection()
    collection_generation = str(
        state.get("index_fingerprint", "")
        or state.get("collection_generation", "")
        or ""
    ).strip()
    if not collection_generation and collection is not None:
        generation = getattr(collection, "generation", "")
        if callable(generation):
            generation = generation()
        collection_generation = str(generation or "").strip()
    trace = []

    retrieval_targets = _normalized_retrieval_targets(worker_plan)
    retrieval_targets = _ensure_report_section_target(
        retrieval_targets, state.get("user_query", "")
    )
    grounded_premises = _grounded_premises_for_evidence_cap(
        state,
        worker_plan,
    )
    grounded_premise_keys = {
        normalize_narrative_text(premise)
        for premise in grounded_premises
    }
    planned_analysis_agents = _effective_analysis_agents(
        state,
        worker_plan,
        _planned_analysis_agents(worker_plan),
    )

    if collection is None and any(target.get("mode") != "web" for target in retrieval_targets):
        return {
            "evidence_pack": {
                "items": [],
                "facts_by_table": {},
                "targets": retrieval_targets,
                "error": "collection_not_set",
            },
            "worker_results": {},
            "expected_workers": [],
            "dispatch_phase": "synth",
            "collect_decision": "synth",
            "trace": [
                make_log(
                    state,
                    "evidence:error",
                    error="collection not set. Call set_collection(collection) before running workflow.",
                )
            ],
        }

    existing_cache = state.get("evidence_cache", {}) or {}
    evidence_cache_updates = {}
    evidence_items = []
    current_worker_results: dict[str, dict] = {}
    current_prompt_worker_results: dict[str, dict] = {}
    processed_keys = set()
    retrieval_calls = 0
    targeted_retries = 0
    cache_hits = 0
    web_unsupported = 0
    web_refreshes = 0
    web_stale = 0
    web_errors = 0
    retrieval_ledger_entries: list[dict[str, Any]] = []

    primary_specs: list[dict] = []
    for target in retrieval_targets:
        table = normalize_evidence_table(target.get("table", ""))
        mode = (
            str(target.get("mode", "") or "").strip().lower()
            or ("table" if table else "web")
        )
        requirements = _dedupe_keep_order(
            target.get("requirements", []) or []
        )

        if mode == "web":
            query = (
                " ".join(requirements)
                or str(state.get("user_query", "") or "").strip()
            )
            route_metadata = _target_metadata_for_query(target, query)
            needby = _dedupe_keep_order(
                agent
                for requirement in requirements
                for agent in (target.get("needby_by_query", {}) or {}).get(
                    requirement,
                    [],
                )
            )
            cache_key = evidence_cache_key(
                dataset_id=str(state.get("dataset_id", "") or ""),
                table="",
                query=query,
                mode="web",
                generation=(
                    f"{collection_generation}:"
                    f"{str(getattr(web_provider, 'provider_identity', '') or 'unsupported')}"
                ),
            )
            if cache_key in processed_keys:
                continue
            processed_keys.add(cache_key)
            primary_specs.append(
                {
                    "kind": "web",
                    "query": query,
                    "cache_key": cache_key,
                    "needby": needby,
                    "route_metadata": route_metadata,
                }
            )
            continue

        for requirement in requirements:
            query = str(requirement or "").strip()
            route_metadata = _target_metadata_for_query(target, query)
            needby = _dedupe_keep_order(
                (target.get("needby_by_query", {}) or {}).get(query, []) or []
            )
            canonical_query = normalize_evidence_query(
                (target.get("canonical_queries") or {}).get(query) or query,
                table=table,
            )
            search_query = str(
                (target.get("search_queries") or {}).get(query, "") or query
            ).strip()
            operand_scoped = (
                str(route_metadata.get("operation", "") or "").strip()
                in {"ratio", "share"}
                and str(route_metadata.get("operand_role", "") or "").strip()
                in {"numerator", "denominator"}
            )
            premise_scoped = (
                normalize_narrative_text(query) in grounded_premise_keys
            )
            semantic_requirement = query if premise_scoped else ""
            retrieval_intent = (
                search_query
                if operand_scoped or premise_scoped
                else str(state.get("user_query", "") or "").strip()
            )
            matching_query = query or canonical_query or search_query
            cache_key = evidence_cache_key(
                dataset_id=str(state.get("dataset_id", "") or ""),
                table=table,
                query=search_query,
                mode=_table_chain_cache_mode(
                    matching_query,
                    search_query=search_query,
                    structured_slots=not premise_scoped,
                ),
                intent=retrieval_intent,
                generation=collection_generation,
            )
            if cache_key in processed_keys:
                continue
            processed_keys.add(cache_key)
            facts_limit = _facts_limit_for_table(state, worker_plan, table)
            retrieval_limit = (
                max(NOTE_REF_FACTS_SCAN_LIMIT, facts_limit)
                if table == TABLE_NOTE
                else facts_limit
            )
            primary_specs.append(
                {
                    "kind": "table",
                    "table": table,
                    "query": query,
                    "route_metadata": route_metadata,
                    "needby": needby,
                    "canonical_query": canonical_query,
                    "search_query": search_query,
                    "premise_scoped": premise_scoped,
                    "semantic_requirement": semantic_requirement,
                    "retrieval_intent": retrieval_intent,
                    "matching_query": matching_query,
                    "cache_key": cache_key,
                    "cached_result": (
                        existing_cache.get(cache_key)
                        or get_runtime_cache_item(cache_key)
                    ),
                    "retrieval_limit": retrieval_limit,
                }
            )

    def execute_primary_chain(spec: dict) -> _EvidenceChainResult:
        if spec.get("kind") == "web":
            return _execute_web_evidence_chain(
                state,
                spec,
                web_provider=web_provider,
            )
        return _execute_table_evidence_chain(
            state,
            worker_plan,
            collection,
            spec,
        )

    primary_results = _run_evidence_chains_in_plan_order(
        primary_specs,
        execute_primary_chain,
    )
    primary_counts = _merge_evidence_chain_results(
        primary_results,
        evidence_cache_updates=evidence_cache_updates,
        evidence_items=evidence_items,
        current_worker_results=current_worker_results,
        current_prompt_worker_results=current_prompt_worker_results,
        trace=trace,
        retrieval_ledger_entries=retrieval_ledger_entries,
    )
    retrieval_calls += primary_counts["retrieval_calls"]
    targeted_retries += primary_counts["targeted_retries"]
    cache_hits += primary_counts["cache_hits"]
    web_unsupported += primary_counts["web_unsupported"]
    web_refreshes += primary_counts["web_refreshes"]
    web_stale += primary_counts["web_stale"]
    web_errors += primary_counts["web_errors"]
    if _should_fetch_note_ref_context(
        state,
        worker_plan,
        planned_analysis_agents,
    ):
        # Barrier: note-ref requests are derived only after every primary chain
        # (including its targeted retry) has completed and been merged.
        note_cache = dict(existing_cache)
        note_cache.update(evidence_cache_updates)
        note_specs: list[dict] = []
        for note_request in _note_ref_queries_from_results(
            current_prompt_worker_results
        ):
            query = str(note_request.get("query", "") or "").strip()
            if not query:
                continue
            cache_key = evidence_cache_key(
                dataset_id=str(state.get("dataset_id", "") or ""),
                table=TABLE_NOTE,
                query=query,
                mode="table",
                intent=str(state.get("user_query", "") or ""),
                generation=collection_generation,
            )
            if cache_key in processed_keys:
                continue
            processed_keys.add(cache_key)
            note_specs.append(
                {
                    "cache_key": cache_key,
                    "cached_result": (
                        note_cache.get(cache_key)
                        or get_runtime_cache_item(cache_key)
                    ),
                    "note_request": note_request,
                }
            )

        def execute_note_chain(spec: dict) -> _EvidenceChainResult:
            return _execute_note_ref_evidence_chain(
                state,
                worker_plan,
                collection,
                spec,
            )

        note_results = _run_evidence_chains_in_plan_order(
            note_specs,
            execute_note_chain,
        )
        note_counts = _merge_evidence_chain_results(
            note_results,
            evidence_cache_updates=evidence_cache_updates,
            evidence_items=evidence_items,
            current_worker_results=current_worker_results,
            current_prompt_worker_results=current_prompt_worker_results,
            trace=trace,
            retrieval_ledger_entries=retrieval_ledger_entries,
        )
        retrieval_calls += note_counts["retrieval_calls"]
        targeted_retries += note_counts["targeted_retries"]
        cache_hits += note_counts["cache_hits"]
        web_unsupported += note_counts["web_unsupported"]
        web_refreshes += note_counts["web_refreshes"]
        web_stale += note_counts["web_stale"]
        web_errors += note_counts["web_errors"]
    audit_existing_worker_results = _merge_worker_results(
        _coerce_existing_worker_results(state.get("worker_results", {}) or {}),
        {
            result_key: payload
            for result_key, payload in (
                state.get("ragas_facts_by_table", {}) or {}
            ).items()
            if not is_analysis_agent(str(result_key or "").strip())
        },
    )
    merged_worker_results = _limit_note_facts_for_llm(
        _merge_worker_results(
            audit_existing_worker_results,
            current_worker_results,
        ),
        state=state,
        worker_plan=worker_plan,
    )
    merged_prompt_worker_results = _drop_redundant_prompt_diagnostics(
        _limit_note_facts_for_llm(
            _merge_worker_results(
                _coerce_existing_worker_results(
                    state.get("worker_results", {}) or {}
                ),
                current_prompt_worker_results,
            ),
            state=state,
            worker_plan=worker_plan,
        )
    )
    compact_worker_results = _compact_worker_results(merged_prompt_worker_results)
    audit_facts_n = sum(
        len((payload or {}).get("facts", []) or [])
        for result_key, payload in merged_worker_results.items()
        if isinstance(payload, dict)
        and not is_analysis_agent(str(result_key or "").strip())
    )
    prompt_facts_n = sum(
        len((payload or {}).get("facts", []) or [])
        for result_key, payload in merged_prompt_worker_results.items()
        if isinstance(payload, dict)
        and not is_analysis_agent(str(result_key or "").strip())
    )
    evidence_pack = {
        **(
            {"index_generation": collection_generation}
            if collection_generation
            else {}
        ),
        "targets": _compact_evidence_targets(retrieval_targets),
        "items": _compact_evidence_items(
            _limit_note_item_previews_for_llm(
                evidence_items,
                state=state,
                worker_plan=worker_plan,
            )
        ),
        "facts_by_table": {
            result_key: payload
            for result_key, payload in compact_worker_results.items()
            if not is_analysis_agent(str(result_key or "").strip())
        },
        "stats": {
            "targets_n": len(retrieval_targets),
            "items_n": len(evidence_items),
            "retrieval_calls_n": retrieval_calls,
            "targeted_retries_n": targeted_retries,
            "cache_hits_n": cache_hits,
            "web_unsupported_n": web_unsupported,
            "web_refreshes_n": web_refreshes,
            "web_stale_n": web_stale,
            "web_errors_n": web_errors,
            "facts_n": audit_facts_n,
            "facts_n_for_synth": prompt_facts_n,
            "facts_pruned_from_synth_n": max(
                audit_facts_n - prompt_facts_n,
                0,
            ),
        },
    }

    updates = {
        "evidence_pack": evidence_pack,
        "ragas_facts_by_table": {
            result_key: payload
            for result_key, payload in merged_worker_results.items()
            if not is_analysis_agent(str(result_key or "").strip())
        },
        "evidence_cache": evidence_cache_updates,
        "evidence_ledger": _merge_evidence_ledger(
            state.get("evidence_ledger", {}),
            retrieval_ledger_entries,
        ),
        **(
            {"collection_generation": collection_generation}
            if collection_generation
            else {}
        ),
        "worker_results": compact_worker_results,
        "last_agent": "evidence_executor",
        "dispatch_phase": "analysis" if planned_analysis_agents else "synth",
        "collect_decision": "analysis" if planned_analysis_agents else "synth",
        "trace": list(trace) + [
            make_log(
                state,
                "evidence:built",
                targets_n=len(retrieval_targets),
                analysis_agents=planned_analysis_agents,
                items_n=len(evidence_items),
                retrieval_calls_n=retrieval_calls,
                cache_hits_n=cache_hits,
                facts_n=evidence_pack["stats"]["facts_n"],
                facts_n_for_synth=evidence_pack["stats"]["facts_n_for_synth"],
                facts_pruned_from_synth_n=evidence_pack["stats"][
                    "facts_pruned_from_synth_n"
                ],
                duration_ms=int((time.perf_counter() - started_at) * 1000),
            )
        ],
    }

    if planned_analysis_agents:
        analysis_updates = prepare_analysis_dispatch_state(
            {
                **state,
                **updates,
                # Analysis must consume the route-reserved branch, not the
                # recall-first audit branch. The latter lets the first query's
                # full top-k consume the global table cap and makes later core
                # pairs appear missing even though their retrieval ledgers are
                # matched. RAGAS/ledger state above still retains audit facts.
                "worker_results": merged_prompt_worker_results,
            }
        )
        updates["expected_workers"] = analysis_updates.get("expected_workers", [])
        updates["analysis_dispatch_targets"] = analysis_updates.get("analysis_dispatch_targets", [])
        updates["dispatch_phase"] = analysis_updates.get("dispatch_phase", "analysis")
        updates["trace"] = list(updates.get("trace", []) or []) + list(analysis_updates.get("trace", []) or [])
    else:
        updates["expected_workers"] = []
        updates["analysis_dispatch_targets"] = []

    return updates
