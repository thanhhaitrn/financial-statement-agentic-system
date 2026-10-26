"""The main figure: the evidence-matched chain A → Apre → Apost → D (SVG, no data).

Run from ``backend/``: ``python -m analysis.security_phase2.figure --lang vi --out FILE.svg``.
Only the components the A′ chain evaluates are drawn: what each configuration
gets as report evidence, as news, and how it answers. A darker cell is what
changes from the row above; the bracket on the right names the comparison.
"""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path

TEXT = {
    "vi": {
        "title": "Đối chứng khớp evidence: A → Apre → Apost → D",
        "stages": ("Evidence báo cáo (BCTC)", "Tin tức", "Sinh câu trả lời"),
        "rows": {
            "A": ("Top-10 đoạn BCTC\n(tìm vector, không rerank)", "Bài báo thô", "Một lần gọi model"),
            "Apre": ("Fact BCTC mà D đã truy xuất\n(trước khi cắt cho Synth)", "Bài báo thô", "Một lần gọi model"),
            "Apost": ("Fact BCTC D gửi cho Synth", "Tin đã phân tích\n(+ kết quả agent phân tích)", "Một lần gọi model"),
            "D": ("Truy xuất theo planner/router", "Chọn lọc và phân tích tin", "Pipeline nhiều bước\n(agent, Synth)"),
        },
        "steps": ("đúng fact BCTC", "chọn lọc và\nphân tích tin", "phần còn lại\ncủa pipeline"),
        "note": "Apre và Apost dùng đúng prompt và giới hạn của A; evidence lấy từ log của D trong đợt chính.",
        "legend": ("thay đổi so với hàng trên", "khác A"),
    },
    "en": {
        "title": "Evidence-matched replay: A → Apre → Apost → D",
        "stages": ("Report evidence", "News", "Answer generation"),
        "rows": {
            "A": ("Top-10 report chunks\n(vector search, no rerank)", "Raw articles", "Single model call"),
            "Apre": ("Report facts D retrieved\n(before the Synth cut)", "Raw articles", "Single model call"),
            "Apost": ("Report facts D sent to Synth", "Analysed news\n(+ analysis agents' output)", "Single model call"),
            "D": ("Planner/router retrieval", "Selection and news analysis", "Multi-step pipeline\n(agents, Synth)"),
        },
        "steps": ("correct report facts", "selection and\nnews analysis", "rest of the\npipeline"),
        "note": "Apre and Apost use A's prompt and limits; their evidence comes from D's logs in the main run.",
        "legend": ("changed from the row above", "differs from A"),
    },
}
CHANGED = {"Apre": {0}, "Apost": {0, 1}, "D": {0, 1, 2}}  # cells that differ from the row above
CHANGED_FROM_PREVIOUS = {"Apre": {0}, "Apost": {0, 1}, "D": {2}}


def _text(x: float, y: float, text: str, *, size: int = 13, weight: str = "normal", anchor: str = "middle") -> str:
    lines = text.split("\n")
    first = y - (len(lines) - 1) * size * 0.6
    spans = "".join(f'<tspan x="{x}" y="{first + i * size * 1.2:.1f}">{escape(line)}</tspan>'
                    for i, line in enumerate(lines))
    return (f'<text font-family="Helvetica, Arial, sans-serif" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" dominant-baseline="middle" fill="#111">{spans}</text>')


def render(lang: str) -> str:
    t = TEXT[lang]
    label_w, cell_w, gap, row_h, row_gap = 90, 230, 16, 66, 22
    x0, y0 = 20, 92
    cols = [x0 + label_w + gap + i * (cell_w + gap) for i in range(3)]
    bracket_x = cols[2] + cell_w + 28
    width, rows = bracket_x + 190, list(t["rows"])
    height = y0 + len(rows) * (row_h + row_gap) + 54
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
           f'<rect width="{width}" height="{height}" fill="#fff"/>',
           _text(width / 2, 26, t["title"], size=16, weight="bold")]
    for i, stage in enumerate(t["stages"]):
        out.append(_text(cols[i] + cell_w / 2, 66, stage, size=13, weight="bold"))
    for r, name in enumerate(rows):
        y = y0 + r * (row_h + row_gap)
        out.append(_text(x0 + label_w / 2, y + row_h / 2, name, size=15, weight="bold"))
        for c in range(3):
            changed = c in CHANGED_FROM_PREVIOUS.get(name, set())
            fill = "#b9b9b9" if changed else ("#ececec" if c in CHANGED.get(name, set()) else "#fafafa")
            out.append(f'<rect x="{cols[c]}" y="{y}" width="{cell_w}" height="{row_h}" rx="6" fill="{fill}" '
                       f'stroke="#555" stroke-width="{1.6 if changed else 1}"/>')
            out.append(_text(cols[c] + cell_w / 2, y + row_h / 2, t["rows"][name][c], size=12))
    for k, label in enumerate(t["steps"]):
        top = y0 + k * (row_h + row_gap) + row_h / 2
        bottom = top + row_h + row_gap
        out.append(f'<path d="M{bracket_x - 10},{top} H{bracket_x} V{bottom} H{bracket_x - 10}" fill="none" '
                   f'stroke="#333" stroke-width="1.4"/>')
        out.append(_text(bracket_x + 12, (top + bottom) / 2, label, size=12, anchor="start"))
    legend_y = height - 40
    for k, (fill, label) in enumerate((("#b9b9b9", t["legend"][0]), ("#ececec", t["legend"][1]))):
        lx = cols[0] + k * 260
        out.append(f'<rect x="{lx}" y="{legend_y - 8}" width="22" height="16" rx="3" fill="{fill}" stroke="#555"/>')
        out.append(_text(lx + 30, legend_y, label, size=11, anchor="start"))
    out.append(_text(width / 2, height - 14, t["note"], size=11))
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=sorted(TEXT), default="vi")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(args.lang), encoding="utf-8")
    print(args.out)


if __name__ == "__main__":
    main()
