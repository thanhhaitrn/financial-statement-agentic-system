"""LlamaParse v2 adapter with local content-addressed result caching.

Billing contract: the provider-reported ``job.usage.credits`` is the only source
of a confirmed charge. Pages × tier rate is an *estimate* for reservations and
budgets, never a billed amount; missing usage means "unknown", not zero.

Submission contract: with a ``recorder`` (the import job's attempt ledger), an
attempt row is written before ``parsing.create`` and the remote job ID is stored
as soon as the provider returns it. ``create`` is retried only on HTTP 429 — a
timeout or 5xx may already have created a billable job, so that submission is
recorded as "remote ID lost" and left for reconciliation instead of being sent
again. Polling (``wait_for_completion`` / ``get``) is safe to retry.

Consent is a per-call argument; the constructor flag is only the default for
callers (the trusted CLI, tests) that do not pass one.
"""

from __future__ import annotations

import json
import hashlib
import math
import fcntl
import os
import random
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any, Callable, Optional

from agentfinx.acquisition.local_text import (LOCAL_TEXT_CONTRACT_VERSION,
                                    extract_pdf_text_layer,
                                    local_text_quality_issues)
from agentfinx.acquisition.quality import (document_quality_issues,
                                 markdown_quality_issues,
                                 numeric_retention_issues)
from agentfinx.core.schemas.acquisition import (TERMINAL_REMOTE_STATUSES, ConvertedDocument,
                                 ConvertedPage, PdfArtifact)

LLAMAPARSE_CONTRACT_VERSION = "llamaparse-markdown-v2"
DEFAULT_PARSE_VERSION = "2026-08-19"
TIER_CREDITS_PER_PAGE = {"cost_effective": 3, "agentic": 10}


class LlamaParseError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        billed_pages: int = 0,
        billed_credits: int = 0,
        billing_pending: bool = False,
        estimated_credits: int = 0,
        submission_uncertain: bool = False,
        code: str = "LlamaParseError",
    ):
        super().__init__(message)
        # Stable identifier for callers that must not show raw provider text.
        self.code = code
        self.retryable = retryable
        self.billed_pages = max(0, int(billed_pages))
        # Provider-confirmed credits only; never an estimate.
        self.billed_credits = max(0, int(billed_credits))
        self.billing_pending = billing_pending
        self.estimated_credits = max(0, int(estimated_credits))
        # True when a create request may have been accepted by the provider.
        self.submission_uncertain = submission_uncertain


@dataclass(frozen=True)
class RemoteParseStatus:
    remote_job_id: str
    status: str
    credits: Optional[Decimal]
    terminal: bool


def _get(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _whole_credits(value: Optional[Decimal]) -> int:
    """Budget arithmetic rounds a confirmed amount up; unknown counts as 0 here."""

    return 0 if value is None else int(math.ceil(value))


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


class LlamaParseDocumentConverter:
    # The acquisition service passes per-job consent and the attempt ledger
    # only to converters that declare they honour both.
    supports_attempt_ledger = True

    def __init__(
        self,
        *,
        client: Any = None,
        cache_dir: str | Path,
        tier: str = "cost_effective",
        version: str = DEFAULT_PARSE_VERSION,
        allow_cloud_parse: bool = False,
        max_retries: int = 3,
        circuit_failure_threshold: int = 3,
        circuit_cooldown_seconds: int = 60,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        local_extractor: Callable[[PdfArtifact], list[str]] | None = extract_pdf_text_layer,
    ):
        if tier not in TIER_CREDITS_PER_PAGE:
            raise ValueError("LlamaParse tier must be cost_effective or agentic")
        if version in {"", "latest"}:
            raise ValueError("Pin LLAMAPARSE_VERSION to a dated provider version, not latest")
        self._client = client
        self.cache_dir = Path(cache_dir)
        self.tier = tier
        self.version = version
        self.allow_cloud_parse = allow_cloud_parse
        self.max_retries = max_retries
        self.circuit_failure_threshold = circuit_failure_threshold
        self.circuit_cooldown_seconds = circuit_cooldown_seconds
        self.sleeper = sleeper
        self.clock = clock
        self.local_extractor = local_extractor
        self._failure_count = 0
        self._circuit_open_until = 0.0
        self._circuit_lock = Lock()

    @property
    def converter_identity(self) -> str:
        return (
            f"{LLAMAPARSE_CONTRACT_VERSION}:{LOCAL_TEXT_CONTRACT_VERSION}:"
            f"{self.tier}:{self.version}"
        )

    @property
    def client(self):
        if self._client is None:
            try:
                from llama_cloud import LlamaCloud
            except ImportError as exc:
                raise RuntimeError(
                    "LlamaParse requires the 'web' extra: pip install -e '.[web]'"
                ) from exc
            api_key = (os.getenv("LLAMA_CLOUD_API_KEY") or os.getenv("LLAMAPARSE_API_KEY")
                       or os.getenv("LLAMA_PARSE_API_KEY") or os.getenv("LLAMA_API_KEY"))
            # This adapter owns retries; avoid multiplying them by SDK retries.
            self._client = LlamaCloud(api_key=api_key, max_retries=0)
        return self._client

    def _cache_path(self, artifact: PdfArtifact, *, private: bool) -> Path:
        if private and not artifact.cache_namespace:
            raise ValueError("Private conversion requires an owner-scoped cache namespace")
        namespace = hashlib.sha256(artifact.cache_namespace.encode()).hexdigest() if private else "public"
        raw_contract = f"{LLAMAPARSE_CONTRACT_VERSION}-{LOCAL_TEXT_CONTRACT_VERSION}"
        contract = "".join(
            char for char in raw_contract if char.isalnum() or char in "-_"
        )
        identity = hashlib.sha256(
            f"{artifact.source_sha256}-{self.tier}-{self.version}-{contract}".encode()
        ).hexdigest()
        return self.cache_dir / namespace / f"{identity}.json"

    def _read_cache(self, path: Path) -> ConvertedDocument | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            result = ConvertedDocument.model_validate(payload)
        except (FileNotFoundError, ValueError, OSError):
            return None
        # A cache hit costs nothing *for this use*. It says nothing about whether
        # the job that created the cache has had its own billing confirmed.
        return result.model_copy(
            update={
                "cache_hit": True,
                "billed_pages": 0,
                "billed_credits": 0,
                "estimated_credits": 0,
                "confirmed_credits": "0",
                "credits_confirmed": True,
            }
        )

    @staticmethod
    def _payer_path(cache_path: Path) -> Path:
        return cache_path.with_suffix(".payer.json")

    def _write_payer_reference(self, cache_path: Path, remote_log: list) -> None:
        # Many reusers, one payer: a public cache entry records the remote
        # parse jobs that produced it. The reference sits next to the cache
        # file and leaves the cache identity untouched.
        _atomic_write(
            self._payer_path(cache_path),
            json.dumps({"remote_jobs": remote_log}, sort_keys=True).encode("utf-8"),
        )

    def _link_cached_payer(self, cache_path: Path, recorder: Any) -> None:
        """Link a public cache hit to the job that paid for it.

        The reusing job adds no charge of its own, but it settles only once
        the payer's usage is confirmed, so its reservation is held until then.
        """

        try:
            reference = json.loads(self._payer_path(cache_path).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return  # produced locally or before payers were tracked: nothing to wait for
        for entry in reference.get("remote_jobs") or []:
            remote_id = str((entry or {}).get("remote_job_id") or "")
            if remote_id:
                recorder.adopt(
                    tier=str(entry.get("tier") or self.tier),
                    selection=str(entry.get("selection") or ""),
                    remote_job_id=remote_id,
                    remote_status="COMPLETED",
                )

    def _parse_once(
        self,
        artifact: PdfArtifact,
        *,
        tier: str,
        target_pages: list[int] | None = None,
        private: bool,
        consent: bool,
        recorder: Any = None,
        remote_log: Optional[list] = None,
    ) -> Any:
        if not consent:
            raise LlamaParseError(
                "Cloud parsing requires explicit per-import consent (--allow-cloud-parse)",
                code="cloud_consent_required",
            )
        kwargs = {
            "upload_file": Path(artifact.path),
            "tier": tier,
            "version": self.version,
            "disable_cache": bool(private),
            "output_options": {
                "markdown": {"tables": {"output_tables_as_markdown": True}}
            },
            "processing_options": {"ocr_parameters": {"languages": ["vi", "en"]}},
            "user_metadata": {
                "source_sha256": artifact.source_sha256,
                "contract": LLAMAPARSE_CONTRACT_VERSION,
            },
        }
        if target_pages:
            kwargs["page_ranges"] = {
                "target_pages": ",".join(str(page) for page in target_pages)
            }
        parsing = self.client.parsing
        if not callable(getattr(parsing, "create", None)):
            if recorder is not None:
                # Without create/get there is no remote job ID to store, so a
                # billed parse could never be reconciled. Refuse before sending.
                raise LlamaParseError(
                    "Provider client lacks the create/get job lifecycle; refusing to submit",
                    code="provider_lifecycle_missing",
                )
            # Compatibility for small injected test clients only.
            kwargs.pop("user_metadata", None)
            return parsing.parse(expand=["markdown", "metadata", "usage"], **kwargs)

        selection = hashlib.sha256(json.dumps(target_pages).encode()).hexdigest()[:16]
        reference = self._cache_path(artifact, private=private).with_suffix(f".{tier}.{selection}.job.json")

        job_id = recorder.find_remote(tier=tier, selection=selection) if recorder is not None else None
        if job_id is None and recorder is not None and recorder.unresolved_submission(
            tier=tier, selection=selection
        ):
            raise LlamaParseError(
                "An earlier submission of this parse is unresolved; reconcile before retrying",
                billing_pending=True,
                submission_uncertain=True,
            )
        if job_id is None:
            try:
                job_id = json.loads(reference.read_text(encoding="utf-8"))["job_id"]
            except FileNotFoundError:
                job_id = None
            else:
                if recorder is not None:
                    recorder.adopt(tier=tier, selection=selection, remote_job_id=job_id)

        if job_id is None:
            attempt_id = recorder.begin(tier=tier, selection=selection) if recorder is not None else None
            if attempt_id:
                kwargs["user_metadata"]["attempt_id"] = attempt_id
            try:
                created = self._with_retry(lambda: parsing.create(**kwargs), idempotent=False)
            except LlamaParseError as exc:
                if recorder is not None and attempt_id:
                    if exc.submission_uncertain:
                        recorder.remote_id_lost(attempt_id)
                    else:
                        recorder.rejected(attempt_id)
                if exc.submission_uncertain:
                    raise LlamaParseError(
                        "Remote job creation outcome is unknown; reconcile before retrying",
                        billing_pending=True,
                        submission_uncertain=True,
                    ) from exc
                raise
            job_id = _get(created, "id")
            if not job_id:
                if recorder is not None and attempt_id:
                    recorder.remote_id_lost(attempt_id)
                raise LlamaParseError(
                    "Provider did not return a job ID",
                    billing_pending=True,
                    submission_uncertain=True,
                )
            if recorder is not None and attempt_id:
                # Durable before anything else can fail.
                recorder.submitted(attempt_id, job_id)
            try:
                _atomic_write(reference, json.dumps({"job_id": job_id}).encode())
            except OSError as exc:
                if recorder is None:
                    raise LlamaParseError(
                        "Unable to persist remote job reference", billing_pending=True
                    ) from exc
                # The ledger already holds the ID; the sidecar is only a
                # cross-job de-duplication hint.

        if remote_log is not None:
            remote_log.append({"remote_job_id": job_id, "tier": tier, "selection": selection})
        try:
            self._with_retry(lambda: parsing.wait_for_completion(
                job_id, timeout=900, polling_interval=3, max_interval=10,
            ))
            result = self._with_retry(lambda: parsing.get(job_id, expand=["markdown", "metadata", "usage"]))
        except Exception as exc:
            # Do not release a reservation while a submitted remote job may
            # still be running/billing; reconciliation resolves it by job ID.
            raise LlamaParseError("Remote parse requires status/usage reconciliation",
                                 retryable=True, billing_pending=True) from exc
        if recorder is not None:
            job_info = _get(result, "job", {}) or {}
            recorder.completed(
                job_id,
                remote_status=str(_get(job_info, "status", "") or "COMPLETED"),
                credits=self._provider_credits(result),
            )
        return result

    def _with_retry(self, callback: Callable[[], Any], *, idempotent: bool = True) -> Any:
        with self._circuit_lock:
            if self.clock() < self._circuit_open_until:
                raise LlamaParseError(
                    "LlamaParse circuit breaker is open",
                    retryable=True,
                )
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                result = callback()
                with self._circuit_lock:
                    self._failure_count = 0
                    self._circuit_open_until = 0.0
                return result
            except Exception as exc:
                if isinstance(exc, LlamaParseError):
                    raise
                last_error = exc
                response = getattr(exc, "response", None)
                status_code = int(
                    getattr(exc, "status_code", 0)
                    or getattr(response, "status_code", 0)
                    or 0
                )
                throttled = status_code == 429
                retryable = throttled or (idempotent and status_code >= 500)
                # A create that timed out or hit a 5xx may already have been
                # accepted; retrying it could start a second billable job.
                uncertain = (not idempotent) and not throttled and (
                    status_code == 0 or status_code >= 500
                )
                if not retryable or attempt >= self.max_retries:
                    if retryable or uncertain:
                        with self._circuit_lock:
                            self._failure_count += 1
                            if self._failure_count >= self.circuit_failure_threshold:
                                self._circuit_open_until = (
                                    self.clock() + self.circuit_cooldown_seconds
                                )
                    raise LlamaParseError(
                        f"LlamaParse failed: {type(exc).__name__}",
                        retryable=retryable,
                        submission_uncertain=uncertain,
                    ) from exc
                retry_after = getattr(exc, "retry_after", None)
                if retry_after in (None, "") and response is not None:
                    headers = getattr(response, "headers", {}) or {}
                    retry_after = headers.get("Retry-After")
                delay = float(min(2**attempt, 8))
                if retry_after not in (None, ""):
                    try:
                        delay = max(0.0, float(retry_after))
                    except (TypeError, ValueError):
                        try:
                            retry_time = parsedate_to_datetime(str(retry_after))
                            if retry_time.tzinfo is None:
                                retry_time = retry_time.replace(tzinfo=timezone.utc)
                            delay = max(0.0, (retry_time - datetime.now(timezone.utc)).total_seconds())
                        except (TypeError, ValueError, OverflowError):
                            pass
                self.sleeper(delay + random.uniform(0, 0.25))
        raise LlamaParseError("LlamaParse failed", retryable=True) from last_error

    @staticmethod
    def _pages(result: Any, *, tier: str) -> list[ConvertedPage]:
        markdown_payload = _get(result, "markdown", {})
        raw_pages = _get(markdown_payload, "pages", []) or []
        pages = []
        for fallback_number, raw_page in enumerate(raw_pages, start=1):
            markdown = str(_get(raw_page, "markdown", "") or "").strip()
            page_number = int(
                _get(raw_page, "page_number", None)
                or _get(raw_page, "page", None)
                or fallback_number
            )
            pages.append(
                ConvertedPage(
                    page_number=page_number,
                    markdown=markdown,
                    tier=tier,
                    quality_issues=markdown_quality_issues(markdown),
                )
            )
        return pages

    @staticmethod
    def _billed_pages(result: Any, *, fallback: int) -> int:
        metadata = _get(result, "metadata", {}) or {}
        usage = _get(metadata, "usage", {}) or {}
        for key in ("num_pages_billed", "pages"):
            value = _get(usage, key, None)
            if value is not None:
                return max(0, int(value))
        return max(0, int(fallback))

    @staticmethod
    def _provider_credits(result: Any) -> Optional[Decimal]:
        """Provider-reported credits of one parse job, or ``None`` if unreported.

        Parse v2 reports credits on ``job.usage``. There is no fallback: an
        estimate must never be recorded as a charge, and unknown is not zero.
        """

        usage = _get(_get(result, "job", {}), "usage", {}) or {}
        value = _get(usage, "credits", None)
        if value is None:
            return None
        try:
            credits = Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
        return credits if credits.is_finite() and credits >= 0 else None

    def fetch_remote(self, remote_job_id: str) -> RemoteParseStatus:
        """Current provider status and usage of a known remote job (reconcile)."""

        parsing = self.client.parsing
        if not callable(getattr(parsing, "get", None)):
            raise LlamaParseError("Provider client cannot look up remote jobs")
        result = self._with_retry(lambda: parsing.get(remote_job_id, expand=["metadata", "usage"]))
        job = _get(result, "job", {}) or {}
        status = str(_get(job, "status", "") or "").upper()
        return RemoteParseStatus(
            remote_job_id=remote_job_id,
            status=status,
            credits=self._provider_credits(result),
            terminal=status in TERMINAL_REMOTE_STATUSES,
        )

    def _local_pages(self, artifact: PdfArtifact) -> list[str]:
        if self.local_extractor is None:
            return []
        try:
            return list(self.local_extractor(artifact))
        except Exception:
            # The local pass is only an optimization. Invalid PDFs are already
            # rejected by PdfArtifactValidator; extraction failures go to the
            # configured LlamaParse provider.
            return []

    @staticmethod
    def _local_result(artifact: PdfArtifact, pages: list[str]) -> ConvertedDocument:
        converted_pages = [
            ConvertedPage(page_number=number, markdown=text, tier="local_text")
            for number, text in enumerate(pages, start=1)
        ]
        markdown = "\n\n".join(
            f"--- Page {page.page_number - 1}\n\n{page.markdown}"
            for page in converted_pages
        ).strip()
        return ConvertedDocument(
            markdown=markdown,
            pages=converted_pages,
            provider="local_text_layer",
            provider_version=LOCAL_TEXT_CONTRACT_VERSION,
            tier="local_text",
            source_sha256=artifact.source_sha256,
            billed_pages=0,
            billed_credits=0,
            confirmed_credits="0",
            credits_confirmed=True,
        )

    def convert(
        self,
        artifact: PdfArtifact,
        *,
        private: bool,
        consent: Optional[bool] = None,
        recorder: Any = None,
    ) -> ConvertedDocument:
        effective_consent = self.allow_cloud_parse if consent is None else bool(consent)
        # A content-addressed inter-process lock prevents concurrent cache
        # misses (including different owners of a public PDF) being billed twice.
        lock_path = self._cache_path(artifact, private=private).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._convert_locked(
                    artifact, private=private, consent=effective_consent, recorder=recorder
                )
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _convert_locked(
        self,
        artifact: PdfArtifact,
        *,
        private: bool,
        consent: bool,
        recorder: Any = None,
    ) -> ConvertedDocument:
        cache_path = self._cache_path(artifact, private=private)
        cached = self._read_cache(cache_path)
        if cached is not None:
            if recorder is not None and not private:
                self._link_cached_payer(cache_path, recorder)
            return cached

        local_pages = self._local_pages(artifact)
        if local_pages and not local_text_quality_issues(
            local_pages,
            expected_pages=artifact.page_count,
        ):
            result = self._local_result(artifact, local_pages)
            _atomic_write(
                cache_path,
                json.dumps(
                    result.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ).encode("utf-8"),
            )
            return result

        local_good = {
            number: ConvertedPage(page_number=number, markdown=text, tier="local_text")
            for number, text in enumerate(local_pages, 1)
            if len(local_pages) == artifact.page_count
            and not local_text_quality_issues([text], expected_pages=1)
        }
        targets = [number for number in range(1, artifact.page_count + 1) if number not in local_good]
        remote_log: list = []
        initial = self._with_retry(lambda: self._parse_once(
            artifact, tier=self.tier, private=private, consent=consent, recorder=recorder,
            target_pages=targets if local_good else None, remote_log=remote_log,
        ))
        initial_billed_pages = self._billed_pages(
            initial,
            fallback=len(targets),
        )
        initial_credits = self._provider_credits(initial)
        initial_estimate = initial_billed_pages * TIER_CREDITS_PER_PAGE[self.tier]
        pages = self._pages(initial, tier=self.tier)
        returned = {page.page_number for page in pages}
        pages.extend(ConvertedPage(page_number=number, markdown="", quality_issues=["missing_page"])
                     for number in targets if number not in returned)
        # Unexpected provider pages are retained so the provenance gate fails
        # instead of silently accepting a shifted/duplicated page mapping.
        pages.extend(local_good.values())
        if local_pages:
            pages = [
                page.model_copy(
                    update={
                        "quality_issues": list(page.quality_issues)
                        + numeric_retention_issues(
                            local_pages[page.page_number - 1]
                            if page.page_number <= len(local_pages)
                            else "",
                            page.markdown,
                        )
                    }
                )
                for page in pages
            ]
        repaired = None
        retry_pages = [
            page.page_number for page in pages if page.quality_issues
        ]
        if retry_pages and self.tier == "cost_effective":
            try:
                repaired = self._with_retry(
                    lambda: self._parse_once(
                        artifact,
                        tier="agentic",
                        target_pages=retry_pages,
                        private=private,
                        consent=consent,
                        recorder=recorder,
                        remote_log=remote_log,
                    )
                )
            except LlamaParseError as exc:
                raise LlamaParseError(
                    str(exc),
                    retryable=exc.retryable,
                    billed_pages=initial_billed_pages,
                    billed_credits=_whole_credits(initial_credits),
                    estimated_credits=initial_estimate,
                    billing_pending=exc.billing_pending or initial_credits is None,
                    submission_uncertain=exc.submission_uncertain,
                ) from exc
            repaired_by_number = {
                page.page_number: page for page in self._pages(repaired, tier="agentic")
            }
            for page in pages:
                replacement = repaired_by_number.get(page.page_number)
                if replacement is None:
                    continue
                retention_issues = numeric_retention_issues(
                    page.markdown,
                    replacement.markdown,
                )
                if page.page_number <= len(local_pages):
                    retention_issues.extend(
                        issue
                        for issue in numeric_retention_issues(
                            local_pages[page.page_number - 1],
                            replacement.markdown,
                        )
                        if issue not in retention_issues
                    )
                if retention_issues:
                    repaired_by_number[page.page_number] = replacement.model_copy(
                        update={
                            "quality_issues": list(replacement.quality_issues)
                            + retention_issues
                        }
                    )
            pages = [
                repaired_by_number.get(page.page_number, page)
                if page.page_number in retry_pages
                else page
                for page in pages
            ]

        pages.sort(key=lambda page: page.page_number)
        markdown = "\n\n".join(
            f"--- Page {page.page_number - 1}\n\n{page.markdown}" for page in pages
        ).strip()
        issues = document_quality_issues([page.markdown for page in pages])
        issues.extend(
            f"page_{page.page_number}:{issue}"
            for page in pages
            for issue in page.quality_issues
            if f"page_{page.page_number}:{issue}" not in issues
        )
        actual_pages = [page.page_number for page in pages]
        if (
            len(actual_pages) != artifact.page_count
            or len(set(actual_pages)) != len(actual_pages)
            or set(actual_pages) != set(range(1, artifact.page_count + 1))
        ):
            issues.append("page_provenance_mismatch")
        metadata = _get(initial, "metadata", {}) or {}
        retry_billed_pages = 0
        retry_credits: Optional[Decimal] = Decimal(0)
        retry_estimate = 0
        if repaired is not None:
            retry_billed_pages = self._billed_pages(
                repaired,
                fallback=len(retry_pages),
            )
            retry_credits = self._provider_credits(repaired)
            retry_estimate = retry_billed_pages * TIER_CREDITS_PER_PAGE["agentic"]
        credits_known = initial_credits is not None and retry_credits is not None
        confirmed = (initial_credits + retry_credits) if credits_known else None
        billed_pages = initial_billed_pages + retry_billed_pages
        estimated = initial_estimate + retry_estimate
        result = ConvertedDocument(
            markdown=markdown,
            pages=pages,
            provider="llamaparse",
            provider_version=str(_get(metadata, "version", "") or self.version),
            tier=self.tier,
            source_sha256=artifact.source_sha256,
            billed_pages=billed_pages,
            billed_credits=_whole_credits(confirmed),
            estimated_credits=estimated,
            confirmed_credits=None if confirmed is None else str(confirmed),
            credits_confirmed=credits_known,
            targeted_retry_pages=retry_pages,
            quality_issues=issues,
        )
        if not pages or issues:
            raise LlamaParseError(
                "LlamaParse output failed deterministic quality validation",
                code="conversion_quality_failed",
                retryable=False,
                billed_pages=billed_pages,
                billed_credits=_whole_credits(confirmed),
                estimated_credits=estimated,
                billing_pending=not credits_known,
            )
        if not private and remote_log:
            # Written before the cache entry, so a public cache hit can always
            # find the payer it has to wait for.
            self._write_payer_reference(cache_path, remote_log)
        _atomic_write(
            cache_path,
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
        )
        return result
