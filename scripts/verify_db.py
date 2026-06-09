import os
import sys
from dotenv import load_dotenv
import psycopg2

load_dotenv()

raw_url = os.environ["DATABASE_URL_MIGRATIONS"]
# Strip SQLAlchemy dialect prefix for raw psycopg2 connect
dsn = raw_url.replace("postgresql+psycopg2://", "postgresql://")

conn = psycopg2.connect(dsn)
cur = conn.cursor()

checks = [
    ("pgvector extension", "SELECT 1 FROM pg_extension WHERE extname='vector'"),
    ("memories table",     "SELECT to_regclass('public.memories') IS NOT NULL"),
    ("messages table",     "SELECT to_regclass('public.messages') IS NOT NULL"),
]

failed = False
for label, query in checks:
    cur.execute(query)
    row = cur.fetchone()
    ok = row is not None and row[0]
    status = "PASS" if ok else "FAIL"
    print(f"{status}: {label}")
    if not ok:
        failed = True

cur.close()
conn.close()

if failed:
    sys.exit(1)
