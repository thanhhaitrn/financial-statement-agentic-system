"""P0 regression tests for dataset safety and ingestion boundaries."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from dataset_catalog import registry
from ingestion.frontmatter_parser import build_frontmatter_rows
from ingestion.note_parser import build_note_rows, extract_note_section_pages
from schemas.datasets import DatasetRecord
from schemas.requirements import (
    FACT_STATUS_AMBIGUOUS,
    FACT_STATUS_FOUND,
    is_usable_fact_status,
    normalize_fact_status,
)


def _configure_registry_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(registry, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(registry, "DATASETS_DIR", tmp_path / "dataset_store")
    monkeypatch.setattr(registry, "REGISTRY_PATH", registry.DATASETS_DIR / "registry.json")
    monkeypatch.setattr(registry, "MANIFESTS_DIR", registry.DATASETS_DIR / "manifests")
    monkeypatch.setattr(registry, "RAW_TABLES_DIR", registry.DATASETS_DIR / "raw_tables")
    monkeypatch.setattr(registry, "SQLITE_DIR", registry.DATASETS_DIR / "sqlite")


def _record_payload(**overrides):
    payload = {
        "dataset_id": "safe-dataset",
        "company": "Công ty A",
        "fiscal_quarter": "4",
        "file_path": "data/report.md",
        "sqlite_db_path": "dataset_store/sqlite/safe-dataset.db",
        "vector_collection_name": "financial_statement__safe-dataset",
        "manifest_path": "dataset_store/manifests/safe-dataset.json",
        "raw_tables_path": "dataset_store/raw_tables/safe-dataset.json",
        "facts_count": "12",
        "vector_docs_count": "10",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "dataset_id",
    ("../escape", "safe/../../escape", r"safe\..\escape", "two..dots"),
)
def test_dataset_id_rejects_path_traversal(monkeypatch, tmp_path, dataset_id):
    _configure_registry_paths(monkeypatch, tmp_path)

    with pytest.raises((ValueError, ValidationError), match="dataset_id"):
        registry.build_dataset_record(
            file_path="data/report.md",
            company="Công ty A",
            dataset_id=dataset_id,
        )


def test_dataset_source_path_must_stay_inside_project_root(monkeypatch, tmp_path):
    _configure_registry_paths(monkeypatch, tmp_path)

    with pytest.raises(ValueError, match="file_path must stay within"):
        registry.build_dataset_record(
            file_path="../outside.md",
            company="Công ty A",
            dataset_id="safe-dataset",
        )

    with pytest.raises(ValueError, match="file_path must be a non-empty safe path"):
        registry.build_dataset_record(
            file_path="",
            company="Công ty A",
            dataset_id="safe-dataset",
        )


def test_save_dataset_rejects_noncanonical_managed_path(monkeypatch, tmp_path):
    _configure_registry_paths(monkeypatch, tmp_path)
    record = registry.build_dataset_record(
        file_path="data/report.md",
        company="Công ty A",
        dataset_id="safe-dataset",
    )
    unsafe = record.model_copy(
        update={"manifest_path": str(tmp_path / "other-manifest.json")}
    )

    with pytest.raises(ValueError, match="manifest_path must stay within"):
        registry.save_dataset(unsafe)


def test_dataset_schema_coerces_supported_numeric_metadata():
    record = DatasetRecord.model_validate(_record_payload())

    assert record.fiscal_quarter == 4
    assert record.facts_count == 12
    assert record.vector_docs_count == 10
    assert isinstance(record.file_path, str)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("fiscal_quarter", 0),
        ("fiscal_quarter", 5),
        ("fiscal_quarter", True),
        ("facts_count", -1),
        ("vector_docs_count", -1),
        ("file_path", ""),
    ),
)
def test_dataset_schema_rejects_invalid_typed_metadata(field, value):
    with pytest.raises(ValidationError):
        DatasetRecord.model_validate(_record_payload(**{field: value}))


@pytest.mark.parametrize("status", ("error", "unknown", "unsupported_status"))
def test_unknown_or_error_fact_status_is_never_found(status):
    assert normalize_fact_status(status) == FACT_STATUS_AMBIGUOUS
    assert not is_usable_fact_status(status)


def test_missing_fact_status_remains_legacy_found():
    assert normalize_fact_status(None) == FACT_STATUS_FOUND


def test_document_without_notes_heading_produces_no_note_rows():
    markdown = """# BẢNG CÂN ĐỐI KẾ TOÁN

| Khoản mục | Năm nay |
| --- | ---: |
| Tiền | 100 |
"""

    assert extract_note_section_pages(markdown) == []
    assert build_note_rows(markdown, "Công ty A", "report.md", 2024) == []


def test_qualified_notes_heading_is_recognized():
    markdown = """# THUYẾT MINH BÁO CÁO TÀI CHÍNH RIÊNG

## 1. Đặc điểm hoạt động

Hoạt động chính của doanh nghiệp thuộc lĩnh vực đầu tư dài hạn.
"""

    rows = build_note_rows(markdown, "Công ty A", "report.md", 2024)

    assert rows
    assert any("lĩnh vực đầu tư" in str(row[7]) for row in rows)


def test_note_table_rows_inherit_document_unit_caption():
    markdown = """# BẢNG CÂN ĐỐI KẾ TOÁN

Đơn vị tính: VND

# THUYẾT MINH BÁO CÁO TÀI CHÍNH

Đơn vị tính: nghìn đồng

## 5. Chi phí tài chính

| Chỉ tiêu | Năm nay | Năm trước |
| --- | ---: | ---: |
| Tổng | 58.553.138.851 | 63.578.625.194 |
"""

    rows = build_note_rows(markdown, "Công ty A", "report.md", 2024)
    table_rows = [row for row in rows if row[3] == "note_table"]

    assert table_rows
    assert all(row[13] == "nghìn đồng" for row in table_rows)


def test_frontmatter_without_page_marker_keeps_prefix_on_statement_page():
    markdown = """## BÁO CÁO CỦA BAN GIÁM ĐỐC

Ban Giám đốc chịu trách nhiệm lập báo cáo tài chính trung thực và hợp lý.

# BẢNG CÂN ĐỐI KẾ TOÁN

| Khoản mục | Năm nay |
| --- | ---: |
| Tiền | 100 |
"""

    rows = build_frontmatter_rows(markdown, "Công ty A", "report.md", 2024)
    values = [str(row[7]) for row in rows]

    assert any("chịu trách nhiệm lập báo cáo" in value for value in values)
    assert not any("Khoản mục" in value or "Tiền: 100" in value for value in values)
