"""Read and update the local AgentFinX dataset manifest."""
# Code note: Dataset modules manage local registry records and derived artifact paths.

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from schemas.datasets import (
    DatasetRecord,
    DatasetRegistryEnvelope,
    DatasetRegistryMigration,
    DatasetRegistryQuarantineEntry,
    validate_dataset_id,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DATASETS_DIR = ROOT_DIR / "dataset_store"
REGISTRY_PATH = DATASETS_DIR / "registry.json"
MANIFESTS_DIR = DATASETS_DIR / "manifests"
RAW_TABLES_DIR = DATASETS_DIR / "raw_tables"
SQLITE_DIR = DATASETS_DIR / "sqlite"
REGISTRY_SCHEMA_VERSION = 2

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BUILD_INPUT_FIELDS = (
    "company",
    "file_path",
    "report_type",
    "fiscal_year",
    "fiscal_quarter",
    "scope",
    "audit_status",
    "sqlite_db_path",
    "vector_collection_name",
    "raw_tables_path",
    "ingestion_version",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure_layout() -> None:
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_TABLES_DIR.mkdir(parents=True, exist_ok=True)
    SQLITE_DIR.mkdir(parents=True, exist_ok=True)


def _fsync_directory(directory: Path) -> None:
    """Best-effort directory sync so an atomic replace survives a host crash."""

    try:
        descriptor = os.open(
            directory,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def _atomic_write_json(path: Path, payload: object) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    _atomic_write_bytes(path, content + b"\n")


def _backup_registry(content: bytes, *, source_format: str) -> Path:
    """Keep the exact pre-migration document in a content-addressed backup."""

    digest = hashlib.sha256(content).hexdigest()
    backup_dir = REGISTRY_PATH.parent / "registry_backups"
    backup_path = backup_dir / f"registry-{source_format}-{digest}.backup.json"
    if not backup_path.exists():
        _atomic_write_bytes(backup_path, content)
    return backup_path


def _path_is_within_dir(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
        return True
    except ValueError:
        return False


def _resolve_confined_path(
    path_value: str | Path,
    *,
    allowed_dir: Path,
    field_name: str,
) -> Path:
    raw_path = str(path_value or "").strip()
    if not raw_path or "\x00" in raw_path:
        raise ValueError(f"{field_name} must be a non-empty safe path")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    resolved = path.resolve(strict=False)
    if not _path_is_within_dir(resolved, allowed_dir):
        raise ValueError(f"{field_name} must stay within {allowed_dir.resolve(strict=False)}")
    return resolved


def _expected_managed_path(directory: Path, dataset_id: str, suffix: str) -> Path:
    expected = (directory / f"{dataset_id}{suffix}").resolve(strict=False)
    if not _path_is_within_dir(expected, directory):
        # Defense in depth: validate_dataset_id should make this unreachable.
        raise ValueError(f"unsafe managed path for dataset_id {dataset_id!r}")
    return expected


def _normalize_record_paths(record: DatasetRecord) -> DatasetRecord:
    """Validate confinement and normalize all persisted paths to absolute form."""

    dataset_key = validate_dataset_id(record.dataset_id)
    source_path = _resolve_confined_path(
        record.file_path,
        allowed_dir=ROOT_DIR,
        field_name="file_path",
    )
    managed_paths = {
        "sqlite_db_path": (SQLITE_DIR, ".db"),
        "manifest_path": (MANIFESTS_DIR, ".json"),
        "raw_tables_path": (RAW_TABLES_DIR, ".json"),
    }

    payload = record.model_dump(mode="json")
    payload["file_path"] = str(source_path)
    for field_name, (directory, suffix) in managed_paths.items():
        actual = _resolve_confined_path(
            getattr(record, field_name),
            allowed_dir=directory,
            field_name=field_name,
        )
        expected = _expected_managed_path(directory, dataset_key, suffix)
        if actual != expected:
            raise ValueError(
                f"{field_name} must be the managed path for dataset {dataset_key!r}"
            )
        payload[field_name] = str(actual)

    return DatasetRecord.model_validate(payload)


def slugify(value: str) -> str:
    text = str(value or "").strip().lower()
    text = _SLUG_RE.sub("-", text)
    text = text.strip("-")
    return text or "dataset"


def make_dataset_id(
    company: str,
    *,
    report_type: str = "financial_statement",
    fiscal_year: Optional[int] = None,
    fiscal_quarter: Optional[int] = None,
    scope: str = "unknown",
    audit_status: str = "unknown",
) -> str:
    parts = [slugify(company), slugify(report_type)]
    if fiscal_year is not None:
        parts.append(str(int(fiscal_year)))
    if fiscal_quarter is not None:
        parts.append(f"q{int(fiscal_quarter)}")
    if scope:
        parts.append(slugify(scope))
    if audit_status:
        parts.append(slugify(audit_status))
    return validate_dataset_id("-".join([part for part in parts if part]))


def build_dataset_record(
    *,
    file_path: str,
    company: str,
    dataset_id: str = "",
    ticker: str = "",
    industry: str = "",
    report_type: str = "financial_statement",
    fiscal_year: Optional[int] = None,
    fiscal_quarter: Optional[int] = None,
    scope: str = "unknown",
    audit_status: str = "unknown",
    ingestion_version: str = "v1",
) -> DatasetRecord:
    dataset_key = validate_dataset_id(dataset_id) if dataset_id else make_dataset_id(
        company,
        report_type=report_type,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        scope=scope,
        audit_status=audit_status,
    )
    source_path = _resolve_confined_path(
        file_path,
        allowed_dir=ROOT_DIR,
        field_name="file_path",
    )
    _ensure_layout()

    record = DatasetRecord(
        dataset_id=dataset_key,
        company=company,
        ticker=ticker,
        industry=industry,
        report_type=report_type,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        scope=scope,
        audit_status=audit_status,
        file_path=str(source_path),
        sqlite_db_path=str(_expected_managed_path(SQLITE_DIR, dataset_key, ".db")),
        vector_collection_name=f"financial_statement__{dataset_key}",
        manifest_path=str(_expected_managed_path(MANIFESTS_DIR, dataset_key, ".json")),
        raw_tables_path=str(_expected_managed_path(RAW_TABLES_DIR, dataset_key, ".json")),
        ingestion_version=ingestion_version,
    )
    return _normalize_record_paths(record)


def _quarantine_entry(
    raw_record: Any,
    *,
    reason: str,
    source: str,
    source_index: Optional[int] = None,
    quarantined_at: Optional[str] = None,
) -> DatasetRegistryQuarantineEntry:
    return DatasetRegistryQuarantineEntry(
        source=source,
        source_index=source_index,
        reason=str(reason or "registry item failed validation"),
        raw_record=raw_record,
        quarantined_at=quarantined_at or _utc_now(),
    )


def _validate_registry_items(
    raw_items: Any,
    *,
    source: str = "records",
) -> tuple[List[DatasetRecord], list[DatasetRegistryQuarantineEntry]]:
    if not isinstance(raw_items, list):
        return [], [
            _quarantine_entry(
                raw_items,
                source=source,
                reason="registry records must be a JSON list",
            )
        ]

    records: List[DatasetRecord] = []
    quarantine: list[DatasetRegistryQuarantineEntry] = []
    seen_dataset_ids: set[str] = set()
    for index, raw_record in enumerate(raw_items):
        try:
            record = _normalize_record_paths(
                DatasetRecord.model_validate(raw_record)
            )
            if record.dataset_id in seen_dataset_ids:
                raise ValueError(
                    f"duplicate dataset_id {record.dataset_id!r} in registry"
                )
        except Exception as exc:
            quarantine.append(
                _quarantine_entry(
                    raw_record,
                    source=source,
                    source_index=index,
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        seen_dataset_ids.add(record.dataset_id)
        records.append(record)

    return records, quarantine


def _normalize_existing_quarantine(
    raw_quarantine: Any,
) -> tuple[list[DatasetRegistryQuarantineEntry], bool]:
    if raw_quarantine is None:
        return [], False
    if not isinstance(raw_quarantine, list):
        return [
            _quarantine_entry(
                raw_quarantine,
                source="quarantine",
                reason="registry quarantine must be a JSON list",
            )
        ], True

    entries: list[DatasetRegistryQuarantineEntry] = []
    changed = False
    for index, raw_entry in enumerate(raw_quarantine):
        try:
            entries.append(DatasetRegistryQuarantineEntry.model_validate(raw_entry))
        except Exception as exc:
            changed = True
            entries.append(
                _quarantine_entry(
                    raw_entry,
                    source="quarantine",
                    source_index=index,
                    reason=f"invalid quarantine entry: {type(exc).__name__}: {exc}",
                )
            )
    return entries, changed


def _registry_envelope_payload(
    records: List[DatasetRecord],
    *,
    quarantine: list[DatasetRegistryQuarantineEntry],
    migration: Optional[DatasetRegistryMigration] = None,
    updated_at: Optional[str] = None,
) -> dict:
    envelope = DatasetRegistryEnvelope(
        schema_version=REGISTRY_SCHEMA_VERSION,
        updated_at=updated_at or _utc_now(),
        records=[record.model_dump(mode="json") for record in records],
        quarantine=quarantine,
        migration=migration,
    )
    return envelope.model_dump(mode="json")


def _migration_metadata(
    backup_path: Path,
    source_content: bytes,
    *,
    source_format: str,
) -> DatasetRegistryMigration:
    return DatasetRegistryMigration(
        source_format=source_format,
        source_sha256=hashlib.sha256(source_content).hexdigest(),
        backup_path=str(backup_path),
        migrated_at=_utc_now(),
    )


def _migrate_registry_document(
    raw_items: Any,
    *,
    source_content: bytes,
    source_format: str,
    existing_quarantine: Optional[list[DatasetRegistryQuarantineEntry]] = None,
) -> List[DatasetRecord]:
    backup_path = _backup_registry(source_content, source_format=source_format)
    records, invalid_records = _validate_registry_items(raw_items)
    quarantine = list(existing_quarantine or []) + invalid_records
    payload = _registry_envelope_payload(
        records,
        quarantine=quarantine,
        migration=_migration_metadata(
            backup_path,
            source_content,
            source_format=source_format,
        ),
    )
    _atomic_write_json(REGISTRY_PATH, payload)
    return records


def load_registry() -> List[DatasetRecord]:
    """Load valid records, migrating legacy storage without losing bad data."""

    _ensure_layout()
    if not REGISTRY_PATH.exists():
        return []

    source_content = REGISTRY_PATH.read_bytes()
    try:
        raw = json.loads(source_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # The exact original remains in the backup; the readable copy is kept in
        # quarantine so callers never mistake corruption for an empty registry.
        backup_path = _backup_registry(
            source_content,
            source_format="invalid-document",
        )
        quarantine = [
            _quarantine_entry(
                {"raw_text": source_content.decode("utf-8", errors="replace")},
                source="registry_document",
                reason=f"{type(exc).__name__}: {exc}",
            )
        ]
        payload = _registry_envelope_payload(
            [],
            quarantine=quarantine,
            migration=_migration_metadata(
                backup_path,
                source_content,
                source_format="invalid-document",
            ),
        )
        _atomic_write_json(REGISTRY_PATH, payload)
        return []

    if isinstance(raw, list):
        return _migrate_registry_document(
            raw,
            source_content=source_content,
            source_format="legacy-list-v1",
        )

    if not isinstance(raw, dict):
        return _migrate_registry_document(
            raw,
            source_content=source_content,
            source_format="invalid-top-level",
        )

    schema_version = raw.get("schema_version")
    if schema_version in (None, 1):
        previous_quarantine, _ = _normalize_existing_quarantine(
            raw.get("quarantine")
        )
        return _migrate_registry_document(
            raw.get("records"),
            source_content=source_content,
            source_format=f"legacy-envelope-v{schema_version or 1}",
            existing_quarantine=previous_quarantine,
        )
    if schema_version != REGISTRY_SCHEMA_VERSION:
        raise ValueError(
            "unsupported dataset registry schema_version "
            f"{schema_version!r}; expected {REGISTRY_SCHEMA_VERSION}"
        )

    records, invalid_records = _validate_registry_items(raw.get("records"))
    previous_quarantine, quarantine_changed = _normalize_existing_quarantine(
        raw.get("quarantine")
    )
    quarantine = previous_quarantine + invalid_records

    migration = None
    migration_changed = False
    if raw.get("migration") is not None:
        try:
            migration = DatasetRegistryMigration.model_validate(raw["migration"])
        except Exception as exc:
            migration_changed = True
            quarantine.append(
                _quarantine_entry(
                    raw["migration"],
                    source="migration",
                    reason=f"invalid migration metadata: {type(exc).__name__}: {exc}",
                )
            )

    normalized_records = [record.model_dump(mode="json") for record in records]
    normalized_quarantine = [entry.model_dump(mode="json") for entry in quarantine]
    needs_repair = (
        bool(invalid_records)
        or quarantine_changed
        or migration_changed
        or raw.get("records") != normalized_records
        or raw.get("quarantine", []) != normalized_quarantine
    )
    if needs_repair:
        _backup_registry(source_content, source_format="envelope-v2-repair")
        payload = _registry_envelope_payload(
            records,
            quarantine=quarantine,
            migration=migration,
        )
        _atomic_write_json(REGISTRY_PATH, payload)

    return records


def _load_registry_envelope_context() -> tuple[
    list[DatasetRegistryQuarantineEntry],
    Optional[DatasetRegistryMigration],
]:
    if not REGISTRY_PATH.exists():
        return [], None
    load_registry()
    raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    quarantine, _ = _normalize_existing_quarantine(raw.get("quarantine"))
    migration = None
    if raw.get("migration") is not None:
        migration = DatasetRegistryMigration.model_validate(raw["migration"])
    return quarantine, migration


def save_registry(records: List[DatasetRecord]) -> None:
    _ensure_layout()
    validated_records = [_normalize_record_paths(record) for record in records]
    quarantine, migration = _load_registry_envelope_context()
    payload = _registry_envelope_payload(
        validated_records,
        quarantine=quarantine,
        migration=migration,
    )
    _atomic_write_json(REGISTRY_PATH, payload)


def save_dataset(record: DatasetRecord) -> DatasetRecord:
    _ensure_layout()
    record = _normalize_record_paths(record)

    existing = {item.dataset_id: item for item in load_registry()}
    current = existing.get(record.dataset_id)
    now = _utc_now()

    data = record.model_dump(mode="json")
    data = _merge_preserved_build_fields(
        current=current,
        incoming=record,
        payload=data,
    )
    data["created_at"] = current.created_at if current and current.created_at else (record.created_at or now)
    data["updated_at"] = now

    saved = _normalize_record_paths(DatasetRecord.model_validate(data))
    existing[saved.dataset_id] = saved

    records = sorted(existing.values(), key=lambda item: item.dataset_id)
    save_registry(records)

    manifest_path = Path(saved.manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(saved.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)

    return saved


def _remove_managed_file(path_str: str, *, allowed_dir: Path) -> bool:
    path = Path(path_str or "")
    if not path_str or not _path_is_within_dir(path, allowed_dir):
        return False

    try:
        path.unlink()
    except FileNotFoundError:
        return False

    return True


def _delete_vector_collection_if_exists(
    collection_name: str,
    *,
    delete_vector_collection_fn: Optional[Callable[[str], None]] = None,
) -> bool:
    if not collection_name:
        return False

    delete_fn = delete_vector_collection_fn
    if delete_fn is None:
        from vectorstore.qdrant_store import delete_collection

        delete_fn = delete_collection

    try:
        delete_fn(collection_name)
        return True
    except Exception as exc:
        message = str(exc).strip().lower()
        if "not found" in message or "does not exist" in message:
            return False
        raise


def delete_dataset(
    dataset_id: str,
    *,
    delete_vector_collection_fn: Optional[Callable[[str], None]] = None,
) -> Optional[DatasetRecord]:
    if not str(dataset_id or "").strip():
        return None
    dataset_key = validate_dataset_id(dataset_id)
    existing = {item.dataset_id: item for item in load_registry()}
    current = existing.pop(dataset_key, None)
    if current is None:
        return None

    records = sorted(existing.values(), key=lambda item: item.dataset_id)
    save_registry(records)

    _remove_managed_file(current.manifest_path, allowed_dir=MANIFESTS_DIR)
    _remove_managed_file(current.sqlite_db_path, allowed_dir=SQLITE_DIR)
    _remove_managed_file(current.raw_tables_path, allowed_dir=RAW_TABLES_DIR)
    _delete_vector_collection_if_exists(
        current.vector_collection_name,
        delete_vector_collection_fn=delete_vector_collection_fn,
    )

    return current


def get_dataset(dataset_id: str) -> Optional[DatasetRecord]:
    if not str(dataset_id or "").strip():
        return None
    dataset_key = validate_dataset_id(dataset_id)
    for record in load_registry():
        if record.dataset_id == dataset_key:
            return record
    return None


def _normalize_str(value: str) -> str:
    return str(value or "").strip().lower()


def _same_build_inputs(current: DatasetRecord, incoming: DatasetRecord) -> bool:
    return all(
        getattr(current, field) == getattr(incoming, field)
        for field in _BUILD_INPUT_FIELDS
    )


def _merge_preserved_build_fields(
    *,
    current: Optional[DatasetRecord],
    incoming: DatasetRecord,
    payload: dict,
) -> dict:
    if current is None or not _same_build_inputs(current, incoming):
        return payload

    if payload.get("status") == "registered":
        payload["status"] = current.status

    if int(payload.get("facts_count") or 0) == 0:
        payload["facts_count"] = current.facts_count

    if int(payload.get("vector_docs_count") or 0) == 0:
        payload["vector_docs_count"] = current.vector_docs_count

    return payload


def find_datasets(
    *,
    dataset_id: str = "",
    company: str = "",
    ticker: str = "",
    report_type: str = "",
    fiscal_year: Optional[int] = None,
    fiscal_quarter: Optional[int] = None,
    scope: str = "",
    audit_status: str = "",
) -> List[DatasetRecord]:
    records = load_registry()
    matches: List[DatasetRecord] = []

    dataset_id_norm = _normalize_str(validate_dataset_id(dataset_id)) if dataset_id else ""
    company_norm = _normalize_str(company)
    ticker_norm = _normalize_str(ticker)
    report_type_norm = _normalize_str(report_type)
    scope_norm = _normalize_str(scope)
    audit_status_norm = _normalize_str(audit_status)

    for record in records:
        if dataset_id_norm and _normalize_str(record.dataset_id) != dataset_id_norm:
            continue
        if company_norm and company_norm not in _normalize_str(record.company):
            continue
        if ticker_norm and ticker_norm != _normalize_str(record.ticker):
            continue
        if report_type_norm and report_type_norm != _normalize_str(record.report_type):
            continue
        if fiscal_year is not None and record.fiscal_year != fiscal_year:
            continue
        if fiscal_quarter is not None and record.fiscal_quarter != fiscal_quarter:
            continue
        if scope_norm and scope_norm != _normalize_str(record.scope):
            continue
        if audit_status_norm and audit_status_norm != _normalize_str(record.audit_status):
            continue
        matches.append(record)

    return sorted(matches, key=lambda item: item.dataset_id)


def describe_dataset(record: DatasetRecord) -> str:
    parts = [record.dataset_id, f"company={record.company}"]
    if record.ticker:
        parts.append(f"ticker={record.ticker}")
    fiscal_year = record.fiscal_year if record.fiscal_year is not None else "None"
    fiscal_quarter = record.fiscal_quarter if record.fiscal_quarter is not None else "None"
    parts.append(f"fiscal_year={fiscal_year}")
    parts.append(f"fiscal_quarter={fiscal_quarter}")
    if record.scope:
        parts.append(f"scope={record.scope}")
    if record.audit_status:
        parts.append(f"audit={record.audit_status}")
    parts.append(f"status={record.status}")
    parts.append(f"facts={record.facts_count}")
    parts.append(f"docs={record.vector_docs_count}")
    return " | ".join(parts)
