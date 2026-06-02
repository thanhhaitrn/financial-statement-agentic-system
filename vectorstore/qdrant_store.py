"""Qdrant-backed collection helpers for vector retrieval."""
# Code note: Vectorstore modules turn normalized facts into searchable text and metadata for retrieval.

from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse

from dotenv import load_dotenv
from langchain_ollama import OllamaEmbeddings
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

load_dotenv()

EMBEDDING_MODEL = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
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


def _format_qwen_query(text: str) -> str:
    query = str(text or "").strip()
    return f"Instruct: {EMBEDDING_QUERY_INSTRUCTION.strip()}\nQuery: {query}"


def _format_qwen_document(text: str) -> str:
    document = str(text or "").strip()
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


embedding_function = Qwen3OllamaEmbeddingFunction(
    model=EMBEDDING_MODEL,
    base_url=EMBEDDING_BASE_URL,
)


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


def _collection_metadata() -> dict:
    return {
        "embedding_model": EMBEDDING_MODEL,
        "embedding_document_instruction": EMBEDDING_DOCUMENT_INSTRUCTION,
        "vector_size": QDRANT_VECTOR_SIZE,
    }


def _collection_matches_current_config(info) -> bool:
    vectors = info.config.params.vectors
    vector_size = getattr(vectors, "size", None)
    metadata = info.config.metadata or {}
    return (
        vector_size == QDRANT_VECTOR_SIZE
        and metadata.get("embedding_model") == EMBEDDING_MODEL
        and metadata.get("embedding_document_instruction", "")
        == EMBEDDING_DOCUMENT_INSTRUCTION
    )


def _ensure_payload_indexes(client: QdrantClient, collection_name: str) -> None:
    for field_name in ("heading", "item_name", "item_code", "note_ref", "source"):
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

    def __init__(self, name: str, client: QdrantClient):
        self.name = name
        self._client = client

    def count(self) -> int:
        result = self._client.count(
            collection_name=self.name,
            exact=True,
        )
        return int(result.count or 0)

    def add(self, documents, metadatas, ids) -> None:
        docs = [str(document or "") for document in documents]
        vectors = embedding_function.embed_documents(docs)
        points = [
            models.PointStruct(
                id=_normalize_point_id(self.name, raw_id),
                vector=vector,
                payload=_payload_from_doc_meta(document, metadata, raw_id),
            )
            for document, metadata, raw_id, vector in zip(docs, metadatas, ids, vectors)
        ]
        if not points:
            return

        self._client.upsert(
            collection_name=self.name,
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
                collection_name=self.name,
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
                collection_name=self.name,
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


def create_collection(name: str) -> QdrantCollectionAdapter:
    client = get_client()
    if client.collection_exists(collection_name=name):
        info = client.get_collection(collection_name=name)
        if _collection_matches_current_config(info):
            _ensure_payload_indexes(client, name)
            return QdrantCollectionAdapter(name, client)
        client.delete_collection(collection_name=name)

    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(
            size=QDRANT_VECTOR_SIZE,
            distance=QDRANT_DISTANCE,
        ),
        metadata=_collection_metadata(),
    )
    _ensure_payload_indexes(client, name)
    return QdrantCollectionAdapter(name, client)


def delete_collection(name: str) -> None:
    client = get_client()
    try:
        client.delete_collection(collection_name=name)
    except (UnexpectedResponse, ValueError) as exc:
        message = str(exc).strip().lower()
        if "not found" in message or "doesn't exist" in message:
            return
        raise


def list_collection_names() -> list[str]:
    collections = get_client().get_collections().collections
    return [collection.name for collection in collections]


def delete_all_collections() -> list[str]:
    deleted = []
    for name in list_collection_names():
        delete_collection(name)
        deleted.append(name)
    return deleted


def embed_query_text(query: str) -> list[float]:
    return embedding_function.embed_query(query)


def add_in_batches(collection, documents, metadatas, ids, batch_size=500):
    for i in range(0, len(documents), batch_size):
        collection.add(
            documents=documents[i:i + batch_size],
            metadatas=metadatas[i:i + batch_size],
            ids=ids[i:i + batch_size],
        )
