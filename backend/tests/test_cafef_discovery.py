"""CafeF discovery: provider errors vs. no results, structured periods, catalog."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from agentfinx.acquisition.cafef import (MAX_CANDIDATES, CafeFReportDiscoveryProvider,
                               ReportDiscoveryError)
from agentfinx.acquisition.service import (ReportAcquisitionService,
                                 ReportDiscoveryUnavailable)
from agentfinx.acquisition.store import AcquisitionStore
from agentfinx.core.config.runtime_policy import AcquisitionPolicy
from agentfinx.core.schemas.acquisition import ReportQuery
from agentfinx.news.collect.symbols import (CafeFSymbolCatalogJob, SymbolCatalog,
                                  SymbolCatalogRefreshError,
                                  parse_cafef_symbols, refresh_catalog_if_due)

NOW = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


def row(name, *, year=None, quarter=None, time=None, key=None):
    digest = hashlib.sha256(f"{name}|{year}|{quarter}|{time}|{key}".encode()).hexdigest()[:12]
    item = {"Name": name, "Link": f"https://cafef1.mediacdn.vn/data/{digest}.pdf"}
    for field, value in (("Year", year), ("Quarter", quarter), ("Time", time)):
        if value is not None:
            item[field] = value
    return item


ANNUAL = row("Báo cáo tài chính hợp nhất năm 2025 (đã kiểm toán)", year=2025, quarter=5, time="CN/2025")
Q2 = row("Báo cáo tài chính hợp nhất quý 2 năm 2025", year=2025, quarter=2, time="Q2/2025")
Q1 = row("Báo cáo tài chính công ty mẹ quý 1 năm 2025", year=2025, quarter=1, time="Q1/2025")
OLD = row("Báo cáo tài chính hợp nhất năm 2024 (đã kiểm toán)", year=2024, quarter=5, time="CN/2024")


def _provider(rows=(), *, payload=None, calls=None):
    def fetch(url):
        if calls is not None:
            calls.append(url)
        return payload if payload is not None else {"Success": True, "Data": list(rows)}

    return CafeFReportDiscoveryProvider(fetch_json=fetch)


def _query(**fields):
    return ReportQuery(owner_id="owner", session_id="session", ticker="ADC", **fields)


def _titles(result):
    return [item["title"] for item in result.candidates]


def test_request_uses_the_financial_statement_type_without_any_exchange():
    calls = []
    _provider([ANNUAL], calls=calls).discover(_query())
    assert "Type=1" in calls[0] and "Symbol=adc" in calls[0]


def test_success_false_is_a_provider_error_not_an_empty_result():
    with pytest.raises(ReportDiscoveryError) as error:
        _provider(payload={"Success": False, "Data": [], "Message": "?"}).discover(_query())
    assert error.value.code == "provider_rejected"


@pytest.mark.parametrize("payload", [
    [],
    {"Success": True, "Data": {"not": "a list"}},
    {"Success": True, "Data": ["garbage", {"Name": "", "Link": "https://cafef1.mediacdn.vn/x.pdf"}]},
])
def test_malformed_payloads_are_provider_errors(payload):
    with pytest.raises(ReportDiscoveryError) as error:
        _provider(payload=payload).discover(_query())
    assert error.value.code == "provider_malformed"


def test_network_failures_are_provider_errors():
    def fetch(_url):
        raise TimeoutError("slow")

    with pytest.raises(ReportDiscoveryError) as error:
        CafeFReportDiscoveryProvider(fetch_json=fetch).discover(_query())
    assert error.value.code == "provider_unavailable"


def test_a_valid_empty_answer_is_no_results():
    result = _provider(payload={"Success": True, "Data": []}).discover(_query())
    assert result.candidates == [] and result.total_matched == 0 and not result.truncated


def test_year_known_only_from_structured_metadata_is_verified():
    report = row("Báo cáo tài chính hợp nhất (đã kiểm toán)", year=2025, quarter=5, time="CN/2025")
    [candidate] = _provider([report]).discover(_query(fiscal_year=2025, period="annual")).candidates
    assert candidate["period_verified"] and candidate["period_type"] == "annual"
    assert candidate["fiscal_year"] == 2025 and candidate["period_label"] == "CN/2025"


def test_period_filters_separate_annual_and_quarterly_reports():
    provider = _provider([Q1, ANNUAL, OLD, Q2])
    assert _titles(provider.discover(_query(fiscal_year=2025, period="annual"))) == [ANNUAL["Name"]]
    assert _titles(provider.discover(_query(fiscal_year=2025, period="quarter"))) == [Q2["Name"], Q1["Name"]]
    assert _titles(provider.discover(_query(fiscal_year=2025, fiscal_quarter=2))) == [Q2["Name"]]
    assert _titles(provider.discover(_query())) == [ANNUAL["Name"], Q2["Name"], Q1["Name"], OLD["Name"]]


def test_half_year_is_never_inferred_from_q2_or_review():
    half = row("Báo cáo tài chính bán niên năm 2025 (đã soát xét)", year=2025, quarter=2, time="Q2/2025")
    reviewed_q2 = row("Báo cáo tài chính quý 2 năm 2025 (đã soát xét)", year=2025, quarter=2, time="Q2/2025")
    provider = _provider([half, reviewed_q2])

    half_results = provider.discover(_query(fiscal_year=2025, period="half_year")).candidates
    assert [item["title"] for item in half_results] == [half["Name"]]
    [unverified] = half_results
    assert not unverified["period_verified"]
    assert unverified["verification_note"] == "half_year_without_structured_code"
    assert unverified["period_type"] == "unknown" and unverified["fiscal_year"] is None

    q2 = provider.discover(_query(fiscal_year=2025, fiscal_quarter=2)).candidates
    assert [item["period_verified"] for item in q2] == [True, False]  # verified first


def test_conflicting_metadata_is_unverified_and_never_takes_the_query_year():
    conflict = row("Báo cáo tài chính quý 1 năm 2025", year=2024, quarter=1, time="Q1/2024")
    provider = _provider([conflict])
    for year in (2024, 2025):
        [candidate] = provider.discover(_query(fiscal_year=year)).candidates
        assert candidate["verification_note"] == "title_conflicts_year"
        assert candidate["fiscal_year"] is None and candidate["fiscal_quarter"] is None
    assert provider.discover(_query(fiscal_year=2023)).candidates == []


@pytest.mark.parametrize("report, note", [
    (row("BCTC hợp nhất 2025 đã kiểm toán"), "missing_structured_period"),
    (row("Báo cáo tài chính quý 3 năm 2025", year=2025, quarter=2, time="Q3/2025"),
     "inconsistent_structured_period"),
])
def test_missing_or_inconsistent_structured_periods_are_kept_as_unverified(report, note):
    [candidate] = _provider([report]).discover(_query(fiscal_year=2025)).candidates
    assert not candidate["period_verified"] and candidate["verification_note"] == note


def test_duplicates_and_disallowed_hosts_are_skipped():
    outside = {"Name": "Báo cáo quý 1 năm 2025", "Link": "https://example.com/r.pdf",
               "Year": 2025, "Quarter": 1, "Time": "Q1/2025"}
    result = _provider([Q2, dict(Q2), outside]).discover(_query())
    assert _titles(result) == [Q2["Name"]] and result.unreadable_rows == 1


def test_results_are_capped_and_the_total_is_reported():
    rows = [row(f"Báo cáo tài chính quý {q} năm {year}", year=year, quarter=q, time=f"Q{q}/{year}")
            for year in range(2016, 2026) for q in range(1, 5)]
    result = _provider(rows).discover(_query(period="quarter"))
    assert len(result.candidates) == MAX_CANDIDATES and result.truncated and result.total_matched == 40
    assert result.candidates[0]["fiscal_year"] == 2025 and result.candidates[0]["fiscal_quarter"] == 4


def test_scope_filter_excludes_only_a_known_different_scope():
    unknown_scope = row("Báo cáo tài chính quý 3 năm 2025", year=2025, quarter=3, time="Q3/2025")
    result = _provider([Q2, Q1, unknown_scope]).discover(_query(scope="consolidated"))
    assert set(_titles(result)) == {Q2["Name"], unknown_scope["Name"]}


def test_period_numbers_only_fit_their_period_type():
    assert _query(fiscal_quarter=3).period == "quarter"
    assert _query(fiscal_half=1).period == "half_year"
    with pytest.raises(ValidationError):
        _query(period="annual", fiscal_quarter=2)
    with pytest.raises(ValidationError):
        _query(period="quarter", fiscal_half=1)


# --------------------------------------------------------------------- catalog

SCREENER = """<script>var jsonData = [
  {"Symbol":"VNM","CompanyName":null,"FullName":"Công ty Cổ phần Sữa Việt Nam","TradeCenterID":1,"CenterName":"HSX"},
  {"Symbol":"HPG","CompanyName":null,"FullName":"Công ty cổ phần Tập đoàn Hòa Phát","TradeCenterID":1,"CenterName":"HSX"},
  {"Symbol":"ADC","CompanyName":null,"FullName":"Công ty Cổ phần Mĩ thuật và Truyền thông","TradeCenterID":2,"CenterName":"HNX"},
  {"Symbol":"A32","CompanyName":null,"FullName":"Công ty cổ phần 32","TradeCenterID":9,"CenterName":"UpCom"}
];</script>"""


def _write_catalog(path, symbols, *, retrieved_at=NOW):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "retrieved_at": retrieved_at.isoformat(),
        "symbols": [{"ticker": s.ticker, "company": s.company, "exchange": s.exchange} for s in symbols],
    }), encoding="utf-8")
    return path


def test_parser_maps_trade_center_ids_to_listing_exchanges():
    symbols = {item.ticker: item for item in parse_cafef_symbols(SCREENER)}
    assert {ticker: item.exchange for ticker, item in symbols.items()} == {
        "VNM": "HOSE", "HPG": "HOSE", "ADC": "HNX", "A32": "UPCOM",
    }
    assert symbols["VNM"].company == "Công ty Cổ phần Sữa Việt Nam"


@pytest.mark.parametrize("age, status", [
    (timedelta(hours=1), "fresh"),
    (timedelta(days=3), "stale"),
    (timedelta(days=8), "expired"),
])
def test_catalog_freshness(tmp_path, age, status):
    path = _write_catalog(tmp_path / "symbols.json", parse_cafef_symbols(SCREENER), retrieved_at=NOW - age)
    assert SymbolCatalog.load(path).status(NOW) == status
    assert SymbolCatalog.load(tmp_path / "missing.json").status(NOW) == "missing"


def test_catalog_search_by_ticker_name_and_verified_alias(tmp_path):
    aliases = tmp_path / "aliases.json"
    aliases.write_text(json.dumps({"aliases": [
        {"alias": "Vinamilk", "ticker": "VNM", "source": "https://example.test/official"},
        {"alias": "Unverified", "ticker": "HPG"},
    ]}), encoding="utf-8")
    catalog = SymbolCatalog.load(
        _write_catalog(tmp_path / "symbols.json", parse_cafef_symbols(SCREENER)), alias_path=aliases,
    )
    assert [m.symbol.ticker for m in catalog.search("vnm")] == ["VNM"]
    assert [(m.symbol.ticker, m.matched_by) for m in catalog.search("sữa việt nam")] == [("VNM", "name")]
    assert [m.symbol.ticker for m in catalog.search("Hoa Phat")] == ["HPG"]
    assert [(m.symbol.ticker, m.matched_by) for m in catalog.search("VINAMILK")] == [("VNM", "alias")]
    assert catalog.search("unverified") == []
    assert len(catalog.search("công ty cổ phần", limit=2)) == 3  # "too many": caller must ask


def test_refresh_never_replaces_a_good_catalog_with_bad_data(tmp_path):
    good = [SimpleNamespace(ticker=f"T{i:02d}", company=f"Công ty {i}", exchange="HOSE") for i in range(20)]
    path = _write_catalog(tmp_path / "symbols.json", good)
    before = path.read_text(encoding="utf-8")
    bad_pages = [
        "<html>no data</html>",
        '<script>var jsonData=[' + ",".join(
            f'{{"Symbol":"X{i:02d}","FullName":"X","TradeCenterID":7}}' for i in range(20)) + "];</script>",
        SCREENER,  # 4 symbols replacing 20
    ]
    for html in bad_pages:
        with pytest.raises(SymbolCatalogRefreshError):
            CafeFSymbolCatalogJob(fetch_html=lambda html=html: html).refresh(path)
        assert path.read_text(encoding="utf-8") == before

    fresh = tmp_path / "fresh.json"
    payload = CafeFSymbolCatalogJob(fetch_html=lambda: SCREENER).refresh(fresh)
    assert payload["schema_version"] == 2 and len(payload["symbols"]) == 4


def test_due_refresh_runs_for_missing_catalogs_and_backs_off_after_failure(tmp_path):
    path = tmp_path / "symbols.json"
    calls = []

    def failing():
        calls.append("fetch")
        raise OSError("offline")

    assert refresh_catalog_if_due(path, job=CafeFSymbolCatalogJob(fetch_html=failing), now=NOW) == "failed"
    assert refresh_catalog_if_due(path, job=CafeFSymbolCatalogJob(fetch_html=failing),
                                  now=NOW + timedelta(hours=1)) is None
    assert len(calls) == 1
    job = CafeFSymbolCatalogJob(fetch_html=lambda: SCREENER)
    assert refresh_catalog_if_due(path, job=job, now=NOW + timedelta(hours=7)) == "refreshed"
    assert refresh_catalog_if_due(path, job=job, now=datetime.now(timezone.utc)) is None  # fresh


# --------------------------------------------------------------------- service

def _service(tmp_path, provider, *, catalog_age=timedelta(hours=1), catalog=True):
    path = tmp_path / "symbols.json"
    if catalog:
        _write_catalog(path, parse_cafef_symbols(SCREENER), retrieved_at=NOW - catalog_age)
    return ReportAcquisitionService(
        store=AcquisitionStore(tmp_path / "acquisition.db"),
        discovery_provider=provider,
        converter=SimpleNamespace(),
        validator=SimpleNamespace(),
        downloader=SimpleNamespace(),
        policy=AcquisitionPolicy(),
        artifact_dir=tmp_path / "artifacts",
        converted_dir=tmp_path / "converted",
        symbol_catalog_path=path,
        now=lambda: NOW,
    )


def test_discovery_takes_display_metadata_from_the_catalog_and_never_defaults_hose(tmp_path):
    service = _service(tmp_path, _provider([ANNUAL]))
    outcome = service.discover(_query())
    [candidate] = outcome.candidates
    assert outcome.status == "results" and outcome.ticker_in_catalog is True
    assert candidate.exchange == "HNX" and candidate.company.startswith("Công ty Cổ phần Mĩ thuật")
    assert service.store.get_candidate(candidate.candidate_id, owner_id="owner",
                                       session_id="session", now=NOW).title == ANNUAL["Name"]

    offline = _service(tmp_path / "no-catalog", _provider([ANNUAL]), catalog=False).discover(_query())
    assert offline.candidates[0].exchange == "" and "catalog_unavailable" in offline.warnings
    stale = _service(tmp_path / "stale", _provider([]), catalog_age=timedelta(days=2)).discover(_query())
    assert stale.status == "no_results" and "catalog_stale" in stale.warnings


def test_provider_errors_surface_with_their_code(tmp_path):
    service = _service(tmp_path, _provider(payload={"Success": False}))
    with pytest.raises(ReportDiscoveryUnavailable) as error:
        service.discover(_query())
    assert error.value.code == "provider_rejected"


def test_subject_resolution_never_guesses(tmp_path):
    service = _service(tmp_path, _provider())
    assert [s.ticker for s in service.resolve_subject("Hòa Phát").matches] == ["HPG"]
    assert service.resolve_subject("công ty cổ phần").status == "ambiguous"
    assert service.resolve_subject("XYZ").status == "not_in_catalog"
    assert service.resolve_subject("Không tồn tại gì cả").status == "not_found"

    offline = _service(tmp_path / "offline", _provider(), catalog=False)
    assert offline.resolve_subject("Hòa Phát").status == "catalog_unavailable"
    resolved = offline.resolve_subject("hpg")
    assert resolved.status == "resolved" and resolved.matches[0].exchange == ""
