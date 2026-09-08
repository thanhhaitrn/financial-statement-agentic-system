"""Cheap text-layer preflight before spending LlamaParse credits."""

from __future__ import annotations

import re

from acquisition.quality import markdown_quality_issues
from schemas.acquisition import PdfArtifact

LOCAL_TEXT_CONTRACT_VERSION = "pdf-text-preflight-v1"
FINANCIAL_TABLE_TERMS = (
    "bảng cân đối",
    "báo cáo tình hình tài chính",
    "báo cáo kết quả",
    "doanh thu",
    "tổng tài sản",
    "nguồn vốn",
    "lưu chuyển tiền tệ",
)


def extract_pdf_text_layer(artifact: PdfArtifact) -> list[str]:
    from pypdf import PdfReader

    pages = []
    for page in PdfReader(artifact.path, strict=True).pages:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except TypeError:
            text = page.extract_text() or ""
        pages.append(text.strip())
    return pages


def local_text_quality_issues(
    pages: list[str],
    *,
    expected_pages: int,
) -> list[str]:
    issues = []
    if len(pages) != expected_pages:
        issues.append("page_provenance_mismatch")
    for page_number, text in enumerate(pages, start=1):
        for issue in markdown_quality_issues(text):
            issues.append(f"page_{page_number}:{issue}")
        lines = [line for line in text.splitlines() if line.strip()]
        multi_column_lines = sum(
            1 for line in lines if re.search(r"\S\s{4,}\S\s{4,}\S", line)
        )
        if lines and multi_column_lines / len(lines) > 0.10:
            issues.append(f"page_{page_number}:ambiguous_reading_order")

        folded = text.casefold()
        numeric_tokens = re.findall(r"(?<!\w)\d[\d.,]*", text)
        looks_financial = any(term in folded for term in FINANCIAL_TABLE_TERMS)
        if looks_financial and len(numeric_tokens) >= 4:
            # Plain PDF text has no deterministic row/column boundary. Send
            # table-like pages to LlamaParse instead of guessing a structure.
            issues.append(f"page_{page_number}:financial_table_structure_missing")
    return issues
