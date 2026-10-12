"""Regression tests for test dataset registry."""

# Code note: Tests document expected behavior for the workflow component named by this file.
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]

from agentfinx.datasets import registry


def _configure_registry_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FINX_HOME", str(tmp_path))


def test_delete_dataset_always_purges_managed_artifacts(monkeypatch, tmp_path: Path):
    _configure_registry_paths(monkeypatch, tmp_path)

    saved = registry.save_dataset(
        registry.build_dataset_record(
            file_path="data/document.md",
            company="Cong ty A",
            fiscal_year=2024,
        )
    )
    sqlite_path = Path(saved.sqlite_db_path)
    raw_tables_path = Path(saved.raw_tables_path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    raw_tables_path.parent.mkdir(parents=True, exist_ok=True)
    sqlite_path.write_text("sqlite", encoding="utf-8")
    raw_tables_path.write_text("{}", encoding="utf-8")
    seen = {}

    deleted = registry.delete_dataset(
        saved.dataset_id,
        delete_vector_collection_fn=lambda name: seen.setdefault("collection_name", name),
    )

    assert deleted is not None
    assert registry.get_dataset(saved.dataset_id) is None
    assert not Path(saved.manifest_path).exists()
    assert not sqlite_path.exists()
    assert not raw_tables_path.exists()
    assert seen["collection_name"] == saved.vector_collection_name
