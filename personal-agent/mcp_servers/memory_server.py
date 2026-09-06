import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json, threading
from mcplib import Server, Tool, schema
from config import DIR

SESSFILE = os.path.join(DIR, "graph.json")
_lock = threading.Lock()


def _load():
    try:
        with open(SESSFILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d):
    with open(SESSFILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)


def remember(key, value):
    with _lock:
        d = _load()
        d[key] = value
        _save(d)
    return f"Đã nhớ [{key}] = {value[:120]}... ({len(value)} ký tự)"


def recall(keyword=""):
    with _lock:
        d = _load()
    kw = (keyword or "").strip().lower()
    if not d:
        return "(chưa có gì được ghi nhớ)"
    out = []
    for k in sorted(d):
        v = d[k]
        if not kw or kw in k.lower() or kw in v.lower():
            out.append(f"[{k}] {v[:600]}")
    return "\n".join(out) if out else "(không khớp keyword)"


TOOLS = [
    Tool("remember", "Lưu 1 mẩu thông tin dạng key-value vào bộ nhớ dài hạn của agent.",
         schema({"key": {"type": "string"}, "value": {"type": "string"}}), remember),
    Tool("recall", "Đọc các mẩu thông tin đã nhớ. keyword rỗng = đọc tất cả.",
         schema({"keyword": {"type": "string"}}), recall),
]

if __name__ == "__main__":
    Server(TOOLS, "memory", "0.1.0").serve(sys.stdin, sys.stdout)