"""Bounded PDF validation and allowlisted report download."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import Callable
from urllib.request import Request

from tools.http_safety import allowed_https_url, allowlisted_opener

from schemas.acquisition import PdfArtifact, ReportCandidate

DEFAULT_REPORT_HOST_SUFFIXES = (".cafef.vn", ".mediacdn.vn")


class PdfValidationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def allowed_report_url(
    value: str,
    *,
    host_suffixes: tuple[str, ...] = DEFAULT_REPORT_HOST_SUFFIXES,
) -> bool:
    return allowed_https_url(value, host_suffixes)


def pypdf_page_count(path: Path) -> int:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "PDF validation requires the 'web' extra: pip install -e '.[web]'"
        ) from exc
    return len(PdfReader(str(path), strict=True).pages)


class PdfArtifactValidator:
    def __init__(
        self,
        *,
        max_file_bytes: int,
        max_pdf_pages: int,
        page_counter: Callable[[Path], int] = pypdf_page_count,
    ):
        self.max_file_bytes = max_file_bytes
        self.max_pdf_pages = max_pdf_pages
        self.page_counter = page_counter

    def validate(
        self,
        path: str | Path,
        *,
        origin: str,
        source_url: str = "",
        public_source: bool = False,
        cache_namespace: str = "",
    ) -> PdfArtifact:
        source_path = Path(path).resolve(strict=True)
        size = source_path.stat().st_size
        if size <= 0:
            raise PdfValidationError("empty_file", "PDF file is empty")
        if size > self.max_file_bytes:
            raise PdfValidationError("file_too_large", "PDF exceeds the configured byte limit")
        with source_path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise PdfValidationError("invalid_pdf_magic", "File does not start with %PDF-")
        try:
            page_count = int(self.page_counter(source_path))
        except PdfValidationError:
            raise
        except Exception as exc:
            raise PdfValidationError("invalid_pdf", "PDF page structure is invalid") from exc
        if page_count <= 0:
            raise PdfValidationError("empty_pdf", "PDF has no pages")
        if page_count > self.max_pdf_pages:
            raise PdfValidationError("too_many_pages", "PDF exceeds the configured page limit")
        digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        return PdfArtifact(
            path=str(source_path),
            source_sha256=digest,
            size_bytes=size,
            page_count=page_count,
            origin=origin,
            source_url=source_url,
            public_source=public_source,
            cache_namespace=cache_namespace,
        )


class SecurePdfDownloader:
    def __init__(
        self,
        *,
        validator: PdfArtifactValidator,
        max_file_bytes: int,
        timeout_seconds: int = 30,
        host_suffixes: tuple[str, ...] = DEFAULT_REPORT_HOST_SUFFIXES,
        opener=None,
    ):
        self.validator = validator
        self.max_file_bytes = max_file_bytes
        self.timeout_seconds = timeout_seconds
        self.host_suffixes = host_suffixes
        self.opener = opener or allowlisted_opener(
            lambda url: allowed_report_url(url, host_suffixes=self.host_suffixes),
            lambda message: PdfValidationError("redirect_not_allowed", message),
        )

    def download(
        self,
        candidate: ReportCandidate,
        *,
        target_dir: str | Path,
        cache_namespace: str = "",
    ) -> PdfArtifact:
        if not allowed_report_url(candidate.source_url, host_suffixes=self.host_suffixes):
            raise PdfValidationError("source_not_allowed", "Report source host is not allowed")
        target = Path(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".report-",
            suffix=".pdf.tmp",
            dir=str(target),
        )
        temporary_path = Path(temporary_name)
        try:
            request = Request(
                candidate.source_url,
                headers={"User-Agent": "AgentFinX/0.2 (+report-download)"},
            )
            with self.opener(request, timeout=self.timeout_seconds) as response:
                final_url = response.geturl()
                if not allowed_report_url(final_url, host_suffixes=self.host_suffixes):
                    raise PdfValidationError(
                        "redirect_not_allowed",
                        "Report download redirected outside allowed hosts",
                    )
                content_type = str(response.headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
                if content_type not in {"application/pdf", "application/octet-stream"}:
                    raise PdfValidationError("invalid_content_type", "Response is not a PDF")
                announced = int(response.headers.get("Content-Length", 0) or 0)
                if announced > self.max_file_bytes:
                    raise PdfValidationError("file_too_large", "PDF exceeds the byte limit")
                size = 0
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    while True:
                        chunk = response.read(min(64 * 1024, self.max_file_bytes + 1 - size))
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > self.max_file_bytes:
                            raise PdfValidationError("file_too_large", "PDF exceeds the byte limit")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            artifact = self.validator.validate(
                temporary_path,
                origin="cafef_discovery",
                source_url=final_url,
                public_source=True,
                cache_namespace=cache_namespace,
            )
            final_path = target / f"{artifact.source_sha256}.pdf"
            os.replace(temporary_path, final_path)
            return artifact.model_copy(update={"path": str(final_path.resolve())})
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)
