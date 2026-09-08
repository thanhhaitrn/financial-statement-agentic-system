"""Qdrant client configuration must distinguish local paths from server URLs."""

from types import SimpleNamespace

import pytest

from vectorstore import qdrant_store


def test_filesystem_qdrant_location_uses_persistent_path(monkeypatch, tmp_path):
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(qdrant_store, "QdrantClient", fake_client)
    monkeypatch.setattr(qdrant_store, "QDRANT_LOCATION", str(tmp_path / "qdrant"))

    qdrant_store._make_client()

    assert calls == [
        {"path": str(tmp_path / "qdrant"), "timeout": qdrant_store.QDRANT_TIMEOUT}
    ]


def test_memory_and_http_qdrant_locations_remain_locations(monkeypatch):
    calls = []

    def fake_client(**kwargs):
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(qdrant_store, "QdrantClient", fake_client)
    monkeypatch.setattr(qdrant_store, "QDRANT_LOCATION", ":memory:")
    qdrant_store._make_client()

    monkeypatch.setattr(qdrant_store, "QDRANT_LOCATION", "http://127.0.0.1:6333")
    qdrant_store._make_client()

    assert calls == [
        {"location": ":memory:", "timeout": qdrant_store.QDRANT_TIMEOUT},
        {
            "location": "http://127.0.0.1:6333",
            "timeout": qdrant_store.QDRANT_TIMEOUT,
        },
    ]


def test_query_preserves_real_similarity_and_never_invents_missing_score():
    class FakeClient:
        def query_points(self, **_kwargs):
            return SimpleNamespace(
                points=[
                    SimpleNamespace(
                        payload={"document": "A", "metric_label": "Doanh thu"},
                        score=0.82,
                        id=1,
                    ),
                    SimpleNamespace(
                        payload={"document": "B", "metric_label": "Chi phí"},
                        score=0.31,
                        id=2,
                    ),
                    SimpleNamespace(
                        payload={"document": "C", "metric_label": "Khác"},
                        score=None,
                        id=3,
                    ),
                ]
            )

    adapter = qdrant_store.QdrantCollectionAdapter(
        "test_collection",
        FakeClient(),
    )

    result = adapter.query([[0.0]], n_results=3)

    assert result["similarities"] == [[0.82, 0.31, None]]
    assert result["distances"][0][0] == pytest.approx(0.18)
    assert result["distances"][0][1] == pytest.approx(0.69)
    assert result["distances"][0][2] is None
