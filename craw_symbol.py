from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_URL = "https://cafef.vn/du-lieu/screener.aspx"
DEFAULT_OUTPUT = "cafef_stocks.csv"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/123.0 Safari/537.36"
)

EXCHANGE_MAP = {
    "HSX": "HSX",
    "HOSE": "HSX",
    "HNX": "HNX",
    "UPCOM": "UpCom",
}


# ============================================================
# 1. DOWNLOAD HTML
# ============================================================

def download_html(
    url: str,
    timeout: int = 30,
    retries: int = 3,
) -> str:
    """
    Tai HTML tu URL voi retry.

    Retry khi gap:
    - HTTP 429
    - HTTP 5xx
    - URLError
    """

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,"
            "application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "vi,en-US;q=0.9,en;q=0.8",
        "Connection": "close",
    }

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        request = Request(url, headers=headers)

        try:
            with urlopen(request, timeout=timeout) as response:
                charset = (
                    response.headers.get_content_charset()
                    or "utf-8"
                )

                return response.read().decode(
                    charset,
                    errors="replace",
                )

        except HTTPError as exc:
            last_error = exc

            # Retry cho rate limit hoặc server error
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt < retries:
                    wait_time = 2 ** (attempt - 1)

                    print(
                        f"HTTP {exc.code}. "
                        f"Thu lai sau {wait_time}s "
                        f"({attempt}/{retries})..."
                    )

                    time.sleep(wait_time)
                    continue

            raise RuntimeError(
                f"Khong tai duoc CafeF: HTTP {exc.code}"
            ) from exc

        except URLError as exc:
            last_error = exc

            if attempt < retries:
                wait_time = 2 ** (attempt - 1)

                print(
                    f"Loi ket noi: {exc.reason}. "
                    f"Thu lai sau {wait_time}s "
                    f"({attempt}/{retries})..."
                )

                time.sleep(wait_time)
                continue

            raise RuntimeError(
                f"Khong ket noi duoc toi CafeF: {exc.reason}"
            ) from exc

        except TimeoutError as exc:
            last_error = exc

            if attempt < retries:
                wait_time = 2 ** (attempt - 1)

                print(
                    f"Timeout. "
                    f"Thu lai sau {wait_time}s "
                    f"({attempt}/{retries})..."
                )

                time.sleep(wait_time)
                continue

            raise RuntimeError(
                "Ket noi CafeF bi timeout."
            ) from exc

    raise RuntimeError(
        f"Khong the tai URL: {url}"
    ) from last_error


# ============================================================
# 2. EXTRACT JSON OBJECT AN TOAN
# ============================================================

def extract_balanced_json_array(
    text: str,
    start: int,
) -> str:
    """
    Lay mot JSON array bat dau tai vi tri start.

    Khac voi regex [.*?], ham nay theo doi:
    - []
    - {}
    - string
    - escape character

    Nen an toan hon khi JSON co array/object long nhau.
    """

    if start >= len(text) or text[start] != "[":
        raise ValueError(
            "Vi tri bat dau khong phai JSON array."
        )

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
                return text[start:i + 1]

    raise ValueError(
        "Khong tim thay dau ] ket thuc JSON array."
    )


def extract_json_data(
    html: str,
) -> list[dict[str, Any]]:
    """
    Tim jsonData trong HTML CafeF.

    Ho tro:
        var jsonData = [...]
        let jsonData = [...]
        const jsonData = [...]
    """

    # Cho phep var / let / const
    pattern = re.compile(
        r"(?:var|let|const)\s+jsonData\s*=\s*",
        flags=re.IGNORECASE,
    )

    match = pattern.search(html)

    if not match:
        raise RuntimeError(
            "Khong tim thay bien jsonData."
        )

    array_start = html.find(
        "[",
        match.end(),
    )

    if array_start == -1:
        raise RuntimeError(
            "Tim thay jsonData nhung khong tim thay JSON array."
        )

    try:
        json_text = extract_balanced_json_array(
            html,
            array_start,
        )

    except ValueError as exc:
        raise RuntimeError(
            f"Khong tach duoc JSON array: {exc}"
        ) from exc

    # JavaScript co the co NaN
    json_text = re.sub(
        r"(?<![\w.])NaN(?![\w.])",
        "null",
        json_text,
    )

    try:
        data = json.loads(json_text)

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Khong parse duoc jsonData: {exc}"
        ) from exc

    if not isinstance(data, list):
        raise RuntimeError(
            "jsonData khong phai la list."
        )

    return [
        item
        for item in data
        if isinstance(item, dict)
    ]


# ============================================================
# 3. HTML TABLE FALLBACK
# ============================================================

class CafeFTableParser(HTMLParser):
    """
    Parser HTML bang standard library.

    Khong can BeautifulSoup.
    """

    def __init__(self) -> None:
        super().__init__()

        self.rows: list[list[str]] = []

        self.in_table = False
        self.in_row = False
        self.in_cell = False

        self.current_row: list[str] = []
        self.current_cell: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:

        tag = tag.lower()

        if tag == "table":
            self.in_table = True

        elif tag == "tr" and self.in_table:
            self.in_row = True
            self.current_row = []

        elif (
            tag in ("td", "th")
            and self.in_row
        ):
            self.in_cell = True
            self.current_cell = []

    def handle_endtag(
        self,
        tag: str,
    ) -> None:

        tag = tag.lower()

        if (
            tag in ("td", "th")
            and self.in_cell
        ):
            text = "".join(
                self.current_cell
            )

            text = re.sub(
                r"\s+",
                " ",
                text,
            ).strip()

            self.current_row.append(text)

            self.current_cell = []
            self.in_cell = False

        elif tag == "tr" and self.in_row:

            if self.current_row:
                self.rows.append(
                    self.current_row
                )

            self.current_row = []
            self.in_row = False

        elif tag == "table":
            self.in_table = False

    def handle_data(
        self,
        data: str,
    ) -> None:

        if self.in_cell:
            self.current_cell.append(data)


def extract_stocks_from_html(
    html: str,
) -> list[dict[str, Any]]:
    """
    Fallback parser.

    CafeF hien thi bang theo dang:

    STT | Ten cong ty | Ma | San | ...

    Ham nay tim cac dong co:
    - cot STT
    - cot ten cong ty
    - cot ma
    - cot san
    """

    parser = CafeFTableParser()
    parser.feed(html)

    stocks: list[dict[str, Any]] = []

    symbol_pattern = re.compile(
        r"^[A-Z0-9]{1,10}$"
    )

    exchange_values = {
        "HSX",
        "HOSE",
        "HNX",
        "UPCOM",
        "UPCOM",
        "UPCOM",
        "UpCom",
    }

    for row in parser.rows:

        if len(row) < 4:
            continue

        # CafeF hien tai:
        # STT | Ten cong ty | Ma | San | ...
        stt = row[0].strip()
        name = row[1].strip()
        symbol = row[2].strip().upper()
        exchange = row[3].strip()

        # Kiem tra STT
        if not stt.isdigit():
            continue

        # Kiem tra ma
        if not symbol_pattern.match(symbol):
            continue

        # Kiem tra san
        if exchange.upper() not in {
            "HSX",
            "HOSE",
            "HNX",
            "UPCOM",
        }:
            continue

        stocks.append(
            {
                "Symbol": symbol,
                "Name": name,
                "CenterName": exchange,
            }
        )

    if not stocks:
        raise RuntimeError(
            "Khong tim thay du lieu co phieu trong HTML table."
        )

    return stocks


# ============================================================
# 4. GET STOCK INFORMATION
# ============================================================

def first_non_empty(
    row: dict[str, Any],
    keys: list[str],
) -> str:
    """
    Lay gia tri dau tien khong rong.
    """

    for key in keys:
        value = str(
            row.get(key, "")
        ).strip()

        if value:
            return value

    return ""


def normalize_exchange(
    exchange: str,
) -> str:
    """
    Chuan hoa ten san giao dich.
    """

    exchange = exchange.strip()

    if not exchange:
        return ""

    normalized = EXCHANGE_MAP.get(
        exchange.upper()
    )

    return normalized or exchange


def get_stocks_info(
    rows: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """
    Lay:
        symbol
        name
        exchange

    va loai bo duplicate symbol.
    """

    stocks: list[dict[str, str]] = []
    seen: set[str] = set()

    for row in rows:

        symbol = first_non_empty(
            row,
            [
                "Symbol",
                "symbol",
                "Code",
                "code",
            ],
        ).upper()

        name = first_non_empty(
            row,
            [
                "Name",
                "CompanyName",
                "FullName",
                "OrganName",
                "name",
            ],
        )

        exchange = first_non_empty(
            row,
            [
                "CenterName",
                "Exchange",
                "Floor",
                "San",
                "exchange",
            ],
        )

        exchange = normalize_exchange(
            exchange
        )

        # Normalize ten
        name = re.sub(
            r"\s+",
            " ",
            name,
        ).strip()

        # Bo qua row khong co symbol
        if not symbol:
            continue

        # Bo duplicate
        if symbol in seen:
            continue

        stocks.append(
            {
                "symbol": symbol,
                "name": (
                    name
                    if name
                    else "N/A"
                ),
                "exchange": (
                    exchange
                    if exchange
                    else "N/A"
                ),
            }
        )

        seen.add(symbol)

    return stocks


# ============================================================
# 5. VALIDATION / STATISTICS
# ============================================================

def validate_stocks(
    stocks: list[dict[str, str]],
) -> dict[str, int]:
    """
    Thong ke chat luong du lieu.
    """

    missing_name = sum(
        1
        for stock in stocks
        if stock["name"] == "N/A"
    )

    missing_exchange = sum(
        1
        for stock in stocks
        if stock["exchange"] == "N/A"
    )

    return {
        "total": len(stocks),
        "missing_name": missing_name,
        "missing_exchange": missing_exchange,
    }


# ============================================================
# 6. WRITE OUTPUT
# ============================================================

def write_output(
    stocks: list[dict[str, str]],
    output_path: Path,
    output_format: str,
) -> None:
    """
    Ghi output:
        csv
        txt
        json
    """

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if output_format == "csv":

        with output_path.open(
            "w",
            encoding="utf-8-sig",
            newline="",
        ) as file:

            writer = csv.writer(file)

            writer.writerow(
                [
                    "MÃ CHỨNG KHOÁN",
                    "TÊN CÔNG TY",
                    "SÀN GIAO DỊCH",
                ]
            )

            writer.writerows(
                [
                    stock["symbol"],
                    stock["name"],
                    stock["exchange"],
                ]
                for stock in stocks
            )

        return

    if output_format == "txt":

        with output_path.open(
            "w",
            encoding="utf-8",
        ) as file:

            file.write(
                f"{'MÃ':<10}"
                f"{'SÀN':<10}"
                f"TÊN CÔNG TY\n"
            )

            file.write(
                "-" * 90 + "\n"
            )

            for stock in stocks:

                file.write(
                    f"{stock['symbol']:<10}"
                    f"{stock['exchange']:<10}"
                    f"{stock['name']}\n"
                )

        return

    if output_format == "json":

        output_path.write_text(
            json.dumps(
                stocks,
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        return

    raise ValueError(
        f"Dinh dang khong ho tro: {output_format}"
    )


# ============================================================
# 7. ARGUMENTS
# ============================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=(
            "Crawl ma co phieu, ten cong ty "
            "va san giao dich tu CafeF."
        )
    )

    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=(
            "URL can crawl. "
            f"Mac dinh: {DEFAULT_URL}"
        ),
    )

    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=(
            "File output. "
            f"Mac dinh: {DEFAULT_OUTPUT}"
        ),
    )

    parser.add_argument(
        "-f",
        "--format",
        choices=(
            "csv",
            "txt",
            "json",
        ),
        default="csv",
        help="Dinh dang output.",
    )

    parser.add_argument(
        "--print",
        action="store_true",
        help="In ket qua ra terminal.",
    )

    parser.add_argument(
        "--only-symbols",
        action="store_true",
        help="Chi output ma co phieu.",
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="So lan retry HTTP. Mac dinh: 3.",
    )

    return parser.parse_args()


# ============================================================
# 8. MAIN
# ============================================================

def main() -> int:

    args = parse_args()

    try:

        # ----------------------------------------------------
        # Download
        # ----------------------------------------------------

        print(
            f"Dang tai du lieu tu:\n{args.url}"
        )

        html = download_html(
            args.url,
            retries=max(1, args.retries),
        )

        print(
            f"Da tai {len(html):,} ky tu HTML."
        )

        # ----------------------------------------------------
        # Parse
        # ----------------------------------------------------

        rows: list[dict[str, Any]]

        try:

            print(
                "Dang tim jsonData..."
            )

            rows = extract_json_data(
                html
            )

            print(
                f"Tim thay {len(rows):,} records tu jsonData."
            )

            source = "jsonData"

        except RuntimeError as json_error:

            print(
                "Khong parse duoc jsonData."
            )

            print(
                f"Ly do: {json_error}"
            )

            print(
                "Chuyen sang fallback HTML table..."
            )

            rows = extract_stocks_from_html(
                html
            )

            print(
                f"Tim thay {len(rows):,} records tu HTML table."
            )

            source = "html_table"

        # ----------------------------------------------------
        # Normalize
        # ----------------------------------------------------

        stocks = get_stocks_info(
            rows
        )

        if not stocks:
            raise RuntimeError(
                "Khong tim thay co phieu hop le."
            )

        # ----------------------------------------------------
        # Validate
        # ----------------------------------------------------

        stats = validate_stocks(
            stocks
        )

        print()
        print(
            "===== THONG KE ====="
        )

        print(
            f"Nguon du lieu     : {source}"
        )

        print(
            f"Tong so co phieu  : {stats['total']:,}"
        )

        print(
            f"Thieu ten cong ty : {stats['missing_name']:,}"
        )

        print(
            f"Thieu san         : {stats['missing_exchange']:,}"
        )

        # ----------------------------------------------------
        # ONLY SYMBOLS
        # ----------------------------------------------------

        if args.only_symbols:

            symbols = [
                stock["symbol"]
                for stock in stocks
            ]

            output_path = Path(
                args.output
            )

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            if args.format == "csv":

                with output_path.open(
                    "w",
                    encoding="utf-8-sig",
                    newline="",
                ) as file:

                    writer = csv.writer(
                        file
                    )

                    writer.writerow(
                        ["MÃ"]
                    )

                    writer.writerows(
                        [symbol]
                        for symbol in symbols
                    )

            elif args.format == "txt":

                output_path.write_text(
                    "\n".join(symbols)
                    + "\n",
                    encoding="utf-8",
                )

            elif args.format == "json":

                output_path.write_text(
                    json.dumps(
                        symbols,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )

            output_data = symbols

        # ----------------------------------------------------
        # NORMAL OUTPUT
        # ----------------------------------------------------

        else:

            output_data = stocks

            write_output(
                stocks,
                Path(args.output),
                args.format,
            )

        # ----------------------------------------------------
        # PRINT
        # ----------------------------------------------------

        if args.print:

            print()
            print(
                f"{'MA':<10}"
                f"{'SAN':<10}"
                f"TEN CONG TY"
            )

            print(
                "-" * 90
            )

            if args.only_symbols:

                for symbol in output_data:
                    print(symbol)

            else:

                for stock in output_data:

                    name = stock["name"]

                    if len(name) > 65:
                        name = (
                            name[:65]
                            + "..."
                        )

                    print(
                        f"{stock['symbol']:<10}"
                        f"{stock['exchange']:<10}"
                        f"{name}"
                    )

        print()
        print(
            "===================================="
        )

        print(
            f"Da crawl thanh cong: "
            f"{len(output_data):,} co phieu"
        )

        print(
            f"File output: {args.output}"
        )

        print(
            "===================================="
        )

        return 0

    except (
        RuntimeError,
        OSError,
        ValueError,
    ) as exc:

        print(
            f"\nLoi: {exc}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
