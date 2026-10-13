"""What a company-news analysis is about: the open report's company.

The ticker comes only from the report the run was given (its access was
checked before the run started); never from the question or the planner. A
report without a ticker gets one only when its exact company name matches
exactly one company in the CafeF catalog. If
the question names another listed company, the analysis stops and asks for
that company's report instead of quietly answering about this one.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from agentfinx.core.config import COMPANY_ALIASES_PATH
from agentfinx.core.schemas.web_analysis import WebContext
from agentfinx.news.analysis.window import WindowError, resolve_news_window
from agentfinx.news.dates import iso_utc
from agentfinx.acquisition.symbols import fold_text

ALIAS_PATH = COMPANY_ALIASES_PATH
_TICKER_TOKEN = re.compile(r"\b[A-Z][A-Z0-9]{2}\b")


def default_symbol_catalog():
    """The CafeF symbol catalog if one was downloaded; ``None`` otherwise."""

    from agentfinx.core.config import paths
    from agentfinx.acquisition.symbols import SymbolCatalog

    path = paths.symbol_catalog_path()
    if not path.exists():
        return None
    catalog = SymbolCatalog.load(path, alias_path=ALIAS_PATH)
    return catalog if catalog.symbols else None


def other_companies(question: str, ticker: str, catalog) -> list[str]:
    """Listed tickers the question names that are not the open report's.

    Only exact catalog tickers and verified aliases count: an uppercase word
    such as "ROE" or "BCTC" is not a company.
    """

    if catalog is None:
        return []
    found = []
    for token in _TICKER_TOKEN.findall(str(question or "")):
        if token != ticker and catalog.get(token) is not None and token not in found:
            found.append(token)
    folded = f" {fold_text(question)} "
    for alias, tickers in (getattr(catalog, "aliases", {}) or {}).items():
        if alias and f" {alias} " in folded and ticker not in tickers:
            found.extend(sorted(item for item in tickers if item not in found))
    return found


def ticker_for_company(company: str, catalog) -> str:
    """The one listed ticker whose official name equals ``company`` exactly
    (after folding); "" when there is none or more than one."""

    if catalog is None or not company:
        return ""
    target = fold_text(company)
    matches = [ticker for ticker, symbol in catalog.symbols.items() if fold_text(symbol.company) == target]
    return matches[0] if len(matches) == 1 else ""


def build_web_context(state: dict, *, as_of: datetime, catalog=None) -> WebContext:
    ticker = str(state.get("dataset_ticker", "") or "").strip().upper()
    company = " ".join(str(state.get("dataset_company", "") or "").split())
    question = " ".join(str(state.get("user_query", "") or "").split())
    source = "report" if ticker else ""
    if not ticker:
        ticker = ticker_for_company(company, catalog)
        source = "catalog_name" if ticker else ""
    base = {"company": company, "ticker": ticker, "ticker_source": source, "question": question,
            "as_of": iso_utc(as_of)}

    if not ticker:
        return WebContext(
            status="unsupported", reason="dataset_without_ticker",
            message="Báo cáo đang mở chưa có mã chứng khoán nên chưa tìm được tin doanh nghiệp.", **base,
        )
    others = other_companies(question, ticker, catalog)
    if others:
        named = ", ".join(others)
        return WebContext(
            status="needs_clarification", reason="other_company",
            message=(f"Câu hỏi nhắc tới {named} nhưng báo cáo đang mở là của {ticker}. "
                     f"Hãy mở báo cáo của {named} để xem tin của doanh nghiệp đó."),
            **base,
        )
    try:
        window = resolve_news_window(question, as_of)
    except WindowError as exc:
        return WebContext(
            status="needs_clarification", reason=exc.reason,
            message=f"{exc.message} Hãy nêu lại khoảng thời gian cần xem tin.", **base,
        )
    return WebContext(
        status="ready",
        date_from=iso_utc(window.date_from),
        date_to=iso_utc(window.date_to),
        window_source=window.source,
        **base,
    )


def context_or_none(value: Optional[dict]) -> Optional[WebContext]:
    if not value:
        return None
    try:
        return WebContext.model_validate(value)
    except ValueError:
        return None
