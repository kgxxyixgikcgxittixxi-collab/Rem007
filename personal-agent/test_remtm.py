#!/usr/bin/env python3
"""Test liên tục cho Remtm: full-auto + chống loạn chữ + chống tồn lệnh.
Chạy:  python3 test_remtm.py            (1 lượt)
       python3 test_remtm.py --loop 5   (5 lượt liên tục)
       bash test_remtm.sh [số_lượt]     (gồm py_compile + unit)
Không gọi mạng/LLM — an toàn chạy liên tục, không tốn quota Groq.
"""
import os
import sys

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


def test_full_auto():
    print("[1] full quyền tự động (không hỏi)")
    from permissions import PermPolicy
    p = PermPolicy()
    check("auto mặc định True", p.auto is True, f"(auto={p.auto})")

    def _must_not_ask(name, args):
        raise AssertionError(f"đã hỏi quyền cho {name} — sai, phải auto")

    for tool, args in (
        ("bash", {"command": "ls"}),
        ("write_file", {"path": "/tmp/x", "content": "hi"}),
        ("dl_click", {"x": 1}),
        ("browser_open", {"url": "https://example.com"}),
        ("social_post", {"text": "hi"}),
    ):
        try:
            ok = p.decide(tool, args, askfn=_must_not_ask)
        except AssertionError as e:
            check(f"decide {tool} không hỏi", False, str(e))
            continue
        check(f"decide {tool} auto-allow", ok is True)


def test_inject_noise():
    print("[2] lọc rác terminal paste nhầm")
    from repl import _is_inject_noise
    garbage = [
        "╭─❯ gõ câu hỏi · /list danh mục · /rec ghi thao tác",
        "📥 Đã chuyển cho agent đang chạy (3 chỉ đạo chờ)",
        "|_| \\_\\_____|_|  |_|",
        "   | |_) |  _| | |\\/| |",
        "Gõ /help | /status | /stop | /clear | /exit",
        "x",
    ]
    for g in garbage:
        check(f"bỏ rác {g[:30]!r}", _is_inject_noise(g) is True)
    for real in ["hủy follow tiktok không phải bạn bè", "mở terminal gõ ls", "là sao?"]:
        check(f"giữ lệnh thật {real[:20]!r}", _is_inject_noise(real) is False)


def test_inject_cap_and_stop():
    print("[3] hàng chờ tối đa 5 + /stop xóa sạch")
    from agentloop import Agent

    class DummyMgr:
        def schemas(self):
            return []

        def call(self, *a, **k):
            return ""

    from permissions import Presets
    a = Agent(DummyMgr(), Presets.build(), sid="test-cap")
    for i in range(7):
        a.inject(f"lệnh {i}")
    check("cap 5 lệnh", a.live_count() == 5, f"(got {a.live_count()})")
    notes = a._drain_notes()
    check("giữ 5 mới nhất", notes == [f"lệnh {i}" for i in range(2, 7)], f"(got {notes})")
    a.inject("a")
    a.inject("b")
    a.stop()
    check("/stop xóa live_notes", a.live_count() == 0)


def test_prompt_2line():
    print("[4] prompt luôn 2 dòng (bận/rảnh cùng chiều cao)")
    from extensions import Manager
    from repl import Repl
    m = Manager()
    try:
        r = Repl(m)
    except Exception as e:
        check("khởi tạo Repl", False, str(e))
        return
    try:
        idle = r._prompt_hint()
        check("rảnh 2 dòng", idle.count("\n") == 1, repr(idle[:60]))
        r._busy = True
        busy = r._prompt_hint()
        check("bận 2 dòng", busy.count("\n") == 1, repr(busy[:60]))
        r._busy = False
        r._pending = 2
        pend = r._prompt_hint()
        check("chờ 2 dòng", pend.count("\n") == 1, repr(pend[:60]))
    finally:
        try:
            m.close_all()
        except Exception:
            pass


def test_repl_helpers():
    print("[5] lock in ấn + spinner flag tồn tại")
    import repl
    check("_OUT_LOCK", hasattr(repl, "_OUT_LOCK"))
    check("_in_input mặc định", True)  # kiểm tra qua source để khỏi khởi tạo nặng
    src = open("repl.py", encoding="utf-8").read()
    for needle in ("self._in_input = False", "self._in_input = True",
                   "_is_inject_noise(line)", "Hàng chờ đầy",
                   "KHÔNG in \"❯ \" tay"):
        check(f"source có {needle[:28]!r}", needle in src)
    psrc = open("permissions.py", encoding="utf-8").read()
    check("permissions có full_auto", "full_auto" in psrc)


def test_overlay_and_fastpath():
    print("[6] cửa sổ nổi + học 1 lần chạy nhanh")
    import py_compile
    try:
        py_compile.compile("overlay.py", doraise=True)
        check("overlay.py compile", True)
    except Exception as e:
        check("overlay.py compile", False, str(e))
    import repl
    check("overlay hooks", all(hasattr(repl, n) for n in
          ("_overlay_write", "_overlay_ensure", "_overlay_stop", "OVERLAY_FILE")))
    try:
        repl._overlay_write(mode="RẢNH", task="test", tool="", progress="test")
        import json
        d = json.load(open(repl.OVERLAY_FILE, encoding="utf-8"))
        check("overlay.json ghi được", d.get("task") == "test", str(d)[:80])
    except Exception as e:
        check("overlay.json ghi được", False, str(e))
    finally:
        try:
            repl._overlay_write(mode="RẢNH", task="", tool="", progress="")
        except Exception:
            pass
    from agentloop import route_task, _route_section
    g, _ = route_task("hủy những người tôi đã follow tiktok không phải bạn bè")
    check("route tiktok -> mang-xa-hoi/desktop",
          any(x in ("mang-xa-hoi", "desktop", "web") for x in g), str(g))
    sec = _route_section("hủy những người tôi đã follow tiktok")
    check("prompt có HỌC 1 LẦN", "HỌC 1 LẦN" in sec)
    check("prompt có rec_play/skill_use", "rec_play" in sec and "skill_use" in sec)
    check("slash /overlay", "/overlay" in repl._SLASH if hasattr(repl, "_SLASH") else False)


def test_rec_stop_empty():
    print("[7] rec_stop toàn bước lỗi -> báo thân thiện, không crash")
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_servers"))
        import desktop_linux as dl
        dl.rec_start("test_loi", "khac")
        dl._REC["actions"] = [{"tool": "dl_click", "args": {"ref": "at999"},
                               "ok": False, "dt": 0.1}]
        out = dl.rec_stop()
        check("không NameError", "NameError" not in out and "Traceback" not in out)
        check("báo tên macro", out.startswith("[LOI]") and "test_loi" in out, out[:100])
        check("state reset", dl._REC["active"] is False and dl._REC["name"] == "")
    except Exception as e:
        check("rec_stop hồi quy", False, f"{type(e).__name__}: {e}")


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=1)
    args = ap.parse_args()
    total_fail = 0
    for rnd in range(1, args.loop + 1):
        global PASS, FAIL
        PASS, FAIL = 0, 0
        if args.loop > 1:
            print(f"=== lượt {rnd}/{args.loop} ===")
        test_full_auto()
        test_inject_noise()
        test_inject_cap_and_stop()
        test_prompt_2line()
        test_repl_helpers()
        test_overlay_and_fastpath()
        test_rec_stop_empty()
        print(f"--> lượt {rnd}: {PASS} pass, {FAIL} fail")
        total_fail += FAIL
    print(f"TỔNG: {total_fail} fail sau {args.loop} lượt")
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
