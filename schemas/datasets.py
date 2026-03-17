from typing import Optional

from pydantic import BaseModel, Field, field_validator


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
