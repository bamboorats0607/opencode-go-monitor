"""完整客制化设置面板（PySide6 QDialog，四 Tab）。

Tab1 通用：workspace_id、主监听端口池（逗号分隔）、扩展端口、db 轮询间隔、
        在线拉取间隔、时间范围（24h/7d/30d）。
Tab2 阈值：缓存诊断打分与告警阈值（abs_min / drop_abs / drop_rel /
        cooldown_min / suspicious_input_min / miss_ratio / consecutive_critical）。
Tab3 通知：托盘气泡开关。
Tab4 日志：日志文件路径（含浏览）+ 按天滚动备份天数。

持久化：QSettings("opencode-go-monitor", "opencode-go-monitor")，与主窗口共用。
热加载：保存或恢复默认后 emit settings_saved(dict)，由主窗口消费并应用。

本模块不依赖 src/core/settings.py；默认值通过构造参数 settings: dict 注入
（由调用方传入 AppSettings.get_all() 结果），未注入时回退到 QSettings + 内置默认。
V2 配色：#0f1117 深底、#1a1d26 卡片、#2a2d36 边框、#4dabf7 主蓝。
"""
from copy import deepcopy
from typing import Optional

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget,
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox, QPushButton,
    QLabel, QFileDialog,
)

# ---- spec 默认值（恢复默认按钮写入的值）----
DEFAULT_SETTINGS: dict = {
    "workspace_id": "wrk_01KZAA0VA6E8JQNDSPW60RX3K8",
    "proxy_port_pool": [8899, 8910, 8920, 8930, 8940],
    "extension_port": 8900,
    "refresh_interval_ms": 500,
    "go_refresh_interval_s": 60,
    "time_range": "24h",
    "thresholds": {
        "abs_min": 55.0,              # 诊断打分低于此值判为异常（与 AppSettings 一致）
        "drop_abs": 20.0,             # 命中率骤降百分点
        "drop_rel": 0.5,              # 相对下降比例 0.5 = 50%
        "cooldown_min": 30,           # 同类告警冷却分钟
        "suspicious_input_min": 10000,  # 可疑未命中 input token 下限
        "miss_ratio": 0.01,           # 漏率阈值（未命中 input 占比）
        "consecutive_critical": 3,    # 连续告警条数才升级为 critical
    },
    "notifications_enabled": True,
    "close_to_tray": True,            # 关闭窗口时驻留托盘；False=直接退出（释放单实例锁）
    "log_file": "logs/opencode-go-monitor.log",
    "log_backup_days": 7,             # 按天滚动，保留 7 天（与 spec 一致）
}

ORG_NAME = "opencode-go-monitor"
APP_NAME = "opencode-go-monitor"

SETTINGS_QSS = f"""
QDialog {{ background-color: #0f1117; color: #d8dee9; }}
QLabel {{ color: #c9d1d9; font-size: 13px; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: #1a1d26; border: 1px solid #2a2d36; border-radius: 6px;
    padding: 5px 8px; color: #d8dee9; font-size: 13px;
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border: 1px solid #4dabf7;
}}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{ background-color: #1a1d26; color: #d8dee9;
    selection-background-color: #4dabf7; selection-color: #0f1117; }}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ background: #2a2d36;
    border: none; width: 18px; border-radius: 3px; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover {{ background: #4dabf7; }}
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: #4dabf7; }}
QPushButton {{ background-color: #4dabf7; color: #0f1117; border: none;
    border-radius: 6px; padding: 7px 18px; font-weight: bold; font-size: 13px; }}
QPushButton:hover {{ background-color: #6ec2ff; }}
QPushButton:disabled {{ background-color: #2a2d36; color: #6e7681; }}
QPushButton#secondaryBtn {{ background-color: #1a1d26; color: #c9d1d9;
    border: 1px solid #2a2d36; font-weight: normal; }}
QPushButton#secondaryBtn:hover {{ border-color: #4dabf7; color: #4dabf7; }}
QTabWidget::pane {{ border: 1px solid #2a2d36; top: -1px; background: #0f1117; }}
QTabBar::tab {{ background: #1a1d26; color: #8b949e; padding: 8px 20px;
    font-size: 13px; border-top-left-radius: 6px; border-top-right-radius: 6px; }}
QTabBar::tab:selected {{ background: #1a1d26; color: #4dabf7; border-top: 2px solid #4dabf7; }}
QTabBar::tab:hover {{ color: #4dabf7; }}
QGroupBox {{ border: 1px solid #2a2d36; border-radius: 8px; margin-top: 12px;
    padding-top: 8px; color: #4dabf7; font-weight: bold; font-size: 13px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 4px; }}
QCheckBox {{ color: #c9d1d9; font-size: 13px; spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid #2a2d36;
    border-radius: 4px; background: #1a1d26; }}
QCheckBox::indicator:hover {{ border-color: #4dabf7; }}
QCheckBox::indicator:checked {{ background-color: #4dabf7; border-color: #4dabf7; }}
QScrollBar:vertical {{ background: #0f1117; width: 10px; }}
QScrollBar::handle:vertical {{ background: #2a2d36; border-radius: 5px; }}
QScrollBar::handle:vertical:hover {{ background: #4dabf7; }}
"""

# 各 key 在 QSettings 中的存储路径（嵌套阈值用 "thresholds/<key>"）
_TH_KEY = {
    "abs_min": "thresholds/abs_min",
    "drop_abs": "thresholds/drop_abs",
    "drop_rel": "thresholds/drop_rel",
    "cooldown_min": "thresholds/cooldown_min",
    "suspicious_input_min": "thresholds/suspicious_input_min",
    "miss_ratio": "thresholds/miss_ratio",
    "consecutive_critical": "thresholds/consecutive_critical",
}


def _parse_port_pool(text: str) -> list:
    """解析 "8899, 8910, 8920" 逗号分隔端口字符串为整数列表。非法项忽略。"""
    out = []
    for part in (text or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            p = int(part)
        except ValueError:
            continue
        if 1 <= p <= 65535:
            out.append(p)
    return out


def _as_bool(v, default: bool = False) -> bool:
    """把 QSettings 读回的任意值解析为 bool。

    PySide6 的 QSettings 在 Ini 格式下把 bool 存为字符串 "true"/"false"，
    bool("false") 会得到 True（Python 陷阱），必须显式解析。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y")
    return bool(v)


class SettingsDialog(QDialog):
    """完整设置面板。构造参数 settings 为全量 dict（结构见 DEFAULT_SETTINGS）。"""

    settings_saved = Signal(dict)

    def __init__(self, settings: Optional[dict] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(600, 520)
        self.setStyleSheet(SETTINGS_QSS)
        self.qsettings = QSettings(ORG_NAME, APP_NAME)
        # 优先用调用方注入的全量设置；否则从 QSettings 读取（缺省项回落内置默认）
        self._current = deepcopy(settings) if settings else self._read_qsettings()
        self._build_ui()
        self._load(self._current)

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_tab_general(), "通用")
        self.tabs.addTab(self._build_tab_thresholds(), "阈值")
        self.tabs.addTab(self._build_tab_notify(), "通知")
        self.tabs.addTab(self._build_tab_log(), "日志")
        root.addWidget(self.tabs, 1)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color: #00e09a; font-size: 12px;")
        root.addWidget(self.lbl_status)

        btns = QHBoxLayout()
        btn_reset = QPushButton("恢复默认")
        btn_reset.setObjectName("secondaryBtn")
        btn_reset.clicked.connect(self._reset_defaults)
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save)
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("secondaryBtn")
        btn_cancel.clicked.connect(self.reject)
        btns.addWidget(btn_reset)
        btns.addStretch(1)
        btns.addWidget(btn_save)
        btns.addWidget(btn_cancel)
        root.addLayout(btns)

    def _build_tab_general(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 12)
        form.setSpacing(10)
        self.ed_ws = QLineEdit()
        self.ed_ws.setPlaceholderText("wrk_xxxxxxxxxxxxxxxxxxxxxxxx")
        form.addRow("Workspace ID", self.ed_ws)
        self.ed_ports = QLineEdit()
        self.ed_ports.setPlaceholderText("8899, 8910, 8920, 8930, 8940")
        form.addRow("主监听端口池（逗号分隔）", self.ed_ports)
        self.sp_ext_port = QSpinBox()
        self.sp_ext_port.setRange(1, 65535)
        form.addRow("扩展接收端口 (8900)", self.sp_ext_port)
        self.sp_db_interval = QSpinBox()
        self.sp_db_interval.setRange(500, 10000)
        self.sp_db_interval.setSingleStep(100)
        self.sp_db_interval.setSuffix(" ms")
        form.addRow("DB 轮询间隔", self.sp_db_interval)
        self.sp_go_interval = QSpinBox()
        self.sp_go_interval.setRange(30, 3600)
        self.sp_go_interval.setSingleStep(10)
        self.sp_go_interval.setSuffix(" s")
        form.addRow("在线拉取间隔", self.sp_go_interval)
        self.cb_time_range = QComboBox()
        self.cb_time_range.addItems(["24h", "7d", "30d"])
        form.addRow("时间范围", self.cb_time_range)
        tip = QLabel("提示：端口池按顺序尝试监听，被占用时自动切换到下一个。")
        tip.setStyleSheet("color: #8b949e; font-size: 12px;")
        tip.setWordWrap(True)
        form.addRow("", tip)
        return w

    def _build_tab_thresholds(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 12)
        form.setSpacing(10)

        self.sp_abs_min = QDoubleSpinBox()
        self.sp_abs_min.setRange(0.0, 100.0)
        self.sp_abs_min.setDecimals(1)
        self.sp_abs_min.setSuffix(" 分")
        form.addRow("异常打分下限 abs_min", self.sp_abs_min)

        self.sp_drop_abs = QDoubleSpinBox()
        self.sp_drop_abs.setRange(0.0, 100.0)
        self.sp_drop_abs.setDecimals(1)
        self.sp_drop_abs.setSuffix(" 个百分点")
        form.addRow("命中率骤降 drop_abs", self.sp_drop_abs)

        self.sp_drop_rel = QDoubleSpinBox()
        self.sp_drop_rel.setRange(0.0, 1.0)
        self.sp_drop_rel.setDecimals(2)
        self.sp_drop_rel.setSingleStep(0.05)
        form.addRow("相对下降比例 drop_rel (0-1)", self.sp_drop_rel)

        self.sp_cooldown = QSpinBox()
        self.sp_cooldown.setRange(0, 1440)
        self.sp_cooldown.setSuffix(" min")
        form.addRow("告警冷却 cooldown_min", self.sp_cooldown)

        self.sp_suspicious = QSpinBox()
        self.sp_suspicious.setRange(0, 1_000_000)
        self.sp_suspicious.setSingleStep(1000)
        self.sp_suspicious.setSuffix(" token")
        form.addRow("可疑未命中 input", self.sp_suspicious)

        self.sp_miss_ratio = QDoubleSpinBox()
        self.sp_miss_ratio.setRange(0.0, 1.0)
        self.sp_miss_ratio.setDecimals(2)
        self.sp_miss_ratio.setSingleStep(0.05)
        form.addRow("漏率阈值 miss_ratio (0-1)", self.sp_miss_ratio)

        self.sp_consecutive = QSpinBox()
        self.sp_consecutive.setRange(1, 100)
        form.addRow("连续告警条数 consecutive_critical", self.sp_consecutive)

        tip = QLabel("阈值含义：abs_min 低于该打分判异常；drop_abs/drop_rel 检测命中率骤降；"
                     "cooldown_min 同类告警冷却；consecutive_critical 连续 N 条才升级告警。")
        tip.setStyleSheet("color: #8b949e; font-size: 12px;")
        tip.setWordWrap(True)
        form.addRow(tip)
        return w

    def _build_tab_notify(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(16, 16, 16, 12)
        self.chk_notify = QCheckBox("启用系统托盘气泡通知（缓存告警 / 异常事件）")
        lay.addWidget(self.chk_notify)
        tip = QLabel("关闭后告警仅显示在 UI 横幅与日志面板中，不弹托盘气泡。")
        tip.setStyleSheet("color: #8b949e; font-size: 12px;")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self.chk_close_to_tray = QCheckBox("关闭窗口时最小化到系统托盘（不勾选 = 直接退出程序）")
        lay.addWidget(self.chk_close_to_tray)
        tip2 = QLabel("勾选后点窗口 X 仅隐藏到托盘，进程继续驻留（保持运行、可接收告警）；"
                      "不勾选则点 X 直接退出并释放单实例锁，避免旧进程挡住新版本启动。")
        tip2.setStyleSheet("color: #8b949e; font-size: 12px;")
        tip2.setWordWrap(True)
        lay.addWidget(tip2)
        lay.addStretch(1)
        return w

    def _build_tab_log(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(16, 16, 16, 12)
        form.setSpacing(10)

        row = QHBoxLayout()
        self.ed_log_file = QLineEdit()
        self.ed_log_file.setPlaceholderText("logs/opencode-go-monitor.log")
        btn_browse = QPushButton("浏览…")
        btn_browse.setObjectName("secondaryBtn")
        btn_browse.clicked.connect(self._browse_log_file)
        row.addWidget(self.ed_log_file, 1)
        row.addWidget(btn_browse)
        form.addRow("日志文件", row)

        self.sp_backup_days = QSpinBox()
        self.sp_backup_days.setRange(1, 30)
        self.sp_backup_days.setSuffix(" 天")
        form.addRow("按天滚动保留天数", self.sp_backup_days)

        tip = QLabel("日志文件按天滚动（midnight 轮转），超出保留天数自动清理旧文件；"
                     "最新日志同时显示在 UI 日志面板。")
        tip.setStyleSheet("color: #8b949e; font-size: 12px;")
        tip.setWordWrap(True)
        form.addRow(tip)
        return w

    def _browse_log_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "选择日志文件", self.ed_log_file.text().strip() or "opencode-go-monitor.log",
            "日志文件 (*.log);;所有文件 (*)")
        if path:
            self.ed_log_file.setText(path)

    # ---------- 数据流 ----------
    def _read_qsettings(self) -> dict:
        """从 QSettings 读取全量设置，缺失项用内置默认值兜底（兼容旧版本）。"""
        s = self.qsettings
        data = deepcopy(DEFAULT_SETTINGS)
        data["workspace_id"] = s.value("workspace_id", data["workspace_id"]) or data["workspace_id"]
        raw = s.value("proxy_port_pool", "")
        if raw:
            data["proxy_port_pool"] = _parse_port_pool(str(raw))
        data["extension_port"] = int(s.value("extension_port", data["extension_port"]))
        data["refresh_interval_ms"] = int(s.value("refresh_interval_ms", data["refresh_interval_ms"]))
        data["go_refresh_interval_s"] = int(s.value("go_refresh_interval_s", data["go_refresh_interval_s"]))
        data["time_range"] = s.value("time_range", data["time_range"]) or data["time_range"]
        for k in DEFAULT_SETTINGS["thresholds"]:
            data["thresholds"][k] = float(s.value(_TH_KEY[k], data["thresholds"][k]))
        data["notifications_enabled"] = _as_bool(
            s.value("notifications_enabled", data["notifications_enabled"]),
            data["notifications_enabled"])
        data["close_to_tray"] = _as_bool(
            s.value("close_to_tray", data["close_to_tray"]),
            data["close_to_tray"])
        data["log_file"] = s.value("log_file", data["log_file"]) or data["log_file"]
        data["log_backup_days"] = int(s.value("log_backup_days", data["log_backup_days"]))
        return data

    def _load(self, data: dict) -> None:
        """把设置 dict 填充到 UI 控件。"""
        self.ed_ws.setText(str(data.get("workspace_id", "")))
        self.ed_ports.setText(", ".join(str(p) for p in data.get("proxy_port_pool", [])))
        self.sp_ext_port.setValue(int(data.get("extension_port", 8900)))
        self.sp_db_interval.setValue(int(data.get("refresh_interval_ms", 500)))
        self.sp_go_interval.setValue(int(data.get("go_refresh_interval_s", 60)))
        tr = str(data.get("time_range", "24h"))
        idx = self.cb_time_range.findText(tr)
        self.cb_time_range.setCurrentIndex(idx if idx >= 0 else 0)
        th = data.get("thresholds", {})
        self.sp_abs_min.setValue(float(th.get("abs_min", 55.0)))
        self.sp_drop_abs.setValue(float(th.get("drop_abs", 20.0)))
        self.sp_drop_rel.setValue(float(th.get("drop_rel", 0.5)))
        self.sp_cooldown.setValue(int(th.get("cooldown_min", 30)))
        self.sp_suspicious.setValue(int(th.get("suspicious_input_min", 10000)))
        self.sp_miss_ratio.setValue(float(th.get("miss_ratio", 0.01)))
        self.sp_consecutive.setValue(int(th.get("consecutive_critical", 3)))
        self.chk_notify.setChecked(_as_bool(data.get("notifications_enabled", True), True))
        self.chk_close_to_tray.setChecked(_as_bool(data.get("close_to_tray", True), True))
        self.ed_log_file.setText(str(data.get("log_file", "")))
        self.sp_backup_days.setValue(int(data.get("log_backup_days", 7)))

    def _collect(self) -> dict:
        """从 UI 控件收集全量设置 dict（结构与 DEFAULT_SETTINGS 一致）。"""
        ports = _parse_port_pool(self.ed_ports.text())
        if not ports:
            ports = list(DEFAULT_SETTINGS["proxy_port_pool"])
        ws = self.ed_ws.text().strip()
        if not ws.startswith("wrk_"):  # 格式校验：避免测试/误填污染设置
            ws = str(self.qsettings.value(
                "workspace_id", DEFAULT_SETTINGS["workspace_id"]))
        return {
            "workspace_id": ws,
            "proxy_port_pool": ports,
            "extension_port": self.sp_ext_port.value(),
            "refresh_interval_ms": self.sp_db_interval.value(),
            "go_refresh_interval_s": self.sp_go_interval.value(),
            "time_range": self.cb_time_range.currentText(),
            "thresholds": {
                "abs_min": self.sp_abs_min.value(),
                "drop_abs": self.sp_drop_abs.value(),
                "drop_rel": self.sp_drop_rel.value(),
                "cooldown_min": self.sp_cooldown.value(),
                "suspicious_input_min": self.sp_suspicious.value(),
                "miss_ratio": self.sp_miss_ratio.value(),
                "consecutive_critical": self.sp_consecutive.value(),
            },
            "notifications_enabled": self.chk_notify.isChecked(),
            "close_to_tray": self.chk_close_to_tray.isChecked(),
            "log_file": self.ed_log_file.text().strip(),
            "log_backup_days": self.sp_backup_days.value(),
        }

    def _persist(self, data: dict) -> None:
        """写入 QSettings（端口池以逗号分隔字符串存储）。"""
        s = self.qsettings
        s.setValue("workspace_id", data["workspace_id"])
        s.setValue("proxy_port_pool", ",".join(str(p) for p in data["proxy_port_pool"]))
        s.setValue("extension_port", data["extension_port"])
        s.setValue("refresh_interval_ms", data["refresh_interval_ms"])
        s.setValue("go_refresh_interval_s", data["go_refresh_interval_s"])
        s.setValue("time_range", data["time_range"])
        for k, v in data["thresholds"].items():
            s.setValue(_TH_KEY[k], v)
        s.setValue("notifications_enabled", data["notifications_enabled"])
        s.setValue("close_to_tray", data["close_to_tray"])
        s.setValue("log_file", data["log_file"])
        s.setValue("log_backup_days", data["log_backup_days"])
        s.sync()

    # ---------- 动作 ----------
    def _save(self) -> None:
        data = self._collect()
        self._persist(data)
        self.settings_saved.emit(data)
        self.lbl_status.setText("✓ 设置已保存并写入注册表，主界面将热加载。")
        self.accept()

    def _reset_defaults(self) -> None:
        """恢复默认：UI 重置 + 立即写入 QSettings + 广播热加载（等价 reset_defaults）。"""
        data = deepcopy(DEFAULT_SETTINGS)
        self._load(data)
        self._persist(data)
        self.settings_saved.emit(data)
        self.lbl_status.setText("✓ 已恢复默认设置并写入，主界面将热加载。")
