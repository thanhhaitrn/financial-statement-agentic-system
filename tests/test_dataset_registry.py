import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from datasets import registry


def _configure_registry_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(registry, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(registry, "DATASETS_DIR", tmp_path / "dataset_store")
    monkeypatch.setattr(registry, "REGISTRY_PATH", registry.DATASETS_DIR / "registry.json")
    monkeypatch.setattr(registry, "MANIFESTS_DIR", registry.DATASETS_DIR / "manifests")
    monkeypatch.setattr(registry, "RAW_TABLES_DIR", registry.DATASETS_DIR / "raw_tables")
    monkeypatch.setattr(registry, "SQLITE_DIR", registry.DATASETS_DIR / "sqlite")


def test_save_dataset_preserves_build_state_for_same_dataset(monkeypatch, tmp_path: Path):
    _configure_registry_paths(monkeypatch, tmp_path)

    initial = registry.build_dataset_record(
        file_path="data/document.md",
        company="Cong ty A",
        fiscal_year=2024,
    )
    registry.save_dataset(
        initial.model_copy(
            update={
                "status": "ready",
                "facts_count": 12,
                "vector_docs_count": 12,
            }
        )
    )

    registered_again = registry.build_dataset_record(
        file_path="data/document.md",
        company="Cong ty A",
        fiscal_year=2024,
    )
    saved_again = registry.save_dataset(registered_again)

    assert saved_again.status == "ready"
    assert saved_again.facts_count == 12
    assert saved_again.vector_docs_count == 12


def test_save_dataset_resets_build_state_when_source_changes(monkeypatch, tmp_path: Path):
    _configure_registry_paths(monkeypatch, tmp_path)

    initial = registry.build_dataset_record(
        file_path="data/document.md",
        company="Cong ty A",
        fiscal_year=2024,
    )
    registry.save_dataset(
        initial.model_copy(
            update={
                "status": "ready",
                "facts_count": 12,
                "vector_docs_count": 12,
            }
        )
    )

    changed_source = registry.build_dataset_record(
        file_path="data/document_v2.md",
        company="Cong ty A",
        fiscal_year=2024,
    )
    saved_changed = registry.save_dataset(changed_source)

    assert saved_changed.status == "registered"
    assert saved_changed.facts_count == 0
    assert saved_changed.vector_docs_count == 0


def test_delete_dataset_removes_registry_and_manifest_only_by_default(monkeypatch, tmp_path: Path):
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

    assert Path(saved.manifest_path).exists()

    deleted = registry.delete_dataset(saved.dataset_id)

    assert deleted is not None
    assert deleted.dataset_id == saved.dataset_id
    assert registry.get_dataset(saved.dataset_id) is None
    assert not Path(saved.manifest_path).exists()
    assert sqlite_path.exists()
    assert raw_tables_path.exists()


def test_delete_dataset_can_purge_managed_artifacts(monkeypatch, tmp_path: Path):
    _configure_registry_paths(monkeypatch, tmp_path)

    saved = registry.save_dataset(
        registry.build_dataset_record(
            file_path="data/document.md",
            company="Cong ty B",
            fiscal_year=2025,
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
        purge_artifacts=True,
        delete_vector_collection_fn=lambda name: seen.setdefault("collection_name", name),
    )

    assert deleted is not None
    assert not Path(saved.manifest_path).exists()
    assert not sqlite_path.exists()
    assert not raw_tables_path.exists()
    assert seen["collection_name"] == saved.vector_collection_name
