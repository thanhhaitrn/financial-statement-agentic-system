"""
Test 3 crawl function trong providers/real_crawlers.py — chỉ test phần
PARSE/LOGIC thuần, KHÔNG gọi network thật (mục 9: chạy được không cần
API key/network). Với crawl_financial_reports, ta mock `session.get()`.
Với crawl_stock_directory, ta truyền thẳng `html=` mẫu, không tải mạng.
crawl_vietstock_news/_article cần Selenium + trình duyệt thật nên KHÔNG unit
test ở đây — được validate qua VietstockSearchProvider/Fetcher (test riêng,
mock ở lớp real_crawlers thay vì mock Selenium).
"""

from __future__ import annotations

import json

import pytest

from agent_web.providers import real_crawlers as rc


# ---------------------------------------------------------------------------
# Crawl function #1: crawl_financial_reports
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload: dict):
        self.payload = payload
        self.last_url = None

    def get(self, url, timeout=None):
        self.last_url = url
        return _FakeResponse(self.payload)


def test_crawl_financial_reports_parses_valid_reports():
    session = _FakeSession(
        {
            "Success": True,
            "Data": [
                {"Name": "BCTC hop nhat quy 2 nam 2026", "Link": "https://cafef.vn/a.pdf"},
                {"Name": "BCTC cong ty me nam 2025", "Link": "https://cafef.vn/b.pdf"},
                {"Name": "Thieu link", "Link": ""},  # phải bị loại
                {"Name": "Link hong", "Link": "not-a-url"},  # phải bị loại
            ],
        }
    )
    reports = rc.crawl_financial_reports("FPT", "HOSE", session=session)

    assert len(reports) == 2
    assert reports[0].report_type == "consolidated"
    assert reports[0].year == 2026
    assert reports[1].report_type == "parent_company"
    assert reports[1].year == 2025
    assert "Symbol=fpt" in session.last_url
    assert "Type=1" in session.last_url  # HOSE -> 1


def test_crawl_financial_reports_raises_on_api_error():
    session = _FakeSession({"Success": False, "Message": "Mã không tồn tại"})
    with pytest.raises(RuntimeError, match="Mã không tồn tại"):
        rc.crawl_financial_reports("XXXX", "HOSE", session=session)


# ---------------------------------------------------------------------------
# Crawl function #2: crawl_stock_directory
# ---------------------------------------------------------------------------

def test_crawl_stock_directory_parses_json_data():
    html = (
        "<html><script>var jsonData = "
        + json.dumps(
            [
                {"Symbol": "FPT", "Name": "CTCP FPT", "CenterName": "HOSE"},
                {"Symbol": "vcb", "Name": "Vietcombank", "CenterName": "HOSE"},
                {"Symbol": "FPT", "Name": "Duplicate", "CenterName": "HOSE"},
            ]
        )
        + ";</script></html>"
    )
    items = rc.crawl_stock_directory(html=html)

    assert len(items) == 2  # loại duplicate theo symbol
    symbols = {i.symbol for i in items}
    assert symbols == {"FPT", "VCB"}
    fpt = next(i for i in items if i.symbol == "FPT")
    assert fpt.exchange == "HSX"  # HOSE -> chuẩn hoá thành HSX


def test_crawl_stock_directory_falls_back_to_html_table_when_no_json():
    html = """
    <table>
      <tr><td>1</td><td>CTCP FPT</td><td>FPT</td><td>HSX</td></tr>
      <tr><td>2</td><td>Vietcombank</td><td>VCB</td><td>HOSE</td></tr>
    </table>
    """
    items = rc.crawl_stock_directory(html=html)
    assert {i.symbol for i in items} == {"FPT", "VCB"}


def test_resolve_company_exact_symbol_match():
    directory = [
        rc.StockDirectoryItem(symbol="FPT", name="CTCP FPT", exchange="HSX"),
        rc.StockDirectoryItem(symbol="VCB", name="Ngan hang TMCP Ngoai Thuong", exchange="HSX"),
    ]
    assert rc.resolve_company("fpt", directory).symbol == "FPT"


def test_resolve_company_ambiguous_returns_none():
    directory = [
        rc.StockDirectoryItem(symbol="ABC", name="Cong ty Bao bi ABC", exchange="HSX"),
        rc.StockDirectoryItem(symbol="XYZ", name="Cong ty Bao bi XYZ", exchange="HSX"),
    ]
    # "Bao bi" khớp cả 2 -> không được đoán, phải trả None
    assert rc.resolve_company("Bao bi", directory) is None


def test_resolve_company_no_match_returns_none():
    directory = [rc.StockDirectoryItem(symbol="FPT", name="CTCP FPT", exchange="HSX")]
    assert rc.resolve_company("Khong ton tai", directory) is None


# ---------------------------------------------------------------------------
# extract_date_from_url (dùng bởi VietstockSearchProvider để lọc thời gian)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://finance.vietstock.vn/FPT/2026/03/15/tin-abc.htm", (2026, 3, 15)),
        ("https://finance.vietstock.vn/FPT/tin-khong-co-ngay.htm", None),
    ],
)
def test_extract_date_from_url(url, expected):
    result = rc.extract_date_from_url(url)
    if expected is None:
        assert result is None
    else:
        assert (result.year, result.month, result.day) == expected
