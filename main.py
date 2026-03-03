import sqlite3
import pandas as pd

DB_PATH = "financial_kb.db"  # change if yours differs

conn = sqlite3.connect(DB_PATH)

# See distinct headings + how many rows each one has
df = pd.read_sql("""
    SELECT heading, COUNT(*) as n
    FROM financial_facts
    GROUP BY heading
    ORDER BY n DESC
""", conn)

print(df.head(50))  # top 50 headings