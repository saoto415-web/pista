"""
db.py — DB接続の抽象化
DATABASE_URL 環境変数があれば PostgreSQL（Supabase）、なければ SQLite を使用
"""
import os
import sqlite3
from pathlib import Path

DATABASE_URL: str = os.environ.get("DATABASE_URL", "")


def is_pg() -> bool:
    return bool(DATABASE_URL)


def get_connection():
    if is_pg():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
    path = Path(__file__).parent / "data" / "pista.db"
    path.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def get_cursor(conn):
    if is_pg():
        import psycopg2.extras
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def sql(query: str) -> str:
    """SQLite 構文を PostgreSQL 構文に変換"""
    if not is_pg():
        return query
    query = query.replace("?", "%s")
    if "INSERT OR IGNORE INTO" in query:
        query = query.replace("INSERT OR IGNORE INTO", "INSERT INTO")
        query = query.rstrip() + " ON CONFLICT DO NOTHING"
    return query


def serial_pk() -> str:
    return "SERIAL PRIMARY KEY" if is_pg() else "INTEGER PRIMARY KEY AUTOINCREMENT"
