"""Checks, in code, of what the model says about an event.

- A title or explanation sentence stands only if the event's verified quotes
  (and its articles' own titles) support it: every number and proper name in
  it appears there, and most of its content words do. Otherwise the title
  falls back to the article's title and the sentence is dropped.
- An event date is kept only if the article text states that date.
- Related metrics are the financial terms the quotes themselves contain.
- The confirmation level follows fixed source rules, not the model's reading.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

from agentfinx.news.collect.dates import VIETNAM_TZ, parse_published
from agentfinx.news.collect.topics import _STOP, fold

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*\s*%?")
_SENTENCE = re.compile(r"(?<=[.!?;])\s+")
# Capitalized words after the first word of a sentence: names of people,
# companies, places. "Vinamilk", "Fortune", "Campuchia".
_NAME = re.compile(r"(?<![\w])([A-ZĐÂĂÊÔƠƯÁÀẢÃẠÉÈẺẼẸÍÌỈĨỊÓÒỎÕỌÚÙỦŨỤÝỲỶỸỴ][\wÀ-ỹ&-]+)")
_WORD_COVERAGE = 0.6

METRICS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Doanh thu", ("doanh thu", "doanh so")),
    ("Lợi nhuận", ("loi nhuan", "lai rong", "lai sau thue", "lai truoc thue", "lo rong")),
    ("Biên lợi nhuận", ("bien loi nhuan", "bien gop")),
    ("Giá vốn", ("gia_von",)),
    ("Chi phí", ("chi phi",)),
    ("Giá nguyên liệu", ("nguyen lieu",)),
    ("Sản lượng", ("san luong",)),
    ("Xuất khẩu", ("xuat khau",)),
    ("Cổ tức", ("co_tuc",)),
    ("Vốn và cổ phiếu", ("von dieu le", "phat hanh", "co_phieu")),
    ("Nợ vay", ("no_vay", "khoan vay")),
    ("Dòng tiền", ("dong tien",)),
    ("Tồn kho", ("ton kho",)),
    ("Đầu tư và mở rộng", ("dau tu", "mo rong", "nha may")),
    ("Thị phần", ("thi phan",)),
    ("Quản trị và nhân sự", ("hdqt", "hoi dong quan tri", "tong giam doc", "ceo", "lanh dao", "bo nhiem")),
)

# (folded marker, as shown to the reader)
_RUMOR = (("tin don", "tin đồn"), ("chua duoc xac nhan", "chưa được xác nhận"), ("chua xac nhan", "chưa xác nhận"),
          ("nguon tin", "nguồn tin"), ("giau ten", "giấu tên"), ("duoc cho la", "được cho là"),
          ("theo mot so nguon", "theo một số nguồn"), ("chua chinh thuc", "chưa chính thức"))
_FORECAST = (("du kien", "dự kiến"), ("ke hoach", "kế hoạch"), ("muc tieu", "mục tiêu"), ("ky vong", "kỳ vọng"),
             ("du bao", "dự báo"), ("uoc tinh", "ước tính"), ("se ", "sẽ"))
_ATTRIBUTION = (("cong bo", "công bố"), ("cho biet", "cho biết"), ("theo bao cao", "theo báo cáo"),
                ("nghi quyet", "nghị quyết"), ("thong bao", "thông báo"), ("theo thong tin tu", "theo thông tin từ"),
                ("dai dien", "đại diện"), ("chia se", "chia sẻ"), ("khang dinh", "khẳng định"),
                ("bao cao ket qua", "báo cáo kết quả"), ("xac nhan", "xác nhận"))


def _numbers(text: str) -> set[str]:
    return {re.sub(r"[\s.,]", "", item) for item in _NUMBER.findall(str(text or ""))}


def _names(sentence: str) -> set[str]:
    words = str(sentence or "").split()
    names = set()
    for index, word in enumerate(words):
        match = _NAME.match(word)
        if match and index > 0:
            names.add(fold(match.group(1)))
    return {name for name in names if name}


def _content_words(text: str) -> list[str]:
    return [token for token in fold(text).split() if token not in _STOP and len(token) > 1 and not token.isdigit()]


def supported(statement: str, evidence: Iterable[str]) -> bool:
    """Every number and name of ``statement`` is in ``evidence``, and most
    of its content words are."""

    joined = " ".join(evidence)
    if not _numbers(statement) <= _numbers(joined):
        return False
    folded_evidence = f" {fold(joined)} "
    if any(f" {name} " not in folded_evidence for name in _names(statement)):
        return False
    words = _content_words(statement)
    if not words:
        return True
    present = set(folded_evidence.split())
    return sum(1 for word in words if word in present) / len(words) >= _WORD_COVERAGE


def supported_sentences(text: str, evidence: list[str]) -> tuple[str, int]:
    """The sentences of ``text`` the evidence supports, and how many were dropped."""

    sentences = [part.strip() for part in _SENTENCE.split(" ".join(str(text or "").split())) if part.strip()]
    kept = [sentence for sentence in sentences if supported(sentence, evidence)]
    return " ".join(kept), len(sentences) - len(kept)


def stated_date(value: str, texts: Iterable[str]) -> str:
    """``value`` (YYYY-MM-DD) if one of the texts writes that date (d/m/yyyy,
    d/m or "ngày d tháng m"); otherwise ""."""

    try:
        day = date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return ""
    forms = (
        rf"(?<!\d)0?{day.day}[/-]0?{day.month}[/-]{day.year}(?!\d)",
        rf"(?<!\d)0?{day.day}[/-]0?{day.month}(?![/\d-])",
        rf"\bngay 0?{day.day} thang 0?{day.month}\b",
    )
    for text in texts:
        raw = str(text or "")
        folded = fold(raw)
        if re.search(forms[0], raw) or re.search(forms[1], raw) or re.search(forms[2], folded):
            return day.isoformat()
    return ""


def related_metrics(quotes: Iterable[str], question: str = "") -> list[str]:
    """Financial terms the quotes contain, those the question also names first."""

    quoted = f" {fold(' '.join(quotes))} "
    asked = f" {fold(question)} "
    found = [name for name, patterns in METRICS if any(f" {pattern}" in quoted for pattern in patterns)]
    return sorted(found, key=lambda name: not any(
        f" {pattern}" in asked for item, patterns in METRICS if item == name for pattern in patterns))


def confirmation(ticker: str, titles: list[str], quotes: list[str]) -> tuple[str, str]:
    """(level, basis) by fixed rules on the source, strongest first."""

    for title in titles:
        if ticker and re.match(rf"^\s*{re.escape(ticker)}\s*:", title, re.IGNORECASE):
            return ("official_disclosure",
                    f"Bài “{title}” là tin công bố thông tin của {ticker}, được Vietstock đăng lại.")
    text = f" {fold(' '.join(quotes))} "
    for level, markers, note in (
        ("unconfirmed", _RUMOR, "Trích dẫn ghi nhận đây là thông tin chưa xác nhận"),
        ("forecast", _FORECAST, "Trích dẫn nói về kế hoạch, dự kiến hoặc dự báo"),
        ("attributed_report", _ATTRIBUTION, "Trích dẫn nêu nguồn công bố hoặc phát ngôn"),
    ):
        for marker, shown in markers:
            if f" {marker}" in text:
                return level, f"{note} (có cụm “{shown}”)."
    if not quotes:
        return "unattributed", "Không có trích dẫn khớp nguyên văn với bài gốc."
    return "unattributed", "Trích dẫn không nêu nguồn công bố hay phát ngôn."


def reported_at(published: Iterable[str]) -> str:
    moments = [moment for value in published if (moment := parse_published(value)) is not None]
    return min(moments).astimezone(VIETNAM_TZ).date().isoformat() if moments else ""


def vn_day(value: str) -> Optional[str]:
    moment = parse_published(value)
    return moment.astimezone(VIETNAM_TZ).strftime("%d/%m/%Y") if moment else None
