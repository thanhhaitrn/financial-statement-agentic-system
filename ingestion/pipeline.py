"""Build the SQLite knowledge base from a registered dataset document."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from pathlib import Path

from ingestion.kb_builder import build_fact_rows
from ingestion.frontmatter_parser import build_frontmatter_rows
from ingestion.note_parser import (
    build_note_rows,
    infer_audit_status,
    infer_company,
    infer_fiscal_quarter,
    infer_fiscal_year,
    infer_report_scope,
    infer_ticker,
)
from ingestion.table_parser import attach_context
from kb.sqlite_repo import (
    SQLITE_SCHEMA_VERSION,
    facts_sha256,
    init_db,
    insert_financial_facts,
    kb_manifest_matches,
    normalize_financial_fact_rows,
    open_db_readonly,
    validate_kb_database,
    write_kb_manifest,
)
from schemas.datasets import DatasetRecord


PARSER_CONTRACT_VERSION = "agentfinx-parser-v13"
# Modules whose regex/lexicon decide what a fact IS. Their combined digest is
# pinned so a behaviour change cannot ship without bumping the contract version
# above — otherwise two KBs built by different parsers share one version string.
PARSER_CONTRACT_SOURCE_FILES = (
    "ingestion/frontmatter_parser.py",
    "ingestion/note_parser.py",
    "ingestion/table_parser.py",
    "ingestion/period_normalize.py",
    "ingestion/semantic_dimensions.py",
    "ingestion/topic_atoms.py",
    "ingestion/kb_builder.py",
)
PARSER_CONTRACT_SOURCE_SHA256 = (
    "3505cb60e70cb7efe6b2c162fcfa1a9c377e51c816c675fedd5ad8949736e6cb"
)


def parser_contract_source_sha256(root: "Path | None" = None) -> str:
    """Digest of every parser module that decides fact extraction behaviour."""

    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    digest = hashlib.sha256()
    for relative_path in PARSER_CONTRACT_SOURCE_FILES:
        digest.update(relative_path.encode("utf-8"))
        digest.update((base / relative_path).read_bytes())
    return digest.hexdigest()

_UNKNOWN_METADATA = {"", "unknown", "none", "null", "n/a", "na"}
_SCOPE_ALIASES = {
    "separate": "separate",
    "standalone": "separate",
    "rieng": "separate",
    "consolidated": "consolidated",
    "hop nhat": "consolidated",
    "combined": "combined",
    "tong hop": "combined",
}
_AUDIT_ALIASES = {
    "audited": "audited",
    "audit": "audited",
    "kiem toan": "audited",
    "reviewed": "reviewed",
    "review": "reviewed",
    "soat xet": "reviewed",
    "unaudited": "unaudited",
    "unreviewed": "unaudited",
    "chua kiem toan": "unaudited",
}


def _metadata_key(value: object) -> str:
    text = str(value or "").strip().replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _known_metadata(value: object) -> bool:
    return _metadata_key(value) not in _UNKNOWN_METADATA


def _canonical_metadata(value: object, aliases: dict[str, str]) -> str:
    key = _metadata_key(value)
    return aliases.get(key, key)


def _company_core(value: object) -> str:
    key = _metadata_key(value)
    legal_prefixes = (
        "cong ty co phan ",
        "cong ty trach nhiem huu han ",
        "cong ty tnhh ",
        "cong ty ",
    )
    for prefix in legal_prefixes:
        if key.startswith(prefix):
            return key[len(prefix):].strip()
    return key


def _token_subsequence(shorter: str, longer: str) -> bool:
    short_tokens = shorter.split()
    long_tokens = longer.split()
    if not short_tokens or len(short_tokens) > len(long_tokens):
        return False
    width = len(short_tokens)
    return any(
        long_tokens[index : index + width] == short_tokens
        for index in range(len(long_tokens) - width + 1)
    )


def _resolve_source_field(
    *,
    field: str,
    registry_value: object,
    source_value: object,
    aliases: dict[str, str] | None = None,
) -> str:
    """Prefer a source-backed canonical value and reject contradictions."""

    registry_text = str(registry_value or "").strip()
    source_text = str(source_value or "").strip()
    registry_known = _known_metadata(registry_text)
    source_known = _known_metadata(source_text)
    if not source_known:
        if registry_known and aliases is not None:
            return _canonical_metadata(registry_text, aliases)
        return registry_text or "unknown"
    if not registry_known:
        return source_text

    if field == "company":
        registry_key = _company_core(registry_text)
        source_key = _company_core(source_text)
        compatible = bool(
            registry_key
            and source_key
            and (
                registry_key == source_key
                or _token_subsequence(registry_key, source_key)
                or _token_subsequence(source_key, registry_key)
            )
        )
    else:
        registry_key = (
            _canonical_metadata(registry_text, aliases)
            if aliases is not None
            else _metadata_key(registry_text)
        )
        source_key = (
            _canonical_metadata(source_text, aliases)
            if aliases is not None
            else _metadata_key(source_text)
        )
        compatible = registry_key == source_key

    if not compatible:
        raise ValueError(
            f"dataset {field} conflicts with source metadata: "
            f"registry={registry_text!r} source={source_text!r}"
        )
    if field == "company" and _metadata_key(registry_text) == _metadata_key(
        source_text
    ):
        # Preserve curated capitalization when registry and source carry the
        # exact same legal name. A compatible abbreviation still expands to
        # the complete source-backed name below.
        return registry_text
    if aliases is not None:
        return _canonical_metadata(source_text, aliases)
    return source_text


def _temporary_sibling(path: Path, *, suffix: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=suffix,
        dir=str(path.parent),
    )
    os.close(descriptor)
    return Path(temp_name)


def _stage_raw_tables(raw_path: Path, tables_with_context: list[dict]) -> Path:
    staged_path = _temporary_sibling(raw_path, suffix=".tmp")
    completed = False
    try:
        with staged_path.open("w", encoding="utf-8") as handle:
            json.dump(tables_with_context, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        # Prove the staged artifact is valid JSON before it can replace the
        # previous parser output.
        with staged_path.open("r", encoding="utf-8") as handle:
            decoded = json.load(handle)
        if not isinstance(decoded, list):
            raise ValueError("raw tables artifact must contain a JSON list")
        completed = True
        return staged_path
    finally:
        if not completed:
            staged_path.unlink(missing_ok=True)


def _write_raw_tables(dataset: DatasetRecord, tables_with_context: list[dict]) -> None:
    raw_path = Path(dataset.raw_tables_path)
    staged_path = _stage_raw_tables(raw_path, tables_with_context)
    os.replace(staged_path, raw_path)


class UnsupportedSourceFormat(ValueError):
    """A source must be converted to the Markdown ingestion contract first."""


def _read_source(dataset: DatasetRecord) -> tuple[Path, bytes, str]:
    source_path = Path(dataset.file_path).resolve(strict=True)
    if source_path.suffix.lower() != ".md":
        raise UnsupportedSourceFormat("Ingestion accepts .md only; import PDF via agentfinx reports")
    source_bytes = source_path.read_bytes()
    md_text = source_bytes.decode("utf-8")
    return source_path, source_bytes, md_text


def _resolve_ingestion_metadata(dataset: DatasetRecord, md_text: str) -> dict:
    """Resolve source identity and reject contradictory registry metadata."""

    source_year = str(infer_fiscal_year(md_text) or "").strip()
    registry_year = str(getattr(dataset, "fiscal_year", "") or "").strip()
    if source_year and registry_year and source_year != registry_year:
        raise ValueError(
            "dataset fiscal_year conflicts with the reporting period in source: "
            f"registry={registry_year} source={source_year}"
        )

    source_company = infer_company(md_text)
    source_ticker = infer_ticker(md_text)
    source_quarter = infer_fiscal_quarter(md_text)
    source_scope = infer_report_scope(md_text)
    source_audit_status = infer_audit_status(md_text)
    company = _resolve_source_field(
        field="company",
        registry_value=getattr(dataset, "company", ""),
        source_value=source_company,
    )
    ticker = _resolve_source_field(
        field="ticker",
        registry_value=getattr(dataset, "ticker", ""),
        source_value=source_ticker,
    )
    scope = _resolve_source_field(
        field="scope",
        registry_value=getattr(dataset, "scope", "unknown"),
        source_value=source_scope,
        aliases=_SCOPE_ALIASES,
    )
    audit_status = _resolve_source_field(
        field="audit_status",
        registry_value=getattr(dataset, "audit_status", "unknown"),
        source_value=source_audit_status,
        aliases=_AUDIT_ALIASES,
    )
    fiscal_year = registry_year or source_year
    registry_quarter = getattr(dataset, "fiscal_quarter", None)
    if (
        source_quarter is not None
        and registry_quarter is not None
        and int(registry_quarter) != int(source_quarter)
    ):
        raise ValueError(
            "dataset fiscal_quarter conflicts with source metadata: "
            f"registry={registry_quarter!r} source={source_quarter!r}"
        )
    fiscal_quarter = (
        int(registry_quarter)
        if registry_quarter is not None
        else source_quarter
    )
    identity_payload = {
        "company": company,
        "ticker": ticker if _known_metadata(ticker) else "",
        "report_type": str(
            getattr(dataset, "report_type", "financial_statement")
            or "financial_statement"
        ).strip(),
        "fiscal_year": fiscal_year,
        "fiscal_quarter": (
            str(fiscal_quarter) if fiscal_quarter is not None else ""
        ),
        "scope": scope,
        "audit_status": audit_status,
    }
    encoded = json.dumps(
        identity_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        **identity_payload,
        "metadata_sha256": hashlib.sha256(encoded).hexdigest(),
        "source_company": source_company,
        "source_fiscal_year": source_year,
        "source_fiscal_quarter": (
            str(source_quarter) if source_quarter is not None else ""
        ),
        "source_ticker": source_ticker,
        "source_scope": source_scope,
        "source_audit_status": source_audit_status,
    }


def resolve_dataset_metadata(dataset: DatasetRecord) -> DatasetRecord:
    """Return a registry record reconciled with source-backed metadata."""

    _source_path, _source_bytes, md_text = _read_source(dataset)
    resolved = _resolve_ingestion_metadata(dataset, md_text)
    fiscal_year = str(resolved.get("fiscal_year", "") or "").strip()
    fiscal_quarter = str(resolved.get("fiscal_quarter", "") or "").strip()
    return dataset.model_copy(
        update={
            "company": str(resolved.get("company", "") or "").strip(),
            "ticker": str(resolved.get("ticker", "") or "").strip(),
            "fiscal_year": int(fiscal_year) if fiscal_year else None,
            "fiscal_quarter": (
                int(fiscal_quarter) if fiscal_quarter else None
            ),
            "scope": str(resolved.get("scope", "unknown") or "unknown").strip(),
            "audit_status": str(
                resolved.get("audit_status", "unknown") or "unknown"
            ).strip(),
        }
    )


def _manifest_identity(
    dataset: DatasetRecord,
    source_path: Path,
    source_bytes: bytes,
    resolved_metadata: dict,
) -> dict:
    ingestion_version = str(dataset.ingestion_version or "v1").strip() or "v1"
    return {
        "source_path": str(source_path),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "metadata_sha256": str(resolved_metadata["metadata_sha256"]),
        "parser_version": f"{PARSER_CONTRACT_VERSION}:{ingestion_version}",
        "schema_version": SQLITE_SCHEMA_VERSION,
        **({
            "converter_identity": dataset.source_converter_identity,
            "converter_provider_version": getattr(dataset, "source_converter_version", ""),
        } if getattr(dataset, "source_converter_identity", "") else {}),
    }


def _existing_kb_matches(db_path: Path, expected_manifest: dict) -> bool:
    if not db_path.is_file():
        return False
    try:
        conn = open_db_readonly(str(db_path))
        try:
            return kb_manifest_matches(conn, expected_manifest)
        finally:
            conn.close()
    except sqlite3.Error:
        # A missing/legacy/corrupt database is rebuilt in staging. It is never
        # modified or deleted before the replacement passes validation.
        return False


def _parse_fact_rows(
    dataset: DatasetRecord,
    md_text: str,
    resolved_metadata: dict | None = None,
) -> tuple[list[dict], list[tuple]]:
    resolved_metadata = resolved_metadata or _resolve_ingestion_metadata(
        dataset,
        md_text,
    )
    company = str(resolved_metadata.get("company", "") or "")
    fiscal_year = str(resolved_metadata.get("fiscal_year", "") or "")
    tables_with_context = attach_context(md_text)
    if not isinstance(tables_with_context, list):
        raise ValueError("table parser must return a list")

    rows = build_fact_rows(
        tables_with_context,
        company=company,
        source=dataset.file_path,
        fiscal_year=fiscal_year,
    )
    rows.extend(
        build_frontmatter_rows(
            md_text,
            company=company,
            source=dataset.file_path,
            fiscal_year=fiscal_year,
            include_table_rows=False,
        )
    )
    rows.extend(
        build_note_rows(
            md_text,
            company=company,
            source=dataset.file_path,
            fiscal_year=fiscal_year,
            include_table_rows=False,
        )
    )
    normalized_rows = normalize_financial_fact_rows(rows)
    if not normalized_rows:
        raise ValueError("ingestion produced zero validated financial facts")
    return tables_with_context, normalized_rows


def build_knowledge_base(dataset: DatasetRecord, *, reset: bool = False):
    print("\n=== BUILDING KNOWLEDGE BASE ===")

    db_path = Path(dataset.sqlite_db_path)
    raw_path = Path(dataset.raw_tables_path)
    source_path, source_bytes, md_text = _read_source(dataset)
    resolved_metadata = _resolve_ingestion_metadata(dataset, md_text)
    manifest_identity = _manifest_identity(
        dataset,
        source_path,
        source_bytes,
        resolved_metadata,
    )

    if not reset and _existing_kb_matches(db_path, manifest_identity):
        print("SQLite manifest matches source and parser → skipping KB build")
        return init_db(str(db_path)), 0

    # Parse and validate everything before creating or replacing a persistent
    # artifact. A parser exception therefore leaves the current KB untouched.
    tables_with_context, rows = _parse_fact_rows(
        dataset,
        md_text,
        resolved_metadata,
    )
    manifest = {
        **manifest_identity,
        "facts_count": len(rows),
        "facts_sha256": facts_sha256(rows),
    }

    staged_raw_path = _stage_raw_tables(raw_path, tables_with_context)
    staged_db_path = _temporary_sibling(db_path, suffix=".sqlite.tmp")
    staged_conn = None
    try:
        staged_conn = init_db(str(staged_db_path), reset=True)
        insert_financial_facts(staged_conn, rows)
        write_kb_manifest(staged_conn, manifest)
        validate_kb_database(staged_conn, manifest)
        staged_conn.close()
        staged_conn = None

        with staged_db_path.open("rb") as handle:
            os.fsync(handle.fileno())

        # SQLite is the authoritative artifact and is replaced last. If either
        # staging or the raw-table swap fails, the current queryable KB remains.
        os.replace(staged_raw_path, raw_path)
        os.replace(staged_db_path, db_path)
    finally:
        if staged_conn is not None:
            staged_conn.close()
        staged_db_path.unlink(missing_ok=True)
        staged_raw_path.unlink(missing_ok=True)

    conn = init_db(str(db_path))
    print(f"Inserted {len(rows)} facts into SQLite")
    return conn, len(rows)
