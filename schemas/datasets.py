"""Dataset metadata model used by registry, ingestion, and vector indexing."""
# Code note: Schema modules normalize model/tool payloads; comments here clarify validation side effects.

from __future__ import annotations

import os
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator


_DATASET_ID_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$"
)


def validate_dataset_id(value: object) -> str:
    """Return a filesystem-safe dataset id without silently rewriting it."""

    text = str(value or "").strip()
    if not text:
        raise ValueError("dataset_id must not be empty")
    if ".." in text or not _DATASET_ID_RE.fullmatch(text):
        raise ValueError(
            "dataset_id must contain only letters, numbers, '.', '_' or '-' "
            "and must not contain path traversal segments"
        )
    return text


class DatasetRecord(BaseModel):
    dataset_id: str
    company: str
    ticker: str = ""
    industry: str = ""
    report_type: str = "financial_statement"
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[int] = None
    scope: str = "unknown"
    audit_status: str = "unknown"
    file_path: str
    sqlite_db_path: str
    vector_collection_name: str
    manifest_path: str
    raw_tables_path: str
    ingestion_version: str = "v1"
    status: str = "registered"
    facts_count: int = 0
    vector_docs_count: int = 0
    created_at: str = ""
    updated_at: str = ""

    @field_validator("dataset_id", mode="before")
    @classmethod
    def validate_safe_dataset_id(cls, value):
        return validate_dataset_id(value)

    @field_validator(
        "file_path",
        "sqlite_db_path",
        "manifest_path",
        "raw_tables_path",
        mode="before",
    )
    @classmethod
    def normalize_paths(cls, value):
        if value is None:
            raise ValueError("dataset paths must not be empty")
        try:
            text = os.fspath(value).strip()
        except (AttributeError, TypeError):
            raise ValueError("dataset paths must be strings or path-like values") from None
        if not text:
            raise ValueError("dataset paths must not be empty")
        if "\x00" in text:
            raise ValueError("dataset paths must not contain NUL bytes")
        return text

    @field_validator("fiscal_quarter", "facts_count", "vector_docs_count", mode="before")
    @classmethod
    def reject_boolean_numbers(cls, value):
        if isinstance(value, bool):
            raise ValueError("boolean values are not valid numeric metadata")
        return value

    @field_validator("fiscal_quarter")
    @classmethod
    def validate_fiscal_quarter(cls, value):
        if value is not None and value not in {1, 2, 3, 4}:
            raise ValueError("fiscal_quarter must be between 1 and 4")
        return value

    @field_validator("facts_count", "vector_docs_count")
    @classmethod
    def validate_non_negative_counts(cls, value):
        if value < 0:
            raise ValueError("dataset counts must be non-negative")
        return value

    @field_validator(
        "dataset_id",
        "company",
        "ticker",
        "industry",
        "report_type",
        "scope",
        "audit_status",
        "file_path",
        "sqlite_db_path",
        "vector_collection_name",
        "manifest_path",
        "raw_tables_path",
        "ingestion_version",
        "status",
        mode="before",
    )
    @classmethod
    def normalize_strings(cls, value):
        if value is None:
            return ""
        return str(value).strip()


class DatasetRegistryQuarantineEntry(BaseModel):
    """A recoverable registry item that failed record-level validation."""

    source: str = "records"
    source_index: Optional[int] = None
    reason: str
    raw_record: Any
    quarantined_at: str


class DatasetRegistryMigration(BaseModel):
    """Audit information for the source document replaced by a migration."""

    source_format: str
    source_sha256: str
    backup_path: str
    migrated_at: str


class DatasetRegistryEnvelope(BaseModel):
    """Versioned on-disk registry while public APIs continue returning records."""

    schema_version: Literal[2] = 2
    updated_at: str
    records: list[DatasetRecord]
    quarantine: list[DatasetRegistryQuarantineEntry] = Field(default_factory=list)
    migration: Optional[DatasetRegistryMigration] = None
