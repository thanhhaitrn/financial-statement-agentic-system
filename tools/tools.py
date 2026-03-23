import re

from schemas.table_names import normalize_table_heading


_SPACE_RE = re.compile(r"\s+")
_TOKEN_RE = re.compile(r"\w+", flags=re.UNICODE)


def _normalize_text(value):
    text = str(value or "").strip().lower()
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


def get_related_info(query: str, table: str, collection):
    requested_table = normalize_table_heading(table)
    results = collection.query(
        query_texts=[query],
        n_results=20,
        where={"heading": requested_table},
    )

    docs, metas = _extract_docs_and_metas(results)
    docs, metas = _rerank_matches(query, docs, metas, limit=5)

    if not docs:
        fallback = collection.query(query_texts=[query], n_results=20)
        fallback_docs, fallback_metas = _extract_docs_and_metas(fallback)
        filtered_docs = []
        filtered_metas = []

        for doc, meta in zip(fallback_docs, fallback_metas):
            if not isinstance(meta, dict):
                continue
            if normalize_table_heading(meta.get("heading", "")) != requested_table:
                continue
            filtered_docs.append(doc)
            filtered_metas.append(meta)

        if filtered_docs:
            docs, metas = _rerank_matches(query, filtered_docs, filtered_metas, limit=5)

    context = "\n".join(docs)
    return {"context": context, "source": _join_sources(metas)}

def web_search(query: str):
    return {"context": "Sample return from web.", "source": "Web"}

def calculate_dti():
    return {"context": 0.36, "source": "calculate_dti"}
