"""Deterministic quality diagnostics for converter Markdown."""

from __future__ import annotations

import re
from collections import Counter
from schemas.numbers import parse_financial_decimal

NUMERIC_TOKEN_RE = re.compile(
    r"(?<![\w])[-+]?\(?\d[\d.,]*(?:\s*(?:%|VND|VNĐ|USD))?\)?",
    flags=re.IGNORECASE,
)


def numeric_retention_issues(before: str, after: str) -> list[str]:
    """Flag a rewrite that unexpectedly discards many source numeric tokens."""

    source = Counter(token.upper().replace(" ", "") for token in NUMERIC_TOKEN_RE.findall(before))
    target = Counter(token.upper().replace(" ", "") for token in NUMERIC_TOKEN_RE.findall(after))
    def negatives(tokens):
        result = Counter()
        for token, count in tokens.items():
            value = parse_financial_decimal(token)
            if value is not None and value < 0:
                result[value] += count
        return result
    issues = []
    if negatives(source) - negatives(target):
        issues.append("negative_numeric_token_loss")
    if sum(source.values()) < 4:
        return issues
    retained = sum((source & target).values())
    missing = sum(source.values()) - retained
    if missing >= 2 and missing / sum(source.values()) > 0.20:
        issues.append("numeric_token_loss")
    return issues


def markdown_quality_issues(markdown: str) -> list[str]:
    text = str(markdown or "").strip()
    issues = []
    if len(text) < 20:
        issues.append("empty_or_too_short")
    if text and text.count("�") / max(len(text), 1) > 0.005:
        issues.append("replacement_character_ratio")
    if re.search(r"<table\b", text, re.IGNORECASE):
        issues.append("html_table_not_supported")
    if re.search(r"(?mi)^-+\s*Page\s+\d+\s*$|<!--\s*page:", text):
        issues.append("embedded_page_marker")

    table_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if table_lines:
        cell_counts = [max(line.count("|") - 1, 0) for line in table_lines]
        if max(cell_counts, default=0) < 2:
            issues.append("malformed_table")
        elif len(set(cell_counts[: min(len(cell_counts), 20)])) > 2:
            issues.append("inconsistent_table_columns")
        separator_present = any(
            re.fullmatch(r"\|?[\s:|-]+\|?", line) is not None for line in table_lines
        )
        if not separator_present:
            issues.append("table_without_header_separator")
    # Validate each table independently; a good separator in one table must
    # not conceal a malformed second table or a single missing numeric cell.
    groups, current = [], []
    for line in [*text.splitlines(), ""]:
        if line.strip().startswith("|"):
            current.append([cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))])
        elif current:
            groups.append(current)
            current = []
    for rows in groups:
        if len(rows) < 3 or len(rows[0]) < 2 or not any(rows[0]):
            issues.append("table_missing_header_or_rows")
            continue
        if not all(re.fullmatch(r":?-+:?", cell) for cell in rows[1]):
            issues.append("table_without_header_separator")
        if any(len(row) != len(rows[0]) for row in rows):
            issues.append("inconsistent_table_columns")
    return list(dict.fromkeys(issues))


def document_quality_issues(page_markdown: list[str]) -> list[str]:
    issues = []
    if not page_markdown:
        return ["no_pages"]
    for page_number, markdown in enumerate(page_markdown, start=1):
        for issue in markdown_quality_issues(markdown):
            issues.append(f"page_{page_number}:{issue}")
    return issues
