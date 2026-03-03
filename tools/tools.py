def get_related_info(query: str, table: str, collection):
    where = {"heading": table}
    results = collection.query(query_texts=[query], n_results=5, where = where)
    context = "\n".join(results["documents"][0])
    return {"context": context, "source": "document.md"}

def web_search(query: str):
    return {"context": "Sample return from web.", "source": "Web"}

def calculate_dti():
    return {"context": 0.36, "source": "calculate_dti"}