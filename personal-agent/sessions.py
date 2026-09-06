import json, os, time

from config import DIR
from providers import groq

SDIR = os.path.join(DIR, "sessions")
os.makedirs(SDIR, exist_ok=True)


def _f(sid):
    return os.path.join(SDIR, f"{sid}.jsonl")


def new():
    return time.strftime("%Y%m%d-%H%M%S")


def append(sid, msg):
    with open(_f(sid), "a", encoding="utf-8") as f:
        f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def load(sid):
    msgs = []
    if not os.path.isfile(_f(sid)):
        return msgs
    with open(_f(sid), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except ValueError:
                continue
    return msgs


def first_user(sid):
    for m in load(sid):
        if m.get("role") == "user":
            return (m.get("content") or "")[:60]
    return ""


def list_all():
    out = []
    for fn in sorted(os.listdir(SDIR)):
        if fn.endswith(".jsonl"):
            sid = fn[:-6]
            try:
                mt = os.path.getmtime(os.path.join(SDIR, fn))
            except Exception:
                mt = 0
            out.append((sid, time.strftime("%H:%M %d/%m", time.localtime(mt)), first_user(sid)))
    return out


def remove(sid):
    try:
        os.remove(_f(sid))
        return True
    except Exception:
        return False


def compact(sid, msgs, budget=90000):
    text = "".join(
        json.dumps(m, ensure_ascii=False) for m in msgs if m.get("role") in ("user", "assistant")
    )
    if len(text) <= budget:
        return msgs
    keep = msgs[-8:]
    cut = msgs[:-8]
    dump = [
        {"role": "user", "content": f"Tóm tắt bằng tiếng Việt, ngắn gọn còn nửa, giữ nguyên sự kiện/quyết định quan trọng. Chi tiết ban đầu:\n{j}"}
        for j in (json.dumps(m, ensure_ascii=False)[:2000] for m in cut)
        if j
    ]
    joined = "\n".join(m["content"] for m in dump)
    summary = groq.text(f"Hãy tóm tắt cuộc trò chuyện sau cho agent mới. Giữ sự kiện, công cụ, lỗi, quyết định:\n{joined[:15000]}", max_tokens=700)
    if summary:
        return [{"role": "user", "content": f"[TÓM TẮT CŨ]\n{summary}"}] + keep
    return msgs