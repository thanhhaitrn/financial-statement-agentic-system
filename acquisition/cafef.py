"""CafeF report discovery adapter isolated behind a typed provider contract."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request

from schemas.acquisition import ReportQuery
from tools.http_safety import allowed_https_url, allowlisted_opener

CAFEF_API = "https://cafef.vn/du-lieu/Ajax/PageNew/FileBCTC.ashx"
EXCHANGE_CODES = {"HOSE": 1, "HSX": 1, "HNX": 2, "UPCOM": 3}
ALLOWED_REPORT_HOST_SUFFIXES = (".cafef.vn", ".mediacdn.vn")


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in text if not unicodedata.combining(char)).lower()


def _allowed_report_url(value: str) -> bool:
    return allowed_https_url(value, ALLOWED_REPORT_HOST_SUFFIXES)


def classify_report_title(title: str) -> dict:
    folded = _fold(title)
    year_match = re.search(r"\b(20\d{2})\b", folded)
    quarter_match = re.search(r"(?:quy|q)\s*([1-4])\b", folded)
    if "hop nhat" in folded or "consolidated" in folded:
        scope = "consolidated"
    elif "cong ty me" in folded or "rieng" in folded or "separate" in folded:
        scope = "separate"
    else:
        scope = "unknown"
    if "chua kiem toan" in folded or "unaudited" in folded:
        audit_status = "unaudited"
    elif "soat xet" in folded or "reviewed" in folded:
        audit_status = "reviewed"
    elif "kiem toan" in folded or "audited" in folded:
        audit_status = "audited"
    else:
        audit_status = "unknown"
    return {
        "fiscal_year": int(year_match.group(1)) if year_match else None,
        "fiscal_quarter": int(quarter_match.group(1)) if quarter_match else None,
        "scope": scope,
        "audit_status": audit_status,
        "report_type": "financial_statement",
    }


class CafeFReportDiscoveryProvider:
    def __init__(
        self,
        *,
        timeout_seconds: int = 15,
        fetch_json: Callable[[str], dict] | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self._fetch_json = fetch_json or self._default_fetch_json

    @property
    def provider_identity(self) -> str:
        return "cafef-report-discovery-v2"

    def _default_fetch_json(self, url: str) -> dict:
        request = Request(
            url,
            headers={
                "User-Agent": "AgentFinX/0.2 (+report-discovery)",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://cafef.vn/",
            },
        )
        with allowlisted_opener(_allowed_report_url)(request, timeout=self.timeout_seconds) as response:
            if not _allowed_report_url(response.geturl()):
                raise ValueError("CafeF discovery redirected outside allowed hosts")
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("CafeF discovery response is too large")
            charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(raw.decode(charset, errors="strict"))

    def discover(self, query: ReportQuery) -> list[dict]:
        if query.report_type != "financial_statement":
            return []
        exchange_code = EXCHANGE_CODES.get(query.exchange.upper())
        if exchange_code is None:
            raise ValueError(f"unsupported exchange: {query.exchange}")
        url = f"{CAFEF_API}?{urlencode({'Symbol': query.ticker.lower(), 'Type': exchange_code, 'Year': 0})}"
        payload = self._fetch_json(url)
        if not bool(payload.get("Success", False)):
            return []
        output = []
        seen = set()
        for raw in payload.get("Data", []) or []:
            if not isinstance(raw, dict):
                continue
            title = " ".join(str(raw.get("Name", "") or "").split())
            source_url = str(raw.get("Link", "") or "").strip()
            if not title or not _allowed_report_url(source_url) or source_url in seen:
                continue
            seen.add(source_url)
            metadata = classify_report_title(title)
            if query.fiscal_year is not None and metadata["fiscal_year"] != query.fiscal_year:
                continue
            if (
                query.fiscal_quarter is not None
                and metadata["fiscal_quarter"] != query.fiscal_quarter
            ):
                continue
            if query.scope and query.scope != metadata["scope"]:
                continue
            if query.audit_status and query.audit_status != metadata["audit_status"]:
                continue
            output.append(
                {
                    "ticker": query.ticker,
                    "company": query.company,
                    "exchange": query.exchange,
                    "title": title,
                    "source_url": source_url,
                    **metadata,
                }
            )
        return output
