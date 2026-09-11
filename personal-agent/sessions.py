import json, os, random, time

from config import DIR, CTX_TOTAL, CTX_CAP, SUM_AT, SUM_BUDGET
from providers import groq

SDIR = os.path.join(DIR, "sessions")
CKPT = os.path.join(DIR, "checkpoints")
os.makedirs(SDIR, exist_ok=True)
os.makedirs(CKPT, exist_ok=True)


def _f(sid):
    return os.path.join(SDIR, f"{sid}.jsonl")


def new():
    # ID duy nhất mỗi lần mở: timestamp tới microgiây + pid + random.
    # (Bản cũ chỉ chính xác tới giây + random 2 số → gọi nhanh liên tiếp vẫn TRÙNG,
    # đọc lộn lịch sử cũ → agent nói linh tinh.)
    import time as _t
    ts = _t.strftime("%Y%m%d-%H%M%S") + f"{_t.time_ns() % 1_000_000:06d}"
    return f"{ts}-{os.getpid() % 100000:05d}{random.randint(0, 9999):04d}"


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


def _char_len(m):
    return sum(len(x or "") for x in m.values() if isinstance(x, (str, bytes)))


def trim(msgs, total=CTX_TOTAL, cap=CTX_CAP):
    """Giữ context gọn trước khi gọi LLM. Giới hạn cứng: tổng ký tự.
    Xóa dứt điểm các tool result cũ nhất (giữ system + cuối) khi vượt ngưỡng,
    và cắt nội dung quá dài. Đảm bảo tổng giảm xuống dưới total."""
    msgs = [dict(m) for m in msgs]
    if not msgs:
        return msgs
    # 1) Cắt nội dung quá dài xuống cap
    for i, m in enumerate(msgs):
        c = m.get("content")
        if isinstance(c, str) and len(c) > cap:
            msgs[i]["content"] = c[:cap] + f"\n...[cắt {len(c) - cap} ký tự]"
    # 2) Xóa dứt điểm message tool cũ (không phải system/cuối) tới khi duoi ngưỡng
    keep = min(6, len(msgs))  # giữ k message cuối (assistant + tool kết quả gần nhất)
    guard_sys = 1 if msgs and msgs[0].get("role") == "system" else 0
    def _total():
        return sum(_char_len(m) for m in msgs)
    guard = 0
    while _total() > total and guard < (len(msgs) * 2):
        guard += 1
        # ưu tiên xóa tool result cũ nhất (khác tool message của cuối)
        idx = None
        for i in range(guard_sys, len(msgs) - keep):
            if msgs[i].get("role") == "tool":
                idx = i
                break
        if idx is not None:
            # chỉ xóa nếu không phải tool message vừa tạo ở cuối (luôn trong keep)
            msgs.pop(idx)
            continue
        # hết tool message để xóa → cắt message text cũ dài
        for i in range(guard_sys, len(msgs) - keep):
            c = msgs[i].get("content")
            if isinstance(c, str) and len(c) > 30:
                msgs[i]["content"] = c[: max(30, len(c) - (_total() - total) - 30)]
                break
        else:
            break
    return msgs


def compact(sid, msgs, budget=SUM_BUDGET, msg_cap=SUM_AT, llm_budget=None):
    """Tóm tắt thông minh khi context vượt ngưỡng ký tự HOẶC số message.
    Tính cả tool messages. Giữ lại system + các message cuối, tóm tắt phần cũ.
    Nếu LLM tóm tắt fail → fallback về trim cứng để không tràn."""
    msgs = [dict(m) for m in msgs]
    if not msgs:
        return msgs
    total_text = sum(len(m.get("content") or "") for m in msgs)
    if total_text <= budget and len(msgs) <= msg_cap:
        return msgs
    # giữ lại: system (đầu) + N message cuối (assistant/user/tool gần đây nhất)
    sys_msg = [msgs[0]] if msgs and msgs[0].get("role") == "system" else []
    keep_n = min(8, max(4, len(msgs) - 2))
    keep = msgs[-keep_n:] if len(msgs) >= keep_n else msgs[len(sys_msg):]
    cut = msgs[len(sys_msg):len(msgs) - keep_n] if len(msgs) > keep_n else []
    if not cut:
        # không có phần cũ để tóm tắt, chỉ còn giới hạn cứng
        return trim(msgs)
    dump = "\n".join(
        json.dumps(m, ensure_ascii=False)[:1500] for m in cut if _char_len(m) > 30
    )
    prompt = (
        "Bạn là bộ phận nén ngữ cảnh của agent Rem. Tóm tắt cuộc hội thoại sau thành "
        "bản tóm tắt tiếng Việt NGẮN GỌN (dưới 600 ký tự), CHỈ giữ thông tin quan trọng: "
        "mục tiêu đang làm, tiến độ, file/đường dẫn đã tạo/sửa, lỗi gặp phải, quyết định kỹ thuật. "
        "BỎ chi tiết vụn vặt.\n\nHội thoại:\n" + dump[:15000]
    )
    summary = groq.text(prompt, max_tokens=600, budget=llm_budget)
    if summary:
        return sys_msg + [{"role": "user", "content": f"[TÓM TẮT NGỮ CẢNH]\n{summary}"}] + keep
    # tóm tắt fail → fallback cứng để đảm bảo không tràn
    return trim(sys_msg + keep)


# ── checkpoint: lưu tiến độ/ngữ cảnh ra file để task lớn không mất khi context đầy ──
def checkpoint(sid, step, msgs, summary=""):
    try:
        path = os.path.join(CKPT, f"{sid}.cp.json")
        data = {"sid": sid, "step": step, "ts": time.time(), "summary": summary}
        # lưu 1 bản tóm tắt ngắn gọn tiến độ thực tế
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return path
    except Exception:
        return None


def load_checkpoint(sid):
    try:
        with open(os.path.join(CKPT, f"{sid}.cp.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None