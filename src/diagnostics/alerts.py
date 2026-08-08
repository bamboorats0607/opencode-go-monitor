"""T2 告警管理：缓存健康 / 额度 / 数据源三类告警的判定、收敛与冷却去重。

设计要点（V2 spec 红线）：
- 只有 cache / quota / datasource 三类告警，不引入三级系统告警概念。
- 无静默时段配置项。
- 纯标准库（time），不依赖 PySide6，可独立单测。
- 横幅为主通道；托盘气泡是否弹出由 UI 依据 notifications_enabled 自行决定，
  本模块只记录开关值。

对外契约（UI 按此集成）：
    AlertManager(cooldown_min=30.0, notifications_enabled=True)
    set_enabled(kind, enabled)                          # kind ∈ {"cache","quota","datasource"}
    evaluate(score_result, feature_hits, go_data,
             health, quota_status=None) -> list[dict]   # 新产生的告警（已冷却去重）
    current_alerts() -> list[dict]                      # 未过期的活跃告警（横幅展示）

告警 dict 统一结构：
    {"kind": "cache"|"quota"|"datasource", "severity": "warning"|"critical"|"info",
     "title": str, "message": str, "ts": float(时间戳秒)}
"""
import time

# ---- 常量 ----

KINDS = ("cache", "quota", "datasource")
"""三类告警的 kind 合法值。"""

SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}
"""severity 排序：数字越大越严重，用于取最高 / 展示排序。"""

DEFAULT_COOLDOWN_MIN = 30.0
"""默认冷却时长（分钟），与 AppSettings.DEFAULT_THRESHOLDS["cooldown_min"] 一致。"""

DEFAULT_ABS_MIN = 55.0
"""缓存健康度绝对下限，与 AppSettings.DEFAULT_THRESHOLDS["abs_min"] 一致。"""

# go_data 中三个用量窗口的 key → 显示标签（5h 滚动 / 周 / 月）
_QUOTA_WINDOWS = (
    ("rollingUsage", "5h"),
    ("weeklyUsage", "周"),
    ("monthlyUsage", "月"),
)

# 各 severity 的额度告警提示语
_QUOTA_NOTES = {
    "critical": "已超 100% 限额",
    "warning": "接近限额（≥90%）",
    "info": "已使用 70% 以上",
}


class AlertManager:
    """三类告警判定器。

    职责：
    - evaluate(): 每次轮询调用，产出"新产生"的告警（已做冷却去重与同类收敛）。
    - current_alerts(): 横幅展示用，返回未过期的全部活跃告警。

    冷却语义（按 kind 记录上次触发时间）：
    - 距上次触发 < cooldown_min 分钟内，同类不再重复产生（防刷屏）；
      但 current_alerts 中未过期的旧告警仍保留展示。
    - 条件消失（无候选）时立即清除该类活跃告警；条件持续时每个冷却周期刷新一次。
    """

    def __init__(self, cooldown_min: float = DEFAULT_COOLDOWN_MIN,
                 notifications_enabled: bool = True,
                 abs_min: float = DEFAULT_ABS_MIN) -> None:
        """初始化告警管理器。

        cooldown_min: 同类告警最小间隔（分钟），UI 设置热加载后可调用
                      set_cooldown 更新，或直接重建实例。
        notifications_enabled: 托盘气泡开关（仅记录供 UI 读取，本模块不做通知）。
        abs_min: 缓存健康度绝对下限（低于即 critical），来自设置 thresholds.abs_min。
        """
        self._cooldown_min = max(float(cooldown_min), 0.0)
        self._notifications_enabled = bool(notifications_enabled)
        self._abs_min = float(abs_min)
        self._enabled: dict[str, bool] = {k: True for k in KINDS}
        self._last_trigger: dict[str, float] = {}   # kind -> 上次产出告警的时间戳
        self._active: dict[str, list[dict]] = {}    # kind -> 当前活跃告警列表

    # ---- 开关与参数 ----

    def set_enabled(self, kind: str, enabled: bool) -> None:
        """开启 / 关闭某一类告警；kind 非法时静默忽略。

        关闭时立即清除该类已活跃告警（横幅不再展示）。
        """
        if kind not in KINDS:
            return
        self._enabled[kind] = bool(enabled)
        if not enabled:
            self._active.pop(kind, None)

    def set_cooldown(self, cooldown_min: float) -> None:
        """热更新冷却时长（分钟）。"""
        self._cooldown_min = max(float(cooldown_min), 0.0)

    def set_abs_min(self, abs_min: float) -> None:
        """热更新缓存健康度绝对下限。"""
        self._abs_min = float(abs_min)

    @property
    def notifications_enabled(self) -> bool:
        """托盘气泡通知开关（供 UI 读取）。"""
        return self._notifications_enabled

    # ---- 对外判定入口 ----

    def evaluate(self, score_result: dict | None, feature_hits: list[dict],
                 go_data: dict | None, health: dict | None,
                 quota_status: str | None = None) -> list[dict]:
        """输入 T1 诊断输出 + go_usage 数据 + 数据源健康度，产出新告警。

        参数：
        score_result: compute_score 的返回（可为 None，表示无可打分数据）。
        feature_hits: detect_features 的命中列表（无命中传 []）。
        go_data: parse_go_page 的返回（可为 None，表示在线用量拉取失败）。
        health: {"db": float, "api": float, "sse": float}，各 ∈ [0,1]（可为 None）。
        quota_status: 在线 401/403 时调用方传 "restricted"，否则 None。

        返回：新产生的告警列表（已做冷却去重与同类收敛），即本轮"实际应展示"的
        新增告警；横幅完整展示请配合 current_alerts()。
        """
        now = time.time()
        new_alerts: list[dict] = []
        hits = feature_hits if isinstance(feature_hits, list) else []

        # 1) 缓存健康类：同类收敛为一条，取最高 severity
        if self._enabled.get("cache"):
            cand = self._cache_candidate(score_result, hits)
            self._update_kind("cache", [cand] if cand else [], now, new_alerts)

        # 2) 额度类：同类收敛为一条，取最高 severity
        if self._enabled.get("quota"):
            cand = self._quota_candidate(go_data)
            self._update_kind("quota", [cand] if cand else [], now, new_alerts)

        # 3) 数据源类：每项独立一条（db / api / sse 不合并）
        if self._enabled.get("datasource"):
            self._update_kind("datasource",
                              self._datasource_candidates(health, quota_status),
                              now, new_alerts)

        return new_alerts

    def current_alerts(self) -> list[dict]:
        """返回当前活跃且未过期的告警（横幅展示用）。

        过期判定：ts < now - cooldown_min*60 视为过期（惰性清理）。
        排序：severity 降序、时间升序（critical 置顶）。
        """
        now = time.time()
        expiry = now - self._cooldown_min * 60.0
        for kind in list(self._active):
            alive = [a for a in self._active[kind] if (a.get("ts") or 0.0) >= expiry]
            if alive:
                self._active[kind] = alive
            else:
                del self._active[kind]
        out = [a for alerts in self._active.values() for a in alerts]
        out.sort(key=lambda a: (-SEVERITY_RANK.get(a.get("severity"), 1),
                                a.get("ts") or 0.0))
        return out

    # ---- 判定规则 ----

    def _cache_candidate(self, score_result: dict | None,
                         feature_hits: list[dict]) -> dict | None:
        """缓存健康类候选（收敛后单条，title 按触发优先级、severity 取最高）。"""
        reasons: list[str] = []
        score_low = feat_crit = feat_warn = drop = False

        if isinstance(score_result, dict):
            score = score_result.get("score")
            if isinstance(score, (int, float)) and score < self._abs_min:
                score_low = True
                reasons.append(f"健康度 {score}/100，低于阈值 {self._abs_min}")
            if score_result.get("drop_flag"):
                drop = True
                det = score_result.get("details") or {}
                win = det.get("window_hit")
                prev = det.get("prev_window_hit")
                if isinstance(win, (int, float)) and isinstance(prev, (int, float)):
                    reasons.append(f"命中率骤降（当前窗口 {win:.1%}，前窗 {prev:.1%}）")
                else:
                    reasons.append("命中率骤降")

        crit_n = sum(1 for h in feature_hits if h.get("level") == "critical")
        warn_n = sum(1 for h in feature_hits if h.get("level") == "warning")
        if crit_n:
            feat_crit = True
            reasons.append(f"检测到 {crit_n} 条连续低命中消息（critical）")
        elif warn_n:
            feat_warn = True
            reasons.append(f"检测到 {warn_n} 条低命中消息（warning）")

        # 触发优先级：低分 > 特征 critical > 特征 warning > 骤降；severity 取最高
        if score_low:
            severity, title = "critical", "缓存健康度异常"
        elif feat_crit:
            severity, title = "critical", "缓存特征异常"
        elif feat_warn:
            severity, title = "warning", "缓存特征异常"
        elif drop:
            severity, title = "warning", "缓存命中率骤降"
        else:
            return None
        return {"severity": severity, "title": title,
                "message": "；".join(reasons)}

    def _quota_candidate(self, go_data: dict | None) -> dict | None:
        """额度类候选（三窗口取最高 severity，title/message 标注窗口与百分比）。"""
        if not isinstance(go_data, dict):
            return None
        rows: list[tuple[str, str, int]] = []   # (severity, label, pct)
        for key, label in _QUOTA_WINDOWS:
            w = go_data.get(key)
            if not isinstance(w, dict):
                continue
            pct = w.get("usagePercent")
            if not isinstance(pct, (int, float)):
                continue
            if pct >= 100:
                sev = "critical"
            elif pct >= 90:
                sev = "warning"
            elif pct >= 70:
                sev = "info"
            else:
                continue
            rows.append((sev, label, int(pct)))
        if not rows:
            return None
        # 取最高 severity；同 severity 时取百分比更大的窗口作为 title 标注
        severity = max(rows, key=lambda r: SEVERITY_RANK[r[0]])[0]
        peak = max(rows, key=lambda r: (SEVERITY_RANK[r[0]], r[2]))
        label, pct = peak[1], peak[2]
        detail = "；".join(f"{lbl}窗口 {p}%" for _, lbl, p in rows)
        return {
            "severity": severity,
            "title": f"Go 订阅额度告警（{label} {pct}%）",
            "message": f"{detail}；{_QUOTA_NOTES[severity]}",
        }

    def _datasource_candidates(self, health: dict | None,
                               quota_status: str | None) -> list[dict]:
        """数据源类候选：每项独立一条，不合并。"""
        h = health if isinstance(health, dict) else {}
        out: list[dict] = []

        db = h.get("db", 1.0)
        if isinstance(db, (int, float)) and db < 0.5:
            out.append({"severity": "critical", "title": "数据库只读异常",
                        "message": f"数据库健康度 {db:.0%}，低于阈值 50%，"
                                   "消息读取可能不完整"})

        api = h.get("api", 1.0)
        if quota_status == "restricted" or (isinstance(api, (int, float)) and api < 0.5):
            if quota_status == "restricted":
                msg = "在线 API 认证失效（401/403），额度与缓存遥测不可用"
            else:
                msg = f"在线 API 健康度 {api:.0%}，低于阈值 50%"
            out.append({"severity": "warning", "title": "在线 API 失效(401/403)",
                        "message": msg})

        sse = h.get("sse", 1.0)
        if isinstance(sse, (int, float)) and sse < 0.5:
            out.append({"severity": "warning", "title": "SSE 数据流中断",
                        "message": f"SSE 数据流健康度 {sse:.0%}，低于阈值 50%，"
                                   "实时事件可能延迟"})
        return out

    # ---- 冷却与活跃状态维护 ----

    def _cooldown_ok(self, kind: str, now: float) -> bool:
        """距上次同类触发是否已过冷却期。"""
        last = self._last_trigger.get(kind)
        if last is None:
            return True
        return (now - last) >= self._cooldown_min * 60.0

    def _update_kind(self, kind: str, candidates: list[dict],
                     now: float, new_alerts: list[dict]) -> None:
        """统一维护某一类的活跃状态。

        - 无候选：条件消失 → 清除该类活跃告警。
        - 有候选且过冷却：产出（打 ts），更新上次触发时间与活跃列表。
        - 有候选但冷却中：不产出，保留旧告警（未过期的仍展示）。
        """
        if not candidates:
            self._active.pop(kind, None)
            return
        if not self._cooldown_ok(kind, now):
            return
        ready: list[dict] = []
        for c in candidates:
            item = dict(c)
            item["ts"] = now
            ready.append(item)
        self._last_trigger[kind] = now
        self._active[kind] = ready
        new_alerts.extend(ready)
