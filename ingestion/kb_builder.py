import sqlite3
import pandas as pd
from ingestion.table_parser import markdown_table_to_df

def init_db(db_path: str, reset: bool = False):
    conn  = sqlite3.connect(db_path)
    cur = conn.cursor()

    if reset:
        cur.execute("DROP TABLE IF EXISTS financial_facts")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS financial_facts (
        company TEXT,
        heading TEXT,
        item_code TEXT,
        item_name TEXT,
        value TEXT,
        source TEXT
        )
        """)
    conn.commit()
    return conn

def df_to_facts(df, heading, company, source):
    facts = []

    df = df.map(lambda x: "" if pd.isna(x) else str(x).strip())
    ignore_cols = {"mã số", "thuyết minh"}
    columns = [str(c).strip() for c in df.columns]

    for _, row in df.iterrows():
        row_label = None

        for col_name, cell in zip(columns, row.values):
                if col_name.lower() in ignore_cols or not cell or cell == row_label:
                    continue

                facts.append({
                    "company": company,
                    "heading": heading,
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

    




    
