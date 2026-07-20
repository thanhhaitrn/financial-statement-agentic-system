"""Utilities for turning markdown tables into cleaned pandas DataFrames."""
# Code note: Ingestion modules convert source reports into normalized facts; comments here mark parsing assumptions.

import pandas as pd
from io import StringIO
import re

from schemas.table_names import (
    TABLE_BS,
    TABLE_CF,
    TABLE_IS,
    TABLE_NOTE,
    TABLE_REPORT_SECTION,
    normalize_table_heading,
)


_SPACE_RE = re.compile(r"\s+")
# The canonical statement sections; a heading that resolves to one of these
# starts a new section context for the tables that follow it.
_SECTION_HEADINGS = {TABLE_BS, TABLE_IS, TABLE_CF, TABLE_NOTE, TABLE_REPORT_SECTION}
_NOTE_HEADING_PREFIXES = (
    "bản thuyết minh",
    "ban thuyet minh",
    "thuyết minh báo cáo tài chính",
    "thuyet minh bao cao tai chinh",
    "thuyết minh bctc",
    "thuyet minh bctc",
)
_SIGNATURE_HEADING_RE = re.compile(
    r"^(người lập|người soát xét|người duyệt)\s*:$",
    flags=re.IGNORECASE,
)


def _normalize_heading_line(line: str) -> str:
    text = str(line or "").strip().replace("#", "").replace("*", "")
    return _SPACE_RE.sub(" ", text).strip()


def is_heading(line: str) -> bool:
    raw_line = str(line or "").strip()
    normalized_line = _normalize_heading_line(line)
    lowered = normalized_line.lower()
    if not normalized_line:
        return False

    return (
        raw_line.startswith("#")
        or lowered.startswith("bảng")
        or lowered.startswith("báo cáo")
        or lowered.startswith(_NOTE_HEADING_PREFIXES)
        or (normalized_line.endswith(":") and not _SIGNATURE_HEADING_RE.match(lowered))
    )


def _is_note_section_start(raw_line: str, heading: str) -> bool:
    lowered = _normalize_heading_line(heading).lower()
    return (
        str(raw_line or "").strip().startswith("#")
        or lowered.startswith(_NOTE_HEADING_PREFIXES)
    )

def attach_context(md_text: str) -> list[dict]:
    # Local import avoids a module cycle (note_parser -> kb_builder -> table_parser).
    from ingestion.note_parser import note_schedule_ref, note_schedule_title

    tables_with_context = []
    current_table = []
    current_heading = None
    current_section = ""
    in_note_section = False
    # The 'V.<n>' reference and numbered title of the current note schedule,
    # latched from its numbered heading (e.g. "### 10. Tài sản cố định vô hình")
    # so descriptive sub-headings that follow (e.g. "Là chương trình phần mềm,
    # chi tiết như sau:") — which overwrite current_heading — do not lose the
    # schedule link nor the line-item tokens retrieval needs.
    current_note_ref = ""
    current_note_title = ""

    for line in md_text.splitlines():
        if not line.strip().startswith("|") and current_table:
            tables_with_context.append({
                    "heading": current_heading,
                    "section": current_section,
                    "note_ref": current_note_ref,
                    "note_title": current_note_title,
                    "table": current_table
                })
            current_table = []

        if is_heading(line):
            current_heading = _normalize_heading_line(line)
            # Latch the active statement section so sub-tables that follow a
            # "Thuyết minh báo cáo tài chính" heading are tagged as notes even
            # when their own heading is an ad-hoc schedule title (e.g. "18a.
            # Vay ngắn hạn"). Orphan headings leave the section unchanged.
            canonical = normalize_table_heading(current_heading)
            if canonical in _SECTION_HEADINGS:
                if canonical == TABLE_NOTE and _is_note_section_start(line, current_heading):
                    current_section = TABLE_NOTE
                    in_note_section = True
                elif not in_note_section:
                    current_section = canonical
                    current_note_ref = ""
                    current_note_title = ""
            # Inside the notes, a numbered schedule heading refreshes the link;
            # descriptive headings return "" and keep the previous schedule ref.
            if in_note_section:
                ref = note_schedule_ref(current_heading)
                if ref:
                    current_note_ref = ref
                    current_note_title = note_schedule_title(current_heading)
        elif in_note_section and not line.strip().startswith("|") and len(line.strip()) <= 100:
            # Schedule titles can also appear as plain/bold lines that is_heading
            # misses ("**10. Tài sản cố định vô hình**"); without this latch the
            # following descriptive heading ("Là chương trình phần mềm…:") keeps
            # the PREVIOUS schedule's ref/title (V.9 leaking onto the intangibles
            # matrix). Length-guarded so numbered prose sentences don't match.
            ref = note_schedule_ref(line)
            if ref:
                current_note_ref = ref
                current_note_title = note_schedule_title(line)

        if line.strip().startswith("|"):
            current_table.append(line)

    if current_table:
        tables_with_context.append({
            "heading": current_heading,
            "section": current_section,
            "note_ref": current_note_ref,
            "note_title": current_note_title,
            "table": current_table
        })

    return tables_with_context


def markdown_table_to_df(table_lines: list[str]) -> pd.DataFrame:
    raw = "\n".join(table_lines)

    df = pd.read_csv(StringIO(raw), sep="|", engine="python")
    df = df.dropna(axis=1, how="all")
    df.columns = [c.strip() for c in df.columns]
    df = df.iloc[1:]
    df = df.map(lambda x: x.strip() if isinstance(x, str) else x)

    return df
