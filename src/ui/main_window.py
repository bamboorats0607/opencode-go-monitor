"""OpenCode GO 用量监控 — 主窗口（PySide6，V2 深色仪表盘风格）。

V2 布局（参考深色仪表盘设计图）：
    顶部栏（Logo+标题+副标题 | 时间切换 24h/7d/30d + 在线状态点 + 刷新）
    + 4 个 KPI 卡（缓存健康度 Score+等级色环 / Token 总量 / 估算成本 / 异常告警数）
    + 订阅三窗口进度条（5h 橙 / 周 紫 / 月 蓝）
    + 最近请求表格（时间/模型/输入/缓存命中/输出/命中率/成本，无逐条打分列）
    + 右侧日志面板（LogPanel，跨线程 Signal）

V2 核心能力：
    - 缓存健康度打分（compute_score）+ 特征检测（detect_features）
    - 三类告警（AlertManager：缓存健康/额度/数据源），横幅主通道 + 托盘气泡可选
    - 端口退避：主监听 8899 池内退避；扩展口 8900 固定，被占硬提示
    - 完整设置面板（SettingsDialog，保存后热加载）
    - 日志：文件按天滚动 + UI 面板
    - 可靠性：QLockFile 单实例（main.py 已集成）、SSE 看门狗、状态指示器

实时性：500ms 文件监听 + 增量查询，5s PRAGMA data_version 兜底（保留 V1）。
"""
import json
import logging
import os
import queue
import sys
import threading
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

from PySide6.QtCore import Qt, QTimer, QSettings, QRectF, QPointF
from PySide6.QtGui import QColor, QIcon, QPixmap, QPainter, QCloseEvent, QBrush, QPen
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QFrame, QTableWidget, QTableWidgetItem, QHeaderView, QPushButton,
    QDialog, QPlainTextEdit, QSystemTrayIcon, QMenu, QProgressBar,
    QButtonGroup, QSplitter, QMessageBox, QTabWidget, QGroupBox,
)

from src.aggregator import hit_rate, aggregate_by_window, aggregate_by_model
from src.watcher import opencode_running, probe_sse
from src.poller import DBPoller
from src.usage_api import fetch_go_page, fetch_usage_page, validate_cookie
from src.go_usage import parse_go_page, fmt_window, fmt_reset_secs
from src.go_usage_list import parse_usage_list, merge_incremental
from src.proxy_capture import LocalProxy, set_cookie_callback
from src.cookie_receiver import CookieReceiver, set_cookie_callback as set_receiver_callback
from src.ui.whale_svg import WHALE_SVG
from src.ui.log_panel import LogPanel
from src.ui.settings_dialog import SettingsDialog
from src.ui.stats_dialog import StatsDialog

# ---- V2 新模块（T0/T1/T2 并行产出）----
from src.core.settings import AppSettings, _PROJECT_ROOT
from src.core.port_manager import PortManager
from src.diagnostics.score import compute_score, detect_features
from src.local_store import LocalStore

try:
    from src.diagnostics.alerts import AlertManager
    HAS_ALERTS = True
except ImportError:  # 防御：alerts 尚未落盘时告警功能降级
    HAS_ALERTS = False

try:
    from src.core.watchdog import Watchdog
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

try:
    import pyqtgraph as pg
    HAS_PG = True
except Exception:  # 图表缺失时降级为无图表 KPI 卡片
    HAS_PG = False

# ---- V2 色板（参考图实测）----
BG = "#0f1117"
CARD = "#1a1d26"
CARD_ALT = "#171a22"
BORDER = "#2a2d36"
HEADER = "#1e2129"
TEXT = "#e6edf3"
MUTED = "#8b949e"
GREEN = "#00e09a"
BLUE = "#4dabf7"
ORANGE = "#ff922b"
RED = "#ff6b6b"
PURPLE = "#9775fa"
YELLOW = "#ffd43b"

# 等级 → 颜色（与 score.grade_of 对应）
GRADE_COLOR = {
    "healthy": GREEN,
    "good": BLUE,
    "attention": ORANGE,
    "critical": RED,
}

# 低命中判定阈值（默认值；运行时从 AppSettings 热加载覆盖）
DEFAULT_LOW_INPUT = 10000
DEFAULT_MISS_RATIO = 0.01

# 时间范围 → 毫秒
TIME_RANGE_MS = {"24h": 24 * 3600 * 1000, "7d": 7 * 24 * 3600 * 1000, "30d": 30 * 24 * 3600 * 1000}

QSS = f"""
QMainWindow, QDialog {{ background-color: {BG}; color: {TEXT}; }}
QWidget {{ color: {TEXT}; }}
QFrame#card {{ background-color: {CARD}; border: 1px solid {BORDER};
    border-radius: 12px; }}
QLabel#kpiCaption {{ font-size: 12px; color: {MUTED}; }}
QLabel#bannerCache {{ background-color: #3d1a1a; color: {RED};
    border-radius: 8px; padding: 8px 12px; font-size: 13px; }}
QLabel#bannerQuota {{ background-color: #3d2f10; color: {ORANGE};
    border-radius: 8px; padding: 8px 12px; font-size: 13px; }}
QLabel#bannerDataSource {{ background-color: #2b2410; color: {YELLOW};
    border-radius: 8px; padding: 8px 12px; font-size: 13px; }}
QLabel#bannerInfo {{ background-color: #16264a; color: {BLUE};
    border-radius: 8px; padding: 8px 12px; font-size: 13px; }}
QTableWidget {{ background-color: {CARD_ALT}; alternate-background-color: #1a1d26;
    gridline-color: {BORDER}; border: none; font-size: 12px; }}
QHeaderView::section {{ background-color: {HEADER}; color: {MUTED};
    border: none; border-bottom: 1px solid {BORDER}; padding: 6px; font-size: 12px; }}
QPushButton {{ background-color: {BLUE}; color: {BG}; border: none;
    border-radius: 6px; padding: 6px 14px; font-weight: bold; }}
QPushButton:hover {{ background-color: #6ec2ff; }}
QPushButton:disabled {{ background-color: #2a2d36; color: #6e7681; }}
QPushButton#rangeBtn {{ background-color: {CARD}; color: {MUTED};
    border: 1px solid {BORDER}; border-radius: 12px; padding: 4px 12px; }}
QPushButton#rangeBtn:checked {{ background-color: {BLUE}; color: {BG};
    border-color: {BLUE}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 10px; margin-top: 10px;
    padding-top: 8px; color: {TEXT}; font-weight: bold; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
QPlainTextEdit, QLineEdit {{ background-color: {CARD_ALT};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 6px; color: {TEXT}; }}
QProgressBar {{ background-color: {CARD_ALT}; border: 1px solid {BORDER};
    border-radius: 6px; text-align: center; color: {TEXT}; font-size: 12px; }}
QProgressBar::chunk {{ border-radius: 6px; }}
QStatusBar {{ background: {BG}; color: {MUTED}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; top: -1px; }}
QTabBar::tab {{ background: {CARD_ALT}; color: {MUTED}; padding: 6px 14px; }}
QTabBar::tab:selected {{ background: {CARD}; color: {BLUE}; }}
QScrollBar:vertical {{ background: {BG}; width: 10px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 5px; }}
"""

logger = logging.getLogger("opencode-ui")


def _fmt_tokens(v: int) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def _fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")


def _make_icon(size: int = 64) -> QIcon:
    """内嵌 SVG 渲染图标，无外部路径依赖（PyInstaller 友好）。"""
    try:
        renderer = QSvgRenderer(bytes(WHALE_SVG.encode("utf-8")))
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        return QIcon(pm)
    except Exception:
        return QIcon()


def _session_model_label(model_json: str | None) -> str:
    if not model_json:
        return ""
    try:
        obj = json.loads(model_json)
        return obj.get("id") or obj.get("modelID") or model_json[:24]
    except Exception:
        return model_json[:24]


def _go_rec_to_usage(r: dict) -> dict:
    """在线调用明细（go_usage_list 原始字段）→ 统一统计字段。

    与 LocalStore 存储口径一致：time_created 毫秒、cost 已 ÷1e8 换算为美元。
    """
    try:
        ts = datetime.fromisoformat(r["timeCreated"].replace("Z", "+00:00"))
        ts_ms = int(ts.timestamp() * 1000)
    except Exception:
        ts_ms = 0
    return {
        "id": r["id"],
        "time_created": ts_ms,
        "modelID": r["model"] or "?",
        "tokens_input": int(r["inputTokens"] or 0),
        "tokens_output": int(r["outputTokens"] or 0),
        "tokens_reasoning": int(r.get("reasoningTokens") or 0),
        "cache_read": int(r["cacheReadTokens"] or 0),
        "cache_write": int(r.get("cacheWrite5mTokens") or r.get("cacheWrite1hTokens") or 0),
        "cost": float(r.get("cost") or 0) / 100_000_000,
        "session_id": r.get("sessionID") or "unknown",
    }


class KpiRing(QWidget):
    """KPI 色环：QPainter 绘制 0-100 弧度圆环 + 中心大字。"""

    def __init__(self, parent=None, radius: int = 30, text: str = "—",
                 color: str = BLUE, caption: str = ""):
        super().__init__(parent)
        self._radius = radius
        self._value = 0.0
        self._text = text
        self._color = QColor(color)
        self._caption = caption
        self.setFixedSize(radius * 2 + 16, radius * 2 + 16)

    def set_value(self, value: float, text: str, color: str):
        self._value = max(0.0, min(100.0, float(value)))
        self._text = text
        self._color = QColor(color)
        self.update()

    def set_caption(self, caption: str):
        self._caption = caption
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        center = QPointF(self.width() / 2, self.height() / 2)
        r = side / 2 - 6
        # 底环
        p.setPen(QPen(QColor(BORDER), 5, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        p.drawArc(QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r), 0, 360 * 16)
        # 值环（-90° 起顺时针）
        p.setPen(QPen(self._color, 5, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.RoundCap))
        span = int(360 * 16 * self._value / 100.0)
        p.drawArc(QRectF(center.x() - r, center.y() - r, 2 * r, 2 * r),
                  -90 * 16, -span)
        # 中心文字
        p.setPen(QColor(TEXT))
        f = p.font()
        f.setBold(True)
        f.setPointSize(11)
        p.setFont(f)
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
        p.end()


class CookieDialog(QDialog):
    """Cookie 设置：优先本地代理自动捕获，也可手动粘贴。禁止解密/提权。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("在线 API — Cookie 设置")
        self.setMinimumSize(560, 320)
        self.setStyleSheet(QSS)
        self.settings = QSettings("opencode-go-monitor", "opencode-go-monitor")
        lay = QVBoxLayout(self)
        tip = QLabel(
            "两种方式获取 opencode.ai 的 auth Cookie：\n"
            "1. 自动捕获（推荐）：关闭本窗口，点「启用代理捕获」，把浏览器代理设为 "
            "127.0.0.1:8899，然后访问 opencode.ai 的 Go 用量页，Cookie 自动保存。\n"
            "2. 手动粘贴：把 opencode.ai 的 auth cookie 值（Fe26.2** 开头）粘到下面。\n"
            "程序不做任何自动导出/解密，Cookie 仅存本机 QSettings。"
        )
        tip.setWordWrap(True)
        tip.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        lay.addWidget(tip)
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("在此粘贴 auth cookie（Fe26.2** 开头，仅粘贴 value）…")
        self.edit.setMaximumHeight(100)
        saved = self.settings.value("cookie", "")
        if saved:
            self.edit.setPlainText(saved)
        lay.addWidget(self.edit)
        row = QHBoxLayout()
        self.btn_test = QPushButton("测试连接")
        self.btn_save = QPushButton("保存并应用")
        self.btn_close = QPushButton("关闭")
        self.btn_test.clicked.connect(self._test)
        self.btn_save.clicked.connect(self._save)
        self.btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_test)
        row.addWidget(self.btn_save)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        lay.addLayout(row)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)

    def _test(self):
        ws = self.settings.value("workspace_id", MainWindow.DEFAULT_WORKSPACE_ID)
        self.result.setText("正在拉取 opencode.ai 的 Go 用量页 …")
        QApplication.processEvents()
        ok, payload, status = fetch_go_page(self.edit.toPlainText().strip(), ws)
        if ok:
            from src.go_usage import parse_go_page, fmt_window
            d = parse_go_page(payload)
            self.result.setStyleSheet(f"color: {BLUE};")
            r = d.get("rollingUsage")
            w = d.get("weeklyUsage")
            m = d.get("monthlyUsage")
            self.result.setText(
                f"✓ Cookie 有效（HTTP 200）。\n5h窗口 {fmt_window(r)} | 周 {fmt_window(w)} | 月 {fmt_window(m)}"
            )
        else:
            self.result.setStyleSheet(f"color: {RED};")
            self.result.setText(f"✗ {payload}")
            if status in (401, 403):
                self.result.setText(f"✗ {payload}\n提示：请重新登录 opencode.ai 后复制新 Cookie。")

    def _save(self):
        self.settings.setValue("cookie", self.edit.toPlainText().strip())
        self.result.setStyleSheet(f"color: {BLUE};")
        self.result.setText("✓ Cookie 已保存。")
        self.accept()

    def cookie(self) -> str:
        return self.edit.toPlainText().strip()


class MainWindow(QMainWindow):
    DEFAULT_WORKSPACE_ID = "wrk_01KZAA0VA6E8JQNDSPW60RX3K8"

    def __init__(self, db_path: str | None = None):
        super().__init__()
        self.setWindowTitle("OpenCode GO 用量监控")
        self.setWindowIcon(_make_icon())
        self.resize(1360, 900)
        self.setStyleSheet(QSS)

        self.app_settings = AppSettings()
        all_s = self.app_settings.get_all()
        self.thresholds = dict(all_s["thresholds"])
        self.workspace_id = all_s["workspace_id"]
        self.time_range = all_s["time_range"]
        self.notifications_enabled = bool(all_s["notifications_enabled"])
        self.close_to_tray = bool(all_s["close_to_tray"])
        self.log_file = str(all_s["log_file"])
        self.log_backup_days = int(all_s["log_backup_days"])

        # 告警管理器（cooldown/abs_min 从阈值热加载）
        if HAS_ALERTS:
            self.alerts = AlertManager(
                cooldown_min=float(self.thresholds.get("cooldown_min", 30)),
                notifications_enabled=self.notifications_enabled,
                abs_min=float(self.thresholds.get("abs_min", 55)),
            )
        else:
            self.alerts = None

        self._setup_logging()

        # 在线明细持久化（独立 SQLite，30 日 LRU 淘汰；写入由轮询后台线程异步执行）
        store_path = os.path.join(_PROJECT_ROOT, "data", "usage.db")
        try:
            self.local_store = LocalStore(store_path)
        except Exception as e:
            self.local_store = None
            logger.warning("local_store init failed: %s", e)
        # 异步轮询：DBWatcher/sqlite 连接独占后台线程，主线程只消费快照渲染
        self.poller = DBPoller(
            db_path, int(all_s["refresh_interval_ms"]), store=self.local_store)
        self.msgs: list[dict] = []
        self.sessions: list = []
        self._tick_count = 0
        self._quitting = False
        self._dirty = False
        # 明细表格分页加载状态（防全量加载 OOM）
        self._msg_page = 0
        self._msg_page_size = 200
        self.go_data: dict | None = None
        self.go_usage_list: list[dict] = []   # 在线调用明细（全部客户端，含 Trae）
        self._go_usage_ok = False             # 在线明细是否成功拉取
        self.go_status_text = "未连接在线额度（可在设置中启用代理自动捕获 Cookie）"
        self._cookie_last_status: int | None = None
        self._cookie_queue: "queue.Queue[str]" = queue.Queue()
        self._go_usage_queue: "queue.Queue[dict]" = queue.Queue()  # 后台拉取结果回主线程
        self._go_refreshing = False
        self._health_db = 1.0
        self._health_api = 1.0
        self._health_sse = 1.0
        self._last_score: dict | None = None
        self._last_feature_hits: list[dict] = []
        self._recent_alerts: list[dict] = []
        self._alert_seen: dict[str, float] = {}   # 托盘气泡去重：title -> ts

        # 端口退避
        self._proxy_pool = list(all_s["proxy_port_pool"])
        self._extension_port = int(all_s["extension_port"])
        self.proxy = LocalProxy(self._proxy_pool[0])
        self.proxy_port = self._proxy_pool[0]
        set_cookie_callback(self._on_captured_cookie)
        self.receiver = CookieReceiver(self._extension_port)
        set_receiver_callback(self._on_captured_cookie)
        if not self.receiver.start():
            self.statusBar().showMessage(
                f"Cookie 接收端 127.0.0.1:{self._extension_port} 启动失败（端口被占用）")
            QTimer.singleShot(300, self._warn_extension_port)

        self._build_ui()
        self._setup_tray()

        # 定时器
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(int(all_s["refresh_interval_ms"]))
        # 渲染节流：数据变更合并为一次渲染，避免高频全量重建表格
        self.render_timer = QTimer(self)
        self.render_timer.setSingleShot(True)
        self.render_timer.timeout.connect(self._do_render)
        self.proc_timer = QTimer(self)
        self.proc_timer.timeout.connect(self._check_process)
        self.proc_timer.start(3000)
        self.go_timer = QTimer(self)
        self.go_timer.timeout.connect(self._refresh_go_usage)
        self.go_timer.start(int(all_s["go_refresh_interval_s"]) * 1000)
        self.diag_timer = QTimer(self)
        self.diag_timer.timeout.connect(self._run_diagnostics)
        self.diag_timer.start(10000)

        # SSE 看门狗（尽力探测，非主通道）
        self._watchdog = None
        QTimer.singleShot(1500, self._setup_watchdog)

        # 首载：后台线程异步完成（首载快照到达后由 _drain_db_queue 消费渲染）
        self.poller.start()
        self._refresh()

        QTimer.singleShot(1000, self._probe_sse)
        self._check_process()
        self._apply_saved_cookie_state()
        QTimer.singleShot(500, self._refresh_go_usage)
        QTimer.singleShot(1500, self._run_diagnostics)

    # ---------- 日志 ----------
    def _setup_logging(self):
        """文件按天滚动（midnight, backupCount）+ UI 面板 handler。"""
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        try:
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            fh = TimedRotatingFileHandler(
                self.log_file, when="midnight", backupCount=self.log_backup_days,
                encoding="utf-8")
            fh.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-8s %(name)s | %(message)s"))
            root.addHandler(fh)
        except Exception as e:
            logger.warning("文件日志不可用: %s", e)

    # ---------- 代理与 Cookie 自动捕获 ----------
    def _warn_extension_port(self):
        QMessageBox.warning(
            self, "扩展端口被占用",
            f"扩展 Cookie 接收端口 {self._extension_port} 被占用，"
            "浏览器扩展 Cookie 通道不可用。\n"
            "可稍后关闭占用程序后在「设置」中修改端口，或改用本地代理捕获方式。")

    def _on_captured_cookie(self, cookie: str):
        if cookie:
            self._cookie_queue.put(cookie)

    def _drain_cookie_queue(self):
        try:
            while True:
                cookie = self._cookie_queue.get_nowait()
                self._handle_captured_cookie(cookie)
        except Exception:
            pass  # queue.Empty

    def _handle_captured_cookie(self, cookie: str):
        self.app_settings.set("cookie", cookie)
        self.statusBar().showMessage("✅ 已捕获 opencode.ai Cookie，正在拉取用量…")
        self._refresh_go_usage()

    def _open_proxy_settings(self):
        """启动/停止本地代理（端口退避：池内找可用端口）。"""
        if self.proxy.running():
            self.proxy.stop()
            self.btn_proxy.setText("启用代理捕获")
            self.lbl_proxy_status.setText("代理已停止")
            self.lbl_proxy_status.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            return
        pm = PortManager(self._proxy_pool, self._extension_port)
        port = pm.find_free_port()
        if port is None:
            self.statusBar().showMessage("代理端口池全占用（8899/8910/8920/8930/8940）")
            return
        self.proxy = LocalProxy(port)
        self.proxy_port = port
        if self.proxy.start():
            self.btn_proxy.setText("停止代理")
            self.lbl_proxy_status.setText(
                f"代理运行中：127.0.0.1:{port}\n"
                "浏览器代理设置指向此地址，然后访问 opencode.ai 的 Go 用量页即可自动捕获 Cookie")
            self.lbl_proxy_status.setStyleSheet(f"color: {BLUE}; font-size: 12px;")
        else:
            self.statusBar().showMessage(f"代理启动失败：端口 {port} 被占用")

    def _refresh_go_usage(self):
        """拉取 go 用量（后台线程执行，避免阻塞 UI 主线程）。

        go 页面 → 三窗口额度；usage 页面 → 全部客户端（opencode CLI/Trae 等）
        走 go 订阅网关的调用明细，作为"最近请求"表格的在线数据源。
        网络请求在 daemon 线程跑，结果经 _go_usage_queue 回主线程应用。
        """
        if getattr(self, "_go_refreshing", False):
            return  # 上一轮未结束则跳过（防重入，避免主线程堆积）
        self._go_refreshing = True
        threading.Thread(target=self._go_fetch_worker, daemon=True).start()

    def _go_fetch_worker(self):
        """后台线程：发网络请求 + 解析，结果入队回主线程。"""
        try:
            cookie = self.app_settings.get("cookie", "")
            if not cookie:
                self._go_usage_queue.put({
                    "ok": True, "go_data": None, "new_recs": [],
                    "status_text": "未配置 Cookie：启用代理捕获或手动粘贴后自动拉取",
                    "status": None,
                })
                return
            ok, payload, status = fetch_go_page(cookie, self.workspace_id)
            go_data = parse_go_page(payload) if ok else None
            status_text = payload  # 失败时 payload 即原因文本
            ok2, html2, _status2 = fetch_usage_page(cookie, self.workspace_id)
            new_recs = parse_usage_list(html2) if ok2 else []
            self._go_usage_queue.put({
                "ok": ok, "go_data": go_data, "new_recs": new_recs,
                "status_text": status_text, "status": status,
                "usage_ok": ok2,
            })
        except Exception as e:
            self._go_usage_queue.put({
                "ok": False, "go_data": None, "new_recs": [],
                "status_text": f"在线拉取异常：{e}", "status": None,
            })
        finally:
            self._go_refreshing = False

    def _drain_go_queue(self):
        """主线程轮询：应用后台拉取结果并更新 UI（由 _tick 调用）。"""
        try:
            while True:
                r = self._go_usage_queue.get_nowait()
                self._apply_go_result(r)
        except Exception:
            pass  # queue.Empty

    def _apply_go_result(self, r: dict):
        go_data = r.get("go_data")
        if go_data is not None:
            self.go_data = go_data
            self._cookie_last_status = 200
            self._health_api = 1.0
            self.banner_cookie.setVisible(False)
        else:
            status = r.get("status")
            self.go_data = None
            self._cookie_last_status = status
            self.go_status_text = r.get("status_text", "")
            if status in (401, 403):
                self._health_api = 0.0
                self.banner_cookie.setVisible(True)
        new_recs = r.get("new_recs") or []
        if new_recs:
            self.go_usage_list = merge_incremental(
                getattr(self, "go_usage_list", []), new_recs)
            self._go_usage_ok = True
            self.go_status_text = (
                f"在线额度已连接 · 全量调用 {len(self.go_usage_list)} 条"
                "（含 opencode CLI / Trae 等所有客户端）")
            # 异步持久化：仅入队，写入由轮询后台线程执行（30 日 LRU 淘汰）
            self.poller.submit([_go_rec_to_usage(r) for r in new_recs])
        elif r.get("usage_ok") is not None and not r["usage_ok"]:
            self._go_usage_ok = False
        if go_data is None and not new_recs and r.get("ok"):
            self.go_status_text = r.get("status_text", self.go_status_text)
        self._update_go_cards()
        self._update_msg_table()

    # ---------- UI 构建 ----------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 8)
        root.setSpacing(8)

        # 横幅区（告警主通道）
        self.banner_cache = QLabel("")
        self.banner_cache.setObjectName("bannerCache")
        self.banner_cache.setVisible(False)
        self.banner_quota = QLabel("")
        self.banner_quota.setObjectName("bannerQuota")
        self.banner_quota.setVisible(False)
        self.banner_datasource = QLabel("")
        self.banner_datasource.setObjectName("bannerDataSource")
        self.banner_datasource.setVisible(False)
        self.banner_process = QLabel("未检测到 opencode 进程（打开 OpenCode 后自动恢复监控）")
        self.banner_process.setObjectName("bannerInfo")
        self.banner_process.setVisible(False)
        self.banner_cookie = QLabel("在线 API Cookie 已失效（401/403），已降级为纯本地模式，不影响本地诊断")
        self.banner_cookie.setObjectName("bannerDataSource")
        self.banner_cookie.setVisible(False)
        for b in (self.banner_cache, self.banner_quota, self.banner_datasource,
                  self.banner_process, self.banner_cookie):
            b.setWordWrap(True)
            root.addWidget(b)

        root.addLayout(self._build_header())
        root.addLayout(self._build_kpi_row())
        root.addLayout(self._build_quota_row())
        self.lbl_proxy_status = QLabel(
            "代理未启动。启用后可自动捕获浏览器访问 opencode.ai 时的 Cookie。")
        self.lbl_proxy_status.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        root.addWidget(self.lbl_proxy_status)

        # 主体：左表格 + 右日志 Tab
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setSizes([820, 520])
        root.addWidget(split, 1)

    def _build_header(self) -> QHBoxLayout:
        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(_make_icon(36).pixmap(36, 36))
        title = QLabel("OpenCode GO 用量监控")
        title.setStyleSheet(f"font-size: 17px; font-weight: bold; color: {TEXT};")
        sub = QLabel("本地 opencode.db 只读监控 · 缓存命中诊断")
        sub.setStyleSheet(f"font-size: 12px; color: {MUTED};")
        tcol = QVBoxLayout()
        tcol.setSpacing(0)
        tcol.addWidget(title)
        tcol.addWidget(sub)
        head.addWidget(logo)
        head.addLayout(tcol)
        head.addStretch(1)

        # 时间切换胶囊
        self.btn_range_24 = QPushButton("24h")
        self.btn_range_7d = QPushButton("7d")
        self.btn_range_30d = QPushButton("30d")
        for b in (self.btn_range_24, self.btn_range_7d, self.btn_range_30d):
            b.setObjectName("rangeBtn")
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.range_group = QButtonGroup(self)
        self.range_group.addButton(self.btn_range_24, 0)
        self.range_group.addButton(self.btn_range_7d, 1)
        self.range_group.addButton(self.btn_range_30d, 2)
        self.range_group.buttonClicked.connect(self._on_range_changed)
        if self.time_range == "7d":
            self.btn_range_7d.setChecked(True)
        elif self.time_range == "30d":
            self.btn_range_30d.setChecked(True)
        else:
            self.btn_range_24.setChecked(True)
        head.addWidget(self.btn_range_24)
        head.addWidget(self.btn_range_7d)
        head.addWidget(self.btn_range_30d)

        # 在线状态点
        self.lbl_status_dot = QLabel("●")
        self.lbl_status_dot.setStyleSheet(f"color: {GREEN}; font-size: 16px;")
        self.lbl_status_text = QLabel("本地监控中")
        self.lbl_status_text.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        head.addSpacing(8)
        head.addWidget(self.lbl_status_dot)
        head.addWidget(self.lbl_status_text)

        self.btn_stats = QPushButton("统计")
        self.btn_stats.clicked.connect(self._open_stats_dialog)
        head.addWidget(self.btn_stats)
        self.btn_settings = QPushButton("设置")
        self.btn_settings.clicked.connect(self._open_settings_dialog)
        head.addWidget(self.btn_settings)
        self.btn_proxy = QPushButton("启用代理捕获")
        self.btn_proxy.clicked.connect(self._open_proxy_settings)
        head.addWidget(self.btn_proxy)
        self.btn_cookie = QPushButton("设置 Cookie")
        self.btn_cookie.clicked.connect(self._open_cookie_dialog)
        head.addWidget(self.btn_cookie)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self._manual_refresh)
        head.addWidget(self.btn_refresh)
        return head

    def _build_kpi_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        # 1) 缓存健康度（色环）
        card1 = QFrame()
        card1.setObjectName("card")
        c1 = QHBoxLayout(card1)
        c1.setContentsMargins(14, 12, 14, 12)
        self.kpi_ring = KpiRing(radius=30, text="—", color=BLUE, caption="缓存健康度")
        c1.addWidget(self.kpi_ring)
        vcol = QVBoxLayout()
        self.lbl_score_val = QLabel("—")
        self.lbl_score_val.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {TEXT};")
        self.lbl_score_cap = QLabel("缓存健康度")
        self.lbl_score_cap.setObjectName("kpiCaption")
        vcol.addWidget(self.lbl_score_val)
        vcol.addWidget(self.lbl_score_cap)
        c1.addLayout(vcol)
        c1.addStretch(1)
        row.addWidget(card1, 1)

        # 2) Token 总量
        row.addWidget(self._kpi_card("Token 总量", "—", key="token"))
        # 3) 估算成本
        row.addWidget(self._kpi_card("估算成本", "—", key="cost"))
        # 4) 异常告警数
        row.addWidget(self._kpi_card("异常告警", "—", key="alerts"))
        return row

    def _kpi_card(self, caption: str, value: str, key: str) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        v = QLabel(value)
        v.setObjectName("kpiValue")
        v.setStyleSheet(f"font-size: 24px; font-weight: bold; color: {TEXT};")
        c = QLabel(caption)
        c.setObjectName("kpiCaption")
        cl.addWidget(v)
        cl.addWidget(c)
        cl.addStretch(1)
        if key == "token":
            self.kpi_token = v
        elif key == "cost":
            self.kpi_cost = v
        elif key == "alerts":
            self.kpi_alerts = v
        return card

    def _build_quota_row(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(4)
        title = QLabel("订阅用量")
        title.setStyleSheet(f"font-size: 13px; font-weight: bold; color: {TEXT};")
        col.addWidget(title)
        self.quota_bars = {}
        for key, caption, color in [
            ("rollingUsage", "5小时窗口", ORANGE),
            ("weeklyUsage", "周窗口", PURPLE),
            ("monthlyUsage", "月窗口", BLUE),
        ]:
            h = QHBoxLayout()
            lbl = QLabel(caption)
            lbl.setStyleSheet(f"color: {MUTED}; font-size: 12px; min-width: 70px;")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(True)
            bar.setFormat("—")
            bar.setFixedHeight(12)
            bar.setStyleSheet(
                f"QProgressBar {{ background-color: {CARD_ALT}; border: 1px solid {BORDER};"
                f" border-radius: 6px; text-align: center; color: {TEXT}; font-size: 11px; }}"
                f"QProgressBar::chunk {{ background-color: {color}; border-radius: 6px; }}")
            txt = QLabel("—")
            txt.setStyleSheet(f"color: {MUTED}; font-size: 12px; min-width: 170px;")
            h.addWidget(lbl)
            h.addWidget(bar, 1)
            h.addWidget(txt)
            self.quota_bars[key] = (bar, txt)
            col.addLayout(h)
        self.lbl_go_status = QLabel("")
        self.lbl_go_status.setStyleSheet(f"color: {MUTED}; font-size: 11px;")
        col.addWidget(self.lbl_go_status)
        return col

    def _build_left(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 4, 0)
        lay.setSpacing(6)

        g_msg = QGroupBox("最近请求（时间范围过滤 · 低命中标红 · 滚动加载更多）")
        mv = QVBoxLayout(g_msg)
        self.table_msgs = QTableWidget(0, 7)
        self.table_msgs.setHorizontalHeaderLabels(
            ["时间", "模型", "输入", "缓存命中", "输出", "命中率", "成本"])
        self.table_msgs.setAlternatingRowColors(True)
        self.table_msgs.verticalHeader().setVisible(False)
        self.table_msgs.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        mh = self.table_msgs.horizontalHeader()
        mh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        mh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for i in range(2, 7):
            mh.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        # 分页加载：滚动到底自动追加下一页（防全量加载 OOM）
        self.table_msgs.verticalScrollBar().valueChanged.connect(self._on_msg_scroll)
        mv.addWidget(self.table_msgs)
        lay.addWidget(g_msg, 1)

        # 会话聚合（保留 V1）
        g_ses = QGroupBox("Session 聚合（命中率 = cache.read / (input + cache.read)）")
        sv = QVBoxLayout(g_ses)
        self.table_sessions = QTableWidget(0, 8)
        self.table_sessions.setHorizontalHeaderLabels(
            ["会话标题", "模型", "input", "output", "cache.read", "cache.write", "成本 $", "命中率"])
        self.table_sessions.setAlternatingRowColors(True)
        self.table_sessions.verticalHeader().setVisible(False)
        self.table_sessions.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch)
        sv.addWidget(self.table_sessions)
        lay.addWidget(g_ses, 1)
        return panel

    def _build_right(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(4, 0, 0, 0)
        lay.setSpacing(6)

        tabs = QTabWidget()
        # 日志面板
        self.log_panel = LogPanel()
        # 挂到 root logger：所有模块日志（继承 root handlers）进 UI 面板
        self.log_panel.connect_handler(logging.getLogger(), level=logging.INFO)
        tabs.addTab(self.log_panel, "日志")
        # 多维聚合
        agg_w = QWidget()
        av = QVBoxLayout(agg_w)
        self.table_models = QTableWidget(0, 7)
        self.table_models.setHorizontalHeaderLabels(
            ["模型", "消息数", "input", "cache.read", "cache.write", "成本 $", "命中率"])
        self.table_windows = QTableWidget(0, 5)
        self.table_windows.setHorizontalHeaderLabels(
            ["窗口（小时）", "消息数", "input", "cache.read", "命中率"])
        for t in (self.table_models, self.table_windows):
            t.setAlternatingRowColors(True)
            t.verticalHeader().setVisible(False)
            t.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.ResizeToContents)
        av.addWidget(self.table_models, 1)
        av.addWidget(self.table_windows, 1)
        tabs.addTab(agg_w, "聚合")
        lay.addWidget(tabs)
        return panel

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(_make_icon(), self)
        self.tray.setToolTip("OpenCode GO 用量监控")
        menu = QMenu()
        act_show = menu.addAction("显示主界面")
        act_quit = menu.addAction("退出")
        act_show.triggered.connect(self._show_from_tray)
        act_quit.triggered.connect(self._really_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._show_from_tray() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    # ---------- 数据刷新 ----------
    def _tick(self):
        self._tick_count += 1
        if self._quitting:
            return
        self._drain_cookie_queue()
        self._drain_go_queue()
        self._drain_db_queue()

    def _drain_db_queue(self):
        """消费后台轮询快照：累积消息/会话，节流调度渲染（主线程只做渲染）。

        轮询/解析/会话查询全部在后台线程（poller）执行，主线程不碰 sqlite。
        """
        snap = self.poller.take_snapshot()
        if not snap:
            return
        if snap.get("type") == "error":
            self._health_db = 0.0
            self.statusBar().showMessage(f"数据库读取异常：{snap.get('error')}")
            return
        self._health_db = 1.0
        new_msgs = snap.get("msgs") or []
        if new_msgs:
            self.msgs.extend(new_msgs)
            # 本地消息内存上限：超过 10 万条丢弃最旧（防长期运行 OOM）
            MAX_LOCAL_MSGS = 100_000
            if len(self.msgs) > MAX_LOCAL_MSGS:
                self.msgs = self.msgs[-MAX_LOCAL_MSGS:]
        if snap.get("sessions"):
            self.sessions = snap["sessions"]
        self._schedule_render()

    def _schedule_render(self):
        """渲染节流：多帧数据变更合并为一次渲染，避免每 500ms 全量重建表格。"""
        self._dirty = True
        if not self.render_timer.isActive():
            self.render_timer.start(800)

    def _do_render(self):
        self.render_timer.stop()
        if self._quitting or not self._dirty:
            return
        self._dirty = False
        self._refresh()

    def _manual_refresh(self):
        """手动刷新：强制后台轮询一次（force 快照总是携带最新 sessions）。"""
        self.poller.request_force()
        QTimer.singleShot(300, self._drain_db_queue)
        QTimer.singleShot(400, self._run_diagnostics)
        self.statusBar().showMessage("正在刷新…")

    def _refresh(self):
        self._update_kpi()
        self._update_go_cards()
        self._update_session_table()
        self._update_model_table()
        self._update_window_table()
        self._update_msg_table()
        self._update_status_dot()

    # ---------- 时间范围过滤 ----------
    def _diagnosis_source(self) -> list[dict]:
        """诊断数据源：在线全量调用（持久化库→实时，含 Trae）优先，本地 db 回退。

        统一字段与诊断引擎口径一致（tokens_input/cache_read/cache_write/
        time_created/session_id/id/modelID/cost），并按 self.time_range 过滤。
        """
        online = self._online_source()
        if online:
            ms = TIME_RANGE_MS.get(self.time_range)
            now = int(time.time() * 1000)
            out = []
            for r in online:
                ts_ms = r.get("time_created") or 0
                if ms and ts_ms < now - ms:
                    continue
                out.append({
                    "tokens_input": r["tokens_input"],
                    "tokens_output": r["tokens_output"],
                    "cache_read": r["cache_read"],
                    "cache_write": r["cache_write"],
                    "time_created": ts_ms,
                    "session_id": r["session_id"],
                    "id": r["id"],
                    "modelID": r["modelID"],
                    "cost": r["cost"],
                })
            return out
        return self._filter_by_range()

    def _filter_by_range(self) -> list[dict]:
        """按 self.time_range 过滤消息（毫秒时间戳）。"""
        if not self.msgs:
            return []
        ms = TIME_RANGE_MS.get(self.time_range)
        if not ms:
            return list(self.msgs)
        now = int(time.time() * 1000)
        cutoff = now - ms
        return [m for m in self.msgs if (m.get("time_created") or 0) >= cutoff]

    def _online_source(self) -> list[dict]:
        """在线数据源（统一字段口径）：持久化库（30 天，启动即有历史）优先。

        返回字段：id/time_created(ms)/modelID/tokens_input/tokens_output/
        tokens_reasoning/cache_read/cache_write/cost/session_id。
        LocalStore 为在线明细的本地镜像（后台线程异步写入），启动即可读取历史，
        不依赖首次在线拉取完成；实时拉取结果经内存 go_usage_list 兜底。
        """
        try:
            if getattr(self, "local_store", None):
                recs = self.local_store.fetch_records()
                if recs:
                    return recs
        except Exception:
            pass
        if self._go_usage_ok and self.go_usage_list:
            return [_go_rec_to_usage(r) for r in self.go_usage_list]
        return []

    def _agg_source(self) -> list[dict]:
        """聚合表格数据源：在线（持久化库→实时）优先，本地 db 回退。"""
        recs = self._online_source()
        return recs if recs else self.msgs

    def _on_range_changed(self, btn):
        mapping = {self.btn_range_24: "24h", self.btn_range_7d: "7d", self.btn_range_30d: "30d"}
        self.time_range = mapping.get(btn, "24h")
        self.app_settings.set("time_range", self.time_range)
        self._refresh()

    # ---------- 诊断集成（V2 核心）----------
    def _run_diagnostics(self):
        """打分 + 特征检测 + 告警 → KPI 健康度卡、告警卡、横幅、状态点。"""
        if self._quitting:
            return
        try:
            filtered = self._diagnosis_source()
            # quota 从 go_data 构造（用滚动窗口百分比近似）
            quota = None
            quota_status = None
            if isinstance(self.go_data, dict):
                rw = self.go_data.get("rollingUsage")
                if isinstance(rw, dict) and isinstance(rw.get("usagePercent"), (int, float)):
                    quota = {"used": float(rw["usagePercent"]), "limit": 100.0}
                if self._cookie_last_status in (401, 403):
                    quota_status = "restricted"
            health = {"db": self._health_db, "api": self._health_api, "sse": self._health_sse}

            if filtered:
                self._last_score = compute_score(
                    filtered, quota=quota, quota_status=quota_status, health=health)
            else:
                self._last_score = None
            self._last_feature_hits = detect_features(
                filtered, thresholds=self.thresholds) if filtered else []

            # 告警
            if self.alerts is not None:
                new_alerts = self.alerts.evaluate(
                    self._last_score, self._last_feature_hits,
                    self.go_data, health, quota_status=quota_status)
                self._recent_alerts = self.alerts.current_alerts()
                self._maybe_tray_notify(new_alerts)
            else:
                self._recent_alerts = []
        except Exception as e:
            logger.warning("诊断计算失败: %s", e)

        self._update_diag_kpi()
        self._update_banners()
        self._update_status_dot()

    def _maybe_tray_notify(self, new_alerts: list[dict]):
        if not self.notifications_enabled:
            return
        if not self.tray.isVisible() or not self.tray.supportsMessages():
            return
        for a in new_alerts:
            if a.get("severity") == "critical" or a.get("severity") == "warning":
                key = a.get("title", "")
                now = time.time()
                if key and (now - self._alert_seen.get(key, 0)) > 300:  # 5min 去重
                    self._alert_seen[key] = now
                    self.tray.showMessage(
                        a.get("title", "告警"), a.get("message", ""),
                        QSystemTrayIcon.MessageIcon.Warning, 5000)

    def _update_diag_kpi(self):
        s = self._last_score
        if s is not None:
            score = int(s["score"])
            grade = s["grade"]
            color = GRADE_COLOR.get(grade, BLUE)
            self.kpi_ring.set_value(score, str(score), color)
            self.lbl_score_val.setText(f"{score} · {s['grade_cn']}")
            self.lbl_score_val.setStyleSheet(
                f"font-size: 24px; font-weight: bold; color: {color};")
            if s.get("untestable_cw"):
                self.lbl_score_cap.setText("缓存健康度（cache_write 不可测）")
            else:
                self.lbl_score_cap.setText("缓存健康度")
        else:
            self.kpi_ring.set_value(0, "—", BLUE)
            self.lbl_score_val.setText("—")
            self.lbl_score_val.setStyleSheet(
                f"font-size: 24px; font-weight: bold; color: {TEXT};")
            self.lbl_score_cap.setText("缓存健康度（暂无数据）")

        warn_n = sum(1 for a in self._recent_alerts
                     if a.get("severity") in ("warning", "critical"))
        self.kpi_alerts.setText(str(warn_n))
        color = GREEN if warn_n == 0 else (ORANGE if warn_n < 3 else RED)
        self.kpi_alerts.setStyleSheet(
            f"font-size: 24px; font-weight: bold; color: {color};")

    def _update_banners(self):
        if self.alerts is None:
            return
        cache_alerts = [a for a in self._recent_alerts if a.get("kind") == "cache"]
        quota_alerts = [a for a in self._recent_alerts if a.get("kind") == "quota"]
        ds_alerts = [a for a in self._recent_alerts if a.get("kind") == "datasource"]

        if cache_alerts:
            self.banner_cache.setText(" ⚠ " + "；".join(a.get("message", "")
                                                       for a in cache_alerts))
            self.banner_cache.setVisible(True)
        else:
            self.banner_cache.setVisible(False)

        if quota_alerts:
            self.banner_quota.setText(" ⚠ " + "；".join(a.get("message", "")
                                                       for a in quota_alerts))
            self.banner_quota.setVisible(True)
        else:
            self.banner_quota.setVisible(False)

        if ds_alerts:
            self.banner_datasource.setText(" ⚠ " + "；".join(a.get("message", "")
                                                            for a in ds_alerts))
            self.banner_datasource.setVisible(True)
        else:
            self.banner_datasource.setVisible(False)

    def _update_status_dot(self):
        """状态点：绿=全健康 / 橙=在线降级 / 红=数据库异常。"""
        if self._go_usage_ok:
            color, txt = GREEN, "在线全量监控中（含 Trae 等全部客户端）"
        elif self._health_db < 0.5:
            color, txt = RED, "数据库异常"
        elif self._health_api < 0.5 or self._cookie_last_status in (401, 403):
            color, txt = ORANGE, "在线 API 已降级"
        elif self._health_sse < 0.5:
            color, txt = ORANGE, "SSE 数据流降级"
        else:
            color, txt = GREEN, "本地监控中"
        self.lbl_status_dot.setStyleSheet(f"color: {color}; font-size: 16px;")
        self.lbl_status_text.setText(txt)

    # ---------- KPI / 表格更新 ----------
    def _update_kpi(self):
        online = self._online_source()
        if online:
            # 在线全量（含 Trae 等所有客户端，持久化库优先）
            ti = sum(r["tokens_input"] for r in online)
            to = sum(r["tokens_output"] for r in online)
            cr = sum(r["cache_read"] for r in online)
            cost = sum(r["cost"] for r in online)
            self.kpi_token.setText(_fmt_tokens(ti + cr))
            self.kpi_cost.setText(f"${cost:,.4f}")
            return
        ti = sum(s[6] or 0 for s in self.sessions)
        cost = sum(s[5] or 0 for s in self.sessions)
        cr = sum(s[9] or 0 for s in self.sessions)
        self.kpi_token.setText(_fmt_tokens(ti + cr))
        self.kpi_cost.setText(f"${cost:,.4f}")

    def _update_go_cards(self):
        """更新订阅三窗口进度条（5h 橙 / 周 紫 / 月 蓝）。"""
        d = self.go_data or {}
        for key, (bar, txt) in self.quota_bars.items():
            w = d.get(key)
            if isinstance(w, dict) and isinstance(w.get("usagePercent"), (int, float)):
                pct = int(w["usagePercent"])
                bar.setValue(min(pct, 100))
                secs = w.get("resetInSec") or 0
                bar.setFormat(f"{pct}%")
                txt.setText(f"已用 {pct}% · 剩余 {max(100 - pct, 0)}% · "
                            f"重置 {fmt_reset_secs(int(secs))}")
            else:
                bar.setValue(0)
                bar.setFormat("—")
                txt.setText("—")
        self.lbl_go_status.setText(self.go_status_text)

    def _update_session_table(self):
        t = self.table_sessions
        t.setRowCount(0)
        t.setUpdatesEnabled(False)
        for row, s in enumerate(self.sessions):
            t.insertRow(row)
            vals = [
                (s[1] or s[2] or "")[:40],
                _session_model_label(s[4]),
                _fmt_tokens(s[6] or 0),
                _fmt_tokens(s[7] or 0),
                _fmt_tokens(s[9] or 0),
                _fmt_tokens(s[10] or 0),
                f"${s[5] or 0:,.4f}",
                f"{hit_rate(s[9] or 0, s[6] or 0) * 100:.1f}%",
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if col in (4, 7):
                    item.setForeground(QColor(BLUE))
                t.setItem(row, col, item)
        t.setUpdatesEnabled(True)

    def _update_model_table(self):
        t = self.table_models
        t.setRowCount(0)
        t.setUpdatesEnabled(False)
        for row, (model, agg) in enumerate(aggregate_by_model(self._agg_source())):
            t.insertRow(row)
            vals = [
                model,
                str(agg["count"]),
                _fmt_tokens(agg["tokens_input"]),
                _fmt_tokens(agg["cache_read"]),
                _fmt_tokens(agg["cache_write"]),
                f"${agg['cost']:,.4f}",
                f"{agg['hit_rate'] * 100:.1f}%",
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if col in (3, 6):
                    item.setForeground(QColor(BLUE))
                t.setItem(row, col, item)
        t.setUpdatesEnabled(True)

    def _update_window_table(self):
        t = self.table_windows
        t.setRowCount(0)
        t.setUpdatesEnabled(False)
        for row, (b, agg) in enumerate(reversed(aggregate_by_window(self._agg_source()))):
            t.insertRow(row)
            vals = [
                datetime.fromtimestamp(b).strftime("%m-%d %H:00"),
                str(agg["count"]),
                _fmt_tokens(agg["tokens_input"]),
                _fmt_tokens(agg["cache_read"]),
                f"{agg['hit_rate'] * 100:.1f}%",
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if col == 4:
                    item.setForeground(QColor(BLUE))
                t.setItem(row, col, item)
        t.setUpdatesEnabled(True)

    def _update_msg_table(self):
        """最近请求表格（分页加载，防全量加载 OOM）。

        数据源优先级（统一字段，单一路径）：
          1) 在线内存实时明细（go_usage_list）——最新拉取即时可见
          2) 在线持久化库（30 天，OFFSET 分页查询）——内存未就绪兜底
          3) 本地 opencode.db 消息——在线不可用时回退
        每次刷新清空并渲染第一页；滚动到底由 _on_msg_scroll 追加下一页。
        """
        t = self.table_msgs
        self._msg_page = 0
        t.setRowCount(0)   # 先清空，避免轮询刷新时重复追加堆积
        self._append_msg_page()

    def _msg_min_time(self) -> int | None:
        ms = TIME_RANGE_MS.get(self.time_range)
        if ms:
            return int(time.time() * 1000) - ms
        return None

    def _fetch_msg_page(self, page: int) -> list[dict]:
        """按页取明细（time_created 降序）。返回统一字段 dict 列表。

        数据源优先级（内存优先，追求可用）：
          1) 在线内存实时明细（go_usage_list）——最新拉取即时可见，无需等写库
          2) 在线持久化库（LocalStore）——内存未就绪（启动期）查库兜底历史
          3) 本地 opencode.db 消息——在线不可用时降级
        """
        size = self._msg_page_size
        min_time = self._msg_min_time()
        # 1) 在线实时（内存）：拉取结果已合并进 go_usage_list，立即渲染
        if self._go_usage_ok and self.go_usage_list:
            all_ = [_go_rec_to_usage(r) for r in self.go_usage_list]
            all_ = [r for r in all_ if (r.get("time_created") or 0) >= (min_time or 0)]
            all_.sort(key=lambda r: -(r.get("time_created") or 0))
            return all_[page * size:(page + 1) * size]
        # 2) 持久化库（启动期内存未就绪时查历史）
        try:
            if getattr(self, "local_store", None):
                recs = self.local_store.fetch_page(size, page * size, min_time)
                if recs:
                    return recs
        except Exception:
            pass
        # 3) 本地 db 降级
        all_ = list(self.msgs)
        all_ = [r for r in all_ if (r.get("time_created") or 0) >= (min_time or 0)]
        all_.sort(key=lambda r: -(r.get("time_created") or 0))
        return all_[page * size:(page + 1) * size]

    def _append_msg_page(self) -> None:
        """渲染当前页（_msg_page）到表格；无更多数据时显示空提示。"""
        t = self.table_msgs
        rows = self._fetch_msg_page(self._msg_page)
        low_input = int(self.thresholds.get("suspicious_input_min", DEFAULT_LOW_INPUT))
        miss_ratio = float(self.thresholds.get("miss_ratio", DEFAULT_MISS_RATIO))
        t.setUpdatesEnabled(False)
        for m in rows:
            r = t.rowCount()
            t.insertRow(r)
            hit = hit_rate(m["cache_read"], m["tokens_input"])
            is_low = (m["tokens_input"] > low_input
                      and (m["cache_read"] / m["tokens_input"]) < miss_ratio)
            vals = [
                _fmt_time(m["time_created"]),
                m["modelID"] or "",
                _fmt_tokens(m["tokens_input"]),
                _fmt_tokens(m["cache_read"]),
                _fmt_tokens(m["tokens_output"]),
                f"{hit * 100:.1f}%",
                f"${m['cost']:,.4f}",
            ]
            for col, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if is_low:
                    item.setForeground(QColor(RED))
                    item.setBackground(QColor("#3d1a1a"))
                elif col in (3, 5):
                    item.setForeground(QColor(BLUE))
                t.setItem(r, col, item)
        t.setUpdatesEnabled(True)

    def _on_msg_scroll(self, value: int) -> None:
        """滚动接近底部时加载下一页。"""
        if self._quitting:
            return
        sb = self.table_msgs.verticalScrollBar()
        if value >= sb.maximum() - 8:
            self._msg_page += 1
            before = self.table_msgs.rowCount()
            self._append_msg_page()
            if self.table_msgs.rowCount() == before:
                self._msg_page -= 1  # 无更多数据，回退页码

    # ---------- 状态与在线 API ----------
    def _check_process(self):
        if self._quitting:
            return
        running = opencode_running()
        self.banner_process.setVisible(not running)

    def _probe_sse(self):
        url = probe_sse(timeout=0.5)
        if url:
            self._health_sse = 1.0
            self.statusBar().showMessage(f"SSE 反代：已连接 {url}")
        else:
            self._health_sse = 0.5  # 尽力探测未实现 → 中性（非故障）
            self.statusBar().showMessage(
                "SSE /event 反代：未实现（桌面版 OpenCode 无 serve/event 端点，本应用以本地文件监听等效）")

    def _setup_watchdog(self):
        """SSE 看门狗：反代心跳超时 2×interval 自动重启（尽力通道，非主通道）。"""
        if not HAS_WATCHDOG:
            return

        def _restart():
            if self._quitting:
                return
            logger.info("SSE 心跳超时，触发看门狗重启探测")
            self._health_sse = 0.5
            url = probe_sse(timeout=0.5)
            if url:
                self._health_sse = 1.0
                self.statusBar().showMessage(f"SSE 反代已恢复：{url}")

        self._watchdog = Watchdog(interval=30.0, timeout=60.0, on_stall=_restart)
        self._watchdog.start()

    def _apply_saved_cookie_state(self):
        cookie = self.app_settings.get("cookie", "")
        if cookie:
            ok, _, status = fetch_go_page(cookie, self.workspace_id)
            if not ok and status in (401, 403):
                self.banner_cookie.setVisible(True)

    def _open_cookie_dialog(self):
        dlg = CookieDialog(self)
        dlg.exec()
        cookie = dlg.cookie()
        if cookie:
            ok, _, status = fetch_go_page(cookie, self.workspace_id)
            self.banner_cookie.setVisible(not ok and status in (401, 403))
            self._refresh_go_usage()

    def _open_settings_dialog(self):
        """完整设置面板：保存后热加载（阈值/端口池/间隔/时间范围/日志）。"""
        dlg = SettingsDialog(settings=self.app_settings.get_all(), parent=self)
        dlg.settings_saved.connect(self._apply_settings)
        dlg.exec()

    def _open_stats_dialog(self):
        """统计面板：本地 db + 在线持久化（30 天）双数据源周期统计 + 导出。"""
        online = self.local_store.fetch_records() if self.local_store else []
        dlg = StatsDialog(
            self.msgs, online, self._recent_alerts, parent=self,
            db_path=getattr(self.poller, "db_path", ""))
        dlg.exec()

    def _apply_settings(self, new_settings: dict):
        """热加载新设置（SettingsDialog settings_saved 信号）。"""
        try:
            self.workspace_id = str(new_settings.get("workspace_id",
                                                     self.workspace_id))
            self.thresholds = dict(new_settings.get("thresholds", self.thresholds))
            self.time_range = str(new_settings.get("time_range", self.time_range))
            self.notifications_enabled = bool(new_settings.get(
                "notifications_enabled", self.notifications_enabled))
            self.close_to_tray = bool(new_settings.get(
                "close_to_tray", self.close_to_tray))
            self.log_backup_days = int(new_settings.get(
                "log_backup_days", self.log_backup_days))
            pool = new_settings.get("proxy_port_pool")
            if isinstance(pool, list) and pool:
                self._proxy_pool = [int(p) for p in pool]
            self._extension_port = int(new_settings.get(
                "extension_port", self._extension_port))

            # 应用时间范围按钮态
            if self.time_range == "7d":
                self.btn_range_7d.setChecked(True)
            elif self.time_range == "30d":
                self.btn_range_30d.setChecked(True)
            else:
                self.btn_range_24.setChecked(True)

            # 告警热更新
            if self.alerts is not None:
                self.alerts.set_cooldown(float(self.thresholds.get("cooldown_min", 30)))
                self.alerts.set_abs_min(float(self.thresholds.get("abs_min", 55)))
                self.alerts.set_enabled("cache", True)

            # 刷新间隔
            ms = int(new_settings.get("refresh_interval_ms", 500))
            self.timer.start(max(ms, 100))
            gs = int(new_settings.get("go_refresh_interval_s", 60))
            self.go_timer.start(max(gs, 10) * 1000)

            self._refresh()
            self._run_diagnostics()
            self.statusBar().showMessage("设置已应用")
        except Exception as e:
            logger.warning("设置应用失败: %s", e)

    # ---------- 窗口/托盘 ----------
    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _really_quit(self):
        self._quitting = True
        try:
            self.poller.stop()
        except Exception:
            pass
        try:
            if getattr(self, "local_store", None):
                self.local_store.close()
        except Exception:
            pass
        if self._watchdog:
            try:
                self._watchdog.stop()
            except Exception:
                pass
        try:
            self.proxy.stop()
        except Exception:
            pass
        try:
            self.receiver.stop()
        except Exception:
            pass
        self.tray.hide()
        QApplication.instance().quit()

    def closeEvent(self, event: QCloseEvent):
        if self._quitting or not self.tray.isVisible():
            event.accept()
            return
        if not self.close_to_tray:
            # 设置「直接退出」：释放单实例锁，避免旧进程挡住新版本启动
            self._really_quit()
            event.accept()
            return
        self.hide()
        self.tray.showMessage("OpenCode GO 用量监控", "已最小化到系统托盘，双击鲸鱼图标恢复。",
                              QSystemTrayIcon.MessageIcon.Information, 2000)
        event.ignore()
