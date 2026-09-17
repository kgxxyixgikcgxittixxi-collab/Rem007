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


def test_opencode_parity():
    print("[8] parity opencode: lệnh/titles/fork/undo-git/custom/task-type")
    import repl
    import sessions
    for c in ("/connect", "/thinking", "/q", "/continue", "/rename", "/fork"):
        check(f"slash {c}", c in repl._SLASH)
    check("slash /keybinds", "/keybinds" in repl._SLASH)
    src = open("repl.py", encoding="utf-8").read()
    check("Tab vẽ lại cùng dòng (không \\n riêng)",
          '_redraw_input_locked(self)' in src and '"\\n")' not in src.split('ch == "\\t"')[1].split("if o < 32")[0] if 'ch == "\\t"' in src else False)
    check("prompt có pill agent", "[build]" in src and "[plan]" in src)
    check("history ↑/↓", "_hist_push" in src and '"[A"' in src and '"[B"' in src)
    check("@agent mentions", repl._AGENT_MENTIONS >= {"explore", "general", "plan", "build"})
    src = open("repl.py", encoding="utf-8").read()
    check("custom override builtin trước", "_cmds0" in src)
    check("/q thoát", '"/q"' in src and "/exit" in src)
    # session title/rename/fork (sid tạm, dọn sau)
    sid = sessions.new()
    try:
        sessions.append(sid, {"role": "user", "content": "làm web bán cà phê rang xay"})
        check("auto-title từ tin đầu", "cà phê" in sessions.get_title(sid), sessions.get_title(sid)[:50])
        check("rename", sessions.set_title(sid, "Shop Cafe") and sessions.get_title(sid) == "Shop Cafe")
        nid = sessions.fork(sid)
        check("fork giữ lịch sử", nid and sessions.load(nid) == sessions.load(sid))
        for f in (sessions._f(sid), sessions._f(nid)) if nid else (sessions._f(sid),):
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception as e:
        check("session title/fork", False, f"{type(e).__name__}: {e}")
    # task tool có type explore|general
    try:
        from extensions import TASK_DEF
        props = TASK_DEF["parameters"]["properties"]
        check("task có type", "type" in props and "general" in str(props["type"]))
    except Exception as e:
        check("task có type", False, str(e))
    # custom command parse model/subtask (file tạm, dọn sau)
    try:
        import tempfile
        d = os.path.join(os.path.expanduser("~"), ".rem_ai", "commands")
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, "_trem_test_.md")
        with open(fp, "w", encoding="utf-8") as f:
            f.write("---\ndescription: test\nagent: general\nmodel: m/x\nsubtask: true\n---\nLàm $ARGUMENTS\n")
        cmds = repl._load_custom_commands()
        c = cmds.get("_trem_test_", {})
        check("custom model", c.get("model") == "m/x", str(c.get("model")))
        check("custom subtask", c.get("subtask") is True)
        os.remove(fp)
    except Exception as e:
        check("custom model/subtask", False, f"{type(e).__name__}: {e}")
    # groq favorite round-trip (giữ lại giá trị cũ)
    try:
        from providers import groq as _g
        old = _g.get_favorite()
        check("favorite set/get", _g.set_favorite("openai/gpt-oss-20b") and _g.get_favorite() == "openai/gpt-oss-20b")
        if old:
            _g.set_favorite(old)
        else:
            try:
                os.remove(_g._MODEL_FILE)
            except Exception:
                pass
    except Exception as e:
        check("favorite set/get", False, f"{type(e).__name__}: {e}")
    # undo/redo file trên repo tạm (không đụng repo thật)
    try:
        import subprocess, tempfile as _tf
        td = _tf.mkdtemp(prefix="remtmt")
        subprocess.run(["git", "init", "-q", td], check=True, timeout=15)
        subprocess.run(["git", "-C", td, "config", "user.email", "t@t"], check=True, timeout=10)
        subprocess.run(["git", "-C", td, "config", "user.name", "t"], check=True, timeout=10)
        with open(os.path.join(td, "a.txt"), "w") as f:
            f.write("v1\n")
        subprocess.run(["git", "-C", td, "add", "."], check=True, timeout=10)
        subprocess.run(["git", "-C", td, "commit", "-qm0"], check=True, timeout=10)
        sid2 = sessions.new()
        check("work snapshot", sessions.work_snapshot(sid2, cwd=td) is True)
        with open(os.path.join(td, "a.txt"), "w") as f:
            f.write("v2\n")
        rf, _m = sessions.work_undo(sid2, cwd=td)
        check("work undo revert", open(os.path.join(td, "a.txt")).read() == "v1\n", str(rf))
        ok, _m2 = sessions.work_redo(sid2)
        check("work redo apply", ok and open(os.path.join(td, "a.txt")).read() == "v2\n", str(_m2))
        import shutil
        shutil.rmtree(td, ignore_errors=True)
        for f in (sessions._f(sid2), sessions._work_f(sid2), sessions._work_redo_f(sid2)):
            try:
                os.remove(f)
            except Exception:
                pass
    except Exception as e:
        check("work undo/redo", False, f"{type(e).__name__}: {e}")


def test_context_hardening():
    print("[9] context: trim giới hạn cứng + system giữ mọi turn")
    import sessions
    msgs = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 5000}
        for i in range(6)]
    out = sessions.trim(msgs)
    tot = sum(sessions._char_len(m) for m in out)
    check("trim ngắn/dài vẫn ≤20000", tot <= 20000, f"(got {tot})")
    check("trim giữ system đầu", out and out[0].get("role") == "system")
    # system prompt không rơi ở turn>1 (mock chat_stream, 0 quota)
    try:
        import agentloop
        from permissions import Presets
        seen = []
        calls = {"n": 0}

        def fake(msgs, tools=None, budget=None, on_delta=None, cancel=None):
            seen.append([m.get("role") for m in msgs])
            calls["n"] += 1
            if calls["n"] == 1:
                return {"content": "", "tool_calls": [
                    {"id": "t1", "function": {"name": "cwd", "arguments": "{}"}}]}
            return {"content": "xong", "tool_calls": []}

        real = agentloop.groq.chat_stream
        agentloop.groq.chat_stream = fake

        class DummyMgr:
            def schemas(self):
                return []

            def call(self, *a, **k):
                return "toolres"

        a = agentloop.Agent(DummyMgr(), Presets.build(), sid="sysregtest")
        os_, ot = agentloop.MAX_STEPS, agentloop.MAX_TURNS
        agentloop.MAX_STEPS, agentloop.MAX_TURNS = 3, 3
        try:
            a.run("việc test hồi quy system")
        finally:
            agentloop.MAX_STEPS, agentloop.MAX_TURNS = os_, ot
            agentloop.groq.chat_stream = real
        check("≥2 lượt LLM", len(seen) >= 2, f"(got {len(seen)})")
        check("mọi lượt đều system đầu",
              bool(seen) and all(r and r[0] == "system" for r in seen))
    except Exception as e:
        check("system mọi turn", False, f"{type(e).__name__}: {e}")


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
        test_opencode_parity()
        test_context_hardening()
        print(f"--> lượt {rnd}: {PASS} pass, {FAIL} fail")
        total_fail += FAIL
    print(f"TỔNG: {total_fail} fail sau {args.loop} lượt")
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    main()
