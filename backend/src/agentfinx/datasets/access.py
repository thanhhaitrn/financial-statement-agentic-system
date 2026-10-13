"""Owner-scoped dataset lookup for multi-user entry points (web API, workers).

``registry.get_dataset(id, owner_id="")`` keeps its CLI meaning, where an empty
owner means "no filter". For a multi-user caller that is fail-open, so web code
uses this module instead:

* the owner is mandatory — an empty owner raises, it never widens access;
* owned and shared lookups are separate paths;
* a shared dataset must be ownerless **and** named in the administrator's
  ``FINX_SHARED_DATASET_IDS`` allowlist, which is empty by default.
"""
# Code note: Dataset modules manage local registry records and derived artifact paths.

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from agentfinx.datasets.registry import load_registry
from agentfinx.core.schemas.datasets import DatasetRecord, validate_dataset_id

SHARED_DATASETS_ENV = "FINX_SHARED_DATASET_IDS"


class DatasetAccessError(LookupError):
    """The dataset does not exist or the caller may not use it."""


@dataclass(frozen=True)
class AccessibleDataset:
    record: DatasetRecord
    access: str  # "owned" | "shared"


def parse_shared_dataset_ids(raw: Optional[str]) -> frozenset[str]:
    """Parse the admin allowlist; an invalid id is a configuration error."""

    ids = set()
    for part in str(raw or "").replace(";", ",").split(","):
        text = part.strip()
        if text:
            ids.add(validate_dataset_id(text))
    return frozenset(ids)


def shared_dataset_ids_from_env(
    environ: Optional[Mapping[str, str]] = None,
) -> frozenset[str]:
    source = os.environ if environ is None else environ
    return parse_shared_dataset_ids(source.get(SHARED_DATASETS_ENV, ""))


def _require_owner(owner_id: object) -> str:
    owner = str(owner_id or "").strip()
    if not owner:
        raise ValueError("owner_id is required for multi-user dataset access")
    return owner


def _safe_dataset_id(dataset_id: object) -> Optional[str]:
    try:
        return validate_dataset_id(dataset_id)
    except ValueError:
        return None


def get_owned_dataset(
    dataset_id: object,
    owner_id: object,
    *,
    records: Optional[Iterable[DatasetRecord]] = None,
) -> Optional[DatasetRecord]:
    owner = _require_owner(owner_id)
    key = _safe_dataset_id(dataset_id)
    if key is None:
        return None
    for record in records if records is not None else load_registry():
        if record.dataset_id == key and str(record.owner_id or "").strip() == owner:
            return record
    return None


def get_shared_dataset(
    dataset_id: object,
    shared_ids: frozenset[str],
    *,
    records: Optional[Iterable[DatasetRecord]] = None,
) -> Optional[DatasetRecord]:
    key = _safe_dataset_id(dataset_id)
    if key is None or key not in shared_ids:
        return None
    for record in records if records is not None else load_registry():
        if record.dataset_id == key and not str(record.owner_id or "").strip():
            return record
    return None


def get_accessible_dataset(
    dataset_id: object,
    owner_id: object,
    shared_ids: frozenset[str],
) -> AccessibleDataset:
    owner = _require_owner(owner_id)
    records = load_registry()
    owned = get_owned_dataset(dataset_id, owner, records=records)
    if owned is not None:
        return AccessibleDataset(owned, "owned")
    shared = get_shared_dataset(dataset_id, shared_ids, records=records)
    if shared is not None:
        return AccessibleDataset(shared, "shared")
    raise DatasetAccessError("dataset not found or not accessible")


def list_accessible_datasets(
    owner_id: object,
    shared_ids: frozenset[str],
) -> list[AccessibleDataset]:
    owner = _require_owner(owner_id)
    result: list[AccessibleDataset] = []
    for record in load_registry():
        record_owner = str(record.owner_id or "").strip()
        if record_owner == owner:
            result.append(AccessibleDataset(record, "owned"))
        elif not record_owner and record.dataset_id in shared_ids:
            result.append(AccessibleDataset(record, "shared"))
    return sorted(
        result,
        key=lambda item: (
            item.access != "owned",
            item.record.company.casefold(),
            item.record.dataset_id,
        ),
    )
