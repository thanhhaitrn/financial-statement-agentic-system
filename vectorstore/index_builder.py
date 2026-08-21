"""Build or rebuild the Qdrant vector index from SQLite financial facts."""
# Code note: Vectorstore modules turn normalized facts into searchable text and metadata for retrieval.

import hashlib
import json

import pandas as pd

from config.settings import VECTOR_BATCH_SIZE
from kb.sqlite_repo import read_kb_manifest, sqlite_has_fact_columns
from vectorstore.lexical_index import reset_lexical_index
from vectorstore.qdrant_store import (
    active_collection_matches_build,
    activate_versioned_collection,
    add_in_batches,
    create_collection,
    create_versioned_collection,
    validate_index_inputs,
    validate_versioned_collection,
)
from vectorstore.text_builder import build_documents_and_metadata


def _stable_vector_ids(
    df: pd.DataFrame,
    documents: list[str],
    metadatas: list[dict],
    *,
    occurrence_by_hash: dict[str, int] | None = None,
) -> list[str]:
    if "fact_id" in df.columns:
        fact_ids = [str(value or "").strip() for value in df["fact_id"].tolist()]
        if all(fact_ids) and len(set(fact_ids)) == len(fact_ids):
            return fact_ids

    # Compatibility fallback for a caller that supplies a legacy SQLite DB.
    # It is independent of DataFrame row order except for truly identical rows.
    occurrence_by_hash = occurrence_by_hash if occurrence_by_hash is not None else {}
    ids = []
    for document, metadata in zip(documents, metadatas):
        identity = json.dumps(
            {"document": document, "metadata": metadata},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        base_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        occurrence = occurrence_by_hash.get(base_id, 0) + 1
        occurrence_by_hash[base_id] = occurrence
        ids.append(base_id if occurrence == 1 else f"{base_id}:{occurrence}")
    return ids


def _update_input_digest(
    digest,
    documents: list[str],
    metadatas: list[dict],
    ids: list[str],
) -> None:
    for document, metadata, raw_id in zip(documents, metadatas, ids):
        payload = json.dumps(
            {"id": raw_id, "document": document, "metadata": metadata},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")


def _vector_build_fingerprint(input_sha256: str, kb_manifest: dict) -> str:
    contract = json.dumps(
        {
            "input_sha256": input_sha256,
            "source_sha256": kb_manifest.get("source_sha256", ""),
            "parser_version": kb_manifest.get("parser_version", ""),
            "schema_version": kb_manifest.get("schema_version", ""),
            "facts_sha256": kb_manifest.get("facts_sha256", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(contract.encode("utf-8")).hexdigest()


def _fact_query(conn) -> str:
    fact_id_projection = "fact_id" if sqlite_has_fact_columns(conn, {"fact_id"}) else "'' AS fact_id"
    return f"""
        SELECT
            company,
            fiscal_year,
            heading,
            item_code,
            note_ref,
            subheading,
            item_name,
            value,
            raw_value,
            normalized_value,
            source,
            period,
            value_type,
            unit,
            {fact_id_projection}
        FROM financial_facts
        ORDER BY rowid
    """


def _fact_chunks(conn):
    return pd.read_sql_query(
        _fact_query(conn),
        conn,
        chunksize=max(1, VECTOR_BATCH_SIZE),
    )


def _chunk_payload(df: pd.DataFrame, occurrence_by_hash: dict[str, int]):
    documents, metadatas, _legacy_ids = build_documents_and_metadata(df)
    ids = _stable_vector_ids(
        df,
        documents,
        metadatas,
        occurrence_by_hash=occurrence_by_hash,
    )
    validate_index_inputs(documents, metadatas, ids)
    return documents, metadatas, ids

def build_vector_store(conn, collection_name: str, *, reset: bool = False):
    print("\n=== BUILDING VECTOR STORE ===")

    # First streaming pass computes an exact corpus fingerprint without holding
    # the complete DataFrame/document corpus in memory.
    digest = hashlib.sha256()
    expected_count = 0
    seen_ids: set[str] = set()
    occurrence_by_hash: dict[str, int] = {}
    for df in _fact_chunks(conn):
        documents, metadatas, ids = _chunk_payload(df, occurrence_by_hash)
        duplicate_ids = seen_ids.intersection(ids)
        if duplicate_ids:
            raise ValueError(f"duplicate stable vector id: {next(iter(duplicate_ids))}")
        seen_ids.update(ids)
        _update_input_digest(digest, documents, metadatas, ids)
        expected_count += len(documents)

    if expected_count == 0:
        raise ValueError("cannot replace a vector collection with an empty corpus")

    kb_manifest = read_kb_manifest(conn)
    input_sha256 = digest.hexdigest()
    build_fingerprint = _vector_build_fingerprint(input_sha256, kb_manifest)

    if not reset and active_collection_matches_build(
        collection_name,
        build_fingerprint=build_fingerprint,
        expected_count=expected_count,
    ):
        collection = create_collection(collection_name)
        print("Vector manifest matches SQLite corpus → skipping vector build")
        return collection, expected_count

    staged_collection = create_versioned_collection(
        collection_name,
        build_fingerprint=build_fingerprint,
        build_metadata={
            "source_sha256": kb_manifest.get("source_sha256", ""),
            "parser_version": kb_manifest.get("parser_version", ""),
            "schema_version": kb_manifest.get("schema_version", ""),
            "facts_sha256": kb_manifest.get("facts_sha256", ""),
            "input_sha256": input_sha256,
            "expected_count": expected_count,
        },
    )

    # Second streaming pass embeds/upserts bounded batches into the staged
    # physical collection. The active alias is untouched until validation.
    occurrence_by_hash = {}
    indexed_count = 0
    for df in _fact_chunks(conn):
        documents, metadatas, ids = _chunk_payload(df, occurrence_by_hash)
        add_in_batches(
            staged_collection,
            documents,
            metadatas,
            ids,
            batch_size=VECTOR_BATCH_SIZE,
        )
        indexed_count += len(documents)
    if indexed_count != expected_count:
        raise ValueError(
            f"staged vector input count mismatch: expected={expected_count} actual={indexed_count}"
        )
    validate_versioned_collection(
        staged_collection,
        expected_count=expected_count,
        build_fingerprint=build_fingerprint,
    )
    collection = activate_versioned_collection(collection_name, staged_collection)
    # Drop any cached lexical index so it rebuilds from the fresh corpus.
    reset_lexical_index(collection_name)
    print(f"Added {expected_count} documents to vector store")
    return collection, expected_count
