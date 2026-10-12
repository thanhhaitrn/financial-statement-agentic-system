"""Versioned and recoverable dataset-registry migration contracts."""

import json
from pathlib import Path

import pytest

from agentfinx.core.config import paths
from agentfinx.datasets import registry


def _configure_registry_paths(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FINX_HOME", str(tmp_path))


def _record(monkeypatch, tmp_path: Path, dataset_id: str = "valid-dataset"):
    _configure_registry_paths(monkeypatch, tmp_path)
    return registry.build_dataset_record(
        file_path="data/report.md",
        company="Công ty A",
        dataset_id=dataset_id,
    )


def test_legacy_list_migration_backs_up_and_quarantines_each_invalid_record(
    monkeypatch,
    tmp_path,
):
    valid = _record(monkeypatch, tmp_path)
    valid_payload = valid.model_dump(mode="json")
    unsafe_payload = {**valid_payload, "dataset_id": "../escape"}
    malformed_payload = {"dataset_id": "missing-required-fields"}
    original = json.dumps(
        [valid_payload, unsafe_payload, malformed_payload],
        ensure_ascii=False,
        indent=2,
    ).encode("utf-8")
    registry.registry_path().parent.mkdir(parents=True, exist_ok=True)
    registry.registry_path().write_bytes(original)

    records = registry.load_registry()

    assert [record.dataset_id for record in records] == ["valid-dataset"]
    envelope = json.loads(registry.registry_path().read_text(encoding="utf-8"))
    assert envelope["schema_version"] == registry.REGISTRY_SCHEMA_VERSION
    assert [item["dataset_id"] for item in envelope["records"]] == [
        "valid-dataset"
    ]
    assert [item["source_index"] for item in envelope["quarantine"]] == [1, 2]
    assert envelope["quarantine"][0]["raw_record"] == unsafe_payload
    assert "dataset_id" in envelope["quarantine"][0]["reason"]
    assert envelope["quarantine"][1]["raw_record"] == malformed_payload
    assert "validation" in envelope["quarantine"][1]["reason"].lower()

    migration = envelope["migration"]
    assert migration["source_format"] == "legacy-list-v1"
    backup_path = paths.from_stored(migration["backup_path"])  # stored relative to FINX_HOME
    assert backup_path.parent == registry.registry_path().parent / "registry_backups"
    assert backup_path.read_bytes() == original

    migrated_content = registry.registry_path().read_bytes()
    assert registry.load_registry() == records
    assert registry.registry_path().read_bytes() == migrated_content


def test_save_registry_keeps_envelope_quarantine_and_migration_audit(
    monkeypatch,
    tmp_path,
):
    valid = _record(monkeypatch, tmp_path)
    invalid = {**valid.model_dump(mode="json"), "facts_count": -1}
    registry.registry_path().parent.mkdir(parents=True, exist_ok=True)
    registry.registry_path().write_text(
        json.dumps([valid.model_dump(mode="json"), invalid]),
        encoding="utf-8",
    )
    registry.load_registry()
    before = json.loads(registry.registry_path().read_text(encoding="utf-8"))

    registry.save_registry([valid])

    after = json.loads(registry.registry_path().read_text(encoding="utf-8"))
    assert after["schema_version"] == 2
    assert after["quarantine"] == before["quarantine"]
    assert after["migration"] == before["migration"]


def test_registry_replace_failure_leaves_previous_document_intact(
    monkeypatch,
    tmp_path,
):
    valid = _record(monkeypatch, tmp_path)
    registry.save_registry([valid])
    previous = registry.registry_path().read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(registry.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        registry.save_registry([])

    assert registry.registry_path().read_bytes() == previous
    assert not list(registry.registry_path().parent.glob(".registry.json.*.tmp"))


def test_future_registry_version_is_not_silently_downgraded(
    monkeypatch,
    tmp_path,
):
    _configure_registry_paths(monkeypatch, tmp_path)
    registry.registry_path().parent.mkdir(parents=True, exist_ok=True)
    future = {"schema_version": 99, "updated_at": "future", "records": []}
    registry.registry_path().write_text(json.dumps(future), encoding="utf-8")
    previous = registry.registry_path().read_bytes()

    with pytest.raises(ValueError, match="unsupported dataset registry"):
        registry.load_registry()

    assert registry.registry_path().read_bytes() == previous
