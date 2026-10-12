"""The publication window for a company-news question.

Deterministic on purpose: only a time expression inside the clause that asks
for news counts, so "doanh thu năm 2025 và tin gần đây" reads the news window
as the last 30 days, not 2025. Calendar days are Vietnam days (UTC+7); the
window is returned in UTC and never ends after ``as_of``.

- nothing said: the 30 days up to ``as_of``;
- only a start: from it to ``as_of``;
- only an end: the 30 days up to it;
- a reversed range, or one that starts after ``as_of``: ask again;
- a time expression it cannot read: ask again, never a silent 30 days.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from agentfinx.news.collect.dates import VIETNAM_TZ

DEFAULT_DAYS = 30


@dataclass(frozen=True)
class NewsWindow:
    date_from: datetime
    date_to: datetime
    source: str  # "default" | "explicit" | "relative"


class WindowError(ValueError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


def fold(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").replace("đ", "d").replace("Đ", "D"))
    value = "".join(char for char in value if not unicodedata.combining(char)).lower()
    return " ".join(value.split())


# "tin" as a word, but not the "tin" of "thông tin" (information).
_NEWS_WORDS = re.compile(r"(?<!thong )\btin\b|\bsu kien\b|\bbao chi\b|\btruyen thong\b|\bnews\b")
_CLAUSE_SPLIT = re.compile(r"[?.;!\n]|,|\bva\b|\bcon\b")

_DATE = r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?"
_RANGE = re.compile(rf"\btu (?:ngay )?{_DATE} (?:den|toi|-) (?:ngay )?{_DATE}")
_FROM = re.compile(rf"\b(?:tu|ke tu|sau) (?:ngay )?{_DATE}")
_TO = re.compile(rf"\b(?:den|truoc) (?:ngay )?{_DATE}")
_DAY = re.compile(rf"\bngay {_DATE}|(?<![\d/-]){_DATE}(?![\d/-])")
_MONTH = re.compile(r"\bthang (\d{1,2})(?:(?:/| nam )(\d{4}))?\b")
_QUARTER = re.compile(r"\bquy (\d)(?:(?:/| nam )(\d{4}))?\b")
_YEAR = re.compile(r"\bnam (\d{4})\b")
_LAST_N = re.compile(r"\b(\d{1,3}) (ngay|tuan|thang) (?:qua|gan day|gan nhat|vua qua|truoc)\b")
# Anything that looks like a time reference; left unread, it means "ask".
_TIME_MARKERS = re.compile(
    r"\d{1,2}[/-]\d{1,2}|\bthang \d|\bquy \d|\bnam \d{4}|\bngay \d|\b(?:dau|cuoi|giua) (?:nam|thang|quy|tuan)\b"
    r"|\b(?:tuan|thang|quy|nam) (?:nay|truoc|qua|sau|toi|ngoai)\b|\bhom (?:nay|qua|kia)\b"
)


def _news_clause(question: str) -> str:
    folded = fold(question)
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(folded) if part and part.strip()]
    news = [part for part in parts if _NEWS_WORDS.search(part)]
    return " ".join(news)


def _day(day: int, month: int, year: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        raise WindowError("invalid_date", f"Ngày {day}/{month}/{year} không hợp lệ.") from None


def _start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=VIETNAM_TZ).astimezone(timezone.utc)


def _end(day: date) -> datetime:
    return datetime.combine(day, time.max.replace(microsecond=0), tzinfo=VIETNAM_TZ).astimezone(timezone.utc)


def _month_end(year: int, month: int) -> date:
    first_next = date(year + (month == 12), month % 12 + 1, 1)
    return first_next - timedelta(days=1)


def _explicit_date(groups: tuple, today: date) -> date:
    day, month, year = groups
    return _day(int(day), int(month), int(year) if year else today.year)


def _quarter_bounds(year: int, quarter: int) -> tuple[date, date]:
    first = 3 * quarter - 2
    return date(year, first, 1), _month_end(year, first + 2)


def _year_for(period: int, current: int, today: date) -> int:
    """A month or quarter named without a year is the latest one that has
    started: in October, "tháng 12" is last December."""

    return today.year if period <= current else today.year - 1


def _bounded(start: datetime, end: datetime, as_of: datetime, source: str) -> NewsWindow:
    if start > end:
        raise WindowError("reversed_range", "Khoảng thời gian bị đảo ngược (ngày bắt đầu sau ngày kết thúc).")
    if start > as_of:
        raise WindowError("future_range", "Khoảng thời gian hỏi nằm sau thời điểm hiện tại.")
    return NewsWindow(start, min(end, as_of), source)


def resolve_news_window(question: str, as_of: datetime) -> NewsWindow:
    as_of = as_of.astimezone(timezone.utc)
    today = as_of.astimezone(VIETNAM_TZ).date()
    clause = _news_clause(question)

    if clause:
        match = _RANGE.search(clause)
        if match:
            start = _explicit_date(match.groups()[:3], today)
            end = _explicit_date(match.groups()[3:], today)
            return _bounded(_start(start), _end(end), as_of, "explicit")
        start_match, end_match = _FROM.search(clause), _TO.search(clause)
        if start_match and not end_match:
            return _bounded(_start(_explicit_date(start_match.groups(), today)), as_of, as_of, "explicit")
        if end_match and not start_match:
            end = _end(_explicit_date(end_match.groups(), today))
            return _bounded(end - timedelta(days=DEFAULT_DAYS), end, as_of, "explicit")
        match = _MONTH.search(clause)
        if match:
            month = int(match.group(1))
            if not 1 <= month <= 12:
                raise WindowError("invalid_date", f"Tháng {month} không hợp lệ.")
            year = int(match.group(2)) if match.group(2) else _year_for(month, today.month, today)
            return _bounded(_start(date(year, month, 1)), _end(_month_end(year, month)), as_of, "explicit")
        match = _QUARTER.search(clause)
        if match:
            quarter = int(match.group(1))
            if not 1 <= quarter <= 4:
                raise WindowError("invalid_date", f"Quý {quarter} không hợp lệ.")
            current = (today.month - 1) // 3 + 1
            year = int(match.group(2)) if match.group(2) else _year_for(quarter, current, today)
            first, last = _quarter_bounds(year, quarter)
            return _bounded(_start(first), _end(last), as_of, "explicit")
        match = _YEAR.search(clause)
        if match:
            year = int(match.group(1))
            return _bounded(_start(date(year, 1, 1)), _end(date(year, 12, 31)), as_of, "explicit")
        match = _DAY.search(clause)
        if match:
            groups = match.groups()
            day = _explicit_date(groups[:3] if groups[0] else groups[3:], today)
            return _bounded(_start(day), _end(day), as_of, "explicit")
        match = _LAST_N.search(clause)
        if match:
            count, unit = int(match.group(1)), match.group(2)
            days = count * {"ngay": 1, "tuan": 7, "thang": 30}[unit]
            return _bounded(as_of - timedelta(days=max(days, 1)), as_of, as_of, "relative")
        quarter_now = (today.month - 1) // 3 + 1
        previous_quarter = _quarter_bounds(*((today.year, quarter_now - 1) if quarter_now > 1
                                             else (today.year - 1, 4)))
        relative = (
            (r"\bhom nay\b", lambda: (_start(today), as_of)),
            (r"\bhom qua\b", lambda: (_start(today - timedelta(days=1)), _end(today - timedelta(days=1)))),
            (r"\btuan nay\b", lambda: (_start(today - timedelta(days=today.weekday())), as_of)),
            (r"\btuan truoc\b", lambda: (_start(today - timedelta(days=today.weekday() + 7)),
                                         _end(today - timedelta(days=today.weekday() + 1)))),
            (r"\btuan qua\b", lambda: (as_of - timedelta(days=7), as_of)),
            (r"\bdau thang\b|\bthang nay\b", lambda: (_start(today.replace(day=1)), as_of)),
            (r"\bthang truoc\b", lambda: (_start((today.replace(day=1) - timedelta(days=1)).replace(day=1)),
                                          _end(today.replace(day=1) - timedelta(days=1)))),
            (r"\bthang qua\b", lambda: (as_of - timedelta(days=DEFAULT_DAYS), as_of)),
            (r"\bdau quy\b|\bquy nay\b", lambda: (_start(_quarter_bounds(today.year, quarter_now)[0]), as_of)),
            (r"\bquy truoc\b", lambda: (_start(previous_quarter[0]), _end(previous_quarter[1]))),
            (r"\bdau nam\b|\bnam nay\b", lambda: (_start(date(today.year, 1, 1)), as_of)),
            (r"\bnam ngoai\b|\bnam truoc\b", lambda: (_start(date(today.year - 1, 1, 1)),
                                                       _end(date(today.year - 1, 12, 31)))),
        )
        for pattern, bounds in relative:
            if re.search(pattern, clause):
                start, end = bounds()
                return _bounded(start, end, as_of, "relative")
        if _TIME_MARKERS.search(clause):
            raise WindowError(
                "unclear_time",
                "Chưa hiểu mốc thời gian của phần hỏi về tin (ví dụ nêu “từ 1/9/2026 đến 30/9/2026”, "
                "“tháng 9/2026” hoặc “7 ngày qua”).",
            )
    return NewsWindow(as_of - timedelta(days=DEFAULT_DAYS), as_of, "default")
