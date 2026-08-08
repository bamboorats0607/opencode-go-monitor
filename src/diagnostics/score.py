"""T1 诊断引擎：缓存健康度打分模型 + 特征检测器（终裁定版）。

打分模型（严格遵守公式，不跨维度叠加，不引入绝对阈值）：
    Score = round(60·sub_HD + 20·sub_V + 10·sub_S + 10·sub_C)
    其中各 sub_* 为 0-100 分，计算 Score 时按 /100 转 0-1 比例再乘权重。

    sub_HD = clamp(100·H − 25·drop_flag, 0, 100)
      H = 最近 20 条消息窗口加权 cache_read/(input+cache_read)
      drop_flag = 当前窗口 H 较前 10 条加权 H 下降 ≥20pp（绝对百分点）或 ≤50%（相对）→ 1
    sub_V = clamp(100·剩余额度/总额度, 0, 100)；配额不可用返回 60 中性分
    sub_S = 0.4·db + 0.3·api + 0.3·sse（health dict 传入，默认全 1.0）
    sub_C = cache_write 维度；恒 0 时不可测，Score 按 /0.9 归一化

特征检测器（比例阈值，禁止绝对 1024 检测器）：
    - 单消息 tokens_input > suspicious_input_min（默认 10000）
      且 cache_read/tokens_input < miss_ratio（默认 0.01）→ 触发
    - 同 session 前 10 条加权 H ≥ 0.90 → 抑制为 hint（防大段粘贴假阳性）
    - 连续 ≥3 条触发且未被抑制 → critical；1-2 条 → warning
"""
from src.aggregator import hit_rate

# ---- 基础工具 ----

def clamp(value: float, lo: float, hi: float) -> float:
    """数值截断到 [lo, hi]。"""
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _weighted_hit(msgs: list[dict]) -> float:
    """窗口加权命中率：sum(cache_read) / (sum(input) + sum(cache_read))。

    与全项目统一公式一致（aggregator.hit_rate）。
    """
    total_input = sum(m.get("tokens_input") or 0 for m in msgs)
    total_read = sum(m.get("cache_read") or 0 for m in msgs)
    return hit_rate(total_read, total_input)


# ---- 等级映射 ----

GRADE_TABLE = [
    (90, "healthy", "健康"),
    (75, "good", "良好"),
    (55, "attention", "关注"),
    (0, "critical", "异常"),
]


def grade_of(score: int) -> tuple[str, str]:
    """分数 → (等级 key, 中文标签)。90+ 健康 / 75-89 良好 / 55-74 关注 / <55 异常。"""
    for threshold, key, cn in GRADE_TABLE:
        if score >= threshold:
            return key, cn
    return "critical", "异常"


# ---- 打分模型 ----

def compute_score(msgs: list[dict], quota: dict | None = None,
                  quota_status: str | None = None,
                  health: dict | None = None) -> dict:
    """综合健康度打分（0-100）。

    msgs: parse_rows 结果（含 tokens_input / cache_read / cache_write）。
    quota: {"used": x, "limit": y}，None 表示配额不可用 → sub_V 取中性 60 分。
    quota_status: 在线 401 时调用方传 "restricted" → 标记受限（不影响分数）。
    health: {"db": float, "api": float, "sse": float}，各 ∈[0,1]，默认全 1.0。

    返回: {"score", "grade", "grade_cn", "sub_scores",
           "untestable_cw", "drop_flag", "restricted", "details"}
    """
    sorted_msgs = sorted(msgs, key=lambda m: m.get("time_created") or 0)

    # ---- sub_HD：窗口命中率 + 骤降惩罚 ----
    window = sorted_msgs[-20:]          # 当前窗口：最近 20 条
    prev = sorted_msgs[-30:-20]         # 前窗：窗口前紧邻 10 条（基线）
    H = _weighted_hit(window)
    prev_H = _weighted_hit(prev) if prev else 0.0
    # drop_flag：当前窗口较前窗下降 ≥20pp（绝对）或 ≤50%（相对）
    drop_flag = 0
    if prev and prev_H > 0:
        if (prev_H - H) >= 0.20 or H <= 0.5 * prev_H:
            drop_flag = 1
    sub_HD = clamp(100.0 * H - 25.0 * drop_flag, 0.0, 100.0)

    # ---- sub_V：剩余额度占比（配额不可用 → 中性 60 分）----
    restricted = quota_status == "restricted"
    if isinstance(quota, dict) and quota.get("limit", 0) > 0:
        limit = float(quota["limit"])
        remaining = max(limit - float(quota.get("used") or 0.0), 0.0)
        sub_V = clamp(100.0 * remaining / limit, 0.0, 100.0)
    else:
        sub_V = 60.0

    # ---- sub_S：数据源健康度（db 0.4 / api 0.3 / sse 0.3，默认全健康）----
    h = {"db": 1.0, "api": 1.0, "sse": 1.0}
    if isinstance(health, dict):
        for k in h:
            if health.get(k) is not None:
                h[k] = clamp(float(health[k]), 0.0, 1.0)
    sub_S = clamp(100.0 * (0.4 * h["db"] + 0.3 * h["api"] + 0.3 * h["sse"]), 0.0, 100.0)

    # ---- sub_C：cache_write 维度（恒 0 则不可测，Score 按 /0.9 归一化）----
    total_cw = sum(m.get("cache_write") or 0 for m in sorted_msgs)
    if total_cw <= 0:
        untestable_cw = True
        sub_C = 0.0
    else:
        untestable_cw = False
        denom = sum(m.get("tokens_input") or 0 for m in sorted_msgs) + total_cw
        cw_hit = total_cw / (denom + 1e-9)  # 防除零
        sub_C = clamp(100.0 * cw_hit, 0.0, 100.0)

    # ---- 总分：sub_* 转 0-1 比例 × 权重 60/20/10/10 ----
    raw = 60.0 * (sub_HD / 100.0) + 20.0 * (sub_V / 100.0) + 10.0 * (sub_S / 100.0)
    if not untestable_cw:
        raw += 10.0 * (sub_C / 100.0)
    else:
        # 缺失 C 维度时总分最大 90，归一化回 100 分制
        raw = raw / 0.9
    score = int(round(clamp(raw, 0.0, 100.0)))

    grade_key, grade_cn = grade_of(score)
    return {
        "score": score,
        "grade": grade_key,
        "grade_cn": grade_cn,
        "sub_scores": {
            "HD": round(sub_HD, 2),
            "V": round(sub_V, 2),
            "S": round(sub_S, 2),
            "C": round(sub_C, 2),
        },
        "untestable_cw": untestable_cw,
        "drop_flag": bool(drop_flag),
        "restricted": restricted,
        "details": {
            "window_hit": round(H, 4),
            "prev_window_hit": round(prev_H, 4),
            "window_count": len(window),
            "quota_remaining": None if not isinstance(quota, dict) else
            max(float(quota.get("limit") or 0.0) - float(quota.get("used") or 0.0), 0.0),
            "cache_write_total": total_cw,
        },
    }


# ---- 特征检测器 ----

DEFAULT_THRESHOLDS = {
    "suspicious_input_min": 10000,  # 单条输入超过此值才可疑
    "miss_ratio": 0.01,             # cache_read/tokens_input 低于此比例视为失配
}


def detect_features(msgs: list[dict], thresholds: dict | None = None) -> list[dict]:
    """低缓存命中消息特征检测（比例阈值，无绝对 1024 检测器）。

    触发：tokens_input > suspicious_input_min 且 cache_read/tokens_input < miss_ratio。
    抑制：同 session 前 10 条加权命中率 ≥ 0.90 → 降级 hint（疑似大段粘贴）。
    升级：同 session 连续 ≥3 条触发且未被抑制 → critical；1-2 条 → warning。

    返回按时间升序的命中列表，每条:
    {"session_id", "msg_id", "level", "tokens_input", "cache_read",
     "ratio", "suppressed", "reason"}
    """
    t = dict(DEFAULT_THRESHOLDS)
    if isinstance(thresholds, dict):
        t.update(thresholds)

    sorted_msgs = sorted(msgs, key=lambda m: m.get("time_created") or 0)
    seen: dict[str, list] = {}   # session_id -> 该 session 已见消息（时间升序）
    streak: dict[str, int] = {}  # session_id -> 连续未抑制触发计数
    out = []

    for idx, m in enumerate(sorted_msgs):
        sid = m.get("session_id") or "unknown"
        inp = int(m.get("tokens_input") or 0)
        cr = int(m.get("cache_read") or 0)
        seen.setdefault(sid, []).append(m)

        # 触发判定（inp 必 > 0，因为需大于 suspicious_input_min）
        if inp <= t["suspicious_input_min"] or cr / inp >= t["miss_ratio"]:
            streak[sid] = 0  # 非触发消息打断连续计数
            continue

        # 抑制判定：同 session 前 10 条加权命中率（当前消息之前的最近 10 条）
        prev10 = seen[sid][-11:-1] if len(seen[sid]) > 1 else []
        prev10_h = _weighted_hit(prev10) if prev10 else 0.0
        suppressed = bool(prev10) and prev10_h >= 0.90

        if suppressed:
            level = "hint"
            reason = (f"同 session 前 10 条加权命中率 {prev10_h * 100:.1f}% ≥ 90%，"
                      "疑似大段粘贴，降级为 hint")
            # 被抑制消息仍是触发流的一部分：不累计也不重置连续计数
        else:
            streak[sid] = streak.get(sid, 0) + 1
            level = "critical" if streak[sid] >= 3 else "warning"
            reason = (f"input={inp} > {t['suspicious_input_min']} 且 "
                      f"cache_read/input={cr / inp:.2%} < {t['miss_ratio']:.0%}")

        out.append({
            "session_id": sid,
            "msg_id": str(m.get("id") or idx),
            "level": level,
            "tokens_input": inp,
            "cache_read": cr,
            "ratio": round(cr / inp, 6),
            "suppressed": suppressed,
            "reason": reason,
        })
    return out
