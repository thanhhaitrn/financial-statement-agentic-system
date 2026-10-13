"""Project-wide defaults for ingestion, vector indexing, and dataset metadata."""
# Code note: Config modules centralize constants used by routing, ingestion, and retrieval.

# Relative to FINX_HOME: the versioned sample report of a source checkout, used
# by `agentfinx ask` when no dataset is chosen.
DEFAULT_DATA_FILE = "backend/benchmarks/reports/document.md"

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
    "ingestion_version": "v25",
}

VECTOR_BATCH_SIZE = 64
