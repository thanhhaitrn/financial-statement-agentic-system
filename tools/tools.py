import re

from schemas.table_names import normalize_table_heading


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
_REPORT_WIDE_LIMIT = 50
_REPORT_WIDE_MIN_COVERAGE = 0.6
_REPORT_WIDE_MIN_OVERLAP = 4


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
    if isinstance(meta, dict):
        item_name = str(meta.get("item_name", "") or "")

    query_norm = _normalize_text(query)
    item_norm = _normalize_text(item_name)
    doc_norm = _normalize_text(doc)
    query_tokens = _text_tokens(query)
    item_tokens = _text_tokens(item_name)
    doc_tokens = _text_tokens(doc)

    score = 0.0

    if item_norm == query_norm:
        score += 100.0
    if item_norm.startswith(query_norm) and query_norm:
        score += 40.0
    if query_norm and query_norm in item_norm:
        score += 30.0

    if query_tokens and item_tokens:
        overlap = len(query_tokens & item_tokens)
        coverage = overlap / len(query_tokens)
        precision = overlap / len(item_tokens)
        score += coverage * 25.0
        score += precision * 10.0

    if query_tokens and doc_tokens:
        overlap = len(query_tokens & doc_tokens)
        score += (overlap / len(query_tokens)) * 5.0

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


def _heading_matches_requested_table(meta, requested_table):
    if not requested_table or not isinstance(meta, dict):
        return False
    return normalize_table_heading(meta.get("heading", "")) == requested_table


def _is_strong_report_wide_match(query, meta, doc):
    item_name = ""
    if isinstance(meta, dict):
        item_name = str(meta.get("item_name", "") or "")

    candidate_text = item_name or str(doc or "")
    query_norm = _normalize_text(query)
    candidate_norm = _normalize_text(candidate_text)
    if not query_norm or not candidate_norm:
        return False

    if candidate_norm == query_norm or query_norm in candidate_norm:
        return True

    query_tokens = _text_tokens(query)
    candidate_tokens = _text_tokens(candidate_text)
    if not query_tokens or not candidate_tokens:
        return False

    overlap = len(query_tokens & candidate_tokens)
    coverage = overlap / len(query_tokens)
    return coverage >= _REPORT_WIDE_MIN_COVERAGE or overlap >= _REPORT_WIDE_MIN_OVERLAP


def _report_wide_fallback_matches(query, requested_table, docs, metas, limit=5):
    if not docs:
        return [], []

    same_table_docs = []
    same_table_metas = []
    same_table_strong_docs = []
    same_table_strong_metas = []
    report_wide_docs = []
    report_wide_metas = []

    for doc, meta in zip(docs or [], metas or []):
        strong_match = _is_strong_report_wide_match(query, meta, doc)

        if _heading_matches_requested_table(meta, requested_table):
            same_table_docs.append(doc)
            same_table_metas.append(meta)
            if strong_match:
                same_table_strong_docs.append(doc)
                same_table_strong_metas.append(meta)

        if strong_match:
            report_wide_docs.append(doc)
            report_wide_metas.append(meta)

    if same_table_strong_docs:
        return _rerank_matches(query, same_table_strong_docs, same_table_strong_metas, limit=limit)

    if report_wide_docs:
        return _rerank_matches(query, report_wide_docs, report_wide_metas, limit=limit)

    if same_table_docs:
        return _rerank_matches(query, same_table_docs, same_table_metas, limit=limit)

    return _rerank_matches(query, report_wide_docs, report_wide_metas, limit=limit)


def _has_strong_match(query, docs, metas):
    for doc, meta in zip(docs or [], metas or []):
        if _is_strong_report_wide_match(query, meta, doc):
            return True
    return False


def get_related_info(query: str, table: str, collection):
    requested_table = normalize_table_heading(table)
    results = collection.query(
        query_texts=[query],
        n_results=20,
        where={"heading": requested_table},
    )

    docs, metas = _extract_docs_and_metas(results)
    docs, metas = _rerank_matches(query, docs, metas, limit=5)

    if not docs or not _has_strong_match(query, docs, metas):
        fallback = collection.query(query_texts=[query], n_results=_REPORT_WIDE_LIMIT)
        fallback_docs, fallback_metas = _extract_docs_and_metas(fallback)
        fallback_ranked_docs, fallback_ranked_metas = _report_wide_fallback_matches(
            query,
            requested_table,
            fallback_docs,
            fallback_metas,
            limit=5,
        )
        if fallback_ranked_docs:
            docs, metas = fallback_ranked_docs, fallback_ranked_metas

    context = "\n".join(docs)
    return {"context": context, "source": _join_sources(metas)}

def web_search(query: str):
    return {"context": "Sample return from web.", "source": "Web"}

def calculate_dti():
    return {"context": 0.36, "source": "calculate_dti"}
