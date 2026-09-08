"""CafeF symbol-catalog parser adapted from the crawl branch."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import Request
from tools.http_safety import allowed_https_url, allowlisted_opener

EXCHANGE_MAP = {"HSX": "HSX", "HOSE": "HSX", "HNX": "HNX", "UPCOM": "UpCom"}
CAFEF_SCREENER_URL = "https://cafef.vn/du-lieu/screener.aspx"


@dataclass(frozen=True)
class StockSymbol:
    ticker: str
    company: str
    exchange: str


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


def _normalize_rows(rows: list[dict]) -> list[StockSymbol]:
    output = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = _value(row, "Symbol", "Ticker", "MaCK", "StockCode").upper()
        company = _value(
            row,
            "CompanyName",
            "Company",
            "Name",
            "TenCongTy",
            "FullName",
        )
        exchange_raw = _value(row, "TradeCenter", "Exchange", "San", "Floor").upper()
        exchange = EXCHANGE_MAP.get(exchange_raw, exchange_raw)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        output.append(StockSymbol(ticker=ticker, company=company, exchange=exchange))
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


def load_symbol_catalog(path: str | Path) -> dict[str, StockSymbol]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return {}
    output = {}
    for row in payload.get("symbols", []) if isinstance(payload, dict) else []:
        if not isinstance(row, dict):
            continue
        normalized = _normalize_rows([row])
        if normalized:
            output[normalized[0].ticker] = normalized[0]
    return output


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
        return "cafef-symbol-catalog-v1"

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

    def refresh(self, output_path: str | Path) -> dict:
        html = self._fetch_html()
        symbols = parse_cafef_symbols(html)
        if not symbols:
            raise ValueError("CafeF symbol catalog contained no usable symbols")
        rows = [
            {"ticker": item.ticker, "company": item.company, "exchange": item.exchange}
            for item in symbols
        ]
        payload = {
            "provider": self.provider_identity,
            "retrieved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "source_url": CAFEF_SCREENER_URL,
            "content_hash": sha256(
                json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "symbols": rows,
        }
        target = Path(output_path)
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
