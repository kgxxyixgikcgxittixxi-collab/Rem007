#!/usr/bin/env python3
"""20 bài test SIÊU KHÓ cho Remtm v3.57: concurrency, cancel, subagent, loop,
atomic writes, pair-fixing... Không tốn quota LLM (mock Groq khi cần).
Chạy: python3 test_hard20.py  (exit 0 = đậu hết)"""
import json
import os
import sys
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


# ── helper: fake MCP proc (stdin nuốt, stdout treo tới khi stop) ──
class _FakeStdin:
    def __init__(self):
        self.data = []

    def write(self, s):
        self.data.append(s)

    def flush(self):
        pass

    def close(self):
        pass


class _FakeStdout:
    def __init__(self, stop):
        self._stop = stop

    def readline(self):
        while not self._stop.is_set():
            time.sleep(0.05)
        return ""


def _fake_proc(stop):
    p = types.SimpleNamespace()
    p.stdin = _FakeStdin()
    p.stdout = _FakeStdout(stop)
    p.poll = lambda: None
    p.returncode = 0
    return p


def t01_sessions_concurrent():
    print("[01] 20 luồng x 50 append session -> đủ 1000 dòng JSON hợp lệ")
    import sessions
    sid = "hard01"
    try:
        os.remove(sessions._f(sid))
    except Exception:
        pass

    def w(n):
        for i in range(50):
            sessions.append(sid, {"role": "user", "content": f"t{n}-{i}"})

    ths = [threading.Thread(target=w, args=(n,)) for n in range(20)]
    [t.start() for t in ths]
    [t.join(30) for t in ths]
    bad, n = 0, 0
    with open(sessions._f(sid), encoding="utf-8") as f:
        for line in f:
            n += 1
            try:
                json.loads(line)
            except Exception:
                bad += 1
    check("đủ dòng + hợp lệ", n == 1000 and bad == 0, f"(n={n} bad={bad})")
    try:
        os.remove(sessions._f(sid))
    except Exception:
        pass


def t02_overlay_hammer():
    print("[02] 10 luồng x 30 overlay_write + đọc liên tục -> không rách JSON")
    import repl
    errs = []

    def w(n):
        try:
            for i in range(30):
                repl._overlay_write(mode="QUAN SÁT", task=f"t{n}-{i}", progress="hammer")
        except Exception as e:
            errs.append(repr(e))

    def r():
        try:
            for _ in range(100):
                try:
                    json.load(open(repl.OVERLAY_FILE, encoding="utf-8"))
                except FileNotFoundError:
                    pass
                time.sleep(0.005)
        except Exception as e:
            errs.append("CORRUPT:" + repr(e))

    ths = [threading.Thread(target=w, args=(n,)) for n in range(10)]
    ths.append(threading.Thread(target=r))
    [t.start() for t in ths]
    [t.join(60) for t in ths]
    check("không lỗi/rách", not errs, str(errs[:2]))
    repl._overlay_write(mode="RẢNH", task="", tool="", progress="")


def t03_client_interrupt():
    print("[03] request đang treo + cancel -> InterruptedError trong 2s")
    from mcplib import Client
    stop = threading.Event()
    c = Client(_fake_proc(stop))
    box = {}

    def do():
        try:
            c.request("tools/call", {}, timeout=60)
            box["r"] = "NO-RAISE"
        except InterruptedError:
            box["r"] = "INTERRUPTED"
        except Exception as e:
            box["r"] = f"OTHER:{type(e).__name__}"

    th = threading.Thread(target=do, daemon=True)
    t0 = time.time()
    th.start()
    time.sleep(0.4)
    c.cancel_pending()
    th.join(10)
    dt = time.time() - t0
    stop.set()
    check("ngắt nhanh + đúng lỗi", box.get("r") == "INTERRUPTED" and dt < 5,
          f"({box.get('r')} {dt:.1f}s)")


def t04_client_timeout_kept():
    print("[04] không cancel + timeout ngắn -> vẫn TimeoutError")
    from mcplib import Client
    stop = threading.Event()
    c = Client(_fake_proc(stop))
    t0 = time.time()
    try:
        c.request("tools/call", {}, timeout=1)
        ok = False
    except TimeoutError:
        ok = True
    except Exception:
        ok = False
    stop.set()
    check("timeout đúng hẹn", ok and time.time() - t0 < 5, f"({time.time()-t0:.1f}s)")


def t05_reset_cancel():
    print("[05] reset_cancel cho task mới dùng tiếp được")
    from mcplib import Client
    stop = threading.Event()
    c = Client(_fake_proc(stop))
    c.cancel_pending()
    c.reset_cancel()
    check("flag sạch", c._cancel.is_set() is False)
    stop.set()


def t06_manager_interrupt():
    print("[06] Manager.interrupt/reset lan tới mọi client")
    from extensions import Manager
    rec = []

    class FakeClient:
        def cancel_pending(self):
            rec.append("cancel")

        def reset_cancel(self):
            rec.append("reset")

    m = Manager(specs=[])
    m.extensions.append(types.SimpleNamespace(client=FakeClient(), enabled=True,
                                              tools=[], name="x", desc=""))
    m.interrupt()
    m.reset_interrupt()
    check("lan đủ 2 lệnh", rec == ["cancel", "reset"], str(rec))


def t07_task_schema_depth():
    print("[07] task có trong schemas ở tầng 0, ẩn ở tầng sâu")
    from extensions import Manager
    m = Manager(specs=[])
    names0 = [t["function"]["name"] for t in m.schemas()]
    m._task_n = 1
    names1 = [t["function"]["name"] for t in m.schemas()]
    m._task_n = 0
    check("hiện/ẩn đúng", "task" in names0 and "task" not in names1)


def t08_task_missing_prompt():
    print("[08] task thiếu prompt -> [LOI], không chạy agent")
    from extensions import Manager
    m = Manager(specs=[])
    out = m.call("task", {"description": "x"}, timeout=20)
    check("từ chối lịch sự", out.startswith("[LOI]"), out[:60])


def t09_task_nested_guard():
    print("[09] task lồng nhau -> chặn, không đệ quy vô hạn")
    from extensions import Manager
    m = Manager(specs=[])
    m._task_n = 1
    out = m.call("task", {"prompt": "làm gì đó"}, timeout=20)
    m._task_n = 0
    check("chặn tầng 2", "lồng nhau" in out, out[:60])


def t10_subagent_readonly():
    print("[10] agent con bị deny ghi/shell/task nhưng được đọc")
    import agentloop
    from extensions import Manager
    seen = {}

    class FakeAgent:
        def __init__(self, manager, perm, sid=None, on_event=None):
            seen["perm"] = perm

        def run(self, prompt):
            seen["prompt"] = prompt
            return "TÓM TẮT FAKE"

    orig = agentloop.Agent
    agentloop.Agent = FakeAgent
    try:
        m = Manager(specs=[])
        out = m._run_subagent({"description": "t", "prompt": "tìm hàm login"}, timeout=30)
    finally:
        agentloop.Agent = orig
    p = seen.get("perm")
    denies = p is not None and all(
        p.decide(t, {}, askfn=None) is False
        for t in ("write_file", "edit_file", "bash", "task", "dl_click", "browser_open"))
    allows = p is not None and p.decide("read_file", {}, askfn=None) is True
    check("deny đủ + cho đọc + trả tóm tắt",
          denies and allows and out == "TÓM TẮT FAKE", out[:60])


def t11_repeat_failing():
    print("[11] cùng tool fail 2 lần liên tiếp -> kẹt; success thì không")
    from agentloop import Agent
    from permissions import Presets

    class DM:
        def schemas(self):
            return []

    a = Agent(DM(), Presets.build(), sid="hard11")
    a._last_steps = [{"tool": "bash", "args": {}, "result_ok": False},
                     {"tool": "bash", "args": {}, "result_ok": False}]
    bad = a._repeat_failing("bash")
    a._last_steps = [{"tool": "read_file", "args": {"p": "a"}, "result_ok": True},
                     {"tool": "read_file", "args": {"p": "b"}, "result_ok": True},
                     {"tool": "read_file", "args": {"p": "c"}, "result_ok": True}]
    good = a._repeat_failing("read_file")
    a._last_steps = [{"tool": "bash", "args": {}, "result_ok": False},
                     {"tool": "read_file", "args": {}, "result_ok": True}]
    mixed = a._repeat_failing("bash")
    check("fail-liên-tiếp=kẹt", bad is True)
    check("success-liên-tiếp=không kẹt", good is False)
    check("đan xen=không kẹt", mixed is False)


def t12_exact_loop():
    print("[12] gọi y hệt 2 lần -> loop")
    from agentloop import Agent
    from permissions import Presets

    class DM:
        def schemas(self):
            return []

    a = Agent(DM(), Presets.build(), sid="hard12")
    first = a._is_looping("read_file", {"path": "a.py"})
    second = a._is_looping("read_file", {"path": "a.py"})
    check("lần 1 qua, lần 2 chặn", first is False and second is True)


def t13_fingerprint_order():
    print("[13] fingerprint không phụ thuộc thứ tự key JSON")
    from agentloop import Agent
    from permissions import Presets

    class DM:
        def schemas(self):
            return []

    a = Agent(DM(), Presets.build(), sid="hard13")
    f1 = a._tool_fingerprint("bash", {"a": 1, "b": 2})
    f2 = a._tool_fingerprint("bash", {"b": 2, "a": 1})
    check("ổn định", f1 == f2)


def t14_fix_pairs():
    print("[14] tool_calls mồ côi bị trim cắt -> _fix_pairs dọn sạch")
    import sessions
    msgs = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "1", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}},
                        {"id": "2", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "name": "bash", "content": "ok"},
    ]
    fixed = sessions._fix_pairs([dict(m) for m in msgs])
    asm = [m for m in fixed if m.get("role") == "assistant"][0]
    tools = [m for m in fixed if m.get("role") == "tool"]
    check("giữ cặp đúng, bỏ mồ côi",
          [tc["id"] for tc in asm.get("tool_calls", [])] == ["1"] and len(tools) == 1)


def t15_compact_no_llm():
    print("[15] LLM tóm tắt chết -> compact fallback trim, không crash")
    import sessions
    from providers import groq
    orig = groq.text
    groq.text = lambda *a, **k: None
    try:
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(80):
            msgs.append({"role": "user", "content": f"câu {i} " + "x" * 2000})
        out = sessions.compact("hard15", msgs)
        check("trả list gọn", isinstance(out, list) and len(out) < len(msgs), f"({len(out)})")
    finally:
        groq.text = orig


def t16_inject_storm():
    print("[16] bão 10 lệnh giữa chừng -> giữ đúng 5 mới nhất")
    from agentloop import Agent
    from permissions import Presets

    class DM:
        def schemas(self):
            return []

    a = Agent(DM(), Presets.build(), sid="hard16")
    for i in range(10):
        a.inject(f"lệnh {i}")
    notes = a._drain_notes()
    check("cap 5 mới nhất", notes == [f"lệnh {i}" for i in range(5, 10)], str(notes))
    a.inject("x")
    a.stop()
    check("stop xóa", a.live_count() == 0)


def t17_noise_adversarial():
    print("[17] rác đội lốt lệnh thật và ngược lại")
    from repl import _is_inject_noise
    check("status echo=rác", _is_inject_noise("📥 Đã chuyển cho agent (2 chỉ đạo)") is True)
    check("logo fragment=rác", _is_inject_noise("|  _ <| |___| |  | |") is True)
    check("lệnh có dấu ❯=thật", _is_inject_noise("so sánh a ❯ b trong bash") is False)
    check("lệnh ngắn thật", _is_inject_noise("tiếp tục đi") is False)
    check("số trần=rác", _is_inject_noise("7") is True)


def t18_route_coverage():
    print("[18] định tuyến trúng nhóm việc khó")
    from agentloop import route_task
    cases = [
        ("quét toàn repo tìm hàm login xử lý sai", "lap-trinh"),
        ("mở app Terminal gõ lệnh kiểm tra", "desktop"),
        ("hủy follow tiktok không phải bạn bè", "mang-xa-hoi"),
        ("làm video shorts giới thiệu sản phẩm", "media"),
        ("tìm giá cà phê hôm nay trên web", "web"),
    ]
    ok = True
    for text, want in cases:
        g, _ = route_task(text)
        if want not in g:
            ok = False
            print(f"    route sai: {text[:30]!r} -> {g}, muốn {want}")
    check("5/5 trúng", ok)


def t19_perm_fullauto():
    print("[19] full-auto: tool lạ cũng cho qua, không hỏi")
    from permissions import PermPolicy
    p = PermPolicy()
    fired = []

    def ask(n, a):
        fired.append(n)
        return False

    ok = p.decide("tool_lạ_chưa_từng_thấy", {"x": 1}, askfn=ask)
    check("auto-allow + không hỏi", ok is True and not fired, str(fired))


def t20_trim_huge_single():
    print("[20] 1 tool result 200KB -> trim cắt gọn dưới ngưỡng")
    import sessions
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "làm đi"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "9", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "9", "name": "bash", "content": "Z" * 200000},
            {"role": "assistant", "content": "xong"}]
    out = sessions.trim([dict(m) for m in msgs])
    total = sum(len(str(v)) for m in out for v in m.values() if isinstance(v, str))
    check("tổng dưới ngưỡng", total <= 20000, f"({total})")


def t21_task_dispatch_e2e():
    print("[21] cha -> tool task -> con chạy -> tóm tắt về (mock LLM, 0 quota)")
    from agentloop import Agent
    from extensions import Manager
    from permissions import PermPolicy
    import providers.groq as _g
    calls = {"n": 0}

    def fake_chat_stream(msgs, tools=None, budget=None, on_delta=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "task", "arguments": json.dumps(
                     {"description": "liệt kê file",
                      "prompt": "liệt kê 3 file .py"})}}]}
        if calls["n"] == 2:
            return {"content": "child: a.py, b.py, c.py", "tool_calls": []}
        return {"content": "parent xong: a.py, b.py, c.py", "tool_calls": []}

    orig = _g.chat_stream
    _g.chat_stream = fake_chat_stream
    try:
        m = Manager(specs=[])
        a = Agent(m, PermPolicy(), sid="hard21")
        out = a.run("Dùng task giao cho agent con liệt kê file")
    finally:
        _g.chat_stream = orig
    check("con chạy + tóm tắt về cha", "a.py" in (out or ""), (out or "")[:150])


def main():
    global FAIL
    tests = [t01_sessions_concurrent, t02_overlay_hammer, t03_client_interrupt,
             t04_client_timeout_kept, t05_reset_cancel, t06_manager_interrupt,
             t07_task_schema_depth, t08_task_missing_prompt, t09_task_nested_guard,
             t10_subagent_readonly, t11_repeat_failing, t12_exact_loop,
             t13_fingerprint_order, t14_fix_pairs, t15_compact_no_llm,
             t16_inject_storm, t17_noise_adversarial, t18_route_coverage,
             t19_perm_fullauto, t20_trim_huge_single, t21_task_dispatch_e2e]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            print(f"  FAIL {t.__name__} CRASH: {type(e).__name__}: {e}")
    print(f"TỔNG 20 bài: {PASS} pass, {FAIL} fail")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
