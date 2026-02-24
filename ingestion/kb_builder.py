import pandas as pd
from ingestion.table_parser import markdown_table_to_df
import re

LABEL_PREFIX = re.compile(
    r"""
    ^\s*(
        \d+(\.\d+)*\.?      |   # 1, 1.1, 1.2.3
        [IVXLC]+(\.)?   |   # I, II, III.
        [A-Z]\.         |   # A.
    )\s+
    """,
    re.VERBOSE | re.IGNORECASE
)

def looks_like_value(x: str) -> bool:
    x = x.strip()

    if not x:
        return False

    # If it starts like "1. Tiền", "2.1 Nợ", etc → LABEL
    if LABEL_PREFIX.match(x):
        return False

    # Remove common formatting
    cleaned = x.replace(",", "").replace(".", "").replace(" ", "")

    # Accounting negative: (12345)
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]

    # Pure number
    if cleaned.isdigit():
        return True

    # Date-like
    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", x):
        return False

    return False

def clean_label(text: str) -> str:
    return LABEL_PREFIX.sub("", text).strip()

def df_to_facts(df, heading, company, source):
    facts = []

    df = df.map(lambda x: "" if pd.isna(x) else str(x).strip())
    ignore_cols = {"mã số", "thuyết minh"}
    columns = [str(c).strip() for c in df.columns]

    for _, row in df.iterrows():
        row_label = None

        # find row label
        for col_name, cell in zip(columns, row.values):
            if col_name.lower() in ignore_cols:
                continue
            if cell and not looks_like_value(cell):
                row_label = clean_label(cell)
                break
        
        if not row_label:
            continue
        
        # create facts
        for col_name, cell in zip(columns, row.values):
                if col_name.lower() in ignore_cols:
                    continue
                if not cell:
                    continue
                if cell == row_label:
                    continue
                if not looks_like_value(cell):
                    continue

                facts.append({
                    "company": company,
                    "heading": clean_label(heading),
                    "item_code": row.get("Mã số") if "Mã số" in df.columns else None,
                    "item_name": f"{row_label} | {col_name}",
                    "value": cell,
                    "source": source
                })
        
    return facts

def build_fact_rows(tables_with_context, company, source):
    rows = []

    for block in tables_with_context:
        df = markdown_table_to_df(block["table"])

        if df is None or df.empty:
            continue

        facts = df_to_facts(
            df,
            heading=block["heading"],
            company=company,
            source=source
        )

        for f in facts:
            if not f.get("item_name") or not f.get("value"):
                continue

            rows.append((
                f["company"],
                f["heading"],
                f.get("item_code"),
                f["item_name"],
                f["value"],
                f["source"],
            ))

    return rows

    




    
