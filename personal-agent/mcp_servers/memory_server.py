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
    from mcplib import atomic_write_json
    atomic_write_json(SESSFILE, d)


def remember(key, value):
    key = str(key or "").strip()[:200]
    if not key:
        return "[LOI] key không được để trống"
    value = str(value or "")
    with _lock:
        d = _load()
        d[key] = value
        _save(d)
    return f"Đã nhớ [{key}] = {value[:120]}... ({len(value)} ký tự)"


def recall(keyword="", max_results=50):
    with _lock:
        d = _load()
    kw = str(keyword or "").strip().lower()   # ép str — keyword số/dict không được crash
    if not d:
        return "(chưa có gì được ghi nhớ)"
    out = []
    try:
        max_results = max(1, min(int(max_results or 50), 200))   # trần — tránh dump toàn DB tràn context
    except Exception:
        max_results = 50
    for k in sorted(d):
        v = str(d[k])
        if not kw or kw in k.lower() or kw in v.lower():
            out.append(f"[{k}] {v[:600]}")
            if len(out) >= max_results:
                break
    body = "\n".join(out)
    if not body:
        return "(không khớp keyword)"
    if len(out) >= max_results and len(d) > max_results:
        body += f"\n...(chỉ hiện {max_results}/{len(d)} mục, dùng keyword để lọc)"
    return body


TOOLS = [
    Tool("remember", "Lưu 1 mẩu thông tin dạng key-value vào bộ nhớ dài hạn của agent.",
         schema({"key": {"type": "string"}, "value": {"type": "string"}}), remember),
    Tool("recall", "Đọc các mẩu thông tin đã nhớ. keyword rỗng = đọc tất cả.",
         schema({"keyword": {"type": "string"}, "max_results": {"type": "integer", "description": "tối đa số mục trả về (mặc định 50)"}},
                ["keyword"]), recall),
]

if __name__ == "__main__":
    Server(TOOLS, "memory", "0.1.0").serve(sys.stdin, sys.stdout)
