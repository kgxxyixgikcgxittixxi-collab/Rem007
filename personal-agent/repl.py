import os, sys, time, threading, queue

import config
import sessions
from agentloop import Agent
from extensions import Manager
from permissions import PermPolicy, Presets
from providers import groq

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "cy": "\033[96m", "gr": "\033[92m", "ye": "\033[93m",
    "rd": "\033[91m", "mg": "\033[95m", "bl": "\033[94m",
    "clear": "\033[2J", "home": "\033[H",
}
T = 0.015
CLEAR_SEQ = C["clear"] + C["home"]
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇"


def _p(s, col="cy", end="\n"):
    print(C.get(col, "") + str(s) + C["reset"], end=end, flush=True)


def _type(s, col="gr"):
    if not sys.stdin.isatty() or config.DEBUG:
        _p(s, col)
        return
    try:
        for ch in s:
            print(ch, end="", flush=True)
            sys.stdout.write("\033[?25l")
            time.sleep(T)
    except Exception:
        _p(s, col)
    finally:
        print("\033[?25h" + C["reset"])
    print()


def _klines(logo, colors):
    out = []
    for i, ln in enumerate(logo.strip("\n").split("\n")):
        col = colors[i % len(colors)]
        out.append(C.get(col, "") + ln + C["reset"])
    return "\n".join(out)


def _logo_banner():
    logo = _klines(config.LOGO, ["rd", "ye", "gr", "cy", "mg", "bl"])
    meta = (C["bold"] + C["bl"] + config.NAME + C["reset"] + "  v" + config.VERSION +
            C["dim"] + "  ·  MCP-native  ·  Groq  ·  opencode-style" + C["reset"])
    line = C["reset"] + C["dim"] + ("─" * 48) + C["reset"]
    m = "?"
    c = "?"
    try:
        cm = groq.chat_models()
        m = cm[0] if cm else "?"
        cc = groq.clone_models()
        c = cc[0] if cc else "?"
    except Exception:
        pass
    return ("\n" + logo + "\n" + line + "\n" + meta + "\n" + line + "\n" +
            C["dim"] + "  Model: " + C["reset"] + C["gr"] + m + C["reset"] +
            C["dim"] + "   Compact: " + C["reset"] + C["gr"] + c + C["reset"] + "\n")


class Repl:
    def __init__(self, manager):
        self.manager = manager
        self.sid = sessions.new()
        self.presets = "build"
        self.q = queue.Queue()
        self._busy = False
        self._pending = 0
        self._status_msg = ""
        self._spin_on = False
        self._mk_agent()

    def _mk_agent(self, sid=None):
        self._agent = Agent(self.manager, Presets.build(), sid=sid or self.sid,
                            on_event=self._on_ev)
        self._agent.askfn = self._ask

    # ── sự kiện từ agent (chạy trong worker thread) ──
    def _on_ev(self, ev):
        t = ev.get("type")
        if t == "thinking":
            self._status_msg = f"bước {ev['step']}: đang suy luận…"
        elif t == "llm":
            self._status_msg = f"bước {ev['step']}: gọi LLM…"
        elif t == "retry":
            self._status_msg = "call lại (rate-limit)…"
        elif t == "tool_start":
            self._status_msg = f"⚡ {ev['name']} {ev.get('args_note', '')}"
        elif t == "tool_done":
            r = (ev.get("result") or "").replace("\n", " ")[:100]
            self._status_msg = f"✓ {ev['name']} → {r}"

    def _spinner(self):
        i = 0
        while self._spin_on:
            f = SPIN[i % len(SPIN)]
            msg = f"{f}  {self._status_msg}" if self._status_msg else f
            sys.stdout.write("\r\033[36m" + msg[:160] + "\033[0m\033[K")
            sys.stdout.flush()
            time.sleep(0.09)
            i += 1
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def _clear_spin_line(self):
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    # ── worker: xử lý câu hỏi theo hàng đợi, cho phép soạn câu mới chờ lượt ──
    def _worker(self):
        while True:
            kind, payload = self.q.get()
            if kind == "quit":
                return
            if kind == "stop":
                try:
                    self._agent.stop()
                except Exception:
                    pass
                continue
            if kind == "task":
                self._busy = True
                self._spin_on = True
                threading.Thread(target=self._spinner, daemon=True).start()
                try:
                    out = self._agent.run(payload)
                except Exception as e:
                    out = f"[LỖI] {type(e).__name__}: {e}"
                self._spin_on = False
                self._busy = False
                self._clear_spin_line()
                if out:
                    _type(out, "gr")
                self._pending = 0

    # ── câu hỏi quyền (safe mode): chạy trong worker, hỏi trực tiếp ──
    def _ask(self, name, args):
        self._clear_spin_line()
        _p(f"→ tool '{name}' cần quyền", "ye")
        mini = str(args)[:120]
        _p(f"  {mini}", "dim")
        try:
            a = input("  Cho phép? [y/N] ").strip().lower()
        except Exception:
            return False
        return a in ("y", "yes", "ok", "cho", "phep", "1", "c")

    def _status(self):
        print(CLEAR_SEQ)
        try:
            print(_logo_banner())
        except Exception:
            pass
        print()
        _p("Extensions (MCP)", "mg")
        print(self.manager.status())
        _p(f"\nTools ({len(self.manager.tool_names())})", "bold")
        print("  " + ", ".join(self.manager.tool_names()))
        st = f"\nSession: {self.sid} | preset: {self.presets} | auto: {self.agent_auto()}"
        if self._busy or self._pending:
            st += f" | 🌀 đang chạy | {self._pending} câu đang chờ"
        _p(st, "dim")

    def agent_auto(self):
        try:
            return self._agent.perm.auto
        except Exception:
            return "?"

    def _sessions(self):
        rows = sessions.list_all()
        if not rows:
            _p("(chưa có session nào)", "dim")
            return
        for sid, t, first in rows:
            mark = "*" if sid == self.sid else " "
            _p(f"{mark} {sid}  ({t})  {first}", "cy")

    def _keys(self):
        ks = groq.keys()
        _p(f"Có {len(ks)} Groq keys trong DB", "bold")
        for k in ks:
            print("  " + k[:14] + "..." + k[-6:])
        _p("Thêm key: /key gsk_...", "dim")

    def _clear(self):
        self._clear_spin_line()
        print(CLEAR_SEQ)

    def slash(self, line):
        cmd = line.strip()
        parts = cmd.split()
        if cmd == "/help":
            _p(
                "\n".join([
                    "/help    trợ giúp",
                    "/status  xem extension + tool + session",
                    "/sessions liệt kê session cũ",
                    "/models  xem model đang dùng (chat/compact)",
                    "/new     tạo session mới",
                    "/del <id>  xoá 1 session cũ",
                    "/plan    chuyển preset PLAN (chỉ đọc)",
                    "/build   quay lại preset BUILD",
                    "/auto    tự động — không hỏi (mặc định)",
                    "/safe    hỏi xác nhận trước tool ghi/bash/fetch",
                    "/stop    dừng agent đang xử lý (giữ session)",
                    "/debug   bật/tắt chế độ gỡ lỗi",
                    "/clear   xoá màn hình (hiện logo REM)",
                    "/keys    xem số Groq keys",
                    "/key gsk_...  thêm Groq key",
                    "/exit    thoát",
                ]), "dim",
            )
        elif cmd == "/clear":
            self._clear()
        elif cmd == "/stop":
            if self._busy:
                self._agent.stop()
                _p("⏹  Đang dừng agent…", "ye")
            else:
                _p("Agent đang rảnh.", "dim")
        elif cmd == "/models":
            _p(f"Chat  : {', '.join(groq.chat_models()[:4]) or '(chưa có keys)'}", "cy")
            _p(f"Compact: {', '.join(groq.clone_models()[:2]) or '(chưa có keys)'}", "cy")
        elif cmd == "/debug":
            config.DEBUG = not config.DEBUG
            _p(f"Chế độ gỡ lỗi: {'BẬT' if config.DEBUG else 'TẮT'}", "gr")
        elif cmd == "/del":
            if len(parts) < 2:
                _p("Cú pháp: /del <session-id>  (xem /sessions)", "dim")
            elif sessions.remove(parts[1]):
                _p(f"Đã xoá session {parts[1]}", "gr")
            else:
                _p(f"Không xoá được session {parts[1]}", "rd")
        elif cmd in ("/auto", "/safe"):
            on = cmd == "/auto"
            self._agent.perm.set_auto(on)
            _p("Chế độ TỰ ĐỘNG: không hỏi xác nhận." if on else "Chế độ AN TOÀN: hỏi xác nhận trước tool ghi/bash/fetch.", "gr")
        elif cmd == "/status":
            self._status()
        elif cmd == "/sessions":
            self._sessions()
        elif cmd == "/new":
            self.sid = sessions.new()
            self._mk_agent(sid=self.sid)
            self.presets = "build"
            _p(f"Session mới: {self.sid}", "gr")
        elif cmd == "/plan":
            self._agent.perm = Presets.plan()
            self.presets = "plan"
            _p("Đã chuyển preset PLAN — tool ghi/đổi thư mục/fetch web bị CẤM.", "ye")
        elif cmd == "/build" or cmd == "/agent":
            self._agent.perm = Presets.build()
            self.presets = "build"
            _p("Đã quay lại preset BUILD.", "gr")
        elif cmd == "/keys":
            self._keys()
        elif cmd.startswith("/key "):
            k = cmd[5:].strip()
            if groq.add_key(k):
                _p(f"Đã thêm key {k[:10]}... ({len(groq.keys())} keys tổng)", "gr")
            else:
                _p("Thêm key thất bại.", "rd")
        elif cmd == "/exit" or cmd == "/quit":
            return False
        else:
            _p("Không rõ lệnh. Gõ /help.", "dim")
        return True

    def _prompt_hint(self):
        if self._busy:
            return "⏳(câu mới sẽ chờ lượt) Rem> "
        if self._pending:
            return f"⏳({self._pending} chờ) Rem> "
        return "Rem> "

    def run(self):
        self._clear()
        try:
            print(_logo_banner())
        except Exception:
            pass
        _p(f"Gõ /help | /status | /stop | /clear | /exit", "dim")
        if not groq.keys():
            _p(f"⚠  CHƯA CÓ GROQ KEY — gõ: /key gsk_...  để thêm", "rd")
        _p(f"Đang khởi chạy extensions...", "dim")
        self.manager.start_all()
        threading.Thread(target=self._worker, daemon=True).start()
        while True:
            try:
                line = input(self._prompt_hint()).strip()
            except EOFError:
                _p("\nTạm biệt!", "dim")
                self.q.put(("quit", None))
                break
            except KeyboardInterrupt:
                if self._busy:
                    _p("\n⏹  Đang dừng agent…", "ye")
                    self.q.put(("stop", None))
                    continue
                _p("\nTạm biệt!", "dim")
                self.q.put(("quit", None))
                break
            if not line:
                continue
            if line.startswith("/"):
                if self.slash(line) is False:
                    self.q.put(("quit", None))
                    break
                continue
            if self._busy:
                self._pending += 1
                _p(f"⏳ Câu hỏi đã xếp hàng (#{self._pending}). Agent sẽ trả lời sau lượt hiện tại — gõ /stop để dừng.",
                   "ye")
            self.q.put(("task", line))