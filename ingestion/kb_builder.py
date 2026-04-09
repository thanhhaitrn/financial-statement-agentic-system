import pandas as pd
from ingestion.table_parser import markdown_table_to_df
import re
from schemas.table_names import normalize_table_heading

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
_MARKDOWN_EMPHASIS_RE = re.compile(r"(\\\*\\\*|\\\*|\*\*|\*|__|_)")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_inline_formatting(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""

    value = _HTML_TAG_RE.sub(" ", value)
    value = _MARKDOWN_EMPHASIS_RE.sub("", value)
    return re.sub(r"\s+", " ", value).strip()


def _normalize_value_text(text: str) -> str:
    value = _strip_inline_formatting(text)
    if not value:
        return ""

    compact = value.replace(",", "").replace(".", "").replace(" ", "")
    if compact.startswith("(") and compact.endswith(")") and compact[1:-1].isdigit():
        inner = value[1:-1].strip()
        if inner:
            return f"-{inner}"

    return value

def looks_like_value(x: str) -> bool:
    x = _normalize_value_text(x)

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

    if cleaned.startswith("-"):
        cleaned = cleaned[1:]

    # Pure number
    if cleaned.isdigit():
        return True

    # Date-like
    if re.match(r"\d{1,2}/\d{1,2}/\d{4}", x):
        return False

    return False

def clean_label(text: str) -> str:
    return _strip_inline_formatting(LABEL_PREFIX.sub("", text)).strip()

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

                raw_value = _strip_inline_formatting(cell)
                normalized_value = _normalize_value_text(cell)

                facts.append({
                    "company": company,
                    "heading": normalize_table_heading(clean_label(heading)),
                    "item_code": row.get("Mã số") if "Mã số" in df.columns else None,
                    "item_name": f"{row_label} | {col_name}",
                    "value": normalized_value,
                    "raw_value": raw_value,
                    "normalized_value": normalized_value,
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
                f.get("raw_value", ""),
                f.get("normalized_value", ""),
                f["source"],
            ))

    return rows

    




    
