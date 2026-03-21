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
