"""日志面板：跨线程 Qt 日志 Handler + 只读深色日志视图（PySide6）。

职责：
- QtLogHandler：把标准 logging 记录通过 Signal 跨线程转发到 UI 主线程。
  工作线程里 logger.info(...) → handler.emit() → Signal 排队 → 主线程槽刷新 QPlainTextEdit。
  不直接操作控件，天然线程安全（Qt AutoConnection：跨线程自动 QueuedConnection）。
- LogPanel：只读日志视图，setMaximumBlockCount(5000) 限制行数上限，
  按级别着色（ERROR 红 / WARNING 橙 / INFO 蓝 / DEBUG 紫），HTML 转义防注入。

V2 配色参考：#0f1117 深底、#1a1d26 卡片、#2a2d36 边框、绿 #00e09a、蓝 #4dabf7、
橙 #ff922b、紫 #9775fa、红 #ff6b6b。
"""
import html
import logging
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit

# ---- V2 深色配色（与参考图一致）----
COLOR_BG = "#0f1117"
COLOR_CARD = "#1a1d26"
COLOR_BORDER = "#2a2d36"
COLOR_TEXT = "#d8dee9"
COLOR_MUTED = "#8b949e"
COLOR_BLUE = "#4dabf7"
COLOR_ORANGE = "#ff922b"
COLOR_PURPLE = "#9775fa"
COLOR_RED = "#ff6b6b"

# 默认日志格式：时间 级别 来源 | 内容
_DEFAULT_FMT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"
_DEFAULT_DATEFMT = "%H:%M:%S"

LOG_PANEL_QSS = f"""
QWidget {{ background-color: {COLOR_BG}; color: {COLOR_TEXT}; }}
QPlainTextEdit {{ background-color: {COLOR_CARD}; color: {COLOR_TEXT};
    border: 1px solid {COLOR_BORDER}; border-radius: 8px;
    padding: 6px; font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px; }}
QPlainTextEdit:focus {{ border: 1px solid {COLOR_BLUE}; }}
QScrollBar:vertical {{ background: {COLOR_BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {COLOR_BORDER}; border-radius: 5px; }}
QScrollBar::handle:vertical:hover {{ background: {COLOR_BLUE}; }}
"""


def level_color(level) -> str:
    """把日志级别映射为 V2 配色中的告警色。

    参数可以是 int levelno（logging.INFO）或 str 级别名（"ERROR"），
    未知级别统一返回灰色。ERROR/CRITICAL 红、WARNING 橙、INFO 蓝、DEBUG 紫。
    """
    name = level if isinstance(level, str) else logging.getLevelName(level)
    name = str(name).upper()
    if name in ("CRITICAL", "FATAL", "ERROR"):
        return COLOR_RED
    if name in ("WARNING", "WARN"):
        return COLOR_ORANGE
    if name == "INFO":
        return COLOR_BLUE
    if name in ("DEBUG", "NOTSET"):
        return COLOR_PURPLE
    return COLOR_MUTED


class QtLogHandler(QObject, logging.Handler):
    """把 logging 记录跨线程转发到 UI 的 Handler。

    类属性 Signal：message(levelno: int, text: str)。emit 从任意工作线程调用，
    Signal 的自动连接方式会将其排队投递到 UI 所在线程（AutoConnection），
    因此绝不直接触碰控件，线程安全由 Qt 保证。

    用法：:
        logger = logging.getLogger("opencode-proxy")
        handler = QtLogHandler()
        handler.message.connect(panel.append_line)   # 或由 LogPanel.connect_handler 代劳
        logger.addHandler(handler)
    """

    message = Signal(int, str)

    def __init__(self, level: int = logging.NOTSET):
        QObject.__init__(self)
        logging.Handler.__init__(self, level)
        self.setFormatter(logging.Formatter(_DEFAULT_FMT, datefmt=_DEFAULT_DATEFMT))

    def emit(self, record: logging.LogRecord) -> None:
        """logging 回调：格式化后经 Signal 转发到 UI 线程。任何异常都不外抛。"""
        try:
            text = self.format(record)
        except Exception:
            return
        try:
            self.message.emit(record.levelno, text)
        except RuntimeError:
            # QObject 已被销毁（如窗口关闭后日志仍在写入），静默丢弃
            pass


class LogPanel(QWidget):
    """只读日志面板：深色风格、5000 行上限、按级别着色。"""

    def __init__(self, parent: Optional[QWidget] = None, max_lines: int = 5000):
        super().__init__(parent)
        self.setStyleSheet(LOG_PANEL_QSS)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        self.text = QPlainTextEdit(self)
        self.text.setReadOnly(True)
        # 行数上限：超出后自动丢弃最旧行，防止日志面板无限膨胀
        self.text.setMaximumBlockCount(max_lines)
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self.text)

        # 跨线程 Handler：LogPanel 自身作为接收槽
        self._handler = QtLogHandler()
        self._handler.message.connect(self.append_line)

    # ---------- 公开 API ----------
    @property
    def handler(self) -> QtLogHandler:
        """本面板关联的跨线程日志 Handler（可自行连接或设置级别）。"""
        return self._handler

    def connect_handler(self, logger: logging.Logger,
                        level: int = logging.DEBUG) -> QtLogHandler:
        """把本面板的 Handler 挂到指定 logger 上并返回该 Handler。

        同一 logger 重复挂载时自动跳过，避免一条日志显示多遍。
        挂载后自行把 logger 级别放开（NOTSET 会继承 root 导致低级别日志被拦），
        保证传入的 level 生效；也可事后修改 handler.level 调整过滤。
        """
        # logger 未显式设级（NOTSET）时继承 root，可能拦截 INFO/DEBUG —— 显式放开
        if logger.level == logging.NOTSET:
            logger.setLevel(level)
        else:
            logger.setLevel(min(logger.level, level))
        if self._handler not in logger.handlers:
            self._handler.setLevel(level)
            logger.addHandler(self._handler)
        return self._handler

    def append_line(self, level, text: str) -> None:
        """追加一行日志并着色（level 为 int levelno 或 str 级别名）。

        只允许主线程调用（Signal 已保证）；文本经 HTML 转义，防止日志内容
        中的 <> 等字符破坏富文本结构。
        """
        color = level_color(level)
        safe = html.escape(text)
        self.text.appendHtml(f'<span style="color:{color};">{safe}</span>')
        # 保持始终滚动到底部，看到最新日志
        sb = self.text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self) -> None:
        """清空面板内容。"""
        self.text.clear()
