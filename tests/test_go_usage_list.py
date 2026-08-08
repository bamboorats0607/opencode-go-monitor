"""回归：go_usage_list 解析器对在线 usage 页面实测数据的验证。"""
import sys, json
sys.path.insert(0, "D:/python/opencode-go-monitor")
from PySide6.QtCore import QSettings
from src.usage_api import fetch_usage_page
from src.go_usage_list import parse_usage_list, hit_rate, fmt_cost, merge_incremental

qs = QSettings("opencode-go-monitor", "opencode-go-monitor")
cookie = qs.value("cookie", "")
ws = qs.value("workspace_id", "")

ok, payload, status = fetch_usage_page(cookie, ws)
print("fetch_usage_page ok =", ok, "status =", status)
assert ok, payload

records = parse_usage_list(payload)
print("解析条数 =", len(records))
assert len(records) >= 1, "应至少解析出 1 条"

# 字段完整性
r0 = records[0]
for key in ["id", "model", "provider", "timeCreated", "inputTokens",
            "outputTokens", "cacheReadTokens", "cost", "sessionID", "plan"]:
    assert key in r0, f"缺少字段 {key}"
print("字段完整 ✓")

# 命中率
hr = hit_rate(r0)
print("首条命中率 =", hr)
# 模型分布
models = {}
for r in records:
    models[r["model"]] = models.get(r["model"], 0) + 1
print("模型分布 =", models)

# 增量合并（模拟两次拉取）
merged = merge_incremental(records, records)
print("去重后 =", len(merged), "条")
assert len(merged) == len(records)

# 成本格式
print("fmt_cost 示例 =", fmt_cost(r0["cost"]))
print("全部断言通过 ✓")
