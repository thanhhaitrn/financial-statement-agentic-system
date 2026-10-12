"""Lexical (sparse) retrieval index used to widen the dense candidate pool.

Dense embeddings rank exact financial figures poorly: in an offline recall@k
benchmark on the APEC report, lexical retrieval beat qwen3/bge-m3 dense at every
k>=5 (recall@20 0.96 vs ~0.82-0.84) because the distinctive VND amounts and
registration IDs match lexically. This module provides that lexical signal as a
candidate-recall booster fused into ``get_related_info`` — the existing
``_item_match_score`` reranker stays the final authority on ordering.

It uses a small dependency-free BM25 so it works in any environment. The index
is built lazily from whatever corpus the Qdrant collection exposes via ``.get``;
if the collection cannot enumerate documents (e.g. unit-test doubles) the index
degrades to empty and retrieval falls back to pure dense — no extra ``.query``
calls are issued.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from threading import RLock

# Keep dotted/comma VND amounts ("202.406.369.251") intact as single tokens
# alongside ordinary word tokens.
_TOKEN_RE = re.compile(r"\d[\d.,]*\d|\w+", flags=re.UNICODE)
_BM25_K1 = 1.5
_BM25_B = 0.75

_LOCK = RLock()
_INDEXES: dict[str, "LexicalIndex"] = {}


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(str(text or "").lower())


def _normalize_heading(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


class LexicalIndex:
    """BM25 retrieval over fact documents, filterable by table heading."""

    def __init__(self, documents: list[str], metadatas: list[dict]):
        self.documents = documents
        self.metadatas = metadatas
        self._headings = [_normalize_heading((m or {}).get("heading", "")) for m in metadatas]
        self._tf: list[dict[str, int]] = []
        self._len: list[int] = []
        self._df: dict[str, int] = defaultdict(int)
        self._postings: dict[str, list[int]] = defaultdict(list)
        self._avgdl = 0.0
        self._n = len(documents)
        if documents:
            self._fit(documents)

    def _fit(self, documents: list[str]) -> None:
        total_len = 0
        for i, doc in enumerate(documents):
            tokens = _tokenize(doc)
            tf: dict[str, int] = defaultdict(int)
            for tok in tokens:
                tf[tok] += 1
            self._tf.append(tf)
            self._len.append(len(tokens))
            total_len += len(tokens)
            for tok in tf:
                self._df[tok] += 1
                self._postings[tok].append(i)
        self._avgdl = (total_len / self._n) if self._n else 0.0

    @property
    def ready(self) -> bool:
        return self._n > 0 and self._avgdl > 0

    def _idf(self, term: str) -> float:
        df = self._df.get(term, 0)
        if df == 0:
            return 0.0
        return math.log(1 + (self._n - df + 0.5) / (df + 0.5))

    def query(self, text: str, *, table: str = "", top_n: int = 50) -> list[tuple[str, dict]]:
        if not self.ready:
            return []
        wanted = _normalize_heading(table)
        q_terms = set(_tokenize(text))
        scores: dict[int, float] = defaultdict(float)
        for term in q_terms:
            idf = self._idf(term)
            if idf <= 0:
                continue
            for i in self._postings.get(term, ()):
                if wanted and self._headings[i] != wanted:
                    continue
                tf = self._tf[i][term]
                denom = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * self._len[i] / self._avgdl)
                scores[i] += idf * (tf * (_BM25_K1 + 1)) / denom

        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[:top_n]
        return [(self.documents[i], self.metadatas[i]) for i, _ in ranked]


def _scroll_collection(collection) -> tuple[list[str], list[dict]]:
    """Best-effort enumeration of every document/metadata pair in a collection."""
    getter = getattr(collection, "get", None)
    if not callable(getter):
        return [], []
    result = getter(where=None, include=["documents", "metadatas"])
    docs = result.get("documents", []) or []
    metas = result.get("metadatas", []) or []
    docs = [str(d or "") for d in docs]
    metas = [m if isinstance(m, dict) else {} for m in metas]
    n = min(len(docs), len(metas))
    return docs[:n], metas[:n]


def get_lexical_index(collection_name: str, collection) -> LexicalIndex:
    """Return a cached lexical index for ``collection``, building it once.

    Failures (no ``.get``, empty corpus) are cached as an empty index so
    retrieval silently falls back to dense and never thrashes.
    """
    key = str(collection_name or "").strip() or f"anon:{id(collection)}"
    with _LOCK:
        existing = _INDEXES.get(key)
        if existing is not None:
            return existing
        try:
            docs, metas = _scroll_collection(collection)
            index = LexicalIndex(docs, metas)
        except Exception:
            index = LexicalIndex([], [])
        _INDEXES[key] = index
        return index


def reset_lexical_index(collection_name: str | None = None) -> None:
    """Drop cached indexes (call after rebuilding the vector store)."""
    with _LOCK:
        if collection_name is None:
            _INDEXES.clear()
        else:
            _INDEXES.pop(str(collection_name).strip() or "default", None)
