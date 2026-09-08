"""LlamaParse v2 adapter with local content-addressed result caching."""

from __future__ import annotations

import json
import hashlib
import math
import fcntl
import os
import random
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any, Callable

from acquisition.local_text import (LOCAL_TEXT_CONTRACT_VERSION,
                                    extract_pdf_text_layer,
                                    local_text_quality_issues)
from acquisition.quality import (document_quality_issues,
                                 markdown_quality_issues,
                                 numeric_retention_issues)
from schemas.acquisition import ConvertedDocument, ConvertedPage, PdfArtifact

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
    ):
        super().__init__(message)
        self.retryable = retryable
        self.billed_pages = max(0, int(billed_pages))
        self.billed_credits = max(0, int(billed_credits))
        self.billing_pending = billing_pending


def _get(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


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
        return result.model_copy(update={"cache_hit": True, "billed_pages": 0, "billed_credits": 0})

    def _parse_once(
        self,
        artifact: PdfArtifact,
        *,
        tier: str,
        target_pages: list[int] | None = None,
        private: bool,
    ) -> Any:
        if not self.allow_cloud_parse:
            raise LlamaParseError("Cloud parsing requires explicit --allow-cloud-parse consent")
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
            # Compatibility for small injected clients; production uses the
            # explicit job lifecycle below so poll retries cannot resubmit PDFs.
            kwargs.pop("user_metadata", None)
            return parsing.parse(expand=["markdown", "metadata", "usage"], **kwargs)
        selection = hashlib.sha256(json.dumps(target_pages).encode()).hexdigest()[:16]
        reference = self._cache_path(artifact, private=private).with_suffix(f".{tier}.{selection}.job.json")
        try:
            job_id = json.loads(reference.read_text(encoding="utf-8"))["job_id"]
        except FileNotFoundError:
            created = self._with_retry(lambda: parsing.create(**kwargs))
            job_id = _get(created, "id")
            if not job_id:
                raise LlamaParseError("Provider did not return a job ID", billing_pending=True)
            try:
                _atomic_write(reference, json.dumps({"job_id": job_id}).encode())
            except OSError as exc:
                raise LlamaParseError("Unable to persist remote job reference", billing_pending=True) from exc
        try:
            self._with_retry(lambda: parsing.wait_for_completion(
                job_id, timeout=900, polling_interval=3, max_interval=10,
            ))
            return self._with_retry(lambda: parsing.get(job_id, expand=["markdown", "metadata", "usage"]))
        except Exception as exc:
            # Do not release a reservation while a submitted remote job may
            # still be running/billing. The API rollout needs reconciliation.
            raise LlamaParseError("Remote parse requires status/usage reconciliation",
                                 retryable=True, billing_pending=True) from exc

    def _with_retry(self, callback: Callable[[], Any]) -> Any:
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
                retryable = status_code == 429 or status_code >= 500
                if not retryable or attempt >= self.max_retries:
                    if retryable:
                        with self._circuit_lock:
                            self._failure_count += 1
                            if self._failure_count >= self.circuit_failure_threshold:
                                self._circuit_open_until = (
                                    self.clock() + self.circuit_cooldown_seconds
                                )
                    raise LlamaParseError(
                        f"LlamaParse failed: {type(exc).__name__}",
                        retryable=retryable,
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
    def _billed_credits(result: Any, *, fallback: int) -> int:
        # Parse v2 reports credits on job.usage; the old fake/Extract-shaped
        # metadata.usage.num_pages_billed must not override real provider usage.
        usage = _get(_get(result, "job", {}), "usage", {}) or {}
        value = _get(usage, "credits", None)
        return max(0, math.ceil(float(value))) if value is not None else fallback

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
        )

    def convert(self, artifact: PdfArtifact, *, private: bool) -> ConvertedDocument:
        # A content-addressed inter-process lock prevents concurrent cache
        # misses (including different owners of a public PDF) being billed twice.
        lock_path = self._cache_path(artifact, private=private).with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                return self._convert_locked(artifact, private=private)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _convert_locked(self, artifact: PdfArtifact, *, private: bool) -> ConvertedDocument:
        cache_path = self._cache_path(artifact, private=private)
        cached = self._read_cache(cache_path)
        if cached is not None:
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
        initial = self._with_retry(lambda: self._parse_once(
            artifact, tier=self.tier, private=private,
            target_pages=targets if local_good else None,
        ))
        initial_billed_pages = self._billed_pages(
            initial,
            fallback=len(targets),
        )
        initial_billed_credits = self._billed_credits(
            initial, fallback=initial_billed_pages * TIER_CREDITS_PER_PAGE[self.tier]
        )
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
                    )
                )
            except LlamaParseError as exc:
                raise LlamaParseError(
                    str(exc),
                    retryable=exc.retryable,
                    billed_pages=initial_billed_pages,
                    billed_credits=initial_billed_credits,
                    billing_pending=exc.billing_pending,
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
        if repaired is not None:
            retry_billed_pages = self._billed_pages(
                repaired,
                fallback=len(retry_pages),
            )
        billed_pages = initial_billed_pages + retry_billed_pages
        billed_credits = initial_billed_credits + (
            self._billed_credits(repaired, fallback=retry_billed_pages * TIER_CREDITS_PER_PAGE["agentic"])
            if repaired is not None else 0
        )
        result = ConvertedDocument(
            markdown=markdown,
            pages=pages,
            provider="llamaparse",
            provider_version=str(_get(metadata, "version", "") or self.version),
            tier=self.tier,
            source_sha256=artifact.source_sha256,
            billed_pages=billed_pages,
            billed_credits=billed_credits,
            targeted_retry_pages=retry_pages,
            quality_issues=issues,
        )
        if not pages or issues:
            raise LlamaParseError(
                "LlamaParse output failed deterministic quality validation",
                retryable=False,
                billed_pages=billed_pages,
                billed_credits=billed_credits,
            )
        _atomic_write(
            cache_path,
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2).encode(
                "utf-8"
            ),
        )
        return result
