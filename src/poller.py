"""后台异步轮询线程：DBWatcher 独占在后台线程（sqlite 连接线程绑定），
主线程只消费快照并渲染，避免 500ms 轮询 + 解析阻塞 UI。

数据流：
    后台线程: poll → parse_rows → fetch_sessions → 发布快照（锁保护）
    主线程:   take_snapshot() 取走最新快照（取走即清空，保证最多积压一帧）

快照字段：
    {"type": "first"|"inc"|"error",
     "msgs": [parse_rows 增量消息], "sessions": [...], "error": str | None}
"""
from __future__ import annotations

import threading
import time

from src.watcher import DBWatcher
from src.parser import parse_rows


class DBPoller(threading.Thread):
    def __init__(self, db_path: str | None = None, interval_ms: int = 500):
        super().__init__(name="db-poller", daemon=True)
        self.db_path = db_path  # 供 UI（统计面板等）展示数据源路径
        self._db_path = db_path
        self._interval = max(0.1, interval_ms / 1000.0)
        self._lock = threading.Lock()
        self._stop = False
        self._force = False
        self._snapshot: dict | None = None
        self._ready = threading.Event()
        self._error: str | None = None

    # ---------- 主线程调用 ----------
    def stop(self) -> None:
        with self._lock:
            self._stop = True

    def request_force(self) -> None:
        """请求一次强制轮询（手动刷新用），后台线程下个周期执行。"""
        with self._lock:
            self._force = True

    def take_snapshot(self) -> dict | None:
        """取走最新快照（幂等：取走即清空，避免主线程重复消费）。"""
        with self._lock:
            s = self._snapshot
            self._snapshot = None
            return s

    def wait_ready(self, timeout: float = 10.0) -> bool:
        """等待首载完成（首次快照发布）。"""
        return self._ready.wait(timeout)

    # ---------- 后台线程 ----------
    def run(self) -> None:
        # DBWatcher 与 sqlite 连接只在本线程创建使用（connect_readonly 默认线程绑定）
        w = DBWatcher(self._db_path)
        try:
            sessions, rows = w.first_load()
            self._publish("first", parse_rows(rows), sessions)
        except Exception as e:
            self._error = str(e)
            self._publish("error", [], [])
        self._ready.set()

        while True:
            time.sleep(self._interval)
            with self._lock:
                if self._stop:
                    break
                force = self._force
                self._force = False
            try:
                rows = w.poll(force=force)
                if not rows and not force:
                    continue
                self._publish("inc", parse_rows(rows), w.fetch_sessions())
            except Exception as e:
                self._error = str(e)
                self._publish("error", [], [])

    def _publish(self, kind: str, msgs: list, sessions: list) -> None:
        with self._lock:
            self._snapshot = {
                "type": kind,
                "msgs": msgs,
                "sessions": sessions,
                "error": self._error,
            }
