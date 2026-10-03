"""
3 hàm crawl thật, chuyển thể từ 3 script gốc của bạn (Cafef PDF downloader,
Cafef stock-list crawler, Vietstock news crawler). Đây là lớp "provider"
theo interfaces.py: chỉ lấy dữ liệu thô, KHÔNG tự đánh giá relevance hay
sentiment — việc đó do model quyết định trong pipeline.py.

Khác biệt so với script gốc:
- Bỏ toàn bộ phần CLI tương tác (input(), vòng lặp menu, print tiến độ)
  vì agent gọi các hàm này lập trình, không có người ngồi gõ phím.
- Bỏ side-effect ghi file (log, MASTER_ARTICLES.txt...) — provider chỉ trả
  dữ liệu, việc lưu trữ (nếu cần) do tầng gọi (evidence/pipeline) quyết định
  để tránh 2 nơi cùng ghi state chồng chéo.
- Giữ nguyên phần logic "lõi": gọi API/HTML, parse, trích xuất, retry.
- Mỗi hàm là sync (dùng requests/urllib/selenium như bản gốc) — provider
  bọc chúng bằng `asyncio.to_thread` để không chặn event loop (mục 8).

3 hàm:
    1. crawl_financial_reports()  — Cafef: danh sách báo cáo tài chính (PDF)
    2. crawl_stock_directory()    — Cafef: danh mục mã CK / tên DN / sàn
    3. crawl_vietstock_news()     — Vietstock: danh sách tin + nội dung bài
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
)


# =============================================================================
# 1) CRAWL FUNCTION #1 — Cafef: danh sách báo cáo tài chính (PDF)
#    Chuyển thể từ PDFDownloader.get_financial_reports (script Cafef PDF).
# =============================================================================

@dataclass
class FinancialReportItem:
    name: str
    link: str
    year: Optional[int]
    report_type: str  # parent_company | consolidated | quarterly | annual | other


_EXCHANGE_CODE_MAP = {"HOSE": 1, "HNX": 2, "UPCOM": 3}


def _is_valid_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except Exception:
        return False


def _extract_report_year(name: str) -> Optional[int]:
    match = re.search(r"(20\d{2})", name)
    return int(match.group(1)) if match else None


def _classify_report(name: str) -> str:
    name_lower = name.lower()
    if "cong ty me" in name_lower or "rieng" in name_lower:
        return "parent_company"
    if "hop nhat" in name_lower or "consolidated" in name_lower:
        return "consolidated"
    if "quy" in name_lower:
        return "quarterly"
    if "nam" in name_lower:
        return "annual"
    return "other"


def crawl_financial_reports(
    stock_code: str,
    exchange: str = "HOSE",
    *,
    timeout_s: float = 20.0,
    session=None,
) -> list[FinancialReportItem]:
    """Gọi API FileBCTC.ashx của Cafef, trả danh sách báo cáo tài chính hợp lệ.

    `session` cho phép test truyền vào một object có `.get()` giả (mock) thay
    vì gọi network thật.
    """
    if session is None:
        import requests  # import trễ để module này import được khi chưa cài requests

        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": "https://cafef.vn/",
            }
        )

    exchange_code = _EXCHANGE_CODE_MAP.get(exchange.upper(), 1)
    api_url = (
        "https://cafef.vn/du-lieu/Ajax/PageNew/FileBCTC.ashx"
        f"?Symbol={stock_code.lower()}&Type={exchange_code}&Year=0"
    )

    response = session.get(api_url, timeout=timeout_s)
    response.raise_for_status()
    data = response.json()

    if not data.get("Success", False):
        raise RuntimeError(
            f"Cafef API báo lỗi: {data.get('Message', 'không rõ lý do')}"
        )

    reports = data.get("Data", [])
    if not isinstance(reports, list):
        raise RuntimeError("Dữ liệu Cafef BCTC không đúng định dạng.")

    items: list[FinancialReportItem] = []
    for report in reports:
        link = report.get("Link")
        if not link or not _is_valid_url(link):
            continue
        name = report.get("Name", "Không có tên")
        items.append(
            FinancialReportItem(
                name=name,
                link=link,
                year=_extract_report_year(name),
                report_type=_classify_report(name),
            )
        )
    return items


# =============================================================================
# 2) CRAWL FUNCTION #2 — Cafef: danh mục mã CK / tên DN / sàn giao dịch
#    Chuyển thể từ script crawl screener Cafef (jsonData + fallback HTML table).
# =============================================================================

DEFAULT_SCREENER_URL = "https://cafef.vn/du-lieu/screener.aspx"
_EXCHANGE_NAME_MAP = {"HSX": "HSX", "HOSE": "HSX", "HNX": "HNX", "UPCOM": "UpCom"}


def _download_html(url: str, timeout_s: float = 20.0, retries: int = 3) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout_s) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                return resp.read().decode(charset, errors="replace")
        except (HTTPError, URLError) as exc:
            last_error = exc
            if attempt < retries:
                time.sleep(2 ** (attempt - 1))
                continue
            raise RuntimeError(f"Không tải được {url}: {exc}") from exc
    raise RuntimeError(f"Không tải được {url}") from last_error


def _extract_balanced_json_array(text: str, start: int) -> str:
    if start >= len(text) or text[start] != "[":
        raise ValueError("Vị trí bắt đầu không phải JSON array.")
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("Không tìm thấy dấu ] kết thúc JSON array.")


def _extract_json_data(html: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"(?:var|let|const)\s+jsonData\s*=\s*", re.IGNORECASE)
    match = pattern.search(html)
    if not match:
        raise RuntimeError("Không tìm thấy biến jsonData.")
    array_start = html.find("[", match.end())
    if array_start == -1:
        raise RuntimeError("Tìm thấy jsonData nhưng không thấy JSON array.")
    json_text = _extract_balanced_json_array(html, array_start)
    json_text = re.sub(r"(?<![\w.])NaN(?![\w.])", "null", json_text)
    data = json.loads(json_text)
    if not isinstance(data, list):
        raise RuntimeError("jsonData không phải list.")
    return [item for item in data if isinstance(item, dict)]


class _CafeFTableParser(HTMLParser):
    """Parser bảng HTML bằng standard library (không cần BeautifulSoup)."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.in_table = False
        self.in_row = False
        self.in_cell = False
        self.current_row: list[str] = []
        self.current_cell: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self.in_table = True
        elif tag == "tr" and self.in_table:
            self.in_row = True
            self.current_row = []
        elif tag in ("td", "th") and self.in_row:
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self.in_cell:
            text = re.sub(r"\s+", " ", "".join(self.current_cell)).strip()
            self.current_row.append(text)
            self.current_cell = []
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.current_row:
                self.rows.append(self.current_row)
            self.current_row = []
            self.in_row = False
        elif tag == "table":
            self.in_table = False

    def handle_data(self, data):
        if self.in_cell:
            self.current_cell.append(data)


def _extract_stocks_from_html(html: str) -> list[dict[str, Any]]:
    parser = _CafeFTableParser()
    parser.feed(html)
    symbol_pattern = re.compile(r"^[A-Z0-9]{1,10}$")
    stocks: list[dict[str, Any]] = []
    for row in parser.rows:
        if len(row) < 4:
            continue
        stt, name, symbol, exchange = row[0].strip(), row[1].strip(), row[2].strip().upper(), row[3].strip()
        if not stt.isdigit() or not symbol_pattern.match(symbol):
            continue
        if exchange.upper() not in {"HSX", "HOSE", "HNX", "UPCOM"}:
            continue
        stocks.append({"Symbol": symbol, "Name": name, "CenterName": exchange})
    if not stocks:
        raise RuntimeError("Không tìm thấy dữ liệu cổ phiếu trong HTML table.")
    return stocks


def _first_non_empty(row: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = str(row.get(key, "")).strip()
        if value:
            return value
    return ""


def _normalize_exchange(exchange: str) -> str:
    exchange = exchange.strip()
    if not exchange:
        return ""
    return _EXCHANGE_NAME_MAP.get(exchange.upper(), exchange)


@dataclass
class StockDirectoryItem:
    symbol: str
    name: str
    exchange: str


def crawl_stock_directory(
    url: str = DEFAULT_SCREENER_URL,
    *,
    retries: int = 3,
    html: Optional[str] = None,
) -> list[StockDirectoryItem]:
    """Crawl danh mục mã CK/tên DN/sàn từ Cafef screener.

    `html` cho phép test truyền sẵn nội dung HTML (mock) thay vì tải mạng.
    Dùng để RESOLVE company_name <-> ticker trước khi gọi search tin tức
    (mục 3: xác định doanh nghiệp mục tiêu).
    """
    raw_html = html if html is not None else _download_html(url, retries=retries)

    try:
        rows = _extract_json_data(raw_html)
    except RuntimeError:
        rows = _extract_stocks_from_html(raw_html)

    seen: set[str] = set()
    items: list[StockDirectoryItem] = []
    for row in rows:
        symbol = _first_non_empty(row, ["Symbol", "symbol", "Code", "code"]).upper()
        if not symbol or symbol in seen:
            continue
        name = re.sub(
            r"\s+",
            " ",
            _first_non_empty(row, ["Name", "CompanyName", "FullName", "OrganName", "name"]),
        ).strip()
        exchange = _normalize_exchange(
            _first_non_empty(row, ["CenterName", "Exchange", "Floor", "San", "exchange"])
        )
        items.append(StockDirectoryItem(symbol=symbol, name=name or "N/A", exchange=exchange or "N/A"))
        seen.add(symbol)
    return items


def resolve_company(
    query: str, directory: list[StockDirectoryItem]
) -> Optional[StockDirectoryItem]:
    """Map tên công ty hoặc mã CK (không phân biệt hoa/thường, khớp gần
    đúng theo substring) -> StockDirectoryItem. Trả None nếu không chắc
    chắn — code KHÔNG đoán bừa doanh nghiệp mục tiêu (mục 3)."""
    q = query.strip().upper()
    if not q:
        return None
    # Khớp chính xác theo mã trước
    for item in directory:
        if item.symbol == q:
            return item
    # Khớp theo tên công ty (substring, phải khá đặc trưng để tránh nhầm)
    candidates = [item for item in directory if q.lower() in item.name.lower()]
    if len(candidates) == 1:
        return candidates[0]
    return None  # 0 hoặc >1 kết quả -> để pipeline hỏi lại thay vì đoán


# =============================================================================
# 3) CRAWL FUNCTION #3 — Vietstock: danh sách tin + nội dung bài
#    Chuyển thể từ VietstockCrawler (script Selenium). Import selenium trễ
#    để agent_web không bắt buộc phải cài selenium nếu không dùng provider này.
# =============================================================================

@dataclass
class NewsHit:
    title: str
    link: str
    index: int


def extract_date_from_url(url: str) -> Optional[date]:
    """Vietstock nhúng ngày đăng vào path URL (/YYYY/MM/DD/...). Dùng để
    lọc theo date_from/date_to ngay ở bước search, trước khi fetch toàn văn."""
    try:
        parts = url.split("/")
        for i, part in enumerate(parts):
            if part.isdigit() and len(part) == 4 and i + 1 < len(parts) and parts[i + 1].isdigit():
                year, month = int(part), int(parts[i + 1])
                if i + 2 < len(parts) and parts[i + 2].isdigit() and len(parts[i + 2]) == 2:
                    return date(year, month, int(parts[i + 2]))
        return None
    except Exception:
        return None


def _get_driver(headless: bool = True):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options

    options = Options()
    if headless:
        options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-images")
    options.add_argument(f"user-agent={USER_AGENT}")
    return webdriver.Chrome(options=options)


def crawl_vietstock_news(
    stock_code: str,
    *,
    target_count: int = 10,
    headless: bool = True,
    timeout_s: float = 30.0,
) -> list[NewsHit]:
    """Mở trang tin Vietstock của mã CK, bấm 'xem thêm' tới khi đủ
    `target_count` tin, trả danh sách (title, link) — KHÔNG tải nội dung
    chi tiết (đó là việc của crawl_vietstock_article, tách riêng để
    ArticleFetcher chỉ fetch những URL model thực sự cần)."""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    url = f"https://finance.vietstock.vn/{stock_code}/tin-tuc-su-kien.htm"
    driver = _get_driver(headless=headless)
    news_selectors = [
        "a.stock-news__title[target='_blank']",
        "a.stock-news__title",
        "tbody.stock-news__table-body a",
    ]
    see_more_selectors = [
        "span.stock-news__button-see-more",
        "button.load-more",
        "[class*='see-more']",
    ]
    try:
        driver.set_page_load_timeout(timeout_s)
        driver.get(url)

        selector = news_selectors[0]
        for candidate in news_selectors:
            if driver.find_elements(By.CSS_SELECTOR, candidate):
                selector = candidate
                break

        while len(driver.find_elements(By.CSS_SELECTOR, selector)) < target_count:
            btn = None
            for btn_selector in see_more_selectors:
                try:
                    btn = WebDriverWait(driver, 1).until(
                        EC.element_to_be_clickable((By.CSS_SELECTOR, btn_selector))
                    )
                    break
                except Exception:
                    continue
            if btn is None:
                break
            driver.execute_script("arguments[0].scrollIntoView();arguments[0].click();", btn)

        links = driver.find_elements(By.CSS_SELECTOR, selector)
        hits: list[NewsHit] = []
        for i, link in enumerate(links[:target_count]):
            title = link.text.strip()
            href = link.get_attribute("href")
            if title and href:
                hits.append(NewsHit(title=title, link=href, index=i + 1))
        return hits
    finally:
        driver.quit()


def crawl_vietstock_article(
    url: str, *, headless: bool = True, timeout_s: float = 15.0
) -> dict[str, Any]:
    """Tải nội dung chi tiết 1 bài viết Vietstock (title, content, publish_date)."""
    from selenium.webdriver.common.by import By

    driver = _get_driver(headless=headless)
    try:
        driver.set_page_load_timeout(timeout_s)
        driver.get(url)

        title = ""
        for selector in ("h1", ".article-title", ".news-title", ".title"):
            try:
                title = driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if title:
                    break
            except Exception:
                continue

        content = ""
        for selector in (".news-content", ".article-content", ".detail-content", ".content"):
            try:
                content = driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if content and len(content) > 100:
                    break
            except Exception:
                continue
        if not content:
            try:
                content = driver.find_element(By.CSS_SELECTOR, "body").text.strip()
            except Exception:
                pass

        publish_date = ""
        for selector in (".date", ".time", ".publish-date", ".news-date"):
            try:
                publish_date = driver.find_element(By.CSS_SELECTOR, selector).text.strip()
                if publish_date:
                    break
            except Exception:
                continue

        return {
            "title": title,
            "publish_date": publish_date,
            "content": content,
            "content_length": len(content),
        }
    finally:
        driver.quit()
