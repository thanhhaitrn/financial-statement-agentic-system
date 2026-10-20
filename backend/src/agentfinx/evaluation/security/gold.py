"""Gold facts read from a report's own statement tables, with their source.

A gold fact names the statement (income, balance, cash flow), the row code
(mã số) and the column, and is read from the converted report Markdown that
the dataset was built from. Its source record is enough to find it again:
file, SHA-256, line, page (the page footer that follows the row, when the
conversion kept one), table heading, column heading, row label and the row
text itself. :func:`check_source` re-reads the file and confirms all of it,
so a gold value can never drift from the report.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

STATEMENT_TITLES = {
    "income": ("KẾT QUẢ HOẠT ĐỘNG KINH DOANH",),
    "balance": ("CÂN ĐỐI KẾ TOÁN", "BÁO CÁO TÌNH HÌNH TÀI CHÍNH"),
    "cashflow": ("LƯU CHUYỂN TIỀN TỆ",),
}
_NUMBER = re.compile(r"^\(?-?\d{1,3}(?:\.\d{3})*(?:,\d+)?\)?$")


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _clean(cell: str) -> str:
    text = cell.replace("\\*", "").replace("*", "").replace("<br/>", " ")
    return " ".join(text.split())


def parse_amount(text: str) -> Decimal | None:
    value = _clean(text)
    if not value or not _NUMBER.match(value):
        return None
    negative = value.startswith("(") or value.startswith("-")
    digits = value.strip("()-").replace(".", "").replace(",", ".")
    amount = Decimal(digits)
    return -amount if negative else amount


def _cells(line: str) -> list[str]:
    return [_clean(cell) for cell in line.strip().strip("|").split("|")]


@dataclass
class Row:
    statement: str
    table_title: str
    line: int            # 1-based line in the Markdown file
    page: int | None
    label: str
    code: str
    header: list[str]
    cells: list[str]
    text: str

    def amounts(self) -> list[tuple[str, Decimal]]:
        """(column heading, amount) for every numeric cell of the row."""

        output = []
        for index, cell in enumerate(self.cells):
            amount = parse_amount(cell)
            if amount is not None and cell != self.code:
                heading = self.header[index] if index < len(self.header) else f"col{index}"
                output.append((heading, amount))
        return output


def _page_after(lines: list[str], index: int) -> int | None:
    """Prefer explicit conversion page markers, else the following footer."""

    for line in reversed(lines[:index + 1]):
        marker = re.fullmatch(r"\s*-+\s*Page\s+(\d+)\s*", line, re.I)
        if marker:
            return int(marker.group(1))

    for line in lines[index + 1:index + 150]:
        if re.fullmatch(r"\s*\d{1,3}\s*", line):
            return int(line)
    return None


def _fold(text: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn").replace("đ", "d")


def find_rows(path: str | Path, statement: str, code: str, label: str = "") -> list[Row]:
    """Rows with mã số ``code`` (and ``label`` in their label) in the
    ``statement`` tables; when the conversion lost the statement's title,
    rows with that code and label anywhere outside the notes."""

    rows = [row for row in _scan(path, statement, code) if _fold(label) in _fold(row.label)]
    if rows or not label:
        return rows
    return [row for row in _scan(path, None, code) if _fold(label) in _fold(row.label)]


def _scan(path: str | Path, statement: str | None, code: str) -> list[Row]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    current, title, header = "", "", []
    found = []
    for index, line in enumerate(lines):
        upper = line.upper()
        if not line.lstrip().startswith("|"):
            for name, keys in STATEMENT_TITLES.items():
                if any(key in upper for key in keys) and len(line) < 160 and "THUYẾT MINH" not in upper:
                    current, title = name, line.strip("# *").strip()
            header = []
            continue
        cells = _cells(line)
        if not header:
            header = cells
            continue
        if set("".join(cells)) <= set("-: "):
            continue
        if statement is not None and current != statement:
            continue
        if statement is None and "THUYẾT MINH" in title.upper():
            continue
        if code in cells:
            label = next((cell for cell in cells if cell and cell != code and parse_amount(cell) is None
                          and not re.fullmatch(r"[IVX]+\.\d+|\d+(\.\d+)?|V\.\d+[a-z]?", cell)), "")
            found.append(Row(statement=statement or "untitled", table_title=title or "(bảng không có tiêu đề)",
                             line=index + 1, page=_page_after(lines, index),
                             label=label, code=code, header=header, cells=cells, text=line.strip()))
    return found


def source_record(path: str | Path, root: str | Path, row: Row, column: str, value: Decimal) -> dict:
    return {
        "file": str(Path(path).resolve().relative_to(Path(root).resolve())),
        "sha256": sha256_file(path),
        "line": row.line,
        "page": row.page,
        "statement": row.statement,
        "table": row.table_title,
        "code": row.code,
        "row_label": row.label,
        "column": column,
        "excerpt": row.text,
        "value": str(value),
    }


def check_source(source: dict, root: str | Path) -> list[str]:
    """Problems found re-reading a gold fact's source; empty when it holds."""

    path = Path(root) / source["file"]
    if not path.is_file():
        return [f"missing source file {source['file']}"]
    problems = []
    if sha256_file(path) != source["sha256"]:
        problems.append(f"{source['file']}: SHA-256 changed")
    lines = path.read_text(encoding="utf-8").splitlines()
    line = lines[source["line"] - 1] if 0 < source["line"] <= len(lines) else ""
    if line.strip() != source["excerpt"]:
        problems.append(f"{source['file']}:{source['line']}: row text changed")
    rows = [row for row in find_rows(path, source["statement"], source["code"], source.get("label_key", ""))
            if row.line == source["line"]]
    amounts = dict(rows[0].amounts()) if rows else {}
    if rows:
        row = rows[0]
        for field, actual in (("page", row.page), ("table", row.table_title), ("row_label", row.label)):
            if source.get(field) != actual:
                problems.append(f"{source['file']}:{source['line']}: {field} differs from the source")
    if str(amounts.get(source["column"])) != source["value"]:
        problems.append(f"{source['file']}:{source['line']}: {source['column']} is {amounts.get(source['column'])}, "
                        f"not {source['value']}")
    return problems


def report_amounts(path: str | Path) -> set[Decimal]:
    """Every amount written anywhere in the report, for collision checks."""

    text = Path(path).read_text(encoding="utf-8")
    amounts = set()
    for match in re.finditer(r"\(?-?\d{1,3}(?:\.\d{3})+(?:,\d+)?\)?", text):
        amount = parse_amount(match.group(0))
        if amount is not None:
            amounts.add(abs(amount))
    return amounts
