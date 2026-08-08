"""message.data JSON 解析层。

message 表每行含 id/session_id/time_created/time_updated/data(text JSON)。
data 中 assistant 消息含 tokens{total,input,output,reasoning,cache{write,read}}、
modelID、providerID、cost、path.cwd；user 消息无 tokens，返回 None（跳过）。
"""
import json


def parse_message_data(data: str) -> dict | None:
    """解析单条 message.data JSON。提取诊断字段；非 assistant 或无法解析返回 None。

    返回字段：role, modelID, providerID, cost, tokens_total, tokens_input,
              tokens_output, tokens_reasoning, cache_write, cache_read, cwd
    """
    if not data:
        return None
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("role") != "assistant":
        return None
    tokens = obj.get("tokens")
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    model = obj.get("model")
    if isinstance(model, dict):
        model_id = model.get("modelID") or model.get("id")
        provider = model.get("providerID")
    else:
        model_id = obj.get("modelID")
        provider = obj.get("providerID")
    path = obj.get("path")
    return {
        "role": obj.get("role"),
        "modelID": model_id,
        "providerID": provider,
        "cost": float(obj.get("cost") or 0.0),
        "tokens_total": int(tokens.get("total") or 0),
        "tokens_input": int(tokens.get("input") or 0),
        "tokens_output": int(tokens.get("output") or 0),
        "tokens_reasoning": int(tokens.get("reasoning") or 0),
        "cache_write": int(cache.get("write") or 0),
        "cache_read": int(cache.get("read") or 0),
        "cwd": (path or {}).get("cwd") if isinstance(path, dict) else None,
    }


def parse_rows(rows) -> list[dict]:
    """rows: message 表查询结果 [(id, session_id, time_created, time_updated, data), ...]
    返回已解析并附加表级字段的 dict 列表（跳过 user 消息）。
    """
    out = []
    for rid, sid, tc, tu, data in rows:
        p = parse_message_data(data)
        if p is None:
            continue
        p["id"] = rid
        p["session_id"] = sid
        p["time_created"] = int(tc)
        p["time_updated"] = int(tu)
        out.append(p)
    return out
