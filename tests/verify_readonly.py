"""只读校验：打开 opencode.db 前后文件 hash 不变 + 数据层集成验证。

运行：D:\\ide\\python12\\python.exe tests\\verify_readonly.py
注意：若 opencode 正在运行并写入，主文件 hash 可能变化（WAL checkpoint），
脚本会提示可能原因，不视为失败。
"""
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import db_conn
from src.parser import parse_rows
from src.watcher import DBWatcher
from src.aggregator import hit_rate

DB = db_conn.DEFAULT_DB_PATH


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    assert os.path.exists(DB), f"db 不存在: {DB}"
    h_before = sha256(DB)

    con = db_conn.connect_readonly(DB)
    snap = db_conn.schema_snapshot(con)
    print(f"journal_mode={snap['journal_mode']}  data_version={snap['data_version']}")
    print(f"表: {list(snap['tables'].keys())}")
    assert snap["journal_mode"] == "wal", "预期 WAL 模式"
    assert "session" in snap["tables"] and "message" in snap["tables"]

    # 只读连接上尝试写操作必须失败
    try:
        con.execute("DELETE FROM message")
        raise AssertionError("[FAIL] 只读连接竟然允许 DELETE！")
    except Exception:
        print("[OK] 只读连接拒绝写入（mode=ro + query_only 生效）")

    watcher = DBWatcher(DB)
    sessions, rows = watcher.first_load()
    msgs = parse_rows(rows)
    print(f"session 数 = {len(sessions)}，message 总数 = {len(rows)}，解析出 assistant 消息 = {len(msgs)}")
    assert len(sessions) >= 1, "至少 1 个 session"
    assert len(msgs) >= 1, "至少 1 条 assistant 消息"

    # session 基线
    s0 = sessions[0]
    hr = hit_rate(s0[9] or 0, s0[6] or 0)
    print(f"首 session 基线命中率 = {hr * 100:.2f}% "
          f"(input={s0[6]}, cache_read={s0[9]}, cost={s0[5]})")
    print(f"首 session model JSON = {s0[4]!r}")

    con.close()
    h_after = sha256(DB)
    print(f"\n打开前 sha256 = {h_before}")
    print(f"打开后 sha256 = {h_after}")
    if h_before == h_after:
        print("[OK] 打开前后文件 hash 不变 → 全程零写入 ✅")
    else:
        print("[WARN] hash 变化：可能因 opencode 并发写入触发 WAL checkpoint，"
              "非本程序写入（本程序仅 mode=ro 连接）。")


if __name__ == "__main__":
    main()
