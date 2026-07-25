"""Regression tests for atomic SQLite and versioned Qdrant builds."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from qdrant_client import QdrantClient

from ingestion import pipeline
from kb.sqlite_repo import (
    SQLITE_SCHEMA_VERSION,
    facts_sha256,
    init_db,
    insert_financial_facts,
    read_kb_manifest,
    write_kb_manifest,
)
from vectorstore import index_builder, qdrant_store


def _fact(source: str, value: str, item_name: str = "Tiền") -> tuple:
    return (
        "Công ty A",
        "2024",
        "BẢNG CÂN ĐỐI KẾ TOÁN",
        "110",
        "",
        "",
        item_name,
        value,
        value,
        value,
        source,
        "2024",
        "ending_balance",
        "VND",
    )


def _dataset(tmp_path: Path):
    source_path = tmp_path / "report.md"
    return SimpleNamespace(
        file_path=str(source_path),
        sqlite_db_path=str(tmp_path / "facts.db"),
        raw_tables_path=str(tmp_path / "raw_tables.json"),
        company="Công ty A",
        fiscal_year=2024,
        ingestion_version="test-v1",
    )


def _install_deterministic_parser(monkeypatch, dataset) -> None:
    monkeypatch.setattr(
        pipeline,
        "attach_context",
        lambda markdown: [{"parsed_value": markdown.strip()}],
    )
    monkeypatch.setattr(
        pipeline,
        "build_fact_rows",
        lambda tables, **_kwargs: [
            _fact(dataset.file_path, tables[0]["parsed_value"])
        ],
    )
    monkeypatch.setattr(pipeline, "build_frontmatter_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(pipeline, "build_note_rows", lambda *_args, **_kwargs: [])


def test_changed_source_rebuilds_but_unchanged_manifest_reuses(monkeypatch, tmp_path):
    dataset = _dataset(tmp_path)
    Path(dataset.file_path).write_text("100", encoding="utf-8")
    _install_deterministic_parser(monkeypatch, dataset)

    first_conn, first_count = pipeline.build_knowledge_base(dataset)
    first_manifest = read_kb_manifest(first_conn)
    first_fact_id = first_conn.execute(
        "SELECT fact_id FROM financial_facts"
    ).fetchone()[0]
    first_conn.close()

    reused_conn, reused_count = pipeline.build_knowledge_base(dataset)
    reused_conn.close()

    Path(dataset.file_path).write_text("200", encoding="utf-8")
    changed_conn, changed_count = pipeline.build_knowledge_base(dataset)
    changed_manifest = read_kb_manifest(changed_conn)
    changed_value, changed_fact_id = changed_conn.execute(
        "SELECT raw_value, fact_id FROM financial_facts"
    ).fetchone()
    changed_conn.close()

    assert first_count == 1
    assert reused_count == 0
    assert changed_count == 1
    assert first_manifest["source_sha256"] != changed_manifest["source_sha256"]
    assert changed_manifest["schema_version"] == SQLITE_SCHEMA_VERSION
    assert changed_value == "200"
    assert changed_fact_id != first_fact_id


def test_parser_failure_preserves_existing_db_and_raw_tables(monkeypatch, tmp_path):
    dataset = _dataset(tmp_path)
    Path(dataset.file_path).write_text("100", encoding="utf-8")
    _install_deterministic_parser(monkeypatch, dataset)
    conn, _ = pipeline.build_knowledge_base(dataset)
    conn.close()

    db_path = Path(dataset.sqlite_db_path)
    raw_path = Path(dataset.raw_tables_path)
    db_before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    raw_before = raw_path.read_bytes()
    Path(dataset.file_path).write_text("changed source", encoding="utf-8")
    monkeypatch.setattr(
        pipeline,
        "build_fact_rows",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("parser failed")),
    )

    with pytest.raises(ValueError, match="parser failed"):
        pipeline.build_knowledge_base(dataset, reset=True)

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == db_before
    assert raw_path.read_bytes() == raw_before


def test_stable_fact_id_survives_identical_atomic_rebuild(monkeypatch, tmp_path):
    dataset = _dataset(tmp_path)
    Path(dataset.file_path).write_text("100", encoding="utf-8")
    _install_deterministic_parser(monkeypatch, dataset)

    first_conn, _ = pipeline.build_knowledge_base(dataset, reset=True)
    first_id = first_conn.execute("SELECT fact_id FROM financial_facts").fetchone()[0]
    first_conn.close()
    second_conn, _ = pipeline.build_knowledge_base(dataset, reset=True)
    second_id = second_conn.execute("SELECT fact_id FROM financial_facts").fetchone()[0]
    second_conn.close()

    assert first_id == second_id


class _FakeEmbeddings:
    def __init__(self, dimension: int, error: Exception | None = None):
        self.dimension = dimension
        self.error = error

    def embed_documents(self, documents):
        if self.error is not None:
            raise self.error
        return [[float(index + 1)] * self.dimension for index, _ in enumerate(documents)]


def _vector_conn(rows: list[tuple]):
    conn = init_db(":memory:")
    insert_financial_facts(conn, rows)
    manifest = {
        "source_path": rows[0][10],
        "source_sha256": hashlib.sha256(str(rows).encode("utf-8")).hexdigest(),
        "parser_version": "test-parser-v1",
        "schema_version": SQLITE_SCHEMA_VERSION,
        "facts_count": len(rows),
        "facts_sha256": facts_sha256(rows),
    }
    write_kb_manifest(conn, manifest)
    return conn


def _install_memory_qdrant(monkeypatch, *, dimension: int = 3):
    client = QdrantClient(location=":memory:")
    monkeypatch.setattr(qdrant_store, "_client", client)
    monkeypatch.setattr(qdrant_store, "QDRANT_VECTOR_SIZE", dimension)
    monkeypatch.setattr(
        qdrant_store,
        "embedding_function",
        _FakeEmbeddings(dimension),
    )
    return client


def test_failed_versioned_build_keeps_active_collection(monkeypatch):
    client = _install_memory_qdrant(monkeypatch)
    logical_name = "financial_statement__atomic_test"
    first_rows = [_fact("report.md", "100")]
    first_conn = _vector_conn(first_rows)
    first_collection, first_count = index_builder.build_vector_store(
        first_conn,
        logical_name,
    )
    first_conn.close()
    alias_name = qdrant_store._active_alias_name(logical_name)
    first_target = qdrant_store._alias_targets(client)[alias_name]

    second_rows = [*first_rows, _fact("report.md", "200", "Hàng tồn kho")]
    second_conn = _vector_conn(second_rows)
    monkeypatch.setattr(
        qdrant_store,
        "embedding_function",
        _FakeEmbeddings(3, RuntimeError("embedding failed")),
    )

    with pytest.raises(RuntimeError, match="embedding failed"):
        index_builder.build_vector_store(second_conn, logical_name, reset=True)
    second_conn.close()

    assert first_count == 1
    assert first_collection.count() == 1
    assert qdrant_store._alias_targets(client)[alias_name] == first_target
    assert qdrant_store.create_collection(logical_name).count() == 1


def test_dimension_validation_prevents_alias_swap(monkeypatch):
    client = _install_memory_qdrant(monkeypatch)
    logical_name = "financial_statement__dimension_test"
    first_rows = [_fact("report.md", "100")]
    first_conn = _vector_conn(first_rows)
    index_builder.build_vector_store(first_conn, logical_name)
    first_conn.close()
    alias_name = qdrant_store._active_alias_name(logical_name)
    first_target = qdrant_store._alias_targets(client)[alias_name]

    second_rows = [*first_rows, _fact("report.md", "200", "Hàng tồn kho")]
    second_conn = _vector_conn(second_rows)
    monkeypatch.setattr(qdrant_store, "embedding_function", _FakeEmbeddings(2))

    with pytest.raises(ValueError, match="dimension 2; expected 3"):
        index_builder.build_vector_store(second_conn, logical_name, reset=True)
    second_conn.close()

    assert qdrant_store._alias_targets(client)[alias_name] == first_target
    assert qdrant_store.create_collection(logical_name).count() == 1


def test_successful_versioned_build_atomically_moves_active_alias(monkeypatch):
    client = _install_memory_qdrant(monkeypatch)
    logical_name = "financial_statement__swap_test"
    first_rows = [_fact("report.md", "100")]
    first_conn = _vector_conn(first_rows)
    index_builder.build_vector_store(first_conn, logical_name)
    first_conn.close()
    alias_name = qdrant_store._active_alias_name(logical_name)
    first_target = qdrant_store._alias_targets(client)[alias_name]

    second_rows = [*first_rows, _fact("report.md", "200", "Hàng tồn kho")]
    second_conn = _vector_conn(second_rows)
    collection, count = index_builder.build_vector_store(
        second_conn,
        logical_name,
        reset=True,
    )
    second_conn.close()
    second_target = qdrant_store._alias_targets(client)[alias_name]

    assert second_target != first_target
    assert count == 2
    assert collection.count() == 2
    reopened = qdrant_store.create_collection(logical_name)
    assert reopened.count() == 2
    assert collection.build_fingerprint
    assert collection.generation == collection.build_fingerprint
    assert reopened.generation == collection.generation


def test_vector_input_lengths_are_validated_before_embedding():
    with pytest.raises(ValueError, match="identical lengths"):
        qdrant_store.validate_index_inputs(["document"], [], ["id"])
