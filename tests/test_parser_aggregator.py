"""断言测试：解析层 + 聚合层（基于 POC 实测样本，必须精确匹配）。

运行：D:\\ide\\python12\\python.exe tests\\test_parser_aggregator.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.parser import parse_message_data, parse_rows
from src.aggregator import hit_rate, aggregate_messages, aggregate_by_window, aggregate_by_model

# ---- 样本 A：高命中 ----
MSG_A = ('{"role":"assistant","modelID":"deepseek-v4-flash","providerID":"opencode-go",'
         '"cost":0.0005491164,"path":{"cwd":"D:/文档/Default Project","root":"/"},'
         '"tokens":{"total":319993,"input":569,"output":448,"reasoning":0,'
         '"cache":{"write":0,"read":318976}},'
         '"time":{"created":1786160636042,"completed":1786160643234}}')

# ---- 样本 B：低命中（input 大、cache.read≈0）----
MSG_B = ('{"role":"assistant","modelID":"deepseek-v4-flash","providerID":"opencode-go",'
         '"cost":0.0221891964,"path":{"cwd":"D:/文档/Default Project","root":"/"},'
         '"tokens":{"total":319045,"input":316793,"output":76,"reasoning":0,'
         '"cache":{"write":0,"read":2176}},'
         '"time":{"created":1786160694056,"completed":1786160712371}}')

# user 消息（无 tokens，应跳过）
MSG_USER = ('{"role":"user","time":{"created":1786160636025},"agent":"build",'
            '"model":{"providerID":"opencode-go","modelID":"deepseek-v4-flash"},'
            '"summary":{"diffs":[]}}')


def assert_eq(name, got, want):
    if got != want:
        raise AssertionError(f"[FAIL] {name}: got={got!r}, want={want!r}")
    print(f"[OK] {name} = {got}")


def test_parse_message_a():
    p = parse_message_data(MSG_A)
    assert p is not None
    assert_eq("A.tokens_input", p["tokens_input"], 569)
    assert_eq("A.cache_read", p["cache_read"], 318976)
    assert_eq("A.cache_write", p["cache_write"], 0)
    assert_eq("A.tokens_output", p["tokens_output"], 448)
    assert_eq("A.tokens_reasoning", p["tokens_reasoning"], 0)
    assert_eq("A.modelID", p["modelID"], "deepseek-v4-flash")
    assert_eq("A.cost", p["cost"], 0.0005491164)
    assert_eq("A.cwd", p["cwd"], "D:/文档/Default Project")
    assert_eq("A.role", p["role"], "assistant")
    assert_eq("A.tokens_total", p["tokens_total"], 319993)


def test_parse_message_b():
    p = parse_message_data(MSG_B)
    assert p is not None
    assert_eq("B.tokens_input", p["tokens_input"], 316793)
    assert_eq("B.cache_read", p["cache_read"], 2176)
    assert_eq("B.cache_write", p["cache_write"], 0)
    assert_eq("B.modelID", p["modelID"], "deepseek-v4-flash")
    assert_eq("B.cost", p["cost"], 0.0221891964)


def test_parse_user_skipped():
    assert parse_message_data(MSG_USER) is None, "[FAIL] user 消息应被跳过"


def test_parse_rows():
    rows = [("m1", "s1", 1786160636042, 1786160643234, MSG_A),
            ("m2", "s1", 1786160694056, 1786160712371, MSG_B),
            ("m3", "s1", 1786160636025, 1786160714809, MSG_USER)]
    parsed = parse_rows(rows)
    assert_eq("rows.count", len(parsed), 2)
    assert_eq("rows[0].id", parsed[0]["id"], "m1")
    assert_eq("rows[1].time_created", parsed[1]["time_created"], 1786160694056)


def test_hit_rate_formula():
    # 样本 A 命中率
    assert_eq("A.hit_rate", round(hit_rate(318976, 569), 4), round(318976 / (569 + 318976), 4))
    # 零分母
    assert_eq("zero.hit_rate", hit_rate(0, 0), 0.0)


def test_session_baseline():
    # session 表实测：input=2179330, cache_read=58427520 → 基线 ≈96.4%
    hr = hit_rate(58427520, 2179330)
    assert_eq("session.baseline.pct", round(hr * 100, 1), 96.4)
    print(f"      session 基线命中率 = {hr * 100:.4f}%")


def test_aggregation():
    rows = [("m1", "s1", 1786160636042, 1786160643234, MSG_A),
            ("m2", "s1", 1786160694056, 1786160712371, MSG_B)]
    msgs = parse_rows(rows)
    agg = aggregate_messages(msgs)
    assert_eq("agg.count", agg["count"], 2)
    assert_eq("agg.tokens_input", agg["tokens_input"], 569 + 316793)
    assert_eq("agg.cache_read", agg["cache_read"], 318976 + 2176)
    # 时间窗口：两条消息均在 2026 年的同一小时窗口（1786160636s ~ 1786160xxx）
    wins = aggregate_by_window(msgs, window_seconds=3600)
    assert_eq("wins.count", len(wins), 1)
    assert_eq("wins[0].agg.count", wins[0][1]["count"], 2)
    # 模型聚合
    models = aggregate_by_model(msgs)
    assert_eq("models.count", len(models), 1)
    assert_eq("models[0][0]", models[0][0], "deepseek-v4-flash")
    assert_eq("models[0][1].tokens_input", models[0][1]["tokens_input"], 569 + 316793)


def main():
    test_parse_message_a()
    test_parse_message_b()
    test_parse_user_skipped()
    test_parse_rows()
    test_hit_rate_formula()
    test_session_baseline()
    test_aggregation()
    print("\n全部断言通过 ✅")


if __name__ == "__main__":
    main()
