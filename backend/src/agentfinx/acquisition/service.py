"""Owner-scoped report discovery and import orchestration."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from agentfinx.acquisition.cafef import ReportDiscoveryError
from agentfinx.acquisition.download import (PdfArtifactValidator, PdfValidationError,
                                  SecurePdfDownloader)
from agentfinx.acquisition.llamaparse import LlamaParseError
from agentfinx.acquisition.quota import AcquisitionQuota, QuotaExceeded
from agentfinx.acquisition.store import (TERMINAL_JOB_STATUSES, AcquisitionStore,
                               ParseAttemptRecorder)
from agentfinx.core.config.runtime_policy import AcquisitionPolicy
from agentfinx.core.schemas.acquisition import (ConvertedDocument, ImportJob, PdfArtifact,
                                 ReportCandidate, ReportQuery)

# Statuses a crashed import worker can leave behind.
IN_FLIGHT_JOB_STATUSES = {"downloading", "parsing", "validating", "building"}


class ReportDiscoveryUnavailable(RuntimeError):
    """CafeF could not be searched — never "no reports". Offer direct upload."""

    def __init__(
        self,
        message: str = "Report discovery is temporarily unavailable; upload the PDF directly.",
        *,
        code: str = "provider_unavailable",
    ):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DiscoveryOutcome:
    query: ReportQuery
    candidates: list
    total_matched: int
    truncated: bool
    catalog_status: str
    ticker_in_catalog: Optional[bool]
    warnings: tuple = ()

    @property
    def status(self) -> str:
        return "results" if self.candidates else "no_results"


@dataclass(frozen=True)
class SubjectResolution:
    """What a ticker or company name the user typed refers to.

    ``resolved``: one symbol. ``ambiguous``: several — the user must choose.
    ``not_in_catalog``: a ticker-shaped text a usable catalog does not list; it
    can still be searched, with a warning. ``not_found``: a name nothing
    matches. ``catalog_unavailable``: a name, but no usable catalog — ask for
    the ticker. ``needs_input``: nothing to resolve.
    """

    status: str
    matches: tuple
    catalog_status: str


class ImportJobNotFound(ValueError):
    code = "import_job_not_found"


class IdempotencyConflict(ValueError):
    """The key was already used for a different request, or for a job written
    before request identities existed (``legacy_identity``)."""

    def __init__(self, code: str = "request_mismatch"):
        super().__init__(
            "idempotency_key was used for a different request "
            "(source, session, cloud-parse consent or metadata)"
            if code == "request_mismatch"
            else "idempotency_key belongs to an import recorded before request identities; "
                 "look the job up instead of replaying it"
        )
        self.code = code


def import_request_fingerprint(
    *,
    source_kind: str,
    source_id: str,
    source_session_id: str,
    target_session_id: str,
    allow_cloud_parse: bool,
    metadata: dict,
) -> str:
    body = {
        "source_kind": source_kind,
        "source_id": source_id,
        "source_session_id": source_session_id,
        "target_session_id": target_session_id,
        "allow_cloud_parse": bool(allow_cloud_parse),
        "metadata": metadata or {},
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


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

    from agentfinx.workflow.pipeline import ensure_built

    from agentfinx.core.config.settings import DEFAULT_DATASET
    from agentfinx.datasets.registry import (build_dataset_record,
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
    from agentfinx.datasets.locks import dataset_lock

    # Build/publish takes the dataset lock first; save_dataset then takes the
    # registry lock inside it — the fixed order shared with chat and the CLI.
    with dataset_lock(dataset_id):
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
        symbol_alias_path: str | Path | None = None,
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
        self.symbol_alias_path = Path(symbol_alias_path) if symbol_alias_path is not None else None
        self.dataset_builder = dataset_builder
        self.dataset_attacher = dataset_attacher
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.quota = AcquisitionQuota(store, policy, now=self._now)

    def symbol_catalog(self):
        from agentfinx.acquisition.symbols import SymbolCatalog

        if self.symbol_catalog_path is None:
            return SymbolCatalog({})
        return SymbolCatalog.load(self.symbol_catalog_path, alias_path=self.symbol_alias_path)

    def resolve_subject(self, text: str) -> SubjectResolution:
        """Map a ticker or company name to catalog symbols. Never guesses."""

        from agentfinx.acquisition.symbols import StockSymbol, looks_like_ticker

        raw = " ".join(str(text or "").split())
        catalog = self.symbol_catalog()
        now = self._now()
        status = catalog.status(now)
        if not raw:
            return SubjectResolution("needs_input", (), status)
        if not catalog.usable(now):
            if looks_like_ticker(raw):
                return SubjectResolution("resolved", (StockSymbol(raw.upper(), "", ""),), status)
            return SubjectResolution("catalog_unavailable", (), status)
        matches = tuple(match.symbol for match in catalog.search(raw))
        if len(matches) == 1:
            return SubjectResolution("resolved", matches, status)
        if matches:
            return SubjectResolution("ambiguous", matches, status)
        if looks_like_ticker(raw):
            return SubjectResolution("not_in_catalog", (StockSymbol(raw.upper(), "", ""),), status)
        return SubjectResolution("not_found", (), status)

    def discover(self, query: ReportQuery) -> DiscoveryOutcome:
        """Search CafeF and store the candidates, bound to owner and session.

        No exchange is defaulted: the report endpoint does not use one, and the
        catalog only supplies display metadata when it is fresh enough."""

        catalog = self.symbol_catalog()
        now = self._now()
        catalog_status = catalog.status(now)
        warnings: list[str] = []
        ticker_in_catalog: Optional[bool] = None
        if catalog.usable(now):
            symbol = catalog.get(query.ticker)
            ticker_in_catalog = symbol is not None
            if symbol is not None:
                query = query.model_copy(
                    update={
                        "company": query.company or symbol.company,
                        "exchange": query.exchange or symbol.exchange,
                    }
                )
            else:
                warnings.append("ticker_not_in_catalog")
            if catalog_status == "stale":
                warnings.append("catalog_stale")
        else:
            warnings.append("catalog_unavailable")
        try:
            result = self.discovery_provider.discover(query)
        except ReportDiscoveryError as exc:
            raise ReportDiscoveryUnavailable(code=exc.code) from exc
        except Exception as exc:
            raise ReportDiscoveryUnavailable() from exc
        created_at = now.replace(microsecond=0)
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
            for raw in result.candidates
        ]
        if result.truncated:
            warnings.append("results_truncated")
        self.store.save_candidates(candidates)
        return DiscoveryOutcome(
            query=query,
            candidates=candidates,
            total_matched=result.total_matched,
            truncated=result.truncated,
            catalog_status=catalog_status,
            ticker_in_catalog=ticker_in_catalog,
            warnings=tuple(warnings),
        )

    def _existing_job(self, owner_id: str, idempotency_key: str, *, fingerprint: str) -> ImportJob | None:
        """The job this key already created — only if it was the same request.

        Consent, source and sessions are all part of the identity: replaying a
        key never silently returns a job created for a different request."""

        if not str(idempotency_key or "").strip():
            raise ValueError("idempotency_key is required")
        existing = self.store.get_job_by_idempotency(owner_id, idempotency_key)
        if existing is None:
            return None
        if not existing.request_fingerprint:
            raise IdempotencyConflict("legacy_identity")
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflict("request_mismatch")
        return existing

    def get_candidate(self, candidate_id: str, *, owner_id: str, session_id: str) -> ReportCandidate:
        """An owner's unexpired candidate from the session it was found in."""

        return self.store.get_candidate(
            candidate_id, owner_id=owner_id, session_id=session_id, now=self._now()
        )

    def release_dispatch_hold(self, job_id: str) -> ImportJob | None:
        return self.store.release_dispatch_hold(job_id)

    def cancel_held_job(self, job_id: str, *, reason: str) -> ImportJob | None:
        """Fail a job the web hand-off could not link. Nothing was submitted,
        so the reservation is released and billing stays ``not_submitted``."""

        with self.store.transaction():
            job = self.store.get_job(job_id)
            if job is None or job.status != "queued" or not job.dispatch_hold:
                return job
            return self.store.update_job(job.model_copy(update={
                "status": "failed",
                "dispatch_hold": False,
                "error_code": reason,
                "error_message": "the import could not be linked to its conversation",
                "retryable": False,
            }))

    def _consent_fields(self, allow_cloud_parse: bool) -> dict:
        consented = bool(allow_cloud_parse)
        return {
            "allow_cloud_parse": consented,
            "consented_at": self._now().replace(microsecond=0).isoformat() if consented else "",
        }

    def _admit_job(self, job: ImportJob, *, idempotency_key: str) -> ImportJob:
        with self.store.transaction():
            existing = self._existing_job(
                job.owner_id, idempotency_key, fingerprint=job.request_fingerprint,
            )
            if existing is not None:
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
        allow_cloud_parse: bool = False,
        source_id: str | None = None,
        source_session_id: str | None = None,
        hold: bool = False,
    ) -> ImportJob:
        """``source_id`` is the web upload ID, so a replay is recognised after
        the uploaded file is gone; without it (CLI) the verified SHA is used."""

        namespace = _owner_namespace(owner_id)
        source_session = source_session_id or session_id
        artifact = None
        if source_id is None:
            artifact = self.validator.validate(
                upload_path, origin="upload", public_source=False, cache_namespace=namespace,
            )
            source_id = artifact.source_sha256
        fingerprint = import_request_fingerprint(
            source_kind="upload",
            source_id=source_id,
            source_session_id=source_session,
            target_session_id=session_id,
            allow_cloud_parse=allow_cloud_parse,
            metadata=dict(metadata or {}),
        )
        existing = self._existing_job(owner_id, idempotency_key, fingerprint=fingerprint)
        if existing is not None:
            return existing
        self.quota.check_import_slot(owner_id)
        if artifact is None:
            artifact = self.validator.validate(
                upload_path, origin="upload", public_source=False, cache_namespace=namespace,
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
            source_kind="upload",
            source_id=source_id,
            source_session_id=source_session,
            request_fingerprint=fingerprint,
            dispatch_hold=hold,
            **self._consent_fields(allow_cloud_parse),
        )
        return self._admit_job(job, idempotency_key=idempotency_key)

    def create_candidate_import(
        self,
        *,
        owner_id: str,
        session_id: str,
        candidate_id: str,
        idempotency_key: str,
        allow_cloud_parse: bool = False,
        source_session_id: str | None = None,
        hold: bool = False,
    ) -> ImportJob:
        """``session_id`` is where the import goes; the candidate is checked
        against ``source_session_id`` — the session it was found in. A replay
        of an accepted request returns its job even after the candidate expired."""

        source_session = source_session_id or session_id
        fingerprint = import_request_fingerprint(
            source_kind="candidate",
            source_id=candidate_id,
            source_session_id=source_session,
            target_session_id=session_id,
            allow_cloud_parse=allow_cloud_parse,
            metadata={},
        )
        existing = self._existing_job(owner_id, idempotency_key, fingerprint=fingerprint)
        if existing is not None:
            return existing
        self.quota.check_import_slot(owner_id)
        candidate = self.store.get_candidate(
            candidate_id,
            owner_id=owner_id,
            session_id=source_session,
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
                "period_type": candidate.period_type,
                "period_verified": candidate.period_verified,
                "fiscal_year": candidate.fiscal_year,
                "fiscal_quarter": candidate.fiscal_quarter,
                "fiscal_half": candidate.fiscal_half,
                "scope": candidate.scope,
                "audit_status": candidate.audit_status,
            },
            created_at=now,
            updated_at=now,
            source_kind="candidate",
            source_id=candidate.candidate_id,
            source_session_id=source_session,
            request_fingerprint=fingerprint,
            dispatch_hold=hold,
            **self._consent_fields(allow_cloud_parse),
        )
        return self._admit_job(job, idempotency_key=idempotency_key)

    def _transition(self, job: ImportJob, status: str, **updates) -> ImportJob:
        return self.store.update_job(job.model_copy(update={"status": status, **updates}))

    def _write_converted(self, job: ImportJob, converted: ConvertedDocument) -> Path:
        namespace = _owner_namespace(job.owner_id)
        path = self.converted_dir / namespace / f"{converted.source_sha256}.md"
        _atomic_write(path, (converted.markdown.rstrip() + "\n").encode("utf-8"))
        return path.resolve()

    def _convert(self, job: ImportJob, artifact: PdfArtifact) -> ConvertedDocument:
        private = job.origin == "upload"
        if getattr(self.converter, "supports_attempt_ledger", False):
            # Consent comes from the job, never from how the process started,
            # so one worker can serve consenting and non-consenting imports.
            return self.converter.convert(
                artifact,
                private=private,
                consent=job.allow_cloud_parse,
                recorder=ParseAttemptRecorder(self.store, job),
            )
        return self.converter.convert(artifact, private=private)

    def _remote_parse_may_be_running(self, job_id: str, exc: BaseException) -> bool:
        summary = self.store.billing_summary(job_id)
        if summary.attempts:
            return not summary.remote_terminal
        return bool(getattr(exc, "billing_pending", False))

    def _billing_fields(
        self,
        job: ImportJob,
        *,
        converted: ConvertedDocument | None = None,
        error: BaseException | None = None,
    ) -> dict:
        """Billing state from the attempt ledger, falling back to what the
        converter reported when nothing went through the ledger (local text,
        cache hit, converters without ledger support)."""

        def pending(*, blocking: bool) -> dict:
            return {"billing_status": "pending", "billing_pending": True,
                    "billing_blocking": blocking, "confirmed_credits": "", "billed_credits": 0}

        summary = self.store.billing_summary(job.job_id)
        if summary.attempts:
            if summary.status == "confirmed":
                confirmed = summary.confirmed_credits
                return {
                    "billing_status": "confirmed",
                    "billing_pending": False,
                    "billing_blocking": False,
                    "confirmed_credits": str(confirmed),
                    "billed_credits": int(math.ceil(confirmed)),
                }
            # Only this job's own unsettled parse blocks the owner's next
            # import; waiting on another job's payer holds just the reservation.
            return pending(blocking=summary.payer_pending)
        if converted is not None:
            if converted.credits_confirmed:
                exact = converted.confirmed_credits
                if exact is None:
                    exact = str(converted.billed_credits)
                return {"billing_status": "confirmed", "billing_pending": False,
                        "billing_blocking": False,
                        "confirmed_credits": exact, "billed_credits": converted.billed_credits}
            return pending(blocking=True)
        if error is not None and getattr(error, "billing_pending", False):
            return pending(blocking=True)
        billed = int(getattr(error, "billed_credits", 0) or 0) if error is not None else 0
        if billed:
            return {"billing_status": "confirmed", "billing_pending": False,
                    "billing_blocking": False,
                    "confirmed_credits": str(billed), "billed_credits": billed}
        return {"billing_status": job.billing_status, "billing_pending": job.billing_pending,
                "billing_blocking": job.billing_blocking,
                "confirmed_credits": job.confirmed_credits, "billed_credits": job.billed_credits}

    def run_job(self, job_id: str) -> ImportJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise ImportJobNotFound("import job not found")
        claimed = self.store.claim_job(job_id)
        if claimed is None:
            # Duplicate dispatch is read-only and never waits for the runner.
            return self.store.get_job(job_id)
        with self.store.job_lock(job_id):
            return self._run_claimed(claimed)

    def _run_claimed(self, job: ImportJob) -> ImportJob:
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
            # The slot is released only once no remote parse can still be
            # running for this job; a retained slot is freed by reconcile.
            with self.store.parse_slot(
                job.job_id,
                limit=self.policy.global_parse_concurrency,
                hold_on_error=lambda exc: self._remote_parse_may_be_running(job.job_id, exc),
            ):
                converted = self._convert(job, artifact)
            # Processing continues regardless of billing: a job whose usage is
            # not yet reported still becomes ready and keeps its reservation.
            job = self._transition(
                job,
                "validating",
                billed_pages=converted.billed_pages,
                estimated_credits=converted.estimated_credits,
                **self._billing_fields(job, converted=converted),
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
                        "estimated_credits": converted.estimated_credits,
                        "credits_confirmed": converted.credits_confirmed,
                        "cache_hit": converted.cache_hit,
                        "targeted_retry_pages": converted.targeted_retry_pages,
                    },
                },
            )
            if not converted.markdown or converted.quality_issues:
                raise LlamaParseError(
                    "converted document failed quality validation",
                    code="conversion_quality_failed",
                )
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
                billed_pages=int(getattr(exc, "billed_pages", 0) or job.billed_pages),
                estimated_credits=int(
                    getattr(exc, "estimated_credits", 0) or job.estimated_credits
                ),
                **self._billing_fields(job, error=exc),
                error_code=getattr(exc, "code", type(exc).__name__),
                error_message=str(exc),
                retryable=bool(getattr(exc, "retryable", False)),
            )
        except Exception as exc:
            return self._transition(
                job,
                "failed",
                **self._billing_fields(job, error=exc),
                error_code=type(exc).__name__,
                error_message=str(exc),
                retryable=False,
            )

    # ------------------------------------------------------------ recovery

    def reconcile_job(self, job_id: str) -> ImportJob:
        """Settle billing for a job's known remote parses.

        Looks remote jobs up by the IDs already in the ledger — it never
        submits a parse — and records usage idempotently per remote ID, so
        running it again cannot change or double a charge. A submission whose
        remote ID was lost stays pending (manual follow-up).
        """

        with self.store.job_lock(job_id):
            job = self.store.get_job(job_id)
            if job is None:
                raise ImportJobNotFound("import job not found")
            for attempt in self.store.attempts_for_job(job_id):
                remote_id = attempt["remote_job_id"]
                if not remote_id or attempt["state"] == "rejected":
                    continue
                target = attempt
                if attempt["role"] == "consumer" and attempt["payer_attempt_id"]:
                    target = self.store.get_attempt(attempt["payer_attempt_id"]) or attempt
                settled = (
                    str(target["remote_status"] or "").upper() in {"COMPLETED", "FAILED", "CANCELLED"}
                    and (target["role"] == "consumer" or target["credits"] is not None)
                )
                if settled:
                    continue
                # Provider call outside any store transaction.
                remote = self.converter.fetch_remote(remote_id)
                self.store.complete_remote(
                    remote_id, remote_status=remote.status, credits=remote.credits,
                )
            summary = self.store.billing_summary(job_id)
            if (summary.attempts and summary.remote_terminal
                    and job.status in TERMINAL_JOB_STATUSES):
                self.store.release_parse_slot(job_id)
            if not summary.attempts:
                return job
            fields = self._billing_fields(job)
            if all(getattr(job, key) == value for key, value in fields.items()):
                return job
            return self.store.update_job(job.model_copy(update=fields))

    def reconcile_all_pending(self) -> list[ImportJob]:
        results = []
        for job in self.store.jobs_with_pending_billing():
            try:
                results.append(self.reconcile_job(job.job_id))
            except LlamaParseError:
                # Provider unavailable: leave this job pending, try the rest.
                results.append(self.store.get_job(job.job_id) or job)
        return results

    def recover_interrupted_jobs(self) -> list[ImportJob]:
        """Run once by the import worker on start, before claiming new work.

        A job left mid-flight is requeued; its next run finds any submitted
        remote job in the ledger and polls it instead of submitting again. A
        submission that was written but never got a remote ID may or may not
        exist at the provider, so that job fails with billing left pending.
        """

        recovered = []
        for job in self.store.jobs_in_statuses(IN_FLIGHT_JOB_STATUSES):
            with self.store.job_lock(job.job_id):
                current = self.store.get_job(job.job_id)
                if current is None or current.status not in IN_FLIGHT_JOB_STATUSES:
                    continue
                self.store.mark_unresolved_submissions_lost(current.job_id)
                summary = self.store.billing_summary(current.job_id)
                if summary.remote_id_lost:
                    recovered.append(self._transition(
                        current,
                        "failed",
                        billing_status="pending",
                        billing_pending=True,
                        billing_blocking=True,
                        confirmed_credits="",
                        billed_credits=0,
                        error_code="remote_id_lost",
                        error_message="A parse submission was interrupted before its remote job ID was stored",
                        retryable=False,
                    ))
                else:
                    recovered.append(self._transition(current, "queued"))
        return recovered
