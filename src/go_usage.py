"""opencode.ai Go 订阅用量解析（从 SSR 页面 HTML 提取）。

go 用量系统与主控制台是两个独立系统。数据源为：
    GET https://opencode.ai/workspace/{workspace_id}/go   (Cookie: auth=...)
页面为 SolidJS SSR，内嵌序列化数据，其中：
    rollingUsage: {status, resetInSec, usagePercent}   # 5 小时滚动窗口
    weeklyUsage:  {status, resetInSec, usagePercent}   # 周窗口
    monthlyUsage: {status, resetInSec, usagePercent}   # 月窗口
    balance / reloadAmount / reloadTrigger             # 余额与自动充值
"""
import json
import re

# SSR 内嵌形式：$R[n]={status:"ok",resetInSec:6667,usagePercent:2}
_WINDOW_RE = re.compile(
    r'(rollingUsage|weeklyUsage|monthlyUsage):\s*\$R\[\d+\]=\{status:"([^"]*)",resetInSec:(\d+),usagePercent:(\d+)\}'
)
# 余额等：balance:0,reloadAmount:20,reloadTrigger:5
_BALANCE_RE = re.compile(r'balance:(-?\d+(?:\.\d+)?),reload:null,reloadAmount:(\d+(?:\.\d+)?),reloadAmountMin:(\d+),reloadTrigger:(\d+)')


def parse_go_page(html: str) -> dict:
    """解析 go 页面 HTML，返回用量摘要。找不到任何窗口返回空 dict。"""
    result = {
        "rollingUsage": None,
        "weeklyUsage": None,
        "monthlyUsage": None,
        "balance": None,
        "reload_amount": None,
        "reload_trigger": None,
    }
    for m in _WINDOW_RE.finditer(html):
        name, status, reset_sec, pct = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
        result[name] = {"status": status, "resetInSec": reset_sec, "usagePercent": pct}

    bm = _BALANCE_RE.search(html)
    if bm:
        result["balance"] = float(bm.group(1))
        result["reload_amount"] = float(bm.group(2))
        result["reload_trigger"] = float(bm.group(4))

    return result


def parse_workspace_id(html: str) -> str | None:
    m = re.search(r'name="workspaceID" value="([^"]+)"', html)
    return m.group(1) if m else None


def fmt_window(w: dict | None) -> str:
    """窗口显示文本，如 '2% (重置 1h50m)'。"""
    if not w:
        return "—"
    pct = w["usagePercent"]
    secs = w["resetInSec"]
    if secs >= 3600:
        reset = f"{secs // 3600}h{(secs % 3600) // 60}m"
    else:
        reset = f"{secs // 60}m"
    return f"{pct}% (重置 {reset})"


def fmt_reset_secs(secs: int) -> str:
    """秒数转 '1h50m' / '45m'。"""
    if secs >= 3600:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 60}m"
