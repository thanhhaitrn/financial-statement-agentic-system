"""CLI smoke test for querying only the notes vector slice."""
# Code note: Diagnostic script code compares note-specific retrieval behavior against stored vector data.

import argparse
import json


DEFAULT_NOTE_QUERY = "vay và nợ thuê tài chính"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Query only the notes-to-financial-statements vector slice."
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_NOTE_QUERY,
        help="Vietnamese note query to search in the vector store.",
    )
    parser.add_argument(
        "--dataset-id",
        default="",
        help="Registered dataset id. Defaults to the repo default dataset.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of vector matches to return.",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force rebuild SQLite facts and Qdrant vectors before querying.",
    )
    return parser.parse_args()


def _load_dataset(dataset_id: str):
    from dataset_catalog.registry import get_dataset
    from test import ensure_default_dataset

    # A named dataset lets this script validate a specific registered filing;
    # otherwise it follows the same default dataset path as the main CLI.
    if dataset_id:
        dataset = get_dataset(dataset_id)
        if dataset is None:
            raise SystemExit(f"Dataset not found: {dataset_id}")
        return dataset

    return ensure_default_dataset()


def _force_rebuild(dataset):
    from config.settings import DEFAULT_DATASET
    from dataset_catalog.registry import save_dataset
    from ingestion.pipeline import build_knowledge_base
    from vectorstore.index_builder import build_vector_store

    # Rebuild both layers together so SQLite rows and Qdrant documents stay in
    # sync after parser or ingestion-version changes.
    conn, facts_count = build_knowledge_base(dataset, reset=True)
    collection, vector_docs_count = build_vector_store(
        conn,
        dataset.vector_collection_name,
        reset=True,
    )
    dataset = save_dataset(
        dataset.model_copy(
            update={
                "facts_count": facts_count,
                "vector_docs_count": vector_docs_count,
                "status": "ready",
                "ingestion_version": DEFAULT_DATASET["ingestion_version"],
            }
        )
    )
    return dataset, conn, collection


def _ensure_ready(dataset, rebuild: bool):
    if rebuild:
        return _force_rebuild(dataset)

    from test import ensure_built

    return ensure_built(dataset)


def _raw_matches(collection, query: str, limit: int):
    from schemas.table_names import TABLE_NOTE
    from tools.tools import _match_key, _rerank_matches

    n_results = max(int(limit or 1), 100)
    # This raw Qdrant query mirrors note-scoped retrieval: it searches only rows
    # tagged as notes, so matches from balance sheet / income / cash flow are
    # not allowed to leak into the smoke-test output.
    from vectorstore.qdrant_store import embed_query_text

    raw = collection.query(
        query_embeddings=[embed_query_text(query)],
        n_results=n_results,
        where={"heading": TABLE_NOTE},
    )
    documents = (raw.get("documents") or [[]])[0]
    metadatas = (raw.get("metadatas") or [[]])[0]
    distances = (raw.get("distances") or [[]])[0]
    all_results = collection.get(
        where={"heading": TABLE_NOTE},
        include=["documents", "metadatas"],
    )
    all_documents = all_results.get("documents", []) or []
    all_metadatas = all_results.get("metadatas", []) or []
    paired_distances = {
        _match_key(document, metadata): distances[index] if index < len(distances) else None
        for index, (document, metadata) in enumerate(zip(documents, metadatas))
    }
    seen = set(paired_distances)
    for document, metadata in zip(all_documents, all_metadatas):
        key = _match_key(document, metadata)
        if key in seen:
            continue
        documents.append(document)
        metadatas.append(metadata)
        seen.add(key)
    documents, metadatas = _rerank_matches(query, documents, metadatas, limit=limit)

    rows = []
    for index, document in enumerate(documents):
        metadata = metadatas[index] if index < len(metadatas) else {}
        distance = paired_distances.get(_match_key(document, metadata))
        rows.append(
            {
                "rank": index + 1,
                "distance": distance,
                "document": document,
                "metadata": metadata,
            }
        )
    return rows


def main():
    from schemas.table_names import TABLE_NOTE
    from tools.tools import get_related_info

    args = parse_args()
    dataset = _load_dataset(args.dataset_id)
    dataset, _conn, collection = _ensure_ready(dataset, args.rebuild)

    # Exercise the production tool path first, then include raw vector matches
    # so ranking and metadata can be inspected side by side.
    tool_result = get_related_info(
        args.query,
        TABLE_NOTE,
        collection,
        strict_table=True,
    )
    payload = {
        "dataset_id": dataset.dataset_id,
        "collection": dataset.vector_collection_name,
        "query": args.query,
        "where": {"heading": TABLE_NOTE},
        "tool_context": tool_result,
        "matches": _raw_matches(collection, args.query, args.limit),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
