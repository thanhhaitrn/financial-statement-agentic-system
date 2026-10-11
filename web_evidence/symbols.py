"""CafeF symbol catalog: parser, lookup and the daily refresh job.

The screener page embeds ``jsonData`` rows with ``Symbol``, ``FullName``,
``TradeCenterID`` and ``CenterName``. Verified mapping (September 2026):
``1 → HOSE`` (``CenterName="HSX"``), ``2 → HNX``, ``9 → UPCOM``. This is the
listing exchange; it is unrelated to the report endpoint's ``Type`` parameter.

Freshness: a catalog retrieved within 24 hours is used as is; between 24 hours
and 7 days it is used with a warning and a refresh is due; older or missing,
nothing is inferred from it (a ticker the user typed can still be searched).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.request import Request

from tools.http_safety import allowed_https_url, allowlisted_opener

logger = logging.getLogger("agentfinx.symbols")

EXCHANGE_BY_TRADE_CENTER_ID = {1: "HOSE", 2: "HNX", 9: "UPCOM"}
EXCHANGE_BY_NAME = {"HOSE": "HOSE", "HSX": "HOSE", "HNX": "HNX", "UPCOM": "UPCOM"}
CAFEF_SCREENER_URL = "https://cafef.vn/du-lieu/screener.aspx"
CATALOG_FRESH_FOR = timedelta(hours=24)
CATALOG_USABLE_FOR = timedelta(days=7)
REFRESH_RETRY_AFTER = timedelta(hours=6)
MIN_EXCHANGE_COVERAGE = 0.9
MAX_SHRINK_RATIO = 0.5
_TICKER_RE = re.compile(r"^[A-Za-z0-9]{2,5}$")


class SymbolCatalogRefreshError(ValueError):
    """A refresh produced data that must not replace the current catalog."""


@dataclass(frozen=True)
class StockSymbol:
    ticker: str
    company: str
    exchange: str


@dataclass(frozen=True)
class SymbolMatch:
    symbol: StockSymbol
    matched_by: str  # "ticker" | "alias" | "name"


def fold_text(value: str) -> str:
    """Lowercase, strip Vietnamese diacritics and punctuation for matching."""

    text = unicodedata.normalize("NFKD", str(value or "").replace("đ", "d").replace("Đ", "D"))
    text = "".join(char for char in text if not unicodedata.combining(char)).lower()
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def looks_like_ticker(value: str) -> bool:
    return bool(_TICKER_RE.fullmatch(str(value or "").strip()))


def normalize_exchange(value) -> str:
    return EXCHANGE_BY_NAME.get(str(value or "").strip().upper(), "")


def extract_balanced_json_array(text: str, start: int) -> str:
    if start >= len(text) or text[start] != "[":
        raise ValueError("JSON array must start with '['")
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise ValueError("unterminated JSON array")


def _value(row: dict, *names: str) -> str:
    lower = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        value = lower.get(name.lower())
        if value not in (None, ""):
            return " ".join(str(value).split())
    return ""


def _row_exchange(row: dict) -> str:
    raw_id = _value(row, "TradeCenterID")
    if raw_id.isdigit():
        exchange = EXCHANGE_BY_TRADE_CENTER_ID.get(int(raw_id), "")
        if exchange:
            return exchange
    return normalize_exchange(_value(row, "CenterName", "TradeCenter", "Exchange", "San", "Floor"))


def _normalize_rows(rows: list[dict]) -> list[StockSymbol]:
    output = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = _value(row, "Symbol", "Ticker", "MaCK", "StockCode").upper()
        company = _value(row, "CompanyName", "Company", "Name", "TenCongTy", "FullName")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        output.append(StockSymbol(ticker=ticker, company=company, exchange=_row_exchange(row)))
    return output


class _SymbolTableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_cafef_symbols(html: str) -> list[StockSymbol]:
    match = re.search(r"(?:var|let|const)\s+jsonData\s*=\s*", html)
    if match:
        start = html.find("[", match.end())
        if start >= 0:
            raw = extract_balanced_json_array(html, start)
            rows = json.loads(re.sub(r"\bNaN\b", "null", raw))
            normalized = _normalize_rows(rows)
            if normalized:
                return normalized

    parser = _SymbolTableParser()
    parser.feed(html)
    if not parser.rows:
        return []
    header = [value.lower() for value in parser.rows[0]]
    body = parser.rows[1:]
    ticker_index = next((i for i, value in enumerate(header) if "mã" in value or "symbol" in value), 0)
    company_index = next((i for i, value in enumerate(header) if "công ty" in value or "company" in value), 1)
    exchange_index = next((i for i, value in enumerate(header) if "sàn" in value or "exchange" in value), 2)
    rows = [
        {
            "Symbol": row[ticker_index] if ticker_index < len(row) else "",
            "CompanyName": row[company_index] if company_index < len(row) else "",
            "Exchange": row[exchange_index] if exchange_index < len(row) else "",
        }
        for row in body
    ]
    return _normalize_rows(rows)


def _read_catalog_payload(path: str | Path) -> Optional[dict]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def load_symbol_catalog(path: str | Path) -> dict[str, StockSymbol]:
    payload = _read_catalog_payload(path) or {}
    output = {}
    for row in payload.get("symbols", []) or []:
        if not isinstance(row, dict):
            continue
        normalized = _normalize_rows([row])
        if normalized:
            output[normalized[0].ticker] = normalized[0]
    return output


def load_company_aliases(path: str | Path | None) -> dict[str, frozenset[str]]:
    """Verified aliases (folded alias → tickers). Entries without a ticker,
    alias or source are ignored: an unverified alias must not steer a search."""

    if path is None:
        return {}
    payload = _read_catalog_payload(path) or {}
    aliases: dict[str, set[str]] = {}
    for entry in payload.get("aliases", []) or []:
        if not isinstance(entry, dict):
            continue
        alias = fold_text(entry.get("alias", ""))
        ticker = str(entry.get("ticker", "") or "").strip().upper()
        if not alias or not looks_like_ticker(ticker) or not str(entry.get("source", "") or "").strip():
            logger.warning("ignoring unverified company alias entry")
            continue
        aliases.setdefault(alias, set()).add(ticker)
    return {alias: frozenset(tickers) for alias, tickers in aliases.items()}


class SymbolCatalog:
    def __init__(
        self,
        symbols: dict[str, StockSymbol],
        *,
        retrieved_at: Optional[datetime] = None,
        aliases: Optional[dict[str, frozenset[str]]] = None,
    ):
        self.symbols = dict(symbols)
        self.retrieved_at = retrieved_at
        self.aliases = dict(aliases or {})
        self._folded = {ticker: fold_text(item.company) for ticker, item in self.symbols.items()}

    @classmethod
    def load(cls, path: str | Path, *, alias_path: str | Path | None = None) -> "SymbolCatalog":
        payload = _read_catalog_payload(path) or {}
        retrieved_at = None
        try:
            retrieved_at = datetime.fromisoformat(str(payload.get("retrieved_at", "")).replace("Z", "+00:00"))
            if retrieved_at.tzinfo is None:
                retrieved_at = retrieved_at.replace(tzinfo=timezone.utc)
        except ValueError:
            retrieved_at = None
        return cls(load_symbol_catalog(path), retrieved_at=retrieved_at,
                   aliases=load_company_aliases(alias_path))

    def status(self, now: Optional[datetime] = None) -> str:
        if not self.symbols or self.retrieved_at is None:
            return "missing"
        age = (now or datetime.now(timezone.utc)) - self.retrieved_at
        if age <= CATALOG_FRESH_FOR:
            return "fresh"
        if age <= CATALOG_USABLE_FOR:
            return "stale"
        return "expired"

    def usable(self, now: Optional[datetime] = None) -> bool:
        return self.status(now) in {"fresh", "stale"}

    def get(self, ticker: str) -> Optional[StockSymbol]:
        return self.symbols.get(str(ticker or "").strip().upper())

    def search(self, text: str, *, limit: int = 10) -> list[SymbolMatch]:
        """Exact ticker, then verified alias, then company-name matches.

        Returns up to ``limit + 1`` items so a caller can tell "too many"."""

        raw = str(text or "").strip()
        exact = self.get(raw) if looks_like_ticker(raw) else None
        if exact is not None:
            return [SymbolMatch(exact, "ticker")]
        folded = fold_text(raw)
        if len(folded) < 3:
            return []
        alias_tickers = self.aliases.get(folded, frozenset())
        matches = [SymbolMatch(self.symbols[t], "alias") for t in sorted(alias_tickers) if t in self.symbols]
        if matches:
            return matches[: limit + 1]
        needle = f" {folded} "
        for ticker in sorted(self.symbols):
            if needle in f" {self._folded[ticker]} ":
                matches.append(SymbolMatch(self.symbols[ticker], "name"))
                if len(matches) > limit:
                    break
        return matches


class CafeFSymbolCatalogJob:
    """One-shot catalog refresh intended for a daily scheduler/worker."""

    def __init__(
        self,
        *,
        fetch_html: Callable[[], str] | None = None,
        timeout_seconds: int = 30,
        max_body_bytes: int = 10 * 1024 * 1024,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_body_bytes = max_body_bytes
        self._fetch_html = fetch_html or self._default_fetch_html

    @property
    def provider_identity(self) -> str:
        return "cafef-symbol-catalog-v2"

    def _default_fetch_html(self) -> str:
        request = Request(
            CAFEF_SCREENER_URL,
            headers={"User-Agent": "AgentFinX/0.2 (+symbol-catalog)"},
        )
        allowed = lambda url: allowed_https_url(url, ("cafef.vn",)) and (urlparse(url).hostname or "").lower() == "cafef.vn"
        with allowlisted_opener(allowed)(request, timeout=self.timeout_seconds) as response:
            final = urlparse(response.geturl())
            if final.scheme != "https" or (final.hostname or "").lower() != "cafef.vn":
                raise ValueError("CafeF catalog redirected outside the allowed host")
            raw = response.read(self.max_body_bytes + 1)
            if len(raw) > self.max_body_bytes:
                raise ValueError("CafeF catalog response is too large")
            charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")

    @staticmethod
    def _check_quality(symbols: list[StockSymbol], previous_count: int) -> None:
        if not symbols:
            raise SymbolCatalogRefreshError("CafeF symbol catalog contained no usable symbols")
        coverage = sum(1 for item in symbols if item.exchange) / len(symbols)
        if coverage < MIN_EXCHANGE_COVERAGE:
            raise SymbolCatalogRefreshError(
                f"only {coverage:.0%} of symbols have a known exchange; the page layout may have changed"
            )
        if previous_count and len(symbols) < previous_count * MAX_SHRINK_RATIO:
            raise SymbolCatalogRefreshError(
                f"refresh returned {len(symbols)} symbols, far fewer than the current {previous_count}"
            )

    def refresh(self, output_path: str | Path) -> dict:
        """Fetch, check and atomically replace the catalog under a file lock.

        A failed or implausible refresh raises and leaves the current file
        untouched."""

        from dataset_catalog.locks import file_lock

        target = Path(output_path)
        with file_lock(target.with_name(f"{target.name}.lock")):
            symbols = parse_cafef_symbols(self._fetch_html())
            self._check_quality(symbols, previous_count=len(load_symbol_catalog(target)))
            rows = [
                {"ticker": item.ticker, "company": item.company, "exchange": item.exchange}
                for item in symbols
            ]
            payload = {
                "schema_version": 2,
                "provider": self.provider_identity,
                "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                "source_url": CAFEF_SCREENER_URL,
                "content_hash": sha256(
                    json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                "symbols": rows,
            }
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
            )
            temporary_path = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_path, target)
            finally:
                temporary_path.unlink(missing_ok=True)
        return payload


def refresh_catalog_if_due(
    path: str | Path,
    *,
    job: Optional[CafeFSymbolCatalogJob] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Refresh a stale, expired or missing catalog, at most every 6 hours.

    Returns ``"refreshed"``, ``"failed"`` or ``None`` (nothing due). The last
    attempt time is kept in a marker file so restarts do not hammer CafeF."""

    current = now or datetime.now(timezone.utc)
    target = Path(path)
    if SymbolCatalog.load(target).status(current) == "fresh":
        return None
    marker = target.with_name(f"{target.name}.last-attempt")
    try:
        last = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
        if current - last < REFRESH_RETRY_AFTER:
            return None
    except (FileNotFoundError, OSError, ValueError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(current.replace(microsecond=0).isoformat(), encoding="utf-8")
    try:
        (job or CafeFSymbolCatalogJob()).refresh(target)
    except Exception:
        logger.warning("symbol catalog refresh failed; keeping the current catalog", exc_info=True)
        return "failed"
    return "refreshed"
