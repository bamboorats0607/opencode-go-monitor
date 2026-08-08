"""在线调用明细本地持久化（独立 SQLite，与只读的 opencode.db 完全隔离）。

用途：go 订阅在线调用明细（SSR 只返回最近约 50 条）持久化到本地，
重启不丢、可跨会话累计统计。写入由后台轮询线程异步执行（见 poller）。

存储语义（LRU 30 日淘汰）：
    - 以 id 为主键 INSERT OR IGNORE 去重（增量拉取天然幂等）
    - 每次写入后 purge：time_created < now - 30 天的记录删除（按时间淘汰最旧）

字段统一为统计/导出可直接使用的口径：
    time_created 毫秒时间戳、cost 已换算为美元（SSR 原始 cost × 1e-8）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading

logger = logging.getLogger("opencode-local-store")

RETENTION_DAYS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    id           TEXT PRIMARY KEY,
    time_created INTEGER NOT NULL,
    model        TEXT,
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cache_read   INTEGER NOT NULL DEFAULT 0,
    cache_write  INTEGER NOT NULL DEFAULT 0,
    cost         REAL NOT NULL DEFAULT 0,
    session_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_usage_time ON usage_records(time_created);
"""


class LocalStore:
    """在线明细持久化。单连接跨线程安全：check_same_thread=False + 互斥锁，
    写发生在后台轮询线程，读（统计/导出）发生在主线程。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------- 写（后台线程调用） ----------
    def upsert_records(self, records: list[dict]) -> int:
        """增量写入：INSERT OR IGNORE（id 去重）→ 淘汰 30 天前旧数据。
        返回写入条数。records 字段见模块注释（统一口径）。
        """
        if not records:
            return 0
        now_ms = int(__import__("time").time() * 1000)
        with self._lock:
            try:
                cur = self._conn.executemany(
                    "INSERT OR IGNORE INTO usage_records "
                    "(id, time_created, model, tokens_input, tokens_output, "
                    " cache_read, cache_write, cost, session_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(
                        r.get("id"),
                        int(r.get("time_created") or 0),
                        (r.get("modelID") or "")[:64],
                        int(r.get("tokens_input") or 0),
                        int(r.get("tokens_output") or 0),
                        int(r.get("cache_read") or 0),
                        int(r.get("cache_write") or 0),
                        float(r.get("cost") or 0),
                        (r.get("session_id") or ""),
                    ) for r in records],
                )
                written = cur.rowcount
                # LRU 30 日淘汰：删除窗口之外的旧记录（按时间淘汰最旧）
                cutoff = now_ms - RETENTION_DAYS * 86400 * 1000
                cur = self._conn.execute(
                    "DELETE FROM usage_records WHERE time_created < ?", (cutoff,))
                if cur.rowcount:
                    logger.debug("local_store: purged %d stale rows", cur.rowcount)
                self._conn.commit()
            except sqlite3.Error as e:
                logger.warning("local_store: write failed: %s", e)
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    pass
        return written

    # ---------- 读（主线程调用，统计/导出） ----------
    def fetch_records(self, limit: int = 20000) -> list[dict]:
        """取最近记录（新在前）。字段与写入口径一致，可直接喂 stats/export。"""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT id, time_created, model, tokens_input, tokens_output, "
                    "       cache_read, cache_write, cost, session_id "
                    "FROM usage_records ORDER BY time_created DESC LIMIT ?",
                    (limit,))
                return [
                    {
                        "id": row[0],
                        "time_created": row[1],
                        "modelID": row[2],
                        "tokens_input": row[3],
                        "tokens_output": row[4],
                        "cache_read": row[5],
                        "cache_write": row[6],
                        "cost": row[7],
                        "session_id": row[8],
                    }
                    for row in cur.fetchall()
                ]
            except sqlite3.Error as e:
                logger.warning("local_store: read failed: %s", e)
                return []

    def count_records(self) -> int:
        with self._lock:
            try:
                row = self._conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()
                return int(row[0]) if row else 0
            except sqlite3.Error:
                return 0
