"""Read-only activation of an already-built dataset for one query.

Chat never builds. ``activate_dataset_for_query`` checks readiness, opens the
knowledge base read-only, installs this dataset's routing vocabulary for the
current process and opens the existing active collection. It never calls
``ensure_built`` or ``save_dataset``, never creates a collection and never
creates a payload index; a dataset that is not ready reports a code instead.

Callers hold ``dataset_lock`` around activation *and* the query, so the
generation returned here is the one the whole answer is computed against.
``inspect_dataset_generation`` performs the same checks without installing
vocabulary, for callers (the web API) that only need to pin or re-check it.
"""
# Code note: Dataset modules manage local registry records and derived artifact paths.

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from config.allowed_keywords import set_dynamic_keywords
from kb.sqlite_repo import (derive_keyword_augmentation,
                            derive_query_slot_lexicon, open_db_readonly,
                            read_kb_manifest, sqlite_count_facts)
from tools.query_routing import set_dataset_slot_lexicon


class DatasetNotReady(RuntimeError):
    """The dataset cannot answer queries until it is (re)built."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ActivatedDataset:
    dataset: Any
    collection: Any
    generation: str


def apply_dataset_vocabulary(conn, dataset_id: str) -> None:
    """Install one dataset's routing vocabulary for this process.

    Both replacements are explicit even when derivation fails, so a
    long-lived worker can never keep the previous dataset's keywords or slot
    phrases after switching datasets.
    """

    try:
        dynamic_keywords = derive_keyword_augmentation(conn)
    except Exception:
        dynamic_keywords = {}
    set_dynamic_keywords(dynamic_keywords)

    try:
        slot_lexicon = derive_query_slot_lexicon(conn)
    except Exception:
        slot_lexicon = {}
    set_dataset_slot_lexicon(slot_lexicon, dataset_id=dataset_id)


def dataset_generation(kb_manifest: dict, collection) -> str:
    """One string that changes whenever the index or the fact set changes."""

    index_generation = str(getattr(collection, "generation", "") or "")
    facts_sha = str((kb_manifest or {}).get("facts_sha256", "") or "")
    return f"{index_generation}:{facts_sha[:16]}"


def _check_ready_kb(dataset, *, install_vocabulary: bool) -> dict:
    if str(getattr(dataset, "status", "") or "").strip() != "ready":
        raise DatasetNotReady("dataset_not_ready", "Báo cáo chưa được build xong.")

    db_path = Path(str(getattr(dataset, "sqlite_db_path", "") or ""))
    if not db_path.is_file():
        raise DatasetNotReady("artifact_missing", "Thiếu dữ liệu đã build của báo cáo.")
    try:
        conn = open_db_readonly(str(db_path))
    except Exception as exc:
        raise DatasetNotReady(
            "artifact_missing", "Không mở được dữ liệu đã build của báo cáo."
        ) from exc

    try:
        manifest = read_kb_manifest(conn)
        facts_count = sqlite_count_facts(conn)
        if (
            not manifest
            or facts_count <= 0
            or int(manifest.get("facts_count", -1)) != facts_count
        ):
            raise DatasetNotReady(
                "kb_invalid", "Dữ liệu báo cáo không hợp lệ, cần build lại."
            )
        if install_vocabulary:
            apply_dataset_vocabulary(conn, dataset.dataset_id)
    finally:
        conn.close()
    return manifest


def _open_ready_collection(dataset, open_collection: Optional[Callable[[str], Any]]):
    opener = open_collection
    if opener is None:
        from vectorstore.qdrant_store import open_active_collection

        opener = open_active_collection
    try:
        collection = opener(dataset.vector_collection_name)
    except LookupError as exc:
        raise DatasetNotReady(
            "index_missing", "Chỉ mục của báo cáo chưa sẵn sàng, cần build lại."
        ) from exc
    if int(collection.count() or 0) <= 0:
        raise DatasetNotReady(
            "index_missing", "Chỉ mục của báo cáo đang trống, cần build lại."
        )
    return collection


def activate_dataset_for_query(
    dataset,
    *,
    open_collection: Optional[Callable[[str], Any]] = None,
) -> ActivatedDataset:
    manifest = _check_ready_kb(dataset, install_vocabulary=True)
    collection = _open_ready_collection(dataset, open_collection)
    return ActivatedDataset(
        dataset=dataset,
        collection=collection,
        generation=dataset_generation(manifest, collection),
    )


def inspect_dataset_generation(
    dataset,
    *,
    open_collection: Optional[Callable[[str], Any]] = None,
) -> str:
    """Current generation of a ready dataset, without touching vocabulary."""

    manifest = _check_ready_kb(dataset, install_vocabulary=False)
    collection = _open_ready_collection(dataset, open_collection)
    return dataset_generation(manifest, collection)
