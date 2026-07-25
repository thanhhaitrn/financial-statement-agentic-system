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

from schemas.requirements import (
    FACT_STATUS_AMBIGUOUS,
    FACT_STATUS_FOUND,
    FACT_STATUS_NOT_FOUND,
    normalize_requirement_text,
    normalize_fact_status,
    not_found_after_search_message,
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

ANALYSIS_DEFAULT_TOOL = {
    "agent_profitability": "get_income_statement_info",
    "agent_liquidity_solvency": "get_balance_sheet_info",
    "agent_cashflow_analysis": "get_cashflow_info",
    "agent_efficiency": "get_income_statement_info",
}
MAIN_REPORT_TABLES = {TABLE_BS, TABLE_IS, TABLE_CF}
# Legacy behavior (=1): one exact label match discards all other candidates.
_EXACT_MATCH_COLLAPSE = str(os.getenv("EXACT_MATCH_COLLAPSE", "0")).strip() == "1"

_SPACE_RE = re.compile(r"\s+")
_RUNTIME_CACHE_LOCK = RLock()
_RUNTIME_CACHE_MAX_ITEMS = max(1, int(os.getenv("RUNTIME_EVIDENCE_CACHE_MAX_ITEMS", "256")))
_RUNTIME_EVIDENCE_CACHE: OrderedDict[str, dict] = OrderedDict()


def collapse_text(value: Any) -> str:
    return _SPACE_RE.sub(" ", str(value or "").strip().lower())


def normalize_evidence_table(value: Any) -> str:
    return normalize_table_heading(str(value or "").strip())


def normalize_evidence_query(value: Any, *, table: str = "") -> str:
    normalized = normalize_requirement_text(value, table=table)
    return normalized or collapse_text(value)


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
    text = collapse_text(query)

    if any(
        marker in text
        for marker in (
            "báo cáo của ban tổng giám đốc",
            "bao cao cua ban tong giam doc",
            "báo cáo của ban giám đốc",
            "bao cao cua ban giam doc",
            "báo cáo kiểm toán",
            "bao cao kiem toan",
            "báo cáo soát xét",
            "bao cao soat xet",
            "thông tin công ty",
            "thong tin cong ty",
            "khái quát về công ty",
            "khai quat ve cong ty",
            "địa chỉ",
            "dia chi",
            "trụ sở",
            "tru so",
            "trụ sở chính",
            "tru so chinh",
            "trụ sở hoạt động",
            "tru so hoat dong",
            "hoạt động kinh doanh chính",
            "hoat dong kinh doanh chinh",
            "giấy chứng nhận đăng ký doanh nghiệp",
            "giay chung nhan dang ky doanh nghiep",
            "chuẩn mực kế toán",
            "chuan muc ke toan",
            "chuẩn mực kế toán áp dụng",
            "chuan muc ke toan ap dung",
            "chế độ kế toán",
            "che do ke toan",
            "chế độ kế toán áp dụng",
            "che do ke toan ap dung",
            "tuyên bố tuân thủ chuẩn mực kế toán",
            "tuyen bo tuan thu chuan muc ke toan",
            "ý kiến kiểm toán",
            "y kien kiem toan",
            "kết luận của kiểm toán viên",
            "ket luan cua kiem toan vien",
            "kết luận soát xét",
            "ket luan soat xet",
            "vấn đề cần nhấn mạnh",
            "van de can nhan manh",
            "kiểm toán viên",
            "kiem toan vien",
            "đơn vị kiểm toán",
            "don vi kiem toan",
            "công ty kiểm toán",
            "cong ty kiem toan",
            "hãng kiểm toán",
            "hang kiem toan",
            "công ty thực hiện kiểm toán",
            "cong ty thuc hien kiem toan",
            "đơn vị thực hiện kiểm toán",
            "don vi thuc hien kiem toan",
            "công ty thực hiện kế toán kiểm toán",
            "cong ty thuc hien ke toan kiem toan",
            "ban tổng giám đốc",
            "ban tong giam doc",
            "ban điều hành",
            "ban dieu hanh",
            "hội đồng quản trị",
            "hoi dong quan tri",
            "trách nhiệm của ban",
            "trach nhiem cua ban",
            "người ký",
            "nguoi ky",
            "ngày ký",
            "ngay ky",
            "ngày lập báo cáo",
            "ngay lap bao cao",
        )
    ):
        return "get_report_section_info"

    if any(
        marker in text
        for marker in (
            "thuyết minh",
            "thuyet minh",
            "chính sách kế toán",
            "chinh sach ke toan",
            "bên liên quan",
            "ben lien quan",
            "cam kết",
            "cam ket",
            "rủi ro tài chính",
            "rui ro tai chinh",
            "vay và nợ thuê tài chính",
        )
    ):
        return "get_note_info"

    if any(
        marker in text
        for marker in (
            "lưu chuyển tiền",
            "luu chuyen tien",
            "dòng tiền",
            "dong tien",
            "tiền thu",
            "tien thu",
            "tiền chi",
            "tien chi",
            "trả nợ gốc vay",
            "tra no goc vay",
        )
    ):
        return "get_cashflow_info"

    if any(
        marker in text
        for marker in (
            "tài sản",
            "tai san",
            "nợ ngắn hạn",
            "no ngan han",
            "nợ dài hạn",
            "no dai han",
            "nợ phải trả",
            "no phai tra",
            "vốn chủ sở hữu",
            "von chu so huu",
            "hàng tồn kho",
            "hang ton kho",
            "phải thu",
            "phai thu",
            "phải trả",
            "phai tra",
            "nguồn vốn",
            "nguon von",
        )
    ):
        return "get_balance_sheet_info"

    return ANALYSIS_DEFAULT_TOOL.get(str(agent_name or "").strip(), "get_income_statement_info")


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
        "time_hint",
        "period",
        "unit",
        "value_type",
        "source",
        "message",
        "evidence_text",
    ):
        if not str(merged.get(key, "") or "").strip() and str(incoming.get(key, "") or "").strip():
            merged[key] = str(incoming.get(key, "") or "").strip()

    existing_status = normalize_fact_status(merged.get("status"))
    incoming_status = normalize_fact_status(incoming.get("status"))
    if existing_status != FACT_STATUS_FOUND and incoming_status == FACT_STATUS_FOUND:
        merged["status"] = FACT_STATUS_FOUND
    elif not str(merged.get("status", "") or "").strip() and str(incoming.get("status", "") or "").strip():
        merged["status"] = incoming_status

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
        "message": not_found_after_search_message(item_name, table_name),
    }


def filter_facts_for_query(
    facts: list[dict],
    *,
    table: str,
    query: str,
    source: str = "",
) -> list[dict]:
    table_name = normalize_evidence_table(table)
    query_text = normalize_evidence_query(query, table=table_name)
    clean_facts = []
    exact_facts = []

    for fact in dedupe_facts(facts or []):
        if not isinstance(fact, dict):
            continue
        fact_payload = dict(fact)
        fact_table = normalize_evidence_table(fact_payload.get("table", "")) or table_name
        if fact_table:
            fact_payload["table"] = fact_table

        if table_name in MAIN_REPORT_TABLES:
            if fact_table and fact_table != table_name:
                # Cross-table candidates are usable only when they independently
                # match the requested fact.  A non-empty value is not evidence of
                # relevance.
                if requirement_matches_fact(query_text, fact_payload, table=fact_table):
                    clean_facts.append(fact_payload)
                continue
            if requirement_matches_fact(query_text, fact_payload, table=table_name):
                clean_facts.append(fact_payload)
                if _fact_label_exactly_matches(query_text, fact_payload, table=table_name):
                    exact_facts.append(fact_payload)
            continue

        clean_facts.append(fact_payload)

    multi_fact_markers = (
        " so sánh ", " và ", "liệt kê", "danh sách", "các khoản",
        "những", "nào", "mỗi", "từng", "biến động",
    )
    padded_query = f" {collapse_text(query)} "
    is_multi_fact_query = any(marker in padded_query for marker in multi_fact_markers)

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

    if table_name in MAIN_REPORT_TABLES and query_text and not clean_facts:
        return [not_found_fact(table_name, query_text, source=source)]

    return dedupe_facts(clean_facts)


def result_to_facts(
    result: dict,
    *,
    table: str,
    query: str,
    limit: int = 5,
) -> list[dict]:
    table_name = normalize_evidence_table(table)
    docs, metas = _extract_docs_and_metas(result or {})
    facts = []

    for doc, meta in list(zip_longest(docs, metas, fillvalue=None))[:limit]:
        if doc is None:
            continue
        metadata_missing = not isinstance(meta, dict)
        meta = meta if isinstance(meta, dict) else {}
        evidence_text = doc.strip()
        heading = normalize_evidence_table(meta.get("heading", "")) or table_name
        item_name = str(meta.get("item_name", "") or "").strip() or normalize_evidence_query(query, table=heading)
        raw_value = str(meta.get("raw_value", "") or "").strip()
        normalized_value = str(meta.get("normalized_value", "") or "").strip()
        value = raw_value or normalized_value or evidence_text
        source = str(meta.get("source", "") or result.get("source", "") or "").strip()
        facts.append(
            {
                "content_type": "table_fact",
                "company": str(meta.get("company", "") or "").strip(),
                "item_name": item_name,
                "time_hint": str(meta.get("period", "") or meta.get("time_hint", "") or "").strip(),
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
                "value_type": str(meta.get("value_type", "") or "").strip(),
                "unit": str(meta.get("unit", "") or "").strip(),
                "status": (
                    FACT_STATUS_AMBIGUOUS
                    if metadata_missing
                    else FACT_STATUS_FOUND if value else FACT_STATUS_NOT_FOUND
                ),
                "evidence_text": evidence_text,
                **({"message": "Retrieved document is missing required metadata."} if metadata_missing else {}),
            }
        )

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
                "message": not_found_after_search_message(item_name, table_name),
            }
        )

    return filter_facts_for_query(
        facts,
        table=table_name,
        query=query,
        source=str((result or {}).get("source", "") or "").strip(),
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
