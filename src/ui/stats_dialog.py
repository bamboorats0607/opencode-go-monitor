"""统计面板：天/周/月周期统计（成本、缓存命中率、token、告警）+ 选择位置导出。

数据源：主窗口持有的全量消息（self.msgs，parse_rows 结果）+ 最近告警（带 ts）。
打开面板时按需计算 build_period_stats，不增加日常轮询负担。
导出统一走 QFileDialog 选择保存位置（CSV，UTF-8 BOM，Excel 兼容）。
"""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget, QTableWidgetItem,
    QHeaderView, QTabWidget, QPushButton, QFileDialog, QMessageBox,
)

from src.stats import (
    build_period_stats, export_period_csv, export_msgs_csv, export_alerts_csv,
)

STATS_QSS = """
QDialog { background-color: #0f1117; color: #e6edf3; }
QLabel { color: #c9d1d9; font-size: 13px; }
QLabel#statsTitle { font-size: 15px; font-weight: bold; color: #e6edf3; }
QLabel#statsSub { color: #8b949e; font-size: 12px; }
QTableWidget { background-color: #171a22; alternate-background-color: #1a1d26;
    gridline-color: #2a2d36; border: none; font-size: 12px; }
QHeaderView::section { background-color: #1e2129; color: #8b949e;
    border: none; border-bottom: 1px solid #2a2d36; padding: 6px; font-size: 12px; }
QTabWidget::pane { border: 1px solid #2a2d36; top: -1px; }
QTabBar::tab { background: #171a22; color: #8b949e; padding: 7px 18px; }
QTabBar::tab:selected { background: #1a1d26; color: #4dabf7; border-top: 2px solid #4dabf7; }
QPushButton { background-color: #4dabf7; color: #0f1117; border: none;
    border-radius: 6px; padding: 6px 14px; font-weight: bold; font-size: 13px; }
QPushButton:hover { background-color: #6ec2ff; }
QPushButton#secondaryBtn { background-color: #1a1d26; color: #c9d1d9;
    border: 1px solid #2a2d36; font-weight: normal; }
QPushButton#secondaryBtn:hover { border-color: #4dabf7; color: #4dabf7; }
"""

# 周期表列（与 stats.PERIOD_HEADERS 对齐，去掉告警数后可裁剪）
_PERIOD_COLS = ["周期", "请求数", "成本 $", "缓存命中率", "输入 token", "输出 token",
                "cache.read", "cache.write", "告警数"]
_ALERT_COLS = ["时间", "级别", "标题", "消息"]


class StatsDialog(QDialog):
    def __init__(self, msgs: list[dict], alerts: list[dict],
                 parent=None, db_path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("统计：天 / 周 / 月")
        self.resize(860, 620)
        self.setStyleSheet(STATS_QSS)
        self.msgs = msgs or []
        self.alerts = alerts or []
        self.stats = build_period_stats(self.msgs, self.alerts)
        self._db_path = db_path
        self._build_ui()
        self._load_periods()
        self._load_alerts()

    # ---------- UI ----------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        title = QLabel("周期统计")
        title.setObjectName("statsTitle")
        root.addWidget(title)

        total_in = sum(m.get("tokens_input", 0) for m in self.msgs)
        total_cost = sum(m.get("cost", 0) for m in self.msgs)
        total_read = sum(m.get("cache_read", 0) for m in self.msgs)
        denom = total_in + total_read
        hit = f"{total_read / denom * 100:.1f}%" if denom > 0 else "—"
        sub = QLabel(
            f"数据源：本地 opencode.db（{self._db_path or '默认路径'}）· "
            f"统计 {len(self.msgs)} 条消息 · 总成本 ${total_cost:.4f} · "
            f"累计缓存命中率 {hit} · 告警 {len(self.alerts)} 条")
        sub.setObjectName("statsSub")
        sub.setWordWrap(True)
        root.addWidget(sub)

        self.tabs = QTabWidget()
        self.period_tables: dict[str, QTableWidget] = {}
        for key, name in [("day", "按天"), ("week", "按周"), ("month", "按月")]:
            t = QTableWidget(0, len(_PERIOD_COLS))
            t.setHorizontalHeaderLabels(_PERIOD_COLS)
            t.setAlternatingRowColors(True)
            t.verticalHeader().setVisible(False)
            t.horizontalHeader().setSectionResizeMode(
                QHeaderView.ResizeMode.ResizeToContents)
            self.period_tables[key] = t
            self.tabs.addTab(t, name)
        root.addWidget(self.tabs, 3)

        # 告警区
        al_label = QLabel(f"最近告警（{len(self.alerts)} 条）")
        al_label.setObjectName("statsTitle")
        root.addWidget(al_label)
        self.table_alerts = QTableWidget(0, len(_ALERT_COLS))
        self.table_alerts.setHorizontalHeaderLabels(_ALERT_COLS)
        self.table_alerts.setAlternatingRowColors(True)
        self.table_alerts.verticalHeader().setVisible(False)
        self.table_alerts.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.table_alerts.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents)
        self.table_alerts.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table_alerts, 2)

        # 底部按钮
        btns = QHBoxLayout()
        btn_period = QPushButton("导出统计 CSV")
        btn_period.clicked.connect(lambda: self._export_period())
        btn_msgs = QPushButton("导出请求明细 CSV")
        btn_msgs.clicked.connect(lambda: self._export_msgs())
        btn_alerts = QPushButton("导出告警 CSV")
        btn_alerts.clicked.connect(lambda: self._export_alerts())
        btn_close = QPushButton("关闭")
        btn_close.setObjectName("secondaryBtn")
        btn_close.clicked.connect(self.reject)
        for b in (btn_period, btn_msgs, btn_alerts):
            b.setObjectName("secondaryBtn")
        btns.addWidget(btn_period)
        btns.addWidget(btn_msgs)
        btns.addWidget(btn_alerts)
        btns.addStretch(1)
        btns.addWidget(btn_close)
        root.addLayout(btns)

    # ---------- 数据填充 ----------
    def _load_periods(self) -> None:
        for key, table in self.period_tables.items():
            table.setRowCount(0)
            rows = self.stats.get(key) or []
            for label, agg, cnt in rows:
                r = table.rowCount()
                table.insertRow(r)
                values = [
                    label, str(agg["count"]), f"{agg['cost']:.6f}",
                    f"{agg['hit_rate'] * 100:.1f}%", str(agg["tokens_input"]),
                    str(agg["tokens_output"]), str(agg["cache_read"]),
                    str(agg["cache_write"]), str(cnt),
                ]
                for c, v in enumerate(values):
                    item = QTableWidgetItem(v)
                    if c in (0, 2, 3):
                        item.setTextAlignment(
                            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    table.setItem(r, c, item)

    def _load_alerts(self) -> None:
        self.table_alerts.setRowCount(0)
        ordered = sorted(self.alerts, key=lambda a: a.get("ts") or 0, reverse=True)
        for a in ordered[:50]:
            ts = a.get("ts") or 0
            time_s = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S") if ts else ""
            r = self.table_alerts.rowCount()
            self.table_alerts.insertRow(r)
            for c, v in enumerate([time_s, a.get("severity", ""),
                                   a.get("title", ""), a.get("message", "")]):
                self.table_alerts.setItem(r, c, QTableWidgetItem(str(v)))

    # ---------- 导出 ----------
    def _pick_path(self, default_name: str) -> str:
        path, _ = QFileDialog.getSaveFileName(
            self, "选择导出位置", default_name, "CSV 文件 (*.csv);;所有文件 (*)")
        return path

    def _export_period(self) -> None:
        path = self._pick_path("opencode_stats_periods.csv")
        if not path:
            return
        try:
            export_period_csv(path, self.stats)
            self._ok(f"统计已导出：{path}")
        except Exception as e:
            self._err(f"导出失败：{e}")

    def _export_msgs(self) -> None:
        if not self.msgs:
            self._err("没有可导出的请求明细数据")
            return
        path = self._pick_path("opencode_requests.csv")
        if not path:
            return
        try:
            export_msgs_csv(path, self.msgs)
            self._ok(f"明细已导出：{path}（{len(self.msgs)} 条）")
        except Exception as e:
            self._err(f"导出失败：{e}")

    def _export_alerts(self) -> None:
        if not self.alerts:
            self._err("没有可导出的告警数据")
            return
        path = self._pick_path("opencode_alerts.csv")
        if not path:
            return
        try:
            export_alerts_csv(path, self.alerts)
            self._ok(f"告警已导出：{path}（{len(self.alerts)} 条）")
        except Exception as e:
            self._err(f"导出失败：{e}")

    def _ok(self, msg: str) -> None:
        QMessageBox.information(self, "导出成功", msg)

    def _err(self, msg: str) -> None:
        QMessageBox.warning(self, "提示", msg)
