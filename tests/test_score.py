"""T1 诊断引擎回归测试：打分模型 + 特征检测器。

覆盖 7 个用例：样本 B 触发 / POC 基线不触发 / 大段粘贴抑制 /
连续升级 / 高分 / cache_write 不可测归一化 / drop_flag 骤降惩罚。

运行：D:\\ide\\python12\\python.exe tests\\test_score.py
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.parser import parse_message_data
from src.diagnostics.score import compute_score, grade_of, detect_features

# ---- 样本 B：低命中（input 大、cache.read≈0，来自 test_parser_aggregator.py）----
MSG_B_RAW = ('{"role":"assistant","modelID":"deepseek-v4-flash","providerID":"opencode-go",'
             '"cost":0.0221891964,"path":{"cwd":"D:/文档/Default Project","root":"/"},'
             '"tokens":{"total":319045,"input":316793,"output":76,"reasoning":0,'
             '"cache":{"write":0,"read":2176}},'
             '"time":{"created":1786160694056,"completed":1786160712371}}')


def make_msg(tokens_input, cache_read, cache_write=0, sid="s1", mid=None, created=None):
    """构造 parse_rows 风格的辅助消息（同字段结构）。"""
    return {
        "id": mid if mid is not None else f"m{created}", "session_id": sid,
        "time_created": created if created is not None else 1786160000000,
        "time_updated": 0, "role": "assistant",
        "modelID": "deepseek-v4-flash", "providerID": "opencode-go",
        "cost": 0.0, "tokens_total": tokens_input + cache_read,
        "tokens_input": tokens_input, "tokens_output": 0, "tokens_reasoning": 0,
        "cache_write": cache_write, "cache_read": cache_read, "cwd": None,
    }


def assert_true(name, cond):
    if not cond:
        raise AssertionError(f"[FAIL] {name}")
    print(f"[OK] {name}")


# ---- 用例 1：样本 B 必须触发 ----
def test_case1_sample_b_triggers():
    p = parse_message_data(MSG_B_RAW)
    p["id"] = "m_b"
    p["session_id"] = "s_b"
    p["time_created"] = 1786160694056
    hits = detect_features([p])
    assert_true("样本 B 触发（≥1 条命中）", len(hits) >= 1)
    assert_true(f"样本 B level 为 critical/warning（实际 {hits[0]['level']}）",
                hits[0]["level"] in ("critical", "warning"))
    print(f"      样本 B: input={hits[0]['tokens_input']} ratio={hits[0]['ratio']:.4%} "
          f"level={hits[0]['level']}")


# ---- 用例 2：真实 POC 96.4% 高命中会话不触发 ----
def test_case2_poc_baseline_no_trigger():
    msgs = [make_msg(1000, 27000, sid="s_hi", mid=f"hi{i}",
                     created=1786160000000 + i) for i in range(20)]
    hits = detect_features(msgs)
    assert_true("96.4% 高命中会话无触发（hits 为空）", hits == [])


# ---- 用例 3：大段粘贴被抑制为 hint ----
def test_case3_paste_suppressed():
    # 前 10 条高命中（加权 H≈96.4% ≥ 90%）+ 第 11 条单条低命中
    msgs = [make_msg(1000, 27000, sid="s_paste", mid=f"p{i}",
                     created=1786160100000 + i) for i in range(10)]
    msgs.append(make_msg(20000, 100, sid="s_paste", mid="p10",
                         created=1786160100000 + 10))
    hits = detect_features(msgs)
    assert_true("仅第 11 条命中", len(hits) == 1)
    assert_true("第 11 条 level=hint", hits[0]["level"] == "hint")
    assert_true("第 11 条 suppressed=True", hits[0]["suppressed"] is True)
    assert_true("不产生 critical", not any(h["level"] == "critical" for h in hits))
    print(f"      抑制原因: {hits[0]['reason']}")


# ---- 用例 4：连续 3 条 → critical；单条 → warning ----
def test_case4_streak_upgrade():
    msgs3 = [make_msg(20000, 100, sid="s_streak", mid=f"st{i}",
                      created=1786160200000 + i) for i in range(3)]
    hits = detect_features(msgs3)
    assert_true("3 条连续触发 → [warning, warning, critical]",
                [h["level"] for h in hits] == ["warning", "warning", "critical"])
    one = detect_features([make_msg(20000, 100, sid="s_one", mid="o1",
                                    created=1786160300000)])
    assert_true("单条触发 → warning", one[0]["level"] == "warning")


# ---- 用例 5：高命中样本打分 ≥90 且 healthy ----
def test_case5_high_hit_score():
    msgs = [make_msg(1000, 27000, sid="s_hi", mid=f"hi{i}",
                     created=1786160000000 + i) for i in range(20)]
    r = compute_score(msgs, quota={"used": 50, "limit": 200})
    assert_true(f"score={r['score']} ≥ 90", r["score"] >= 90)
    assert_true(f"grade=healthy（实际 {r['grade']}）", r["grade"] == "healthy")
    assert_true("grade_cn=健康", r["grade_cn"] == "健康")
    print(f"      score={r['score']} sub={r['sub_scores']} "
          f"drop_flag={r['drop_flag']} untestable_cw={r['untestable_cw']}")


# ---- 用例 6：cache_write 全 0 → untestable_cw=True + 归一化 ----
def test_case6_untestable_cw():
    msgs = [make_msg(1000, 27000, sid="s_hi", mid=f"w{i}",
                     created=1786160000000 + i) for i in range(20)]
    r = compute_score(msgs, quota={"used": 50, "limit": 200})
    assert_true("untestable_cw=True", r["untestable_cw"] is True)
    assert_true(f"score={r['score']} ∈ [0,100]", 0 <= r["score"] <= 100)
    assert_true("score 为 int", isinstance(r["score"], int))


# ---- 用例 7：drop_flag 骤降惩罚生效 ----
def test_case7_drop_flag():
    msgs = []
    # 前 10 条高命中 96.4%
    for i in range(10):
        msgs.append(make_msg(1000, 27000, sid="s_drop", mid=f"d{i}",
                             created=1786160400000 + i))
    # 后 20 条命中骤降到 60%
    for i in range(10, 30):
        msgs.append(make_msg(1000, 1500, sid="s_drop", mid=f"d{i}",
                             created=1786160400000 + i))
    r = compute_score(msgs, quota={"used": 50, "limit": 200})
    assert_true("drop_flag=True", r["drop_flag"] is True)
    # 对照组：仅后 20 条（无前窗）→ drop_flag=False，分数应更高
    r2 = compute_score(msgs[10:], quota={"used": 50, "limit": 200})
    assert_true("对照组 drop_flag=False", r2["drop_flag"] is False)
    assert_true(f"drop 惩罚降低分数: {r['score']} < {r2['score']}",
                r["score"] < r2["score"])
    print(f"      drop 序列 score={r['score']} vs 对照组 score={r2['score']} "
          f"(prev_H={r['details']['prev_window_hit']} → H={r['details']['window_hit']})")


# ---- 等级映射 ----
def test_grade_mapping():
    assert_true("grade_of(90)=healthy", grade_of(90)[0] == "healthy")
    assert_true("grade_of(89)=good", grade_of(89)[0] == "good")
    assert_true("grade_of(75)=good", grade_of(75)[0] == "good")
    assert_true("grade_of(74)=attention", grade_of(74)[0] == "attention")
    assert_true("grade_of(55)=attention", grade_of(55)[0] == "attention")
    assert_true("grade_of(54)=critical", grade_of(54)[0] == "critical")
    assert_true("grade_of 中文标签", grade_of(90)[1] == "健康")


def main():
    test_case1_sample_b_triggers()
    test_case2_poc_baseline_no_trigger()
    test_case3_paste_suppressed()
    test_case4_streak_upgrade()
    test_case5_high_hit_score()
    test_case6_untestable_cw()
    test_case7_drop_flag()
    test_grade_mapping()
    print("\n全部断言通过 ✅")


if __name__ == "__main__":
    main()
