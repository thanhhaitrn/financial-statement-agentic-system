"""Project-wide defaults for ingestion, vector indexing, and dataset metadata."""
# Code note: Config modules centralize constants used by routing, ingestion, and retrieval.

DEFAULT_DATA_FILE = "data/document.md"

DEFAULT_DATASET = {
    "dataset_id": "",
    "company": "",
    "ticker": "",
    "industry": "",
    "report_type": "financial_statement",
    "fiscal_year": None,
    "fiscal_quarter": None,
    "scope": "unknown",
    "audit_status": "unknown",
    "ingestion_version": "v16",
}

VECTOR_BATCH_SIZE = 500
