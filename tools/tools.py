from schemas.table_names import normalize_table_heading


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


def get_related_info(query: str, table: str, collection):
    requested_table = normalize_table_heading(table)
    results = collection.query(
        query_texts=[query],
        n_results=5,
        where={"heading": requested_table},
    )

    docs, metas = _extract_docs_and_metas(results)

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
            docs = filtered_docs
            metas = filtered_metas

    context = "\n".join(docs)
    return {"context": context, "source": _join_sources(metas)}

def web_search(query: str):
    return {"context": "Sample return from web.", "source": "Web"}

def calculate_dti():
    return {"context": 0.36, "source": "calculate_dti"}
