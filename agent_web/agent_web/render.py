"""Render WebAnalysisResult ra Markdown — chỉ để demo/đọc nhanh.
Structured result (WebAnalysisResult) mới là output chính (mục 7)."""

from __future__ import annotations

from .models import WebAnalysisResult


def render_markdown(result: WebAnalysisResult) -> str:
    lines: list[str] = []
    lines.append(f"# Kết quả phân tích: {result.target.company_name or result.target.ticker or ''}")
    lines.append("")
    lines.append(f"**Trạng thái:** `{result.status.value}`")
    lines.append(f"**Câu hỏi:** {result.question}")
    if result.time_range:
        lines.append(
            f"**Khoảng thời gian:** {result.time_range.date_from} → "
            f"{result.time_range.date_to}"
            + (" (mặc định)" if result.time_range.is_default_window else "")
        )
    lines.append("")
    lines.append("## Tóm tắt")
    lines.append(result.summary_assessment or "_(không có)_")
    lines.append("")

    agg = result.aggregate
    lines.append("## Điểm tổng hợp")
    lines.append(
        f"- Điểm: **{agg.score if agg.score is not None else 'N/A'}** "
        f"(rubric `{agg.rubric_version}`, aggregation `{agg.aggregation_version}`)"
    )
    lines.append(
        f"- Số sự kiện có điểm: {agg.scored_event_count}; "
        f"không tính điểm: {agg.unscored_event_count}"
    )
    if agg.label_distribution:
        dist = ", ".join(f"{k}: {v}" for k, v in agg.label_distribution.items())
        lines.append(f"- Phân bố nhãn: {dist}")
    lines.append("")

    lines.append("## Chi tiết sự kiện")
    for e in result.events:
        lines.append(f"### {e.event_summary}")
        lines.append(
            f"- Nhãn: `{e.sentiment_label.value}` — Điểm: "
            f"{e.sentiment_score if e.sentiment_score is not None else 'N/A'} "
            f"— Loại: `{e.claim_type.value}`"
        )
        lines.append(f"- Lý do: {e.rationale}")
        if e.caveats:
            lines.append(f"- Giới hạn/mâu thuẫn: {e.caveats}")
        for q in e.supporting_quotes:
            lines.append(f"  - `{q.evidence_id}`: \"{q.quote}\"")
        lines.append("")

    if result.limitations:
        lines.append("## Giới hạn")
        for lim in result.limitations:
            lines.append(f"- {lim}")
        lines.append("")

    lines.append("## Nguồn (evidence)")
    for ev in result.evidence:
        pub = ev.published_at.isoformat() if ev.published_at else "?"
        lines.append(f"- [{ev.evidence_id}] [{ev.title}]({ev.url}) — {ev.source}, {pub}")

    return "\n".join(lines)
