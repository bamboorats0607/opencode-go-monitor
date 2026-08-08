"""实时流等效层：文件 mtime+size 监听（500ms）+ 增量查询 + data_version 兜底。

- message.id 为 text（msg_xxx），无法数值增量 → 用 time_updated > 游标 增量
- WAL 模式新数据可能只进 -wal，主文件 mtime 不变 → 5s PRAGMA data_version 兜底
- serve/event 尽力探测：桌面版无 CLI serve 证据，失败仅标注"未实现"，非主通道
"""
import os
import sqlite3
import subprocess
import sys
import urllib.request

from src import db_conn

# Windows GUI 模式下隐藏子进程控制台窗口，避免每 3s 弹 cmd
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 尽力探测候选（非主通道，任一端点成功才启用）
SSE_CANDIDATES = [
    "http://127.0.0.1:4173/event",
    "http://127.0.0.1:3000/event",
    "http://127.0.0.1:5173/event",
    "http://127.0.0.1:7384/event",
]


class DBWatcher:
    """轮询 opencode.db，返回增量消息行。所有方法在主线程调用即可（查询毫秒级）。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or db_conn.DEFAULT_DB_PATH
        self.con: sqlite3.Connection | None = None
        self.last_mtime: float | None = None
        self.last_size: int | None = None
        self.last_data_version: int | None = None
        self.last_max_updated: int = 0  # 增量游标（毫秒）

    # ---- 连接与首载 ----
    def connect(self) -> sqlite3.Connection:
        self.con = db_conn.connect_readonly(self.db_path)
        try:
            st = os.stat(self.db_path)
            self.last_mtime = st.st_mtime
            self.last_size = st.st_size
        except OSError:
            pass
        self.last_data_version = self._data_version()
        return self.con

    def first_load(self) -> tuple[list, list]:
        """返回 (session 行全量, message 行全量)，并推进增量游标。"""
        self.connect()
        sessions = self.fetch_sessions()
        cur = self.con.cursor()
        rows = cur.execute(
            "SELECT id, session_id, time_created, time_updated, data FROM message "
            "ORDER BY time_updated ASC"
        ).fetchall()
        if rows:
            self.last_max_updated = int(rows[-1][3])
        return sessions, rows

    # ---- 监听与增量 ----
    def file_changed(self) -> bool:
        try:
            st = os.stat(self.db_path)
        except OSError:
            return False
        changed = (st.st_mtime != self.last_mtime) or (st.st_size != self.last_size)
        self.last_mtime = st.st_mtime
        self.last_size = st.st_size
        return changed

    def _data_version(self) -> int | None:
        try:
            return self.con.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error:
            return None

    def poll(self, force: bool = False) -> list:
        """单次轮询：文件监听 + data_version 兜底。有变化返回增量消息行，否则 []。"""
        if self.con is None:
            self.connect()
        changed = self.file_changed() or force
        dv = self._data_version()
        if (
            dv is not None
            and self.last_data_version is not None
            and dv != self.last_data_version
        ):
            changed = True
        self.last_data_version = dv
        if not changed:
            return []
        return self.fetch_incremental()

    def fetch_incremental(self) -> list:
        cur = self.con.cursor()
        rows = cur.execute(
            "SELECT id, session_id, time_created, time_updated, data FROM message "
            "WHERE time_updated > ? ORDER BY time_updated ASC LIMIT 2000",
            (self.last_max_updated,),
        ).fetchall()
        if rows:
            self.last_max_updated = int(rows[-1][3])
        return rows

    def fetch_sessions(self) -> list:
        """session 表行，列序固定：
        id, title, slug, directory, model, cost, tokens_input, tokens_output,
        tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created, time_updated
        """
        cur = self.con.cursor()
        return cur.execute(
            "SELECT id, title, slug, directory, model, cost, tokens_input, "
            "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, "
            "time_created, time_updated FROM session ORDER BY time_updated DESC"
        ).fetchall()


def opencode_running() -> bool:
    """检测 OpenCode 进程（Windows tasklist）。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq OpenCode.exe", "/NH"],
            capture_output=True, text=True, timeout=3,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
        return "OpenCode.exe" in out
    except Exception:
        return False


def probe_sse(timeout: float = 0.5) -> str | None:
    """尽力探测本地 opencode serve/event 端点，限时快速失败。成功返回 url，否则 None。"""
    for url in SSE_CANDIDATES:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                if r.status == 200:
                    return url
        except Exception:
            continue
    return None
