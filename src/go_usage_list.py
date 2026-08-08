"""opencode.ai Go 订阅在线调用明细解析（全部客户端，含 Trae/opencode CLI）。

数据源：
    GET https://opencode.ai/workspace/{workspace_id}/usage   (Cookie: auth=...)
页面为 SolidJS SSR，内嵌 usage.list["ws",0] 的序列化数组，每条：
    id / workspaceID / timeCreated / timeUpdated / model / provider
    inputTokens / outputTokens / reasoningTokens / cacheReadTokens
    cacheWrite5mTokens / cacheWrite1hTokens / cost / keyID / sessionID
    enrichment.plan

provider 均为 inf-go.oa-compat（go 订阅官方推理网关），因此本列表覆盖
所有走 go 订阅的客户端调用（opencode CLI、Trae 等），是"全部模型调用"的唯一来源。
SSR 固定返回最近约 50 条（约 10 分钟窗口），轮询增量合并即可。
"""
import json
import re
from datetime import datetime, timezone

# usage.list 序列化数组起始（在页面内嵌脚本中，注册名形如 usage.list["ws",0]）
_USAGE_RE = re.compile(r'usage\.list\["')

# SolidJS 序列化清理规则
_REF_PREFIX_RE = re.compile(r'\$R\[\d+\]=')
_DATE_RE = re.compile(r'new Date\("([^"]+)"\)')
_BARE_KEY_RE = re.compile(r'([{,]\s*)([a-zA-Z_$][a-zA-Z0-9_$]*)(\s*:)')


def _extract_usage_array(html: str) -> list[dict] | None:
    """从 SSR HTML 提取 usage.list 数组字面量并反序列化为 dict 列表。

    SolidJS SSR 结构：
        _$HY.r["usage.list[\"ws\",0]"]=$R[15]=$R[2]($R[16]={p:0,s:0,f:0});
        ...
        $R[22]($R[16],$R[25]=[ {...usage 数组...} ]);
    即先注册 promise $R[pid]，随后 $R[n]($R[pid],$R[k]=[数组]) 填充数据。
    必须从 promise id 定位填充调用，不能匹配第一个 ]=[（可能是 workspaces 等）。
    """
    # 1) 定位 usage.list 注册行，提取 promise id。
    # 实测结构：_$HY.r["usage.list[\"ws\",0]"]=$R[15]=$R[2]($R[16]={p:0,s:0,f:0});
    m = re.search(
        r'usage\.list\[[^\]]+\]"[^;]*?\(\$R\[(\d+)\]=\{p:0,s:0,f:0\}\)', html)
    if not m:
        return None
    pid = m.group(1)
    # 2) 找填充调用：$R[n]($R[pid],$R[k]=[ ... ]，捕获数组起点 [
    fill = re.search(
        r'\$R\[\d+\]\(\s*\$R\[' + pid + r'\]\s*,\s*\$R\[\d+\]\s*=\s*(\[)', html)
    if not fill:
        return None
    start = fill.start(1)

    depth = 0
    for i in range(start, len(html)):
        c = html[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    else:
        return None
    literal = html[start:end + 1]

    cleaned = re.sub(r"\$R\[\d+\]=", "", literal)
    cleaned = re.sub(r'new Date\("([^"]+)"\)', r'"\1"', cleaned)
    cleaned = re.sub(r"([{,]\s*)([a-zA-Z_$][a-zA-Z0-9_$]*)(\s*:)", r'\1"\2"\3', cleaned)
    try:
        data = json.loads(cleaned)
    except Exception:
        return None
    return data if isinstance(data, list) else None


def parse_usage_list(html: str) -> list[dict]:
    """解析 usage 页面 HTML，返回调用明细列表（按 timeCreated 升序原始顺序）。

    无法解析返回空列表。字段保持原始键名（inputTokens 等），便于 UI 直接使用。
    """
    arr = _extract_usage_array(html)
    if not arr:
        return []
    out = []
    for rec in arr:
        if not isinstance(rec, dict) or "id" not in rec:
            continue
        out.append({
            "id": rec.get("id"),
            "model": rec.get("model") or "?",
            "provider": rec.get("provider") or "",
            "timeCreated": rec.get("timeCreated"),
            "timeUpdated": rec.get("timeUpdated"),
            "inputTokens": int(rec.get("inputTokens") or 0),
            "outputTokens": int(rec.get("outputTokens") or 0),
            "reasoningTokens": int(rec.get("reasoningTokens") or 0),
            "cacheReadTokens": int(rec.get("cacheReadTokens") or 0),
            "cacheWrite5mTokens": rec.get("cacheWrite5mTokens"),
            "cacheWrite1hTokens": rec.get("cacheWrite1hTokens"),
            "cost": float(rec.get("cost") or 0),
            "keyID": rec.get("keyID"),
            "sessionID": rec.get("sessionID"),
            "plan": ((rec.get("enrichment") or {}).get("plan") or ""),
        })
    return out


def hit_rate(rec: dict) -> float | None:
    """单条调用的缓存命中率 = cacheRead/(input+cacheRead)，全为 0 时返回 None。"""
    denom = rec["cacheReadTokens"] + rec["inputTokens"]
    if denom <= 0:
        return None
    return rec["cacheReadTokens"] / denom


def fmt_cost(cost: float) -> str:
    """成本显示。SSR 实测 cost 字段单位 = 1e-8 美元（如 128422 → $0.00128）。

    注：曾误按 1e-6（微美元）换算导致 ×100 显示错误，实测单条
    input=321 tokens 的 deepseek-v4-flash 请求 cost=128422 → $0.00128 合理，
    而 /1e6 得 $0.128 明显不合理，故确定为 1e-8 美元。
    """
    if cost <= 0:
        return "—"
    return f"${cost / 100_000_000:.4f}"


def merge_incremental(prev: list[dict], new: list[dict], max_len: int = 5000) -> list[dict]:
    """增量合并：以 id 去重，保留新旧并集，按 timeCreated 降序（最新在前）。

    prev 为已持有的历史列表，new 为最新一次拉取（可能只含最近 50 条）。
    由于 SSR 窗口有限，旧记录会随时间滚出窗口，本函数只做并集不去旧（保留历史）。
    """
    seen = {r["id"] for r in prev}
    merged = list(prev)
    for r in new:
        if r["id"] not in seen:
            merged.append(r)
            seen.add(r["id"])
    merged.sort(key=lambda r: r["timeCreated"] or "", reverse=True)
    return merged[:max_len]
