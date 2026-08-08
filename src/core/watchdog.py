"""SSE 反代心跳看门狗。

opencode serve 的 /event 是尽力探测通道（非主通道）：一旦连上，需持续
收到数据确认通道健康。Watchdog 周期巡检上次 kick 时间，超过 timeout
未收到心跳 → 回调 on_stall()（如触发重连/降级提示），随后自动重置计时
继续运行，无需人工重启。

线程安全：kick 可被任意线程调用（SSE 接收线程）；on_stall 在 watchdog
线程内执行，回调异常被 try/except 吞掉，不杀死看门狗。
"""
import threading
import time
from collections.abc import Callable


class Watchdog:
    """看门狗线程：interval 周期巡检，timeout 内无 kick 则触发 on_stall 并自动重置。"""

    def __init__(self, interval: float, timeout: float, on_stall: Callable[[], None]) -> None:
        self.interval = max(interval, 0.1)          # 巡检周期（秒），下限防忙转
        self.timeout = max(timeout, self.interval)  # 无心跳超时（秒），至少一个周期
        self.on_stall = on_stall
        self._stop = threading.Event()
        self._lock = threading.Lock()               # 保护 _last_kick（kick 与巡检跨线程）
        self._last_kick = time.monotonic()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动看门狗线程（幂等：已在运行则直接返回）。"""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        with self._lock:
            self._last_kick = time.monotonic()  # 启动即视为一次心跳
        self._thread = threading.Thread(
            target=self._run, name="opencode-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """停止看门狗并等待线程退出（干净退出，不触发 on_stall）。"""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=self.interval + 1.0)
            self._thread = None

    def kick(self) -> None:
        """外部心跳：收到 SSE 数据时调用，重置超时计时。任意线程可调，线程安全。"""
        with self._lock:
            self._last_kick = time.monotonic()

    def _run(self) -> None:
        """巡检主循环：每 interval 检查一次，超时触发 on_stall 并重置计时。"""
        while not self._stop.wait(self.interval):
            with self._lock:
                idle = time.monotonic() - self._last_kick
            if idle >= self.timeout:
                try:
                    self.on_stall()  # 回调异常不得杀死看门狗线程
                except Exception:
                    pass
                # 触发后重置计时：同一停滞周期只报一次，等待下一次 kick 或再次超时
                with self._lock:
                    self._last_kick = time.monotonic()
