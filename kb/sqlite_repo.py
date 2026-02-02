import sqlite3

def init_db(db_path: str):
    """
    Open SQLite DB and ensure schema exists.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

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

def insert_financial_facts(conn, rows):
    if not rows:
        return

    cur = conn.cursor()
    cur.executemany("""
        INSERT INTO financial_facts (
            company,
            heading,
            item_code,
            item_name,
            value,
            source
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, rows)

    conn.commit()