"""Publication-time parsing for Vietnamese news sources.

Every time leaves this module as an aware UTC ``datetime``. A source time
without a zone is Vietnam local time (UTC+7, no daylight saving), never the
server's zone. Unparseable values return ``None``: an article whose date is
unknown is excluded from date-bounded analysis rather than guessed.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional

VIETNAM_TZ = timezone(timedelta(hours=7), "ICT")

_DOTNET_DATE = re.compile(r"^/Date\((-?\d+)(?:[+-]\d{4})?\)/$")
_DMY = re.compile(
    r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$"
)
# Page labels such as "Thứ Hai, 06/10/2026 16:07", "16:07 06/10/2026" or
# "06/10/2026 - 16:07": one date, an optional time before or after it.
_LABEL_DATE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4})(?!\d)")
_LABEL_TIME = re.compile(r"(?<![\d/])(\d{1,2}):(\d{2})(?::(\d{2}))?(?![\d/])")
_MAX_LABEL_CHARS = 48


def to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=VIETNAM_TZ)
    return moment.astimezone(timezone.utc)


def parse_published(value: object) -> Optional[datetime]:
    """Parse ISO 8601, ASP.NET ``/Date(ms)/`` or ``dd/mm/yyyy [HH:MM]``."""

    text = " ".join(str(value or "").split())
    if not text:
        return None
    match = _DOTNET_DATE.match(text)
    if match:
        try:
            return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    match = _DMY.match(text)
    if match:
        day, month, year, hour, minute, second = match.groups()
        try:
            local = datetime(int(year), int(month), int(day), int(hour or 0),
                             int(minute or 0), int(second or 0), tzinfo=VIETNAM_TZ)
        except ValueError:
            return None
        return local.astimezone(timezone.utc)
    try:
        return to_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    return _parse_label(text)


def _parse_label(text: str) -> Optional[datetime]:
    """A short date label with exactly one date in it; anything longer or
    ambiguous (two dates) is not guessed at."""

    if len(text) > _MAX_LABEL_CHARS:
        return None
    dates = _LABEL_DATE.findall(text)
    times = _LABEL_TIME.findall(text)
    if len(dates) != 1 or len(times) > 1:
        return None
    day, month, year = (int(part) for part in dates[0])
    hour, minute, second = (int(part or 0) for part in times[0]) if times else (0, 0, 0)
    try:
        local = datetime(year, month, day, hour, minute, second, tzinfo=VIETNAM_TZ)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def iso_utc(moment: Optional[datetime]) -> str:
    if moment is None:
        return ""
    return to_utc(moment).replace(microsecond=0).isoformat()
