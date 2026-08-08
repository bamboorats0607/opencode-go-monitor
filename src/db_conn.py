"""opencode.db 只读连接与 schema 探测。

红线：全程只读。使用 sqlite URI mode=ro 打开，绝不执行任何写语句；
额外的 PRAGMA query_only=ON 作为双保险。
"""
import os
import sqlite3

DEFAULT_DB_PATH = r"C:\Users\dgx19\.local\share\opencode\opencode.db"


def connect_readonly(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """以只读 URI 打开 opencode.db。

    - 首选 mode=ro：WAL 模式下依赖已存在的 -shm/-wal 文件，可读到最新数据。
    - 若 -shm 缺失导致只读打开失败（opencode 未运行过的冷环境），退回
      immutable=1 只读主库文件（此时忽略 WAL，数据可能不是最新，但完全只读）。
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
    except sqlite3.Error:
        con = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True, timeout=3)
    con.execute("PRAGMA query_only = ON")  # 双保险：本连接禁止任何写入
    return con


def schema_snapshot(con: sqlite3.Connection, db_path: str = DEFAULT_DB_PATH) -> dict:
    """返回 schema 快照：journal_mode / data_version / session、message 建表语句。"""
    cur = con.cursor()
    tables = {}
    for name, sql in cur.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table' "
        "AND name IN ('session','message')"
    ).fetchall():
        tables[name] = sql
    try:
        jm = cur.execute("PRAGMA journal_mode").fetchone()[0]
    except sqlite3.Error:
        jm = "unknown"
    try:
        dv = cur.execute("PRAGMA data_version").fetchone()[0]
    except sqlite3.Error:
        dv = None
    return {
        "journal_mode": jm,
        "data_version": dv,
        "tables": tables,
        "db_path": db_path,
        "db_size": os.path.getsize(db_path) if os.path.exists(db_path) else 0,
    }
