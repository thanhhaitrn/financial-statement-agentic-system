import pandas as pd
from io import StringIO


def is_heading(line: str) -> bool:
    line = line.strip()
    return (
        line.startswith("#")
        or line.endswith(":")
        or "BẢNG" in line
        or "BÁO CÁO" in line
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
            current_heading = line.replace("#", "").strip()

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