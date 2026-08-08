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
import traceback

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

    # 单实例锁：局部变量持有引用，生命周期覆盖整个 app.exec()（防 GC 提前释放锁）
    lock = _try_acquire_lock()
    _trace(f"lock acquired={lock is not None}")
    if lock is None:
        QMessageBox.warning(None, "OpenCode GO 用量监控", "程序已在运行，请勿重复启动。")
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
