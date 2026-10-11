"""CafeF report discovery: typed results and explicit provider errors.

Verified against the public endpoint (September 2026):

* ``Type`` selects the document category, not the exchange. ``Type=1`` lists
  financial statements for tickers on HOSE, HNX and UPCOM alike; ``Type=2``
  and ``Type=9`` answer ``Success=false``. The exchange is display metadata.
* Rows carry structured ``Year``/``Quarter``/``Time``: ``Quarter=1..4`` with
  ``Time="Qn/YYYY"``, and ``Quarter=5`` with ``Time="CN/YYYY"`` for the annual
  report. There is no half-year code, so a half-year report is recognised only
  from its title — and never inferred from Q2 or "soát xét".
* An unknown ticker answers ``Success=true`` with no rows.

A period is *verified* only when the structured fields are present,
consistent, and not contradicted by the title. Anything else is returned in the
"unverified" group with its period left unknown: never dropped silently, and
never filled in from the user's query.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlencode
from urllib.request import Request

from schemas.acquisition import ReportQuery
from tools.http_safety import allowed_https_url, allowlisted_opener

CAFEF_API = "https://cafef.vn/du-lieu/Ajax/PageNew/FileBCTC.ashx"
FINANCIAL_STATEMENTS_DOCUMENT_TYPE = 1
ALLOWED_REPORT_HOST_SUFFIXES = (".cafef.vn", ".mediacdn.vn")
DISCOVERY_DEADLINE_SECONDS = 15
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CANDIDATES = 30
_ANNUAL_QUARTER_CODE = 5
_TIME_RE = re.compile(r"^\s*(?:Q([1-4])|CN)\s*/\s*((?:19|20)\d{2})\s*$", re.IGNORECASE)


class ReportDiscoveryError(RuntimeError):
    """CafeF could not be searched — distinct from a valid search with no rows.

    Codes: ``provider_unavailable`` (network, timeout, HTTP error, blocked
    redirect), ``provider_rejected`` (``Success`` is not true) and
    ``provider_malformed`` (unexpected payload, or no readable row at all).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: list[dict]
    total_matched: int
    truncated: bool
    unreadable_rows: int = 0


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").replace("đ", "d").replace("Đ", "D"))
    return "".join(char for char in text if not unicodedata.combining(char)).lower()


def _allowed_report_url(value: str) -> bool:
    return allowed_https_url(value, ALLOWED_REPORT_HOST_SUFFIXES)


def classify_report_title(title: str) -> dict:
    """What the title alone says. Only one of several inputs to the period."""

    folded = _fold(title)
    years = sorted({int(year) for year in re.findall(r"\b((?:19|20)\d{2})\b", folded)})
    quarter_match = re.search(r"\b(?:quy|q)\s*([1-4])\b", folded)
    half = None
    if re.search(r"\bban nien\b|\bgiua nien do\b|\b(?:6|sau) thang dau\b", folded):
        half = 1
    elif re.search(r"\b(?:6|sau) thang cuoi\b", folded):
        half = 2
    if half is not None and quarter_match:
        period_type = "unknown"
    elif half is not None:
        period_type = "half_year"
    elif quarter_match:
        period_type = "quarter"
    elif years and re.search(r"\bnam\s+(?:19|20)\d{2}\b", folded):
        period_type = "annual"
    else:
        period_type = "unknown"
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
        "period_type": period_type,
        "fiscal_year": years[0] if len(years) == 1 else None,
        "fiscal_quarter": int(quarter_match.group(1)) if quarter_match and period_type == "quarter" else None,
        "fiscal_half": half if period_type == "half_year" else None,
        "scope": scope,
        "audit_status": audit_status,
        "report_type": "financial_statement",
    }


def _structured_period(raw: dict) -> tuple[str, Optional[tuple[str, int, Optional[int]]]]:
    time_text = str(raw.get("Time") or "").strip()
    try:
        year = int(raw.get("Year"))
        quarter = int(raw.get("Quarter"))
    except (TypeError, ValueError):
        return "missing", None
    match = _TIME_RE.match(time_text)
    if not match:
        return ("inconsistent" if time_text else "missing"), None
    if int(match.group(2)) != year:
        return "inconsistent", None
    if match.group(1):
        if quarter != int(match.group(1)):
            return "inconsistent", None
        return "ok", ("quarter", year, quarter)
    if quarter != _ANNUAL_QUARTER_CODE:
        return "inconsistent", None
    return "ok", ("annual", year, None)


def _reconcile_period(raw: dict, title_meta: dict) -> tuple[dict, list[tuple]]:
    """Candidate period fields plus the hypotheses a query may match."""

    title_hypothesis = (
        title_meta["period_type"],
        title_meta["fiscal_year"],
        title_meta["fiscal_quarter"],
        title_meta["fiscal_half"],
    )
    status, structured = _structured_period(raw)
    if status == "ok":
        s_type, s_year, s_quarter = structured
        t_type, t_year, t_quarter, _t_half = title_hypothesis
        note = ""
        if t_year is not None and t_year != s_year:
            note = "title_conflicts_year"
        elif t_type == "half_year":
            note = "half_year_without_structured_code"
        elif t_type == "quarter" and (s_type != "quarter" or t_quarter != s_quarter):
            note = "title_conflicts_period"
        elif t_type == "annual" and s_type != "annual":
            note = "title_conflicts_period"
        structured_hypothesis = (s_type, s_year, s_quarter, None)
        if not note:
            return (
                {
                    "period_type": s_type,
                    "fiscal_year": s_year,
                    "fiscal_quarter": s_quarter,
                    "fiscal_half": None,
                    "period_verified": True,
                    "verification_note": "",
                },
                [structured_hypothesis],
            )
        hypotheses = [structured_hypothesis, title_hypothesis]
    else:
        note = "missing_structured_period" if status == "missing" else "inconsistent_structured_period"
        hypotheses = [title_hypothesis]
    return (
        {
            "period_type": "unknown",
            "fiscal_year": None,
            "fiscal_quarter": None,
            "fiscal_half": None,
            "period_verified": False,
            "verification_note": note,
        },
        hypotheses,
    )


def _hypothesis_matches(hypothesis: tuple, query: ReportQuery) -> bool:
    kind, year, quarter, half = hypothesis
    if query.fiscal_year is not None and year is not None and year != query.fiscal_year:
        return False
    if query.period == "any" or kind == "unknown":
        return True
    if kind != query.period:
        return False
    if query.period == "quarter" and None not in (query.fiscal_quarter, quarter):
        return quarter == query.fiscal_quarter
    if query.period == "half_year" and None not in (query.fiscal_half, half):
        return half == query.fiscal_half
    return True


def _filter_matches(requested: str, actual: str) -> bool:
    # A requested scope/audit status excludes only a known, different value.
    return not requested or requested == "unknown" or actual in ("unknown", requested)


def _sort_key(candidate: dict) -> tuple:
    rank = 5 if candidate["period_type"] == "annual" else (candidate["fiscal_quarter"] or 0)
    return (candidate["fiscal_year"] or 0, rank)


class CafeFReportDiscoveryProvider:
    def __init__(
        self,
        *,
        timeout_seconds: int = DISCOVERY_DEADLINE_SECONDS,
        fetch_json: Callable[[str], dict] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._fetch_json = fetch_json or self._default_fetch_json

    @property
    def provider_identity(self) -> str:
        return "cafef-report-discovery-v3"

    def _default_fetch_json(self, url: str) -> dict:
        deadline = self._clock() + self.timeout_seconds
        request = Request(
            url,
            headers={
                "User-Agent": "AgentFinX/0.2 (+report-discovery)",
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://cafef.vn/",
            },
        )
        chunks: list[bytes] = []
        size = 0
        try:
            opener = allowlisted_opener(_allowed_report_url)
            with opener(request, timeout=self.timeout_seconds) as response:
                if not _allowed_report_url(response.geturl()):
                    raise ReportDiscoveryError(
                        "provider_unavailable", "CafeF discovery redirected outside allowed hosts"
                    )
                charset = response.headers.get_content_charset() or "utf-8"
                while True:
                    # One deadline for the whole request, not per socket read.
                    if self._clock() > deadline:
                        raise ReportDiscoveryError(
                            "provider_unavailable", "CafeF discovery exceeded its deadline"
                        )
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise ReportDiscoveryError(
                            "provider_malformed", "CafeF discovery response is too large"
                        )
                    chunks.append(chunk)
        except ReportDiscoveryError:
            raise
        except (OSError, ValueError) as exc:
            # URLError, HTTPError and timeouts are OSErrors; a blocked
            # redirect raises ValueError from the allowlist handler.
            raise ReportDiscoveryError(
                "provider_unavailable", "CafeF discovery request failed"
            ) from exc
        try:
            return json.loads(b"".join(chunks).decode(charset, errors="strict"))
        except (UnicodeDecodeError, ValueError, LookupError) as exc:
            raise ReportDiscoveryError("provider_malformed", "CafeF discovery returned invalid JSON") from exc

    @staticmethod
    def _rows(payload) -> list:
        if not isinstance(payload, dict):
            raise ReportDiscoveryError("provider_malformed", "CafeF discovery payload is not an object")
        if payload.get("Success") is not True:
            # Observed for invalid request parameters; its meaning is not
            # documented, so it is never reported as "no reports".
            raise ReportDiscoveryError("provider_rejected", "CafeF discovery did not succeed")
        data = payload.get("Data")
        if data is None:
            return []
        if not isinstance(data, list):
            raise ReportDiscoveryError("provider_malformed", "CafeF discovery rows are not a list")
        return data

    def discover(self, query: ReportQuery) -> DiscoveryResult:
        if query.report_type != "financial_statement":
            return DiscoveryResult([], 0, False)
        ticker = query.ticker.strip().lower()
        if not ticker:
            raise ValueError("ticker is required for report discovery")
        url = f"{CAFEF_API}?{urlencode({'Symbol': ticker, 'Type': FINANCIAL_STATEMENTS_DOCUMENT_TYPE, 'Year': 0})}"
        try:
            payload = self._fetch_json(url)
        except ReportDiscoveryError:
            raise
        except Exception as exc:
            raise ReportDiscoveryError("provider_unavailable", "CafeF discovery request failed") from exc
        rows = self._rows(payload)

        matched: list[dict] = []
        seen: set[str] = set()
        readable = unreadable = 0
        for raw in rows:
            if not isinstance(raw, dict):
                unreadable += 1
                continue
            title = " ".join(str(raw.get("Name", "") or "").split())
            source_url = str(raw.get("Link", "") or "").strip()
            if not title or not _allowed_report_url(source_url):
                unreadable += 1
                continue
            readable += 1
            if source_url in seen:
                continue
            seen.add(source_url)
            title_meta = classify_report_title(title)
            period, hypotheses = _reconcile_period(raw, title_meta)
            if not any(_hypothesis_matches(item, query) for item in hypotheses):
                continue
            if not _filter_matches(query.scope, title_meta["scope"]):
                continue
            if not _filter_matches(query.audit_status, title_meta["audit_status"]):
                continue
            matched.append(
                {
                    "ticker": query.ticker.upper(),
                    "company": query.company,
                    "exchange": query.exchange,
                    "title": title,
                    "source_url": source_url,
                    "period_label": str(raw.get("Time") or "").strip()[:20],
                    "scope": title_meta["scope"],
                    "audit_status": title_meta["audit_status"],
                    "report_type": "financial_statement",
                    **period,
                }
            )
        if rows and not readable:
            raise ReportDiscoveryError("provider_malformed", "No CafeF discovery row could be read")

        verified = sorted((item for item in matched if item["period_verified"]), key=_sort_key, reverse=True)
        unverified = [item for item in matched if not item["period_verified"]]
        ordered = verified + unverified
        return DiscoveryResult(
            candidates=ordered[:MAX_CANDIDATES],
            total_matched=len(ordered),
            truncated=len(ordered) > MAX_CANDIDATES,
            unreadable_rows=unreadable,
        )
