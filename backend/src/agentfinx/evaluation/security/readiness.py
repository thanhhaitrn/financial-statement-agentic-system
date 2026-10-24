"""Read-only checks of a built dataset, for the main experiment.

The main runs never build: :func:`open_read_only` activates an already built
dataset the way chat does (no ``ensure_built``, no new collection) and
:func:`freshness` tells, without building anything, whether ``ensure_built``
would rebuild it: the knowledge base's manifest against the source and
parser, and the active vector collection against the knowledge base. Both
mirror ``workflow.pipeline.ensure_built``, ``ingestion.pipeline.
build_knowledge_base`` and ``datasets.vectorstore.index_builder.
build_vector_store``.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def open_read_only(dataset_id: str):
    """(dataset, collection, identity) of a built dataset; raises
    ``DatasetNotReady`` instead of building."""

    from agentfinx.datasets.activation import activate_dataset_for_query
    from agentfinx.datasets.kb.sqlite_repo import open_db_readonly
    from agentfinx.datasets.registry import get_dataset
    from agentfinx.evaluation.batch import built_dataset_identity

    dataset = get_dataset(dataset_id)
    if dataset is None:
        raise LookupError(f"dataset {dataset_id} not found")
    activated = activate_dataset_for_query(dataset)
    conn = open_db_readonly(str(dataset.sqlite_db_path))
    try:
        identity = built_dataset_identity(activated.dataset, conn, activated.collection)
    finally:
        conn.close()
    return activated.dataset, activated.collection, identity


def freshness(dataset_id: str) -> dict:
    """Would ``ensure_built`` rebuild this dataset? Reads only."""

    from agentfinx.core.config.settings import DEFAULT_DATASET
    from agentfinx.datasets.kb.sqlite_repo import open_db_readonly, read_kb_manifest
    from agentfinx.datasets.registry import get_dataset
    from agentfinx.datasets.vectorstore import index_builder
    from agentfinx.datasets.vectorstore.qdrant_store import active_collection_matches_build
    from agentfinx.ingestion import pipeline as ingestion

    dataset = get_dataset(dataset_id)
    if dataset is None:
        return {"current": False, "reason": "dataset_not_found"}
    if str(dataset.ingestion_version or "").strip() != str(DEFAULT_DATASET["ingestion_version"] or "").strip():
        return {"current": False, "reason": "ingestion_version_outdated"}
    effective = ingestion.resolve_dataset_metadata(dataset)
    source_path, source_bytes, md_text = ingestion._read_source(effective)
    resolved = ingestion._resolve_ingestion_metadata(effective, md_text)
    identity = ingestion._manifest_identity(effective, source_path, source_bytes, resolved)
    db_path = Path(str(effective.sqlite_db_path))
    if not ingestion._existing_kb_matches(db_path, identity):
        return {"current": False, "reason": "kb_stale_for_source_or_parser"}

    conn = open_db_readonly(str(db_path))
    try:
        digest, count, seen, occurrences = hashlib.sha256(), 0, set(), {}
        for frame in index_builder._fact_chunks(conn):
            documents, metadatas, ids = index_builder._chunk_payload(frame, occurrences)
            if seen.intersection(ids):
                return {"current": False, "reason": "duplicate_vector_ids"}
            seen.update(ids)
            index_builder._update_input_digest(digest, documents, metadatas, ids)
            count += len(documents)
        fingerprint = index_builder._vector_build_fingerprint(digest.hexdigest(), read_kb_manifest(conn))
    finally:
        conn.close()
    if not active_collection_matches_build(dataset.vector_collection_name, build_fingerprint=fingerprint,
                                           expected_count=count):
        return {"current": False, "reason": "vectors_stale_for_kb"}
    return {"current": True, "reason": "", "vector_docs": count}
