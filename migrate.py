"""
migrate.py — SQLite → Supabase (PostgreSQL) 一回限りのデータ移行
使い方:
  DATABASE_URL=postgresql://... python3 migrate.py
"""
import os, sqlite3
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise SystemExit("環境変数 DATABASE_URL を設定してください")

import psycopg2
import psycopg2.extras

SQLITE_PATH = Path(__file__).parent / "data" / "pista.db"

src = sqlite3.connect(str(SQLITE_PATH))
src.row_factory = sqlite3.Row

dst = psycopg2.connect(DATABASE_URL)
dst.autocommit = False
c_dst = dst.cursor()

TABLES = ["races", "results", "lines", "payouts"]

for table in TABLES:
    print(f"移行中: {table} ...", end=" ", flush=True)
    rows = [dict(r) for r in src.execute(f"SELECT * FROM {table}").fetchall()]
    if not rows:
        print("0件")
        continue

    cols   = list(rows[0].keys())
    ph     = ", ".join(["%s"] * len(cols))
    col_str = ", ".join(cols)
    vals   = [tuple(r[c] for c in cols) for r in rows]

    if table == "races":
        on_conflict = "ON CONFLICT (race_id) DO NOTHING"
    else:
        on_conflict = "ON CONFLICT DO NOTHING"

    psycopg2.extras.execute_batch(
        c_dst,
        f"INSERT INTO {table} ({col_str}) VALUES ({ph}) {on_conflict}",
        vals,
        page_size=500,
    )
    dst.commit()
    print(f"{len(rows)}件")

# picks_cache テーブル作成（存在しない場合）
c_dst.execute("""
    CREATE TABLE IF NOT EXISTS picks_cache (
        date       TEXT PRIMARY KEY,
        report     TEXT,
        updated_at TEXT
    )
""")
dst.commit()

src.close()
dst.close()
print("✅ 移行完了")
