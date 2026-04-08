from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, List, Optional

from schemas.datasets import DatasetRecord


ROOT_DIR = Path(__file__).resolve().parents[1]
DATASETS_DIR = ROOT_DIR / "dataset_store"
REGISTRY_PATH = DATASETS_DIR / "registry.json"
MANIFESTS_DIR = DATASETS_DIR / "manifests"
RAW_TABLES_DIR = DATASETS_DIR / "raw_tables"
SQLITE_DIR = DATASETS_DIR / "sqlite"

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
    return "-".join([part for part in parts if part])


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
    _ensure_layout()

    dataset_key = dataset_id or make_dataset_id(
        company,
        report_type=report_type,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        scope=scope,
        audit_status=audit_status,
    )

    return DatasetRecord(
        dataset_id=dataset_key,
        company=company,
        ticker=ticker,
        industry=industry,
        report_type=report_type,
        fiscal_year=fiscal_year,
        fiscal_quarter=fiscal_quarter,
        scope=scope,
        audit_status=audit_status,
        file_path=str((ROOT_DIR / file_path).resolve()) if not Path(file_path).is_absolute() else file_path,
        sqlite_db_path=str((SQLITE_DIR / f"{dataset_key}.db").resolve()),
        vector_collection_name=f"financial_statement__{dataset_key}",
        manifest_path=str((MANIFESTS_DIR / f"{dataset_key}.json").resolve()),
        raw_tables_path=str((RAW_TABLES_DIR / f"{dataset_key}.json").resolve()),
        ingestion_version=ingestion_version,
    )


def load_registry() -> List[DatasetRecord]:
    _ensure_layout()
    if not REGISTRY_PATH.exists():
        return []

    with REGISTRY_PATH.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    if not isinstance(raw, list):
        return []

    return [DatasetRecord.model_validate(item) for item in raw]


def save_registry(records: List[DatasetRecord]) -> None:
    _ensure_layout()
    payload = [record.model_dump(mode="json") for record in records]
    with REGISTRY_PATH.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def save_dataset(record: DatasetRecord) -> DatasetRecord:
    _ensure_layout()

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

    saved = DatasetRecord.model_validate(data)
    existing[saved.dataset_id] = saved

    records = sorted(existing.values(), key=lambda item: item.dataset_id)
    save_registry(records)

    manifest_path = Path(saved.manifest_path)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", encoding="utf-8") as handle:
        json.dump(saved.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)

    return saved


def _path_is_within_dir(path: Path, directory: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(directory.resolve(strict=False))
        return True
    except ValueError:
        return False


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
        from vectorstore.chroma_store import delete_collection

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
    purge_artifacts: bool = False,
    delete_vector_collection_fn: Optional[Callable[[str], None]] = None,
) -> Optional[DatasetRecord]:
    existing = {item.dataset_id: item for item in load_registry()}
    current = existing.pop(str(dataset_id or "").strip(), None)
    if current is None:
        return None

    records = sorted(existing.values(), key=lambda item: item.dataset_id)
    save_registry(records)

    _remove_managed_file(current.manifest_path, allowed_dir=MANIFESTS_DIR)

    if purge_artifacts:
        _remove_managed_file(current.sqlite_db_path, allowed_dir=SQLITE_DIR)
        _remove_managed_file(current.raw_tables_path, allowed_dir=RAW_TABLES_DIR)
        _delete_vector_collection_if_exists(
            current.vector_collection_name,
            delete_vector_collection_fn=delete_vector_collection_fn,
        )

    return current


def get_dataset(dataset_id: str) -> Optional[DatasetRecord]:
    for record in load_registry():
        if record.dataset_id == dataset_id:
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

    dataset_id_norm = _normalize_str(dataset_id)
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
    if record.fiscal_year is not None:
        parts.append(f"year={record.fiscal_year}")
    if record.fiscal_quarter is not None:
        parts.append(f"quarter={record.fiscal_quarter}")
    if record.scope:
        parts.append(f"scope={record.scope}")
    if record.audit_status:
        parts.append(f"audit={record.audit_status}")
    parts.append(f"status={record.status}")
    parts.append(f"facts={record.facts_count}")
    parts.append(f"docs={record.vector_docs_count}")
    return " | ".join(parts)
