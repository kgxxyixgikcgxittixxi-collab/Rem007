import subprocess, os, sys, time, threading, queue

import config
import sessions
import updater
from agentloop import Agent
from extensions import Manager
from permissions import PermPolicy, Presets
from providers import groq

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "cy": "\033[96m", "gr": "\033[92m", "ye": "\033[93m",
    "rd": "\033[91m", "mg": "\033[95m", "bl": "\033[94m",
    "lm": "\033[92m",  # xanh lá chuối (lime) — dòng người dùng User>
    "ob": "\033[34m",  # xanh biển đậm — dòng agent Rem>
    "clear": "\033[2J", "home": "\033[H",
}
# Prefix phân biệt rõ người dùng vs agent
P_USER = "\033[92mUser>\033[0m "      # xanh lá chuối (chỉ tiền tố, nội dung để trắng)
P_AGENT = "\033[34mRem>\033[0m "       # xanh biển
T = 0.015
CLEAR_SEQ = C["clear"] + C["home"]
# Bộ khung spinner của opencode (packages/tui/src/component/spinner.tsx)
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Nhãn tool theo phong cách opencode (InlineTool/ToolStatusTitle)
_TOOL_LABEL = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Edit",
    "bash": "Bash", "list_dir": "List", "glob_files": "Glob",
    "grep": "Grep", "web_search": "WebSearch", "web_fetch": "WebFetch",
    "remember": "Remember", "recall": "Recall", "ensure_tool": "Setup",
    "pip_install": "PyPI", "github_api": "GitHub", "task": "Task",
    "log": "Log", "kill": "Kill",
}


def _p(s, col="cy", end="\n"):
    print(C.get(col, "") + str(s) + C["reset"], end=end, flush=True)


def _type(s, col="gr"):
    # In theo từ (word) thay vì từng ký tự để KHÔNG làm vỡ chữ tiếng Việt nhiều byte
    if not sys.stdin.isatty() or config.DEBUG:
        _p(s, col)
        return
    try:
        import re as _re
        color_code = C.get(col, "")
        parts = _re.split(r"(\s+)", s)
        sys.stdout.write(color_code)
        for part in parts:
            if not part:
                continue
            # in cả nhóm ký tự liền mạch 1 lần (bảo toàn Unicode)
            print(part, end="", flush=True)
            sys.stdout.write("\033[?25l")
            time.sleep(T if part.startswith((" ", "\n")) else T * 0.5)
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


def _tool_title(ev):
    """Tiêu đề tool kiểu opencode: 'Bash  —  $ ls -la'."""
    name = ev.get("name", "?") if isinstance(ev, dict) else "?"
    note = ""
    args = ev.get("args") or {}
    if isinstance(args, dict):
        for k in ("command", "path", "query", "url", "pattern", "filename", "name", "file", "dir"):
            v = args.get(k)
            if v not in (None, ""):
                note = str(v)
                break
    label = _TOOL_LABEL.get(name, name)
    if name == "bash" and note:
        note = "$ " + note
    note = (note or "")[:72]
    return f"{label}  —  {note}" if note else label


class Repl:
    def __init__(self, manager):
        self.manager = manager
        self.sid = sessions.new()
        self.presets = "build"
        self.q = queue.Queue()
        self._busy = False
        self._pending = 0
        self._status_msg = ""
        self._status_since = 0.0
        self._spin_start = 0.0
        self._spin_on = False
        self._tool_rows = []      # các dòng tool đã xong (giống timeline opencode)
        self._cur_title = ""
        self._mk_agent()

    def _mk_agent(self, sid=None):
        self._agent = Agent(self.manager, Presets.build(), sid=sid or self.sid,
                            on_event=self._on_ev)
        self._agent.askfn = self._ask

    # ── sự kiện từ agent (chạy trong worker thread) ──
    # Trình bày theo kiểu timeline opencode: mỗi tool = 1 dòng "✓ Bash — $ ls -la"
    def _set_status(self, msg):
        self._status_msg = msg
        self._status_since = time.time()

    def _on_ev(self, ev):
        t = ev.get("type")
        if t == "tool_start":
            self._cur_title = _tool_title(ev)
            self._set_status(self._cur_title)
        elif t == "tool_done":
            r = (ev.get("result") or "")
            ok = not r.startswith(("[LOI]", "[TOOL LOI]", "[TU CHOI]"))
            self._tool_rows.append(((("✓ " if ok else "✗ ") + self._cur_title), not ok))
            if len(self._tool_rows) > 14:      # giữ tối đa, không spam màn hình
                self._tool_rows.pop(0)
        elif t in ("thinking", "llm"):
            self._set_status("đang suy nghĩ")
        elif t == "retry":
            self._set_status("mạng bận, đang thử lại")
        elif t == "turn":
            self._set_status("tiếp tục xử lý…")
        elif t == "turn_roll":
            self._set_status("đang chuyển lượt…")
        elif t == "checkpoint":
            self._set_status("đang lưu tiến độ…")
        # bỏ qua tool_done để không in "✓ xyz done" chồng lên — chỉ lưu vào _tool_rows

    def _spinner(self):
        i = 0
        self._spin_start = time.time()
        if self._status_since <= 0:
            self._status_since = time.time()
        while self._spin_on:
            f = SPIN[i % len(SPIN)]
            el = int(time.time() - self._spin_start)
            wait = time.time() - self._status_since
            base = f"  {self._status_msg}" if self._status_msg else ""
            if el >= 60:
                base += f"  [{el // 60}p{el % 60:02d}s]"
            else:
                base += f"  [{el}s]"
            if wait >= config.TOOL_SLOW_WARN:
                base += "  ⏳ lâu quá — /stop nếu kẹt"
                col = "\033[91m"
            else:
                col = "\033[36m"
            # kiểu opencode: frame spinner màu + nội dung mờ
            sys.stdout.write("\r" + col + f + C["dim"] + base[:150] + C["reset"] + "\033[K")
            sys.stdout.flush()
            time.sleep(0.08)
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
                # dọn các câu đang chờ — tránh chạy nối tiếp vô nghĩa sau khi dừng
                drained = 0
                while True:
                    try:
                        k, _ = self.q.get_nowait()
                    except queue.Empty:
                        break
                    if k == "quit":
                        self.q.put(("quit", None))
                        break
                    if k == "task":
                        drained += 1
                self._pending = 0
                if drained:
                    _p(f"Đã bỏ {drained} câu đang chờ.", "dim")
                continue
            if kind == "task":
                self._busy = True
                self._spin_on = True
                self._set_status("đang bắt đầu")
                self._tool_rows = []
                self._cur_title = ""
                spin = threading.Thread(target=self._spinner, daemon=True)
                spin.start()
                try:
                    out = self._agent.run(payload)
                except Exception as e:
                    out = f"[LỖI] {type(e).__name__}: {e}"
                self._spin_on = False
                self._busy = False
                spin.join(timeout=1)      # đợi spinner bỏ dòng cuối xong
                self._clear_spin_line()   # rồi mới in tránh bị đè "Rem>"
                # timeline tool đã xong (giống opencode: "✓ Bash — $ cmd")
                for row, is_err in self._tool_rows:
                    _p("  " + row, "rd" if is_err else "dim")
                if out:
                    # Tiền tố Rem> màu xanh biển + nội dung trả lời của AGENT
                    sys.stdout.write(P_AGENT)
                    sys.stdout.flush()
                    _type(out, "ob")
                    # Đánh dấu rõ: AI ĐÃ TRẢ LỜI XONG → hiện ngay "Rem>" để người dùng biết
                    print(C["ob"] + "Rem>" + C["reset"] + C["dim"] + "  (đã trả lời xong)" + C["reset"], flush=True)
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
                    "/rest N  hẹn máy TỰ NGỦ sau N phút (mặc định 60) — rem-rest",
                    "/debug   bật/tắt chế độ gỡ lỗi",
                    "/clear   xoá màn hình (hiện logo REM)",
                    "/checkupdate  kiểm tra bản mới trên GitHub",
                    "/update  tự cập nhật bản mới nhất (git/tarball)",
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
        elif cmd == "/rest" or cmd.startswith("/rest "):
            try:
                r = subprocess.run(["rem-rest"] + parts[1:],
                                   capture_output=True, text=True, timeout=30)
            except FileNotFoundError:
                _p("Thiếu lệnh 'rem-rest'. Cài lại: bash install.sh", "rd")
                return True
            except Exception as e:
                _p(f"Lỗi rem-rest: {type(e).__name__}: {e}", "rd")
                return True
            out = (r.stdout or "").strip()
            if r.stderr and r.stderr.strip():
                out += ("\n" if out else "") + r.stderr.strip()
            _p("\n".join(f"  {ln}" for ln in out.splitlines()) or "(không có phản hồi)", "gr" if r.returncode == 0 else "ye")
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
        elif cmd == "/checkupdate":
            rv = updater.remote_version()
            if not rv:
                _p("Không lấy được bản mới từ GitHub (kiểm tra mạng).", "rd")
            elif updater._ver_tuple(rv) > updater._ver_tuple(config.VERSION):
                _p(f"Có bản mới: v{config.VERSION} → v{rv}. Gõ /update để cập nhật.", "ye")
            else:
                _p(f"Đã ở bản mới nhất: v{config.VERSION}.", "gr")
        elif cmd == "/update":
            if self._busy:
                _p("Agent đang bận — đợi hết lượt hiện tại rồi gõ /update.", "ye")
                return True
            self._clear_spin_line()
            ok, newv, lines = updater.update()
            for ln in lines:
                _p(ln, "gr" if ok else "rd")
            if ok:
                _p("Khởi động lại để dùng bản mới: /exit rồi gõ lại Remtm.", "dim")
            else:
                _p("Cập nhật không thành công (xem thông tin trên).", "rd")
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
        # Prompt là chỗ NGƯỜI DÙNG gõ → màu xanh lá chuối, tiền tố User>
        if self._busy:
            return "⏳(câu mới sẽ chờ lượt) " + P_USER
        if self._pending:
            return f"⏳({self._pending} chờ) " + P_USER
        return P_USER

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
            # Tô lại dòng người gõ: CHỈ tiền tố "User>" màu xanh lá, nội dung để trắng (giống Rem>)
            sys.stdout.write("\033[1A\r\033[2K")
            sys.stdout.write(C["lm"] + "User> " + C["reset"] + line + "\n")
            sys.stdout.flush()
            if self._busy:
                self._pending += 1
                _p(f"⏳ Câu hỏi đã xếp hàng (#{self._pending}). Agent sẽ trả lời sau lượt hiện tại — gõ /stop để dừng.",
                   "ye")
            self.q.put(("task", line))