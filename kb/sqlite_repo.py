import sqlite3

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

def sqlite_has_facts(conn) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM financial_facts")
    return cur.fetchone()[0] > 0