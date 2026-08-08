"""OpenCode GO 用量监控 — 入口。

用法：
    python main.py                  # 使用默认 db 路径
    python main.py --db <path>      # 指定 opencode.db 路径

单实例保护：QLockFile 锁（系统临时目录），第二个实例启动时弹窗警告并退出，
保证托盘图标/端口监听不冲突。
"""
import argparse
import os
import sys
import tempfile
import threading
import traceback

import faulthandler

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from src import db_conn
from src.ui.main_window import MainWindow

# 单实例锁文件路径（temp 目录，避免与项目目录只读/权限问题纠缠）
LOCK_FILE = os.path.join(tempfile.gettempdir(), "opencode-go-monitor.lock")

# 启动轨迹日志（排障用）：固定写 D:\ai，frozen 模式下 _PROJECT_ROOT 会变，
# 不能依赖项目目录。仅当环境变量 OGM_TRACE=1 时启用。
_TRACE_FILE = os.path.join(os.environ.get("TEMP", "C:/Windows/Temp"), "ogm_boot_trace.log")


def _trace(msg: str):
    if os.environ.get("OGM_TRACE") != "1":
        return
    try:
        with open(_TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{msg}]\n")
    except Exception:
        pass


def _try_acquire_lock() -> QLockFile | None:
    """尝试获取单实例锁。成功返回 QLockFile（调用方须持有引用防 GC），失败返回 None。"""
    lock = QLockFile(LOCK_FILE)
    if not lock.tryLock(100):  # 100ms 内拿不到锁 → 判定已有实例在运行
        return None
    return lock


def _start_stack_dump():
    """排障取证：每 20s 转储所有线程堆栈到 stderr。

    主线程冻结时（无 Python traceback 的 UI 卡死），faulthandler 可从后台
    线程转储主线程堆栈，定位冻结点。正式版（windowed）stderr 丢弃，无害。
    """
    import time as _time

    def _loop():
        while True:
            _time.sleep(20)
            try:
                faulthandler.dump_traceback(file=sys.stderr)
            except Exception:
                pass

    threading.Thread(target=_loop, daemon=True).start()


def main():
    _trace("main start")
    parser = argparse.ArgumentParser(description="OpenCode GO 用量监控")
    parser.add_argument("--db", default=db_conn.DEFAULT_DB_PATH,
                        help=f"opencode.db 路径（默认 {db_conn.DEFAULT_DB_PATH}）")
    args = parser.parse_args()
    _trace(f"args.db={args.db}")

    app = QApplication(sys.argv)
    _trace("QApplication created")
    app.setApplicationName("OpenCode GO 用量监控")
    app.setQuitOnLastWindowClosed(False)  # 托盘驻留

    _start_stack_dump()  # 排障取证：每 20s 转储线程堆栈到 stderr

    # 单实例锁：局部变量持有引用，生命周期覆盖整个 app.exec()（防 GC 提前释放锁）
    lock = _try_acquire_lock()
    _trace(f"lock acquired={lock is not None}")
    if lock is None:
        QMessageBox.warning(
            None, "OpenCode GO 用量监控",
            "程序已在运行（可能驻留在系统托盘）。\n\n"
            "如需启动新版本，请先右键托盘鲸鱼图标选择「退出」，"
            "再重新启动本程序。",
        )
        sys.exit(1)

    try:
        win = MainWindow(db_path=args.db)
        _trace("MainWindow created")
        win.show()
        _trace("win.show() done")
    except Exception:
        _trace("MainWindow FAILED: " + traceback.format_exc())
        raise
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
