"""Retrieval and web-search tool implementations exposed to agents."""
# Code note: Tool modules bridge agent requests to retrieval helpers; comments here mark guardrails around external calls.

import json
import re

from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    normalize_table_heading,
)
from vectorstore.qdrant_store import embed_query_text


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)
_ABBREV_REPLACEMENTS = {
    "tndn": "thu nhập doanh nghiệp",
    "tscd": "tài sản cố định",
    "tscđ": "tài sản cố định",
    "hdkd": "hoạt động kinh doanh",
    "hđkd": "hoạt động kinh doanh",
    "lctt": "lưu chuyển tiền tệ",
    "lnst": "lợi nhuận sau thuế",
    "qldn": "quản lý doanh nghiệp",
}
_GENERIC_TABLE_LABELS = {
    "tong",
    "tổng",
    "cong",
    "cộng",
    "tong cong",
    "tổng cộng",
}
_AGGREGATE_QUERY_TOKENS = {"tong", "tổng", "cong", "cộng", "total"}
_POLICY_QUERY_TOKENS = {
    "chinh",
    "chính",
    "sach",
    "sách",
    "phuong",
    "phương",
    "phap",
    "pháp",
    "hach",
    "hạch",
    "toan",
    "toán",
    "du",
    "dự",
    "phong",
    "phòng",
}


def _normalize_text(value):
    text = str(value or "").strip().lower()
    for short, expanded in _ABBREV_REPLACEMENTS.items():
        text = re.sub(rf"\b{re.escape(short)}\b", expanded, text)
    return _SPACE_RE.sub(" ", text)


def _text_tokens(value):
    return set(_TOKEN_RE.findall(_normalize_text(value)))


def _extract_docs_and_metas(results):
    documents = results.get("documents", [[]])
    metadatas = results.get("metadatas", [[]])
    docs = documents[0] if documents else []
    metas = metadatas[0] if metadatas else []
    return docs, metas


def _extract_flat_docs_and_metas(results):
    docs = results.get("documents", []) or []
    metas = results.get("metadatas", []) or []
    return docs, metas


def _match_key(doc, meta):
    if isinstance(meta, dict):
        stable_meta = {
            key: str(meta.get(key, "") or "")
            for key in (
                "heading",
                "item_code",
                "note_ref",
                "subheading",
                "item_name",
                "source",
                "raw_value",
                "normalized_value",
            )
        }
        return json.dumps(stable_meta, ensure_ascii=False, sort_keys=True)
    return str(doc)


def _merge_docs_and_metas(primary_docs, primary_metas, extra_docs, extra_metas):
    docs = list(primary_docs or [])
    metas = list(primary_metas or [])
    seen = {
        _match_key(doc, meta)
        for doc, meta in zip(docs, metas)
    }

    for doc, meta in zip(extra_docs or [], extra_metas or []):
        key = _match_key(doc, meta)
        if key in seen:
            continue
        docs.append(doc)
        metas.append(meta)
        seen.add(key)

    return docs, metas


def _join_sources(metas):
    sources = []
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        source = str(meta.get("source", "")).strip()
        if source and source not in sources:
            sources.append(source)
    return ", ".join(sources) if sources else ""


def _item_match_score(query, meta, doc):
    item_name = ""
    item_code = ""
    if isinstance(meta, dict):
        item_name = str(meta.get("item_name", "") or "")
        item_code = str(meta.get("item_code", "") or "")
        subheading = str(meta.get("subheading", "") or "")
        if subheading:
            item_name = f"{item_name} {subheading}".strip()

    query_norm = _normalize_text(query)
    item_norm = _normalize_text(item_name)
    row_label = item_name.split("|")[-1].strip() if "|" in item_name else ""
    row_norm = _normalize_text(row_label)
    query_tokens = _text_tokens(query)
    item_tokens = _text_tokens(item_name)
    row_tokens = _text_tokens(row_label)
    doc_tokens = _text_tokens(doc)

    score = 0.0

    # Favor exact item-name matches first; token overlap then rescues common
    # financial abbreviations and slightly different Vietnamese wording.
    if item_norm == query_norm:
        score += 100.0
    if item_norm.startswith(query_norm) and query_norm:
        score += 40.0
    if query_norm and query_norm in item_norm:
        score += 30.0
    if row_norm and query_norm and row_norm == query_norm:
        score += 80.0
    if row_norm and query_norm and query_norm in row_norm:
        score += 45.0

    if query_tokens and item_tokens:
        overlap = len(query_tokens & item_tokens)
        coverage = overlap / len(query_tokens)
        precision = overlap / len(item_tokens)
        score += coverage * 25.0
        score += precision * 10.0

    if query_tokens and row_tokens:
        row_overlap = len(query_tokens & row_tokens)
        score += (row_overlap / len(query_tokens)) * 30.0
        score += (row_overlap / max(len(row_tokens), 1)) * 15.0

    if query_tokens and doc_tokens:
        overlap = len(query_tokens & doc_tokens)
        score += (overlap / len(query_tokens)) * 5.0

    if item_code == "note_table":
        score += 8.0

    if item_code == "note_table" and row_norm in _GENERIC_TABLE_LABELS:
        query_is_aggregate = bool(query_tokens & _AGGREGATE_QUERY_TOKENS)
        score += 8.0 if query_is_aggregate else -18.0
    elif (
        item_code == "note_text"
        and item_norm.startswith("thuyết minh 2.")
        and not (query_tokens & _POLICY_QUERY_TOKENS)
    ):
        score -= 10.0
    elif item_code == "note_text" and item_norm.startswith("thuyết minh 2."):
        score += 20.0

    return score


def _rerank_matches(query, docs, metas, limit=5):
    rows = []
    for idx, (doc, meta) in enumerate(zip(docs or [], metas or [])):
        rows.append(
            (
                _item_match_score(query, meta, doc),
                idx,
                doc,
                meta,
            )
        )

    rows.sort(key=lambda item: (-item[0], item[1]))
    top_rows = rows[:limit]
    ranked_docs = [doc for _score, _idx, doc, _meta in top_rows]
    ranked_metas = [meta for _score, _idx, _doc, meta in top_rows]
    return ranked_docs, ranked_metas


def get_related_info(query: str, table: str, collection, strict_table: bool = False, limit: int = 5):
    requested_table = normalize_table_heading(table)
    primary_n_results = 100 if strict_table else 50
    results = collection.query(
        query_embeddings=[embed_query_text(query)],
        n_results=primary_n_results,
        where={"heading": requested_table},
    )

    docs, metas = _extract_docs_and_metas(results)
    if strict_table:
        try:
            all_results = collection.get(
                where={"heading": requested_table},
                include=["documents", "metadatas"],
            )
            all_docs, all_metas = _extract_flat_docs_and_metas(all_results)
            docs, metas = _merge_docs_and_metas(docs, metas, all_docs, all_metas)
        except Exception:
            pass

    docs, metas = _rerank_matches(query, docs, metas, limit=limit)

    context = "\n".join(docs)
    return {
        "context": context,
        "source": _join_sources(metas),
        "documents": docs,
        "metadatas": metas,
    }


def get_balance_sheet_info(query: str, collection, table: str = "", **_kwargs):
    return get_related_info(query=query, table=TABLE_BS, collection=collection)


def get_income_statement_info(query: str, collection, table: str = "", **_kwargs):
    return get_related_info(query=query, table=TABLE_IS, collection=collection)


def get_cashflow_info(query: str, collection, table: str = "", **_kwargs):
    return get_related_info(query=query, table=TABLE_CF, collection=collection)


def get_note_info(query: str, collection, table: str = "", **_kwargs):
    return get_related_info(query=query, table=TABLE_NOTE, collection=collection, strict_table=True)


def get_report_section_info(query: str, collection, table: str = "", **_kwargs):
    return get_related_info(
        query=query,
        table=TABLE_REPORT_SECTION,
        collection=collection,
        strict_table=True,
        limit=8,
    )


def web_search(query: str):
    return {"context": "Sample return from web.", "source": "Web"}
