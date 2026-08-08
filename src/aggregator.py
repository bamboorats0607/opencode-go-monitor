"""session / 时间窗口 / model 三级聚合。

命中率公式（全项目统一）：hit_rate = cache_read / (input + cache_read)
session 基线来自 session 表字段聚合，与 message 级累计相互印证。
"""
from __future__ import annotations


def hit_rate(cache_read: int, tokens_input: int) -> float:
    denom = tokens_input + cache_read
    return (cache_read / denom) if denom > 0 else 0.0


def _empty() -> dict:
    return {
        "count": 0,
        "tokens_input": 0,
        "tokens_output": 0,
        "tokens_reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost": 0.0,
        "hit_rate": 0.0,
    }


def aggregate_messages(msgs: list[dict]) -> dict:
    """msgs 为 parse_rows 结果。返回累计字段 + 命中率。"""
    agg = _empty()
    for m in msgs:
        agg["count"] += 1
        agg["tokens_input"] += m["tokens_input"]
        agg["tokens_output"] += m["tokens_output"]
        agg["tokens_reasoning"] += m["tokens_reasoning"]
        agg["cache_read"] += m["cache_read"]
        agg["cache_write"] += m["cache_write"]
        agg["cost"] += m["cost"]
    agg["hit_rate"] = hit_rate(agg["cache_read"], agg["tokens_input"])
    return agg


def aggregate_by_window(msgs: list[dict], window_seconds: int = 3600) -> list[tuple]:
    """按时间窗口（默认 1 小时）分组。msgs 需含 time_created（毫秒）。
    返回 [(window_start_epoch_s, agg_dict), ...] 升序。"""
    buckets: dict[int, list] = {}
    for m in msgs:
        b = (m["time_created"] // 1000) // window_seconds * window_seconds
        buckets.setdefault(b, []).append(m)
    return [(b, aggregate_messages(v)) for b, v in sorted(buckets.items())]


def aggregate_by_model(msgs: list[dict]) -> list[tuple]:
    """按 modelID 分组。返回 [(modelID, agg_dict), ...] 按 input 降序。"""
    groups: dict[str, list] = {}
    for m in msgs:
        groups.setdefault(m["modelID"] or "unknown", []).append(m)
    out = [(k, aggregate_messages(v)) for k, v in groups.items()]
    out.sort(key=lambda kv: -kv[1]["tokens_input"])
    return out


def session_baseline(session_row: tuple) -> dict:
    """由 session 表一行计算基线 KPI。session 表列序见 watcher.fetch_sessions。"""
    return {
        "cost": session_row[5] or 0.0,
        "tokens_input": session_row[6] or 0,
        "tokens_output": session_row[7] or 0,
        "tokens_reasoning": session_row[8] or 0,
        "cache_read": session_row[9] or 0,
        "cache_write": session_row[10] or 0,
        "hit_rate": hit_rate(session_row[9] or 0, session_row[6] or 0),
    }
