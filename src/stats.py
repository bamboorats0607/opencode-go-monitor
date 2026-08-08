"""天/周/月周期统计 + CSV 导出（统计面板与导出功能的数据层）。

数据源：本地 opencode.db message 解析结果（parse_rows 输出，含 time_created 毫秒）。
周期口径：本地时区（datetime.fromtimestamp）。指标复用 aggregator.aggregate_messages：
请求数 / 成本 / 缓存命中率 / 输入·输出·推理 token / cache.read / cache.write。

导出统一 UTF-8 BOM（utf-8-sig），Excel 直接打开不乱码。
"""
from __future__ import annotations

import csv
from datetime import datetime

from src.aggregator import aggregate_messages

PERIODS = ("day", "week", "month")
PERIOD_NAMES = {"day": "天", "week": "周", "month": "月"}


def period_label(ts_ms: int, period: str) -> str:
    """毫秒时间戳 → 周期标签（本地时区）。
    day: 2026-08-08 / week: 2026-W32 / month: 2026-08
    """
    dt = datetime.fromtimestamp(ts_ms / 1000)
    if period == "day":
        return dt.strftime("%Y-%m-%d")
    if period == "week":
        y, w, _ = dt.isocalendar()
        return f"{y}-W{w:02d}"
    return dt.strftime("%Y-%m")


def _period_of_alerts(alerts: list[dict], period: str) -> dict[str, int]:
    """按周期统计告警数。alerts 元素须含 ts（秒）。"""
    out: dict[str, int] = {}
    for a in alerts or []:
        ts = a.get("ts")
        if not ts:
            continue
        label = period_label(float(ts) * 1000, period)
        out[label] = out.get(label, 0) + 1
    return out


def aggregate_by_period(msgs: list[dict], period: str) -> list[tuple]:
    """按周期分组聚合。返回 [(label, agg_dict), ...] 最新在前。"""
    groups: dict[str, list] = {}
    for m in msgs:
        ts = m.get("time_created") or 0
        if not ts:
            continue
        groups.setdefault(period_label(ts, period), []).append(m)
    out = [(k, aggregate_messages(v)) for k, v in groups.items()]
    out.sort(key=lambda kv: kv[0], reverse=True)
    return out


def build_period_stats(msgs: list[dict], alerts: list[dict] | None = None) -> dict:
    """三个周期维度统计。始终返回统一三元组 [(label, agg, alert_count), ...]。

    alerts 可选；缺省时告警数一律为 0，保证调用方解包格式稳定。
    """
    stats = {p: aggregate_by_period(msgs, p) for p in PERIODS}
    for p in PERIODS:
        counts = _period_of_alerts(alerts, p) if alerts else {}
        stats[p] = [(label, agg, counts.get(label, 0)) for label, agg in stats[p]]
    return stats


# ---------- CSV 导出 ----------
PERIOD_HEADERS = [
    "周期", "请求数", "成本 $", "缓存命中率", "输入 token", "输出 token",
    "cache.read", "cache.write", "告警数",
]


def _row_of(label: str, agg: dict, alert_count: int = 0) -> list:
    return [
        label,
        agg["count"],
        round(agg["cost"], 6),
        f"{agg['hit_rate'] * 100:.1f}%",
        agg["tokens_input"],
        agg["tokens_output"],
        agg["cache_read"],
        agg["cache_write"],
        alert_count,
    ]


def export_period_csv(path: str, stats: dict) -> None:
    """导出三周期统计到 CSV（一个文件，按天/周/月分段）。"""
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        for p in PERIODS:
            w.writerow([f"按{PERIOD_NAMES[p]}统计"])
            w.writerow(PERIOD_HEADERS)
            rows = stats.get(p) or []
            for item in rows:
                if len(item) == 3:
                    label, agg, cnt = item
                else:
                    label, agg, cnt = item[0], item[1], 0
                w.writerow(_row_of(label, agg, cnt))
            if not rows:
                w.writerow(["（无数据）"])
            w.writerow([])


def export_msgs_csv(path: str, msgs: list[dict]) -> None:
    """导出请求明细 CSV（按时间降序）。字段对齐主窗口「最近请求」表格。"""
    headers = ["时间", "模型", "输入", "缓存命中", "输出", "命中率", "成本 $", "会话"]
    ordered = sorted(
        msgs, key=lambda m: m.get("time_created") or 0, reverse=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for m in ordered:
            hit = m.get("cache_read", 0)
            tin = m.get("tokens_input", 0)
            denom = hit + tin
            rate = f"{hit / denom * 100:.1f}%" if denom > 0 else "—"
            w.writerow([
                datetime.fromtimestamp((m.get("time_created") or 0) / 1000)
                .strftime("%Y-%m-%d %H:%M:%S"),
                m.get("modelID") or "?",
                tin,
                hit,
                m.get("tokens_output", 0),
                rate,
                round(m.get("cost", 0), 6),
                m.get("session_id") or "",
            ])


def export_alerts_csv(path: str, alerts: list[dict]) -> None:
    """导出告警列表 CSV。alerts 元素须含 severity/title/message/ts。"""
    headers = ["时间", "级别", "标题", "消息"]
    ordered = sorted(alerts, key=lambda a: a.get("ts") or 0, reverse=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for a in ordered:
            ts = a.get("ts") or 0
            w.writerow([
                datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                if ts else "",
                a.get("severity", ""),
                a.get("title", ""),
                a.get("message", ""),
            ])
