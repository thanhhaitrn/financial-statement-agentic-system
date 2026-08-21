"""Qdrant-backed collection helpers for vector retrieval."""
# Code note: Vectorstore modules turn normalized facts into searchable text and metadata for retrieval.

from __future__ import annotations

import hashlib
import math
import os
import uuid
from urllib.parse import urlparse

from dotenv import load_dotenv
from langchain_ollama import OllamaEmbeddings
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

load_dotenv()

EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "bge-m3")
EMBEDDING_BASE_URL = os.getenv("OLLAMA_EMBEDDING_BASE_URL") or os.getenv(
    "OLLAMA_LOCAL_BASE_URL",
    "http://127.0.0.1:11434",
)
EMBEDDING_QUERY_INSTRUCTION = os.getenv(
    "OLLAMA_EMBEDDING_QUERY_INSTRUCTION",
    (
        "Given a Vietnamese financial statement analysis query, retrieve the most "
        "relevant fact rows, note passages, line items, values, and fiscal periods "
        "needed to answer it."
    ),
)
EMBEDDING_DOCUMENT_INSTRUCTION = os.getenv(
    "OLLAMA_EMBEDDING_DOCUMENT_INSTRUCTION",
    "",
)

QDRANT_URL = (
    os.getenv("QDRANT_URL", "").strip()
    or os.getenv("QDRANT_CLOUD_URL", "").strip()
)
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip()
QDRANT_LOCATION = os.getenv("QDRANT_LOCATION", "").strip()
QDRANT_TIMEOUT = int(os.getenv("QDRANT_TIMEOUT", "60"))
QDRANT_VECTOR_SIZE = int(os.getenv("QDRANT_VECTOR_SIZE", "1024"))
QDRANT_MAX_DOCUMENT_CHARS = int(os.getenv("QDRANT_MAX_DOCUMENT_CHARS", "32768"))
QDRANT_DISTANCE = models.Distance.COSINE

_DOCUMENT_PAYLOAD_KEY = "document"
_SOURCE_ID_PAYLOAD_KEY = "_source_id"
_POINT_NAMESPACE = uuid.UUID("5f420134-6b1e-4ef5-bfa7-0a8739ad9e26")
_client: QdrantClient | None = None


def _is_local_base_url(base_url: str | None) -> bool:
    host = urlparse(str(base_url or "")).hostname or ""
    return host in {"localhost", "127.0.0.1", "::1"}


def _ollama_client_kwargs(base_url: str | None):
    embedding_api_key = os.getenv("OLLAMA_EMBEDDING_API_KEY", "").strip()
    api_key = embedding_api_key
    if not api_key and not _is_local_base_url(base_url):
        api_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not api_key:
        return {}
    return {"headers": {"Authorization": f"Bearer {api_key}"}}


# The Qwen3 embedding family expects an "Instruct:/Query:" wrapper; other models
# (e.g. bge-m3) retrieve best on plain text, so the wrapper is model-gated.
_USE_QWEN_INSTRUCT = "qwen" in EMBEDDING_MODEL.lower()


def _format_qwen_query(text: str) -> str:
    query = str(text or "").strip()
    if not _USE_QWEN_INSTRUCT:
        return query
    return f"Instruct: {EMBEDDING_QUERY_INSTRUCTION.strip()}\nQuery: {query}"


def _format_qwen_document(text: str) -> str:
    document = str(text or "").strip()
    if not _USE_QWEN_INSTRUCT:
        return document
    instruction = EMBEDDING_DOCUMENT_INSTRUCTION.strip()
    if not instruction:
        return document
    return f"Instruct: {instruction}\nDocument: {document}"


class Qwen3OllamaEmbeddingFunction:
    """Embedding wrapper used by Qdrant ingestion and query code."""

    def __init__(self, model: str, base_url: str | None = None):
        self._embeddings = OllamaEmbeddings(
            model=model,
            base_url=base_url,
            client_kwargs=_ollama_client_kwargs(base_url),
        )

    def embed_documents(self, documents: list[str]) -> list[list[float]]:
        formatted = [_format_qwen_document(document) for document in documents]
        return self._embeddings.embed_documents(formatted)

    def embed_query(self, query: str) -> list[float]:
        return self._embeddings.embed_query(_format_qwen_query(query))


_embedding_function_instance: Qwen3OllamaEmbeddingFunction | None = None


def get_embedding_function() -> Qwen3OllamaEmbeddingFunction:
    global _embedding_function_instance
    if _embedding_function_instance is None:
        _embedding_function_instance = Qwen3OllamaEmbeddingFunction(
            model=EMBEDDING_MODEL,
            base_url=EMBEDDING_BASE_URL,
        )
    return _embedding_function_instance


class _LazyEmbeddingProxy:
    def __getattr__(self, name):
        return getattr(get_embedding_function(), name)


# Compatibility surface for existing tests/callers; construction is now lazy.
embedding_function = _LazyEmbeddingProxy()


def _make_client() -> QdrantClient:
    if QDRANT_LOCATION:
        return QdrantClient(location=QDRANT_LOCATION, timeout=QDRANT_TIMEOUT)

    if not QDRANT_URL:
        raise RuntimeError(
            "Missing QDRANT_URL env var. Set it to your Qdrant Cloud cluster URL."
        )

    if not QDRANT_API_KEY and not _is_local_base_url(QDRANT_URL):
        raise RuntimeError("Missing QDRANT_API_KEY env var for Qdrant Cloud.")

    return QdrantClient(
        url=QDRANT_URL,
        api_key=QDRANT_API_KEY or None,
        timeout=QDRANT_TIMEOUT,
    )


def get_client() -> QdrantClient:
    global _client
    if _client is None:
        _client = _make_client()
    return _client


def _collection_metadata(extra: dict | None = None) -> dict:
    metadata = {
        "embedding_model": EMBEDDING_MODEL,
        "embedding_document_instruction": EMBEDDING_DOCUMENT_INSTRUCTION,
        "vector_size": QDRANT_VECTOR_SIZE,
    }
    metadata.update(dict(extra or {}))
    # Build metadata may add fields but cannot lie about the embedding contract.
    metadata.update(
        {
            "embedding_model": EMBEDDING_MODEL,
            "embedding_document_instruction": EMBEDDING_DOCUMENT_INSTRUCTION,
            "vector_size": QDRANT_VECTOR_SIZE,
        }
    )
    return metadata


def _collection_matches_current_config(info) -> bool:
    vectors = info.config.params.vectors
    vector_size = getattr(vectors, "size", None)
    metadata = getattr(info.config, "metadata", None) or {}
    return (
        vector_size == QDRANT_VECTOR_SIZE
        and metadata.get("embedding_model") == EMBEDDING_MODEL
        and metadata.get("embedding_document_instruction", "")
        == EMBEDDING_DOCUMENT_INSTRUCTION
    )


def _ensure_payload_indexes(client: QdrantClient, collection_name: str) -> None:
    for field_name in ("heading", "note_ref"):
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:
            message = str(exc).lower()
            if "already exists" not in message and "wrong input" not in message:
                raise


def _normalize_point_id(collection_name: str, raw_id) -> int | str:
    raw = str(raw_id)
    if raw.isdigit():
        return int(raw)
    return str(uuid.uuid5(_POINT_NAMESPACE, f"{collection_name}:{raw}"))


def _payload_from_doc_meta(document: str, metadata: dict | None, raw_id) -> dict:
    payload = dict(metadata or {})
    payload[_DOCUMENT_PAYLOAD_KEY] = str(document or "")
    payload[_SOURCE_ID_PAYLOAD_KEY] = str(raw_id)
    return payload


def _metadata_from_payload(payload: dict | None) -> dict:
    payload = dict(payload or {})
    payload.pop(_DOCUMENT_PAYLOAD_KEY, None)
    payload.pop(_SOURCE_ID_PAYLOAD_KEY, None)
    return payload


def _where_filter(where: dict | None):
    if not where:
        return None

    conditions = []
    for key, value in where.items():
        conditions.append(
            models.FieldCondition(
                key=str(key),
                match=models.MatchValue(value=value),
            )
        )

    return models.Filter(must=conditions)


class QdrantCollectionAdapter:
    """Small collection adapter used by the existing retrieval pipeline."""

    def __init__(
        self,
        name: str,
        client: QdrantClient,
        *,
        qdrant_name: str | None = None,
        incompatible_count_is_zero: bool = False,
        build_fingerprint: str = "",
    ):
        self.name = name
        self._client = client
        self._qdrant_name = qdrant_name or name
        self._incompatible_count_is_zero = incompatible_count_is_zero
        self._build_fingerprint = str(build_fingerprint or "").strip().lower()

    @property
    def qdrant_name(self) -> str:
        return self._qdrant_name

    @property
    def build_fingerprint(self) -> str:
        return self._build_fingerprint

    @property
    def generation(self) -> str:
        """Stable cache/report generation, changing whenever the corpus changes."""

        return self._build_fingerprint or self._qdrant_name

    def count(self) -> int:
        if self._incompatible_count_is_zero:
            return 0
        result = self._client.count(
            collection_name=self._qdrant_name,
            exact=True,
        )
        return int(result.count or 0)

    def add(self, documents, metadatas, ids) -> None:
        docs = [str(document or "") for document in documents]
        metadata_items = list(metadatas)
        raw_ids = list(ids)
        validate_index_inputs(docs, metadata_items, raw_ids)
        vectors = embedding_function.embed_documents(docs)
        _validate_embedding_vectors(vectors, expected_count=len(docs))
        points = [
            models.PointStruct(
                id=_normalize_point_id(self.name, raw_id),
                vector=vector,
                payload=_payload_from_doc_meta(document, metadata, raw_id),
            )
            for document, metadata, raw_id, vector in zip(docs, metadata_items, raw_ids, vectors)
        ]
        if not points:
            return

        self._client.upsert(
            collection_name=self._qdrant_name,
            points=points,
            wait=True,
        )

    def query(self, query_embeddings, n_results: int = 10, where: dict | None = None):
        all_documents = []
        all_metadatas = []
        all_distances = []
        all_ids = []
        query_filter = _where_filter(where)

        for query_embedding in query_embeddings:
            response = self._client.query_points(
                collection_name=self._qdrant_name,
                query=query_embedding,
                query_filter=query_filter,
                limit=n_results,
                with_payload=True,
                with_vectors=False,
            )
            points = response.points or []
            documents = [
                str((point.payload or {}).get(_DOCUMENT_PAYLOAD_KEY, "") or "")
                for point in points
            ]
            metadatas = [_metadata_from_payload(point.payload) for point in points]
            distances = [1.0 - float(point.score or 0.0) for point in points]
            ids = [str(point.id) for point in points]

            all_documents.append(documents)
            all_metadatas.append(metadatas)
            all_distances.append(distances)
            all_ids.append(ids)

        return {
            "ids": all_ids,
            "documents": all_documents,
            "metadatas": all_metadatas,
            "distances": all_distances,
        }

    def get(self, where: dict | None = None, include=None):
        include = include or ["documents", "metadatas"]
        scroll_filter = _where_filter(where)
        offset = None
        ids = []
        documents = []
        metadatas = []

        while True:
            records, offset = self._client.scroll(
                collection_name=self._qdrant_name,
                scroll_filter=scroll_filter,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for record in records:
                payload = record.payload or {}
                ids.append(str(record.id))
                documents.append(str(payload.get(_DOCUMENT_PAYLOAD_KEY, "") or ""))
                metadatas.append(_metadata_from_payload(payload))

            if offset is None:
                break

        result = {"ids": ids}
        if "documents" in include:
            result["documents"] = documents
        if "metadatas" in include:
            result["metadatas"] = metadatas
        return result


def validate_index_inputs(documents: list[str], metadatas: list, ids: list) -> None:
    if not (len(documents) == len(metadatas) == len(ids)):
        raise ValueError(
            "documents, metadatas and ids must have identical lengths "
            f"({len(documents)}, {len(metadatas)}, {len(ids)})"
        )
    if len(set(str(raw_id) for raw_id in ids)) != len(ids):
        raise ValueError("Qdrant point ids must be unique within a build batch")
    for index, document in enumerate(documents):
        if not document.strip():
            raise ValueError(f"embedding document at index {index} is empty")
        if len(document) > QDRANT_MAX_DOCUMENT_CHARS:
            raise ValueError(
                f"embedding document at index {index} exceeds "
                f"QDRANT_MAX_DOCUMENT_CHARS={QDRANT_MAX_DOCUMENT_CHARS}"
            )
        if not isinstance(metadatas[index], dict):
            raise ValueError(f"metadata at index {index} must be a mapping")
        if not str(ids[index]).strip():
            raise ValueError(f"point id at index {index} is empty")


def _validate_embedding_vectors(vectors, *, expected_count: int) -> None:
    if len(vectors) != expected_count:
        raise ValueError(
            f"embedding provider returned {len(vectors)} vectors for {expected_count} documents"
        )
    for index, vector in enumerate(vectors):
        if len(vector) != QDRANT_VECTOR_SIZE:
            raise ValueError(
                f"embedding vector {index} has dimension {len(vector)}; "
                f"expected {QDRANT_VECTOR_SIZE}"
            )
        if not all(math.isfinite(float(value)) for value in vector):
            raise ValueError(f"embedding vector {index} contains non-finite values")


def _active_alias_name(logical_name: str) -> str:
    candidate = f"{logical_name}__active"
    if len(candidate) <= 240:
        return candidate
    digest = hashlib.sha256(logical_name.encode("utf-8")).hexdigest()[:16]
    return f"{logical_name[:210]}__active_{digest}"


def _alias_targets(client: QdrantClient) -> dict[str, str]:
    getter = getattr(client, "get_aliases", None)
    if not callable(getter):
        return {}
    response = getter()
    return {
        str(alias.alias_name): str(alias.collection_name)
        for alias in (getattr(response, "aliases", None) or [])
    }


def _active_qdrant_name(client: QdrantClient, logical_name: str) -> str:
    alias_name = _active_alias_name(logical_name)
    if alias_name in _alias_targets(client):
        return alias_name
    return logical_name


def create_collection(name: str) -> QdrantCollectionAdapter:
    """Return the active collection without deleting an incompatible corpus."""

    client = get_client()
    qdrant_name = _active_qdrant_name(client, name)
    if client.collection_exists(collection_name=qdrant_name):
        info = client.get_collection(collection_name=qdrant_name)
        if _collection_matches_current_config(info):
            _ensure_payload_indexes(client, qdrant_name)
            metadata = getattr(info.config, "metadata", None) or {}
            return QdrantCollectionAdapter(
                name,
                client,
                qdrant_name=qdrant_name,
                build_fingerprint=str(metadata.get("build_fingerprint", "") or ""),
            )
        # Report zero so the existing ensure/build flow stages a replacement,
        # while leaving the last known-good collection queryable until swap.
        return QdrantCollectionAdapter(
            name,
            client,
            qdrant_name=qdrant_name,
            incompatible_count_is_zero=True,
        )

    client.create_collection(
        collection_name=qdrant_name,
        vectors_config=models.VectorParams(
            size=QDRANT_VECTOR_SIZE,
            distance=QDRANT_DISTANCE,
        ),
        metadata=_collection_metadata(),
    )
    _ensure_payload_indexes(client, qdrant_name)
    return QdrantCollectionAdapter(name, client, qdrant_name=qdrant_name)


def create_versioned_collection(
    logical_name: str,
    *,
    build_fingerprint: str,
    build_metadata: dict | None = None,
) -> QdrantCollectionAdapter:
    """Create an isolated physical collection for a not-yet-active build."""

    fingerprint = str(build_fingerprint or "").strip().lower()
    if not fingerprint:
        raise ValueError("build_fingerprint is required for a versioned collection")
    unique_suffix = uuid.uuid4().hex[:8]
    physical_name = f"{logical_name}__v_{fingerprint[:16]}_{unique_suffix}"
    client = get_client()
    metadata = {
        **dict(build_metadata or {}),
        "logical_collection": logical_name,
        "build_fingerprint": fingerprint,
    }
    client.create_collection(
        collection_name=physical_name,
        vectors_config=models.VectorParams(
            size=QDRANT_VECTOR_SIZE,
            distance=QDRANT_DISTANCE,
        ),
        metadata=_collection_metadata(metadata),
    )
    _ensure_payload_indexes(client, physical_name)
    return QdrantCollectionAdapter(
        logical_name,
        client,
        qdrant_name=physical_name,
        build_fingerprint=fingerprint,
    )


def validate_versioned_collection(
    collection: QdrantCollectionAdapter,
    *,
    expected_count: int,
    build_fingerprint: str,
) -> None:
    info = collection._client.get_collection(collection_name=collection.qdrant_name)
    if not _collection_matches_current_config(info):
        raise ValueError("staged collection embedding metadata or dimension is invalid")
    metadata = getattr(info.config, "metadata", None) or {}
    if metadata.get("build_fingerprint") != str(build_fingerprint).strip().lower():
        raise ValueError("staged collection build fingerprint does not match")
    actual_count = collection.count()
    if actual_count != int(expected_count):
        raise ValueError(
            f"staged collection contains {actual_count} points; expected {expected_count}"
        )


def activate_versioned_collection(
    logical_name: str,
    staged_collection: QdrantCollectionAdapter,
) -> QdrantCollectionAdapter:
    """Atomically point the logical active alias at a validated staged build."""

    client = staged_collection._client
    physical_name = staged_collection.qdrant_name
    if not client.collection_exists(collection_name=physical_name):
        raise ValueError(f"staged collection does not exist: {physical_name}")
    alias_name = _active_alias_name(logical_name)
    aliases = _alias_targets(client)
    operations = []
    if alias_name in aliases:
        operations.append(
            models.DeleteAliasOperation(
                delete_alias=models.DeleteAlias(alias_name=alias_name)
            )
        )
    operations.append(
        models.CreateAliasOperation(
            create_alias=models.CreateAlias(
                collection_name=physical_name,
                alias_name=alias_name,
            )
        )
    )
    client.update_collection_aliases(change_aliases_operations=operations)
    return QdrantCollectionAdapter(
        logical_name,
        client,
        qdrant_name=alias_name,
        build_fingerprint=staged_collection.build_fingerprint,
    )


def active_collection_matches_build(
    logical_name: str,
    *,
    build_fingerprint: str,
    expected_count: int,
) -> bool:
    client = get_client()
    qdrant_name = _active_qdrant_name(client, logical_name)
    if not client.collection_exists(collection_name=qdrant_name):
        return False
    info = client.get_collection(collection_name=qdrant_name)
    metadata = getattr(info.config, "metadata", None) or {}
    if not _collection_matches_current_config(info):
        return False
    if metadata.get("build_fingerprint") != str(build_fingerprint).strip().lower():
        return False
    return QdrantCollectionAdapter(
        logical_name,
        client,
        qdrant_name=qdrant_name,
    ).count() == int(expected_count)


def delete_collection(name: str) -> None:
    client = get_client()
    aliases = _alias_targets(client)
    alias_name = _active_alias_name(name)
    if alias_name in aliases:
        client.update_collection_aliases(
            change_aliases_operations=[
                models.DeleteAliasOperation(
                    delete_alias=models.DeleteAlias(alias_name=alias_name)
                )
            ]
        )

    candidates = {name}
    candidates.update(
        collection.name
        for collection in client.get_collections().collections
        if collection.name.startswith(f"{name}__v_")
    )
    if alias_name in aliases:
        candidates.add(aliases[alias_name])
    for collection_name in candidates:
        if not client.collection_exists(collection_name=collection_name):
            continue
        _delete_single_collection_compat(collection_name)


def _delete_single_collection_compat(name: str) -> None:
    """Legacy narrow delete helper retained for exception compatibility tests."""

    client = get_client()
    try:
        client.delete_collection(collection_name=name)
    except (UnexpectedResponse, ValueError) as exc:
        message = str(exc).strip().lower()
        if "not found" in message or "doesn't exist" in message:
            return
        raise


def embed_query_text(query: str) -> list[float]:
    return embedding_function.embed_query(query)


def add_in_batches(collection, documents, metadatas, ids, batch_size=500):
    documents = list(documents)
    metadatas = list(metadatas)
    ids = list(ids)
    if int(batch_size) <= 0:
        raise ValueError("batch_size must be greater than zero")
    validate_index_inputs(documents, metadatas, ids)
    for i in range(0, len(documents), batch_size):
        collection.add(
            documents=documents[i:i + batch_size],
            metadatas=metadatas[i:i + batch_size],
            ids=ids[i:i + batch_size],
        )
