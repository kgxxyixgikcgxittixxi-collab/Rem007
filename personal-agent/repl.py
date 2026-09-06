import os, sys, time

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
}
T = 0.015


def _p(s, col="cy", end="\n"):
    print(C.get(col, "") + s + C["reset"], end=end, flush=True)


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


class Repl:
    def __init__(self, manager):
        self.manager = manager
        self.sid = sessions.new()
        self.agent = Agent(manager, Presets.build(), sid=self.sid)
        self.agent.askfn = self._ask
        self.presets = "build"

    def _ask(self, name, args):
        _p(f"→ tool '{name}' cần quyền", "ye")
        mini = str(args)[:120]
        _p(f"  {mini}", "dim")
        try:
            a = input("  Cho phép? [y/N] ").strip().lower()
        except Exception:
            return False
        return a in ("y", "yes", "ok", "cho", "phep", "1", "c")

    def _status(self):
        _p("Extensions (MCP)", "bold")
        print(self.manager.status())
        _p(f"\nTools ({len(self.manager.tool_names())})", "bold")
        print("  " + ", ".join(self.manager.tool_names()))
        _p(f"\nSession: {self.sid} | preset: {self.presets} | auto: {self.agent.perm.auto}", "dim")

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

    def slash(self, line):
        cmd = line.strip()
        parts = cmd.split()
        head = parts[0] if parts else ""
        if cmd == "/help":
            _p(
                "\n".join([
                    "/help    trợ giúp",
                    "/status  xem extension + tool + session",
                    "/sessions liệt kê session cũ",
                    "/models  xem model đang dùng (chat/compact)",
                    "/new     tạo session mới",
                    "/del <id>  xoá 1 session cũ",
                    "/plan    chuyển sang preset PLAN (chỉ đọc, cấm ghi/bash/web_fetch)",
                    "/build   quay lại preset BUILD (hỏi quyền với tool nguy hiểm)",
                    "/auto    chạy tự động — không hỏi xác nhận (mặc định)",
                    "/safe    hỏi xác nhận trước tool ghi/đổi thư mục/fetch web",
                    "/debug   bật/tắt chế độ gỡ lỗi (hiện nội dung đầy đủ, không gõ chữ)",
                    "/keys    xem số Groq keys",
                    "/key gsk_...  thêm Groq key",
                    "/exit    thoát",
                ]), "dim",
            )
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
            self.agent.perm.set_auto(on)
            _p("Chế độ TỰ ĐỘNG: không hỏi xác nhận." if on else "Chế độ AN TOÀN: hỏi xác nhận trước tool ghi/bash/fetch.", "gr")
        elif cmd == "/status":
            self._status()
        elif cmd == "/sessions":
            self._sessions()
        elif cmd == "/new":
            self.sid = sessions.new()
            self.agent = Agent(self.manager, Presets.build(), sid=self.sid)
            self.agent.askfn = self._ask
            self.presets = "build"
            _p(f"Session mới: {self.sid}", "gr")
        elif cmd == "/plan":
            self.agent.perm = Presets.plan()
            self.presets = "plan"
            _p("Đã chuyển preset PLAN — tool ghi/đổi thư mục/fetch web bị CẤM.", "ye")
        elif cmd == "/build" or cmd == "/agent":
            self.agent.perm = Presets.build()
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

    def run(self):
        _p(f"{config.NAME} v{config.VERSION} — MCP-native (goose/opencode style). Gõ /help.", "bold")
        _p("Đang khởi chạy extensions...", "dim")
        self.manager.start_all()
        self._status()
        while True:
            try:
                line = input("\nRem> ").strip()
            except (EOFError, KeyboardInterrupt):
                _p("\nTạm biệt!", "dim")
                break
            if not line:
                continue
            if line.startswith("/"):
                if self.slash(line) is False:
                    break
                continue
            try:
                _type(self.agent.run(line), "gr")
            except KeyboardInterrupt:
                _p("\n[dừng bởi người dùng — trạng thái lệnh có thể dang dở]", "ye")