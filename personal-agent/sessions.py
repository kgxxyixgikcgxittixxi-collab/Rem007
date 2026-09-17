import json, os, random, threading, time

from config import DIR, CTX_TOTAL, CTX_CAP, SUM_AT, SUM_BUDGET

_APPEND_LOCK = threading.Lock()
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
    # Ghi nối tiếp thread-safe + fsync: worker/compact/auto-resume ghi xen kẽ
    # không rách JSONL, crash giữa chừng không mất dòng đã ghi.
    line = json.dumps(msg, ensure_ascii=False) + "\n"
    with _APPEND_LOCK:
        with open(_f(sid), "a", encoding="utf-8") as f:
            f.write(line)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
    # Auto-title kiểu opencode (title agent thu gọn): tin user đầu tiên đặt tên
    # session nếu user chưa /rename. Không tốn LLM.
    try:
        if isinstance(msg, dict) and msg.get("role") == "user":
            ts = _load_titles()
            if sid not in ts:
                t = " ".join(str(msg.get("content") or "").split())[:48] or sid
                ts[sid] = t
                _save_titles(ts)
    except Exception:
        pass


def _titles_f():
    return os.path.join(SDIR, "_titles.json")


def _load_titles():
    try:
        with open(_titles_f(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_titles(t):
    try:
        tmp = _titles_f() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(t, f, ensure_ascii=False)
        os.replace(tmp, _titles_f())
    except Exception:
        pass


def get_title(sid):
    """Tên session: user đặt (/rename) > auto-title > first_user > sid."""
    try:
        t = (_load_titles().get(sid) or "").strip()
        if t:
            return t
    except Exception:
        pass
    try:
        return first_user(sid) or sid
    except Exception:
        return sid


def set_title(sid, title):
    """Đặt tên session (/rename). Trả True nếu lưu được."""
    title = " ".join(str(title or "").split())[:80]
    if not title:
        return False
    try:
        ts = _load_titles()
        ts[sid] = title
        _save_titles(ts)
        return True
    except Exception:
        return False


def fork(sid):
    """Nhân bản session hiện tại thành sid mới (kiểu opencode fork), giữ title gốc."""
    msgs = load(sid)
    nid = new()
    try:
        _write_all(nid, msgs)
    except Exception:
        return ""
    try:
        t = get_title(sid)
        if t and t != sid:
            ts = _load_titles()
            ts[nid] = (t[:70] + " (fork)") if len(t) <= 70 else (t[:70] + "…")
            _save_titles(ts)
    except Exception:
        pass
    return nid


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
            out.append((sid, time.strftime("%H:%M %d/%m", time.localtime(mt)), get_title(sid)))
    return out


def remove(sid):
    try:
        os.remove(_f(sid))
        return True
    except Exception:
        return False


def _redo_f(sid):
    return os.path.join(SDIR, f"{sid}.redo.json")


def _work_f(sid):
    return os.path.join(SDIR, f"{sid}.work.json")


def _work_redo_f(sid):
    return os.path.join(SDIR, f"{sid}.workredo.json")


def _work_bin(sid):
    d = os.path.join(SDIR, f"{sid}.workbin")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _git(cwd, *args, input_data=None):
    """Chạy git -C cwd, trả (returncode, stdout). Timeout ngắn, không crash."""
    import subprocess as _sp
    try:
        p = _sp.run(["git", "-C", cwd or os.getcwd()] + list(args),
                    capture_output=True, text=True, timeout=15,
                    input=input_data)
        return p.returncode, (p.stdout or "")
    except Exception:
        return 127, ""


def _git_repo(cwd):
    rc, _ = _git(cwd, "rev-parse", "--git-dir")
    return rc == 0


def _git_untracked(cwd):
    rc, out = _git(cwd, "status", "--porcelain", "--untracked-files=all", "--", ".")
    if rc != 0:
        return []
    files = []
    for ln in out.splitlines():
        if ln.startswith("??"):
            f = ln[2:].strip().strip('"')
            if f:
                files.append(f)
    return files


def _diff_files(patch):
    """Trích danh sách file từ patch git diff (dòng '+++ b/<path>')."""
    files = []
    for ln in (patch or "").splitlines():
        if ln.startswith("+++ b/"):
            f = ln[6:].strip()
            if f and f != "/dev/null" and f not in files:
                files.append(f)
    return files


def work_snapshot(sid, cwd=None):
    """Chụp trạng thái git TRƯỚC lượt agent chạy (gọi trong _send).
    Lưu patch diff + danh sách untracked vào stack (tối đa 20). Không phải git → no-op."""
    try:
        cwd = cwd or os.getcwd()
        if not _git_repo(cwd):
            return False
        rc, patch = _git(cwd, "diff", "HEAD", "--", ".")
        if rc != 0:
            return False
        if len(patch or "") > 2_000_000:
            return False  # patch quá lớn (binary?) → bỏ qua snapshot này
        ent = {"cwd": cwd, "patch": patch or "", "untracked": _git_untracked(cwd)}
        stack = []
        try:
            with open(_work_f(sid), "r", encoding="utf-8") as f:
                stack = json.load(f) or []
            if not isinstance(stack, list):
                stack = []
        except Exception:
            stack = []
        stack.append(ent)
        stack = stack[-20:]
        tmp = _work_f(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stack, f, ensure_ascii=False)
        os.replace(tmp, _work_f(sid))
        return True
    except Exception:
        return False


def work_undo(sid, cwd=None):
    """Hoàn tác file agent đã đổi trong turn cuối (kiểu opencode /undo).
    Chỉ revert file khác biệt so với snapshot trước lượt chạy. Trả (files, msg).
    files: danh sách file đã revert. Không phải git / không snapshot → ([], lý do)."""
    try:
        stack = []
        try:
            with open(_work_f(sid), "r", encoding="utf-8") as f:
                stack = json.load(f) or []
            if not isinstance(stack, list):
                stack = []
        except Exception:
            stack = []
        if not stack:
            return [], "không có snapshot (phiên này không chạy trong git repo?)"
        ent = stack.pop()
        tmp = _work_f(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stack, f, ensure_ascii=False)
        os.replace(tmp, _work_f(sid))
        cwd = cwd or ent.get("cwd") or os.getcwd()
        if not _git_repo(cwd):
            return [], "không phải git repo — chỉ undo nội dung chat"
        rc, cur_patch = _git(cwd, "diff", "HEAD", "--", ".")
        if rc != 0:
            return [], "không đọc được git diff"
        cur_untracked = _git_untracked(cwd)
        changed = _diff_files(cur_patch)
        new_untracked = [f for f in cur_untracked if f not in (ent.get("untracked") or [])]
        if not changed and not new_untracked:
            return [], "turn này không đổi file nào"
        # Backup untracked mới để /redo khôi phục được
        backups = {}
        bindir = _work_bin(sid)
        for i, f in enumerate(new_untracked):
            try:
                fp = os.path.join(cwd, f)
                if os.path.isfile(fp) and os.path.getsize(fp) <= 1_000_000:
                    with open(fp, "rb") as fh:
                        data = fh.read()
                    bp = os.path.join(bindir, f"new{i}.bin")
                    with open(bp, "wb") as fh:
                        fh.write(data)
                    backups[f] = bp
            except Exception:
                continue
        # Revert tracked về HEAD (CHỈ file đổi trong turn)
        reverted = []
        if changed:
            rc2, _ = _git(cwd, "checkout", "HEAD", "--", *changed)
            if rc2 == 0:
                reverted = list(changed)
        # Xóa untracked mới sinh trong turn
        for f in new_untracked:
            try:
                fp = os.path.join(cwd, f)
                if os.path.isfile(fp):
                    os.remove(fp)
                    reverted.append(f + " (file mới — đã xóa, /redo để lấy lại)")
            except Exception:
                continue
        # Lưu redo entry
        try:
            rstack = []
            try:
                with open(_work_redo_f(sid), "r", encoding="utf-8") as f:
                    rstack = json.load(f) or []
                if not isinstance(rstack, list):
                    rstack = []
            except Exception:
                rstack = []
            rstack.append({"cwd": cwd, "patch": cur_patch or "", "backups": backups})
            rstack = rstack[-20:]
            tmp2 = _work_redo_f(sid) + ".tmp"
            with open(tmp2, "w", encoding="utf-8") as f:
                json.dump(rstack, f, ensure_ascii=False)
            os.replace(tmp2, _work_redo_f(sid))
        except Exception:
            pass
        return reverted, ""
    except Exception as e:
        return [], f"{type(e).__name__}: {e}"


def work_redo(sid):
    """Apply lại patch đã work_undo (kiểu opencode /redo). Trả (ok, msg)."""
    try:
        rstack = []
        try:
            with open(_work_redo_f(sid), "r", encoding="utf-8") as f:
                rstack = json.load(f) or []
            if not isinstance(rstack, list):
                rstack = []
        except Exception:
            rstack = []
        if not rstack:
            return False, "không có gì để redo"
        ent = rstack.pop()
        tmp = _work_redo_f(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rstack, f, ensure_ascii=False)
        os.replace(tmp, _work_redo_f(sid))
        cwd = ent.get("cwd") or os.getcwd()
        if not _git_repo(cwd):
            return False, "không phải git repo"
        if ent.get("patch"):
            rc, _ = _git(cwd, "apply", "--whitespace=nowarn", "-", input_data=ent["patch"])
            if rc != 0:
                # Thử apply 3 chiều khi file đã đổi thêm sau undo
                rc, _ = _git(cwd, "apply", "--3way", "--whitespace=nowarn", "-",
                             input_data=ent["patch"])
                if rc != 0:
                    return False, "không apply được patch (file đã đổi quá nhiều sau undo)"
        n = 0
        for f, bp in (ent.get("backups") or {}).items():
            try:
                fp = os.path.join(cwd, f)
                os.makedirs(os.path.dirname(fp) or cwd, exist_ok=True)
                with open(bp, "rb") as fh:
                    data = fh.read()
                with open(fp, "wb") as fh:
                    fh.write(data)
                n += 1
            except Exception:
                continue
        return True, f"đã redo file ({n} file mới khôi phục)" if n else "đã redo file"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _write_all(sid, msgs):
    """Ghi đè toàn bộ session (dùng cho undo/redo/compact). Atomic tmp+replace."""
    tmp = _f(sid) + ".tmp"
    with _APPEND_LOCK:
        with open(tmp, "w", encoding="utf-8") as f:
            for m in msgs:
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        os.replace(tmp, _f(sid))


def undo_last_turn(sid):
    """Cắt 1 turn user cuối (user + assistant/tool theo sau) khỏi session.
    Lưu phần cắt vào stack .redo để /redo khôi phục. Trả về list popped (rỗng nếu không có)."""
    msgs = load(sid)
    idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            idx = i
            break
    if idx < 0:
        return []
    popped = msgs[idx:]
    remaining = _fix_pairs(msgs[:idx])
    try:
        _write_all(sid, remaining)
    except Exception:
        pass
    try:
        stack = []
        try:
            with open(_redo_f(sid), "r", encoding="utf-8") as f:
                stack = json.load(f) or []
            if not isinstance(stack, list):
                stack = []
        except Exception:
            stack = []
        stack.append(popped)
        stack = stack[-20:]
        tmp = _redo_f(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stack, f, ensure_ascii=False)
        os.replace(tmp, _redo_f(sid))
    except Exception:
        pass
    return popped


def redo_pop(sid):
    """Khôi phục turn vừa undo (pop khỏi stack .redo, append lại vào session)."""
    stack = []
    try:
        with open(_redo_f(sid), "r", encoding="utf-8") as f:
            stack = json.load(f) or []
        if not isinstance(stack, list):
            return []
    except Exception:
        return []
    if not stack:
        return []
    turn = stack.pop()
    try:
        tmp = _redo_f(sid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stack, f, ensure_ascii=False)
        os.replace(tmp, _redo_f(sid))
    except Exception:
        pass
    if not isinstance(turn, list) or not turn:
        return []
    try:
        cur = load(sid)
        _write_all(sid, _fix_pairs(cur + turn))
    except Exception:
        pass
    return turn


def _char_len(m):
    return sum(len(x or "") for x in m.values() if isinstance(x, (str, bytes)))


def _fix_pairs(msgs):
    """Bảo đảm cặp assistant.tool_calls ↔ tool message khớp nhau.
    Groq/OpenAI trả 400 nếu assistant còn tool_calls mồ côi (tool message đã bị
    trim/compact cắt mất) — lỗi này deterministic nhưng agent lại đốt hết key
    để retry vô ích. Hàm này cắt tool_calls thừa + bỏ tool message lạc."""
    ids_tool = {m.get("tool_call_id") for m in msgs
                if m.get("role") == "tool" and m.get("tool_call_id")}
    out = []
    for m in msgs:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            kept = [tc for tc in m["tool_calls"] if tc.get("id") in ids_tool]
            if len(kept) != len(m["tool_calls"]):
                m = dict(m)
                if kept:
                    m["tool_calls"] = kept
                else:
                    m.pop("tool_calls", None)
                    if not (m.get("content") or "").strip():
                        continue
        out.append(m)
    ref = set()
    for m in out:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                ref.add(tc.get("id"))
    return [m for m in out
            if m.get("role") != "tool" or (m.get("tool_call_id") in ref)]


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
    # Fallback cứng: session ngắn (≤6 tin) mà mỗi tin đều dài → vùng giữa rỗng,
    # vòng trên không làm gì được. Cắt message dài nhất (trừ system) tới khi đủ.
    # (Bản cũ trả về nguyên tổng 30-40k ký tự → tràn context Groq.)
    guard = 0
    while _total() > total and guard < 50:
        guard += 1
        bi, bl = -1, 200
        for i in range(guard_sys, len(msgs)):
            c = msgs[i].get("content")
            if isinstance(c, str) and len(c) > bl:
                bi, bl = i, len(c)
        if bi < 0:
            break
        over = _total() - total
        msgs[bi]["content"] = msgs[bi]["content"][: max(200, bl - over - 50)]
    return _fix_pairs(msgs)


def compact(sid, msgs, budget=SUM_BUDGET, msg_cap=SUM_AT, llm_budget=None, cancel=None):
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
    summary = groq.text(prompt, max_tokens=600, budget=llm_budget, cancel=cancel)
    if summary:
        return _fix_pairs(sys_msg + [{"role": "user", "content": f"[TÓM TẮT NGỮ CẢNH]\n{summary}"}] + keep)
    # tóm tắt fail → fallback cứng để đảm bảo không tràn
    return trim(sys_msg + keep)


# ── checkpoint: lưu tiến độ/ngữ cảnh ra file để task lớn không mất khi context đầy ──
def checkpoint(sid, step, msgs, summary=""):
    try:
        path = os.path.join(CKPT, f"{sid}.cp.json")
        data = {"sid": sid, "step": step, "ts": time.time(), "summary": summary}
        # lưu 1 bản tóm tắt ngắn gọn tiến độ thực tế (ghi atomic chống rách file)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
        return path
    except Exception:
        return None


def load_checkpoint(sid):
    try:
        with open(os.path.join(CKPT, f"{sid}.cp.json"), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
