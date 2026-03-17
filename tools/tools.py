def get_related_info(query: str, table: str, collection):
    where = {"heading": table}
    results = collection.query(query_texts=[query], n_results=5, where=where)

    documents = results.get("documents", [[]])
    metadatas = results.get("metadatas", [[]])
    docs = documents[0] if documents else []
    metas = metadatas[0] if metadatas else []

    context = "\n".join(docs)

    sources = []
    for meta in metas:
        if not isinstance(meta, dict):
            continue
        source = str(meta.get("source", "")).strip()
        if source and source not in sources:
            sources.append(source)

    return {"context": context, "source": ", ".join(sources) if sources else ""}

def web_search(query: str):
    return {"context": "Sample return from web.", "source": "Web"}

def calculate_dti():
    return {"context": 0.36, "source": "calculate_dti"}
