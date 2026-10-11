"""The news section of the final answer, written by code, not by the model.

Per event, in the order a reader needs it: what happened, when, which figures
it relates to, the quotes that prove it, how confirmed it is and on what
basis, what is still uncertain; the sentiment label comes last, as secondary
information. Titles, quotes and notes come from untrusted articles or model
output: they are Markdown-escaped, and a link is only built for a
vietstock.vn URL. Quotes are reproduced exactly as verified.
"""

from __future__ import annotations

import re
from typing import Optional

from schemas.web_analysis import WebAnalysis
from tools.http_safety import allowed_https_url
from web_analysis.support import vn_day

LABELS = {
    "very_negative": "rất tiêu cực",
    "negative": "tiêu cực",
    "neutral": "trung tính",
    "positive": "tích cực",
    "very_positive": "rất tích cực",
    "mixed": "trái chiều",
    "insufficient": "chưa đủ căn cứ",
}
CONFIRMATIONS = {
    "official_disclosure": "công bố thông tin chính thức",
    "attributed_report": "tin có dẫn nguồn",
    "forecast": "kế hoạch/dự báo",
    "unconfirmed": "chưa được xác nhận",
    "unattributed": "chưa rõ nguồn xác nhận",
}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+!|<>~])")


def escape(text: str) -> str:
    return _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(str(text or "").split()))


def _score(value: float) -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}".replace(".", ",")


def _link(source: dict) -> str:
    title = escape(source.get("title", "")) or "Bài viết"
    url = str(source.get("url", "") or "")
    day = vn_day(source.get("published_at", "")) or ""
    suffix = f", {day}" if day else ""
    if allowed_https_url(url, ("vietstock.vn",)) and not re.search(r"[\s()<>]", url):
        return f"[{title}]({url}){suffix}"
    return f"{title}{suffix}"


def _iso_day(value: str) -> str:
    parts = str(value or "").split("-")
    return f"{parts[2]}/{parts[1]}/{parts[0]}" if len(parts) == 3 else ""


def _collection_line(result: WebAnalysis) -> str:
    c = result.collection
    if not (c.listed_in_window or c.selected):
        return ""
    parts = [f"đã đọc danh sách {c.listed_in_window} bài trong khoảng"
             + ("" if c.list_read_for_window else " (chưa đọc hết khoảng)")]
    parts.append(f"đã lưu toàn văn {c.full_text_in_window} bài")
    analyzed = f"đã phân tích {c.selected} bài" + (" chọn theo chủ đề câu hỏi" if c.ranked_by_topic else " mới nhất")
    if c.analyzed_summary_only:
        analyzed += f" ({c.analyzed_summary_only} bài chỉ có tóm tắt)"
    parts.append(analyzed)
    if c.relevant:
        parts.append(f"{c.relevant} bài liên quan")
    return "*Thu thập:* " + "; ".join(parts) + "."


def render_news_section(analysis: Optional[dict]) -> str:
    if not analysis:
        return ""
    try:
        result = WebAnalysis.model_validate(analysis)
    except ValueError:
        return ""
    context = result.context
    who = escape(context.ticker or context.company or "doanh nghiệp")
    period = ""
    if context.date_from and context.date_to:
        period = f", {vn_day(context.date_from)}–{vn_day(context.date_to)}"
    lines = [f"**Tin tức doanh nghiệp {who}** (nguồn Vietstock{period})"]

    sources = {item["article_id"]: item for item in result.sources}
    for event in result.events:
        # The article states this date for the event; it may be a period end
        # rather than the day it happened, so it is labelled as stated.
        when = "; ".join(part for part in (
            f"ngày nêu trong bài {_iso_day(event.event_date)}" if event.event_date else "",
            f"đưa tin {_iso_day(event.reported_at)}" if event.reported_at else "") if part)
        lines.append(f"- **{escape(event.title)}**" + (f" ({when})" if when else ""))
        if event.related_metrics:
            lines.append(f"  Liên quan: {escape(', '.join(event.related_metrics))}.")
        if event.explanation:
            lines.append(f"  {escape(event.explanation)}")
        for quote in event.quotes:
            lines.append(f"  > “{escape(quote.text)}” ({_link(sources.get(quote.article_id, {}))})")
        if not event.quotes:
            cited = "; ".join(_link(sources[item]) for item in event.article_ids if item in sources)
            if cited:
                lines.append(f"  Nguồn: {cited}")
        lines.append(f"  Mức xác nhận: {CONFIRMATIONS.get(event.confirmation, event.confirmation)}. "
                     f"{escape(event.confirmation_basis)}")
        uncertain = [note.rstrip(" .;") for note in event.uncertainties if note != event.confirmation_basis]
        if uncertain:
            lines.append(f"  Còn chưa chắc: {escape('; '.join(uncertain))}.")
        score = f" ({event.score:+d})" if event.score is not None else ""
        lines.append(f"  Sắc thái (phụ): {LABELS.get(event.label, event.label)}{score}.")
    if result.aggregate.score is not None:
        lines.append(
            f"\nSắc thái tổng hợp (phụ): {_score(result.aggregate.score)} trên thang -2 đến +2, "
            f"{result.aggregate.scored_events} sự kiện có điểm, theo mức xác nhận "
            f"(rubric {escape(result.aggregate.rubric_version)})."
        )
    collection = _collection_line(result)
    if collection:
        lines.append(f"\n{collection}")
    if result.limitations:
        lines.append("\n*Giới hạn:*")
        lines.extend(f"- {escape(note)}" for note in result.limitations)
    return "\n".join(lines)
