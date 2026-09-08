"""Owner-scoped report discovery and import orchestration."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from acquisition.download import (PdfArtifactValidator, PdfValidationError,
                                  SecurePdfDownloader)
from acquisition.llamaparse import LlamaParseError
from acquisition.quota import AcquisitionQuota, QuotaExceeded
from acquisition.store import AcquisitionStore
from config.runtime_policy import AcquisitionPolicy
from schemas.acquisition import (ConvertedDocument, ImportJob, PdfArtifact,
                                 ReportCandidate, ReportQuery)


class ReportDiscoveryUnavailable(RuntimeError):
    """CafeF discovery is optional; callers should offer direct upload."""


def _owner_namespace(owner_id: str) -> str:
    return hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16]


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def build_imported_dataset(
    job: ImportJob,
    artifact: PdfArtifact,
    converted_path: Path,
) -> str:
    """Register and fully build one converted report with the existing pipeline."""

    from test import ensure_built

    from config.settings import DEFAULT_DATASET
    from dataset_catalog.registry import (build_dataset_record,
                                          make_dataset_id, save_dataset)

    metadata = dict(job.metadata or {})
    company = str(metadata.get("company") or metadata.get("ticker") or "Imported report")
    base_id = make_dataset_id(
        company,
        report_type=str(metadata.get("report_type") or "financial_statement"),
        fiscal_year=metadata.get("fiscal_year"),
        fiscal_quarter=metadata.get("fiscal_quarter"),
        scope=str(metadata.get("scope") or "unknown"),
        audit_status=str(metadata.get("audit_status") or "unknown"),
    )
    dataset_id = f"{_owner_namespace(job.owner_id)}-{base_id}"
    record = build_dataset_record(
        file_path=str(converted_path),
        company=company,
        owner_id=job.owner_id,
        dataset_id=dataset_id,
        ticker=str(metadata.get("ticker") or ""),
        report_type=str(metadata.get("report_type") or "financial_statement"),
        fiscal_year=metadata.get("fiscal_year"),
        fiscal_quarter=metadata.get("fiscal_quarter"),
        scope=str(metadata.get("scope") or "unknown"),
        audit_status=str(metadata.get("audit_status") or "unknown"),
        ingestion_version=DEFAULT_DATASET["ingestion_version"],
        source_origin=job.origin,
        source_sha256=artifact.source_sha256,
        source_pdf_path=artifact.path,
        source_converter_version=str((metadata.get("conversion") or {}).get("provider_version") or ""),
        source_converter_identity=str(
            (metadata.get("conversion") or {}).get("converter_identity") or ""
        ),
        managed_source=True,
    )
    record = save_dataset(record)
    ready, _connection, _collection = ensure_built(record)
    if ready.status != "ready":
        raise RuntimeError(f"dataset build ended in status {ready.status}")
    return ready.dataset_id


class ReportAcquisitionService:
    def __init__(
        self,
        *,
        store: AcquisitionStore,
        discovery_provider,
        converter,
        validator: PdfArtifactValidator,
        downloader: SecurePdfDownloader,
        policy: AcquisitionPolicy,
        artifact_dir: str | Path,
        converted_dir: str | Path,
        symbol_catalog_path: str | Path | None = None,
        dataset_builder: Callable[[ImportJob, PdfArtifact, Path], str] = build_imported_dataset,
        dataset_attacher: Callable[[str, str, str], None] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.store = store
        self.discovery_provider = discovery_provider
        self.converter = converter
        self.validator = validator
        self.downloader = downloader
        self.policy = policy
        self.artifact_dir = Path(artifact_dir)
        self.converted_dir = Path(converted_dir)
        self.symbol_catalog_path = (
            Path(symbol_catalog_path) if symbol_catalog_path is not None else None
        )
        self.dataset_builder = dataset_builder
        self.dataset_attacher = dataset_attacher
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.quota = AcquisitionQuota(store, policy, now=self._now)

    def discover(self, query: ReportQuery) -> list[ReportCandidate]:
        if self.symbol_catalog_path is not None:
            from web_evidence.symbols import load_symbol_catalog

            symbol = load_symbol_catalog(self.symbol_catalog_path).get(query.ticker)
            if symbol is not None:
                query = query.model_copy(
                    update={
                        "company": query.company or symbol.company,
                        "exchange": query.exchange or symbol.exchange,
                    }
                )
        if not query.exchange:
            query = query.model_copy(update={"exchange": "HOSE"})
        try:
            raw_candidates = self.discovery_provider.discover(query)
        except Exception as exc:
            raise ReportDiscoveryUnavailable(
                "Report discovery is temporarily unavailable; upload the PDF directly."
            ) from exc
        created_at = self._now().replace(microsecond=0)
        expires_at = created_at + timedelta(seconds=self.policy.candidate_ttl_seconds)
        candidates = [
            ReportCandidate(
                candidate_id=uuid.uuid4().hex,
                owner_id=query.owner_id,
                session_id=query.session_id,
                provider=self.discovery_provider.provider_identity,
                created_at=created_at.isoformat(),
                expires_at=expires_at.isoformat(),
                **raw,
            )
            for raw in raw_candidates
        ]
        self.store.save_candidates(candidates)
        return candidates

    def _existing_job(self, owner_id: str, idempotency_key: str) -> ImportJob | None:
        if not str(idempotency_key or "").strip():
            raise ValueError("idempotency_key is required")
        return self.store.get_job_by_idempotency(owner_id, idempotency_key)

    def _admit_job(self, job: ImportJob, *, idempotency_key: str) -> ImportJob:
        with self.store.transaction():
            existing = self._existing_job(job.owner_id, idempotency_key)
            if existing is not None:
                if existing.session_id != job.session_id:
                    raise ValueError("idempotency_key belongs to another session")
                return existing
            self.quota.check_import_slot(job.owner_id)
            reserved = self.quota.reserve_import(
                job.owner_id, page_count=job.page_count or self.policy.max_pdf_pages,
            )
            return self.store.create_job(
                job.model_copy(update={"reserved_credits": reserved}),
                idempotency_key=idempotency_key,
            )

    def _stage_upload(self, artifact: PdfArtifact, *, owner_id: str) -> Path:
        target = self.artifact_dir / _owner_namespace(owner_id) / f"{artifact.source_sha256}.pdf"
        if not target.exists():
            _atomic_write(target, Path(artifact.path).read_bytes())
        return target.resolve()

    def create_upload_import(
        self,
        *,
        owner_id: str,
        session_id: str,
        upload_path: str | Path,
        idempotency_key: str,
        metadata: dict | None = None,
    ) -> ImportJob:
        existing = self._existing_job(owner_id, idempotency_key)
        if existing is not None:
            if existing.session_id != session_id:
                raise ValueError("idempotency_key belongs to another session")
            return existing
        self.quota.check_import_slot(owner_id)
        namespace = _owner_namespace(owner_id)
        artifact = self.validator.validate(
            upload_path,
            origin="upload",
            public_source=False,
            cache_namespace=namespace,
        )
        reserved = self.quota.reserve_import(owner_id, page_count=artifact.page_count)
        staged_path = self._stage_upload(artifact, owner_id=owner_id)
        now = self._now().replace(microsecond=0).isoformat()
        job = ImportJob(
            job_id=uuid.uuid4().hex,
            owner_id=owner_id,
            session_id=session_id,
            origin="upload",
            status="queued",
            upload_path=str(staged_path),
            source_sha256=artifact.source_sha256,
            page_count=artifact.page_count,
            reserved_credits=reserved,
            metadata=dict(metadata or {}),
            created_at=now,
            updated_at=now,
        )
        return self._admit_job(job, idempotency_key=idempotency_key)

    def create_candidate_import(
        self,
        *,
        owner_id: str,
        session_id: str,
        candidate_id: str,
        idempotency_key: str,
    ) -> ImportJob:
        existing = self._existing_job(owner_id, idempotency_key)
        if existing is not None:
            if existing.session_id != session_id:
                raise ValueError("idempotency_key belongs to another session")
            return existing
        self.quota.check_import_slot(owner_id)
        candidate = self.store.get_candidate(
            candidate_id,
            owner_id=owner_id,
            session_id=session_id,
            now=self._now(),
        )
        # Discovery does not expose a trustworthy page count. Reserve the
        # configured maximum before enqueue, then reconcile to the validated
        # page count after download.
        reserved = self.quota.reserve_import(
            owner_id,
            page_count=self.policy.max_pdf_pages,
        )
        now = self._now().replace(microsecond=0).isoformat()
        job = ImportJob(
            job_id=uuid.uuid4().hex,
            owner_id=owner_id,
            session_id=session_id,
            origin="cafef_discovery",
            status="queued",
            candidate_id=candidate.candidate_id,
            reserved_credits=reserved,
            metadata={
                "candidate": candidate.model_dump(mode="json"),
                "ticker": candidate.ticker,
                "company": candidate.company,
                "report_type": candidate.report_type,
                "fiscal_year": candidate.fiscal_year,
                "fiscal_quarter": candidate.fiscal_quarter,
                "scope": candidate.scope,
                "audit_status": candidate.audit_status,
            },
            created_at=now,
            updated_at=now,
        )
        return self._admit_job(job, idempotency_key=idempotency_key)

    def _transition(self, job: ImportJob, status: str, **updates) -> ImportJob:
        return self.store.update_job(job.model_copy(update={"status": status, **updates}))

    def _write_converted(self, job: ImportJob, converted: ConvertedDocument) -> Path:
        namespace = _owner_namespace(job.owner_id)
        path = self.converted_dir / namespace / f"{converted.source_sha256}.md"
        _atomic_write(path, (converted.markdown.rstrip() + "\n").encode("utf-8"))
        return path.resolve()

    def run_job(self, job_id: str) -> ImportJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise ValueError("import job not found")
        claimed = self.store.claim_job(job_id)
        if claimed is None:
            return self.store.get_job(job_id)
        job = claimed

        try:
            if job.origin == "cafef_discovery":
                job = self._transition(job, "downloading")
                candidate = ReportCandidate.model_validate(job.metadata.get("candidate", {}))
                artifact = self.downloader.download(
                    candidate,
                    target_dir=self.artifact_dir / _owner_namespace(job.owner_id),
                    cache_namespace=_owner_namespace(job.owner_id),
                )
                reserved = self.quota.estimate_import_credits(artifact.page_count)
                job = self.store.update_job(
                    job.model_copy(
                        update={
                            "upload_path": artifact.path,
                            "source_sha256": artifact.source_sha256,
                            "page_count": artifact.page_count,
                            "reserved_credits": reserved,
                        }
                    )
                )
            else:
                artifact = self.validator.validate(
                    job.upload_path,
                    origin="upload",
                    public_source=False,
                    cache_namespace=_owner_namespace(job.owner_id),
                )

            job = self._transition(job, "parsing")
            with self.store.parse_slot(job.job_id, limit=self.policy.global_parse_concurrency):
                converted = self.converter.convert(
                    artifact,
                    private=job.origin == "upload",
                )
            job = self._transition(
                job,
                "validating",
                billed_pages=converted.billed_pages,
                billed_credits=converted.billed_credits,
                metadata={
                    **dict(job.metadata or {}),
                    "conversion": {
                        "provider": converted.provider,
                        "converter_identity": str(
                            getattr(self.converter, "converter_identity", "") or ""
                        ),
                        "provider_version": converted.provider_version,
                        "tier": converted.tier,
                        "billed_pages": converted.billed_pages,
                        "billed_credits": converted.billed_credits,
                        "cache_hit": converted.cache_hit,
                        "targeted_retry_pages": converted.targeted_retry_pages,
                    },
                },
            )
            if not converted.markdown or converted.quality_issues:
                raise LlamaParseError("converted document failed quality validation")
            converted_path = self._write_converted(job, converted)
            job = self._transition(job, "building", converted_path=str(converted_path))
            dataset_id = self.dataset_builder(job, artifact, converted_path)
            ready = self._transition(job, "ready", dataset_id=dataset_id)
            if self.dataset_attacher is None:
                return ready
            try:
                self.dataset_attacher(ready.owner_id, ready.session_id, dataset_id)
            except Exception as exc:
                # The dataset remains valid and owner-scoped. Surface the
                # session-link failure explicitly instead of rolling a ready
                # dataset back to failed.
                return self.store.update_job(
                    ready.model_copy(
                        update={"session_attachment_error": type(exc).__name__}
                    )
                )
            return self.store.update_job(
                ready.model_copy(update={"session_attached": True})
            )
        except (PdfValidationError, QuotaExceeded, LlamaParseError) as exc:
            return self._transition(
                job,
                "failed",
                billed_pages=int(getattr(exc, "billed_pages", job.billed_pages) or 0),
                billed_credits=int(
                    getattr(exc, "billed_credits", job.billed_credits) or 0
                ),
                error_code=getattr(exc, "code", type(exc).__name__),
                error_message=str(exc),
                retryable=bool(getattr(exc, "retryable", False)),
                billing_pending=bool(getattr(exc, "billing_pending", False)),
            )
        except Exception as exc:
            return self._transition(
                job,
                "failed",
                error_code=type(exc).__name__,
                error_message=str(exc),
                retryable=False,
            )
