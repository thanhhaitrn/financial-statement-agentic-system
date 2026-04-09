import pandas as pd
from io import StringIO
import re


_SPACE_RE = re.compile(r"\s+")
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
        or (normalized_line.endswith(":") and not _SIGNATURE_HEADING_RE.match(lowered))
    )

def attach_context(md_text: str) -> list[dict]:
    tables_with_context = []
    current_table = []
    current_heading = None

    for line in md_text.splitlines():
        if not line.strip().startswith("|") and current_table: 
            tables_with_context.append({
                    "heading": current_heading,
                    "table": current_table
                })
            current_table = []
            
        if is_heading(line):
            current_heading = _normalize_heading_line(line)

        if line.strip().startswith("|"):
            current_table.append(line)

    if current_table:
        tables_with_context.append({
            "heading": current_heading,
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
