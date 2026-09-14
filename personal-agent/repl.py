import subprocess, os, sys, time, threading, queue, re, json

import config
import sessions
import updater
import render
from mcplib import atomic_write_json
from agentloop import Agent
from permissions import Presets
from providers import groq

_OUT_LOCK = threading.Lock()
try:
    import readline
    _HAS_READLINE = True
except Exception:
    _HAS_READLINE = False


def _redisplay_input():
    try:
        if _HAS_READLINE and sys.stdin.isatty():
            readline.redisplay()
    except Exception:
        pass


def _is_inject_noise(text):
    """Lọc dòng rác terminal paste nhầm (prompt/logo/status echo) — không đẩy cho agent."""
    import re as _re
    t = (text or "").strip()
    if not t or len(t) <= 1:
        return True
    if any(k in t for k in ("╭─❯", "╰─❯", "📥 Đã chuyển", "⏳ ❯", "⏳(",
                            "Gõ /help", "gõ câu hỏi", "/list danh mục")):
        return True
    # mảnh logo ASCII / box-drawing còn sót (có thể lẫn < > . " = # * + :)
    s = t.replace("User>", "").replace("❯", "").strip()
    if s and _re.fullmatch(r"[|_\\/()\-─│╭╰/box<>.\"'=:#+* ]+", s):
        return True
    if _re.fullmatch(r"[|_\\/ ]{4,}", s or ""):
        return True
    return False


OVERLAY_FILE = os.path.join(config.DIR, "overlay.json")
OVERLAY_PID = os.path.join(config.DIR, "overlay.pid")
REPLAY_TOOLS = {"skill_use", "rec_play", "skill_find", "rec_list", "rec_show", "skill_list"}
OVERLAY_GROUPS = {"desktop", "web", "mang-xa-hoi", "media"}


def _overlay_write(mode="", task=None, tool=None, progress=None):
    """Ghi trạng thái lên cửa sổ nổi (best-effort, không bao giờ crash agent).
    Read-modify-write dưới lock + ghi atomic (tmp+replace) để overlay đọc
    đồng thời không bao giờ thấy file rách."""
    try:
        with _OUT_LOCK:
            d = {}
            try:
                with open(OVERLAY_FILE, "r", encoding="utf-8") as f:
                    old = json.load(f)
                if isinstance(old, dict):
                    d = old
            except Exception:
                d = {}
            if mode:
                d["mode"] = mode
            if task is not None:
                d["task"] = (task or "")[:220]
            if tool is not None:
                d["tool"] = (tool or "")[:160]
            if progress is not None:
                d["progress"] = (progress or "")[:120]
            d["updated"] = time.time()
            atomic_write_json(OVERLAY_FILE, d)
    except Exception:
        pass


def _overlay_running():
    try:
        with open(OVERLAY_PID, "r", encoding="utf-8") as f:
            pid = int((f.read() or "").strip() or 0)
        if pid > 0:
            os.kill(pid, 0)
            return True
    except Exception:
        pass
    return False


def _overlay_ensure():
    """Tự mở cửa sổ nổi nếu có màn hình và chưa chạy. Trả True nếu đang hiện."""
    try:
        if not os.environ.get("DISPLAY"):
            return False
        if _overlay_running():
            return True
        root = os.path.dirname(os.path.abspath(__file__))
        subprocess.Popen([sys.executable, os.path.join(root, "overlay.py")],
                         cwd=root, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def _overlay_stop():
    try:
        with open(OVERLAY_PID, "r", encoding="utf-8") as f:
            pid = int((f.read() or "").strip() or 0)
        if pid > 0:
            os.kill(pid, 15)
    except Exception:
        pass
    try:
        # Ngoặc [p] để pkill không tự match chính lệnh này (self-match footgun)
        subprocess.run(["pkill", "-f", "[p]ersonal-agent/overlay.py"],
                       timeout=5, capture_output=True)
    except Exception:
        pass
    _overlay_write(mode="RẢNH", task="", tool="", progress="đã tắt cửa sổ nổi")

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "cy": "\033[96m", "gr": "\033[92m", "ye": "\033[93m",
    "rd": "\033[91m", "mg": "\033[95m", "bl": "\033[94m",
    "lm": "\033[92m",  # xanh lá chuối (lime) — dòng người dùng User>
    "ob": "\033[34m",  # xanh biển đậm — tiền tố Rem>
    "wh": "\033[37m",  # trắng — nội dung trả lời của agent
    "clear": "\033[2J", "home": "\033[H",
}
# Prefix phân biệt rõ người dùng vs agent
P_USER = "\033[92mUser>\033[0m "      # xanh lá chuối (chỉ tiền tố, nội dung để trắng)
P_AGENT = "\033[1m\033[96m❯\033[0m "       # kiểu opencode: dấu ❯ nổi bật
T = 0.015
CLEAR_SEQ = C["clear"] + C["home"]

# ── Theme engine (đồng bộ dict C[] với config.THEMES[config.REPL_THEME]) ──
_THEMES_PATH = os.path.join(config.DIR, "rem_theme")
def _apply_theme_c():
    theme = getattr(config, "REPL_THEME", "default") or "default"
    C.update(config.THEMES.get(theme, config.THEMES.get("default", {})))
    try:
        with open(_THEMES_PATH, "w", encoding="utf-8") as f:
            f.write(theme)
    except Exception:
        pass
def _init_history():
    """Tải history gần nhất từ file (giữ ≤150 dòng) để Tab/Up recall."""
    hist = getattr(_init_history, "_hist", [])
    try:
        with open(os.path.join(config.DIR, "input_history"), "r", encoding="utf-8") as f:
            hist[:] = [ln.rstrip("\n") for ln in f if ln.strip()][-150:]
    except Exception:
        pass
    _init_history._hist = hist
    return hist
def _save_history(line):
    hist = getattr(_init_history, "_hist", [])
    hist.append(line)
    try:
        with open(os.path.join(config.DIR, "input_history"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

try:
    _apply_theme_c()
    _init_history()
except Exception:
    pass
# Số lần TỰ ĐỘNG chạy tiếp tối đa khi 1 lượt bị cắt giữa chừng (hết giờ/quota/bước)
# Cao để chạy 24/7: mỗi lượt được quyền tối đa MAX_TASK_SECONDS, tổng lên tới ~40 phút/task.
AUTO_RESUME_MAX = 8
# Bộ khung spinner của opencode (packages/tui/src/component/spinner.tsx)
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Nhãn tool theo phong cách opencode (InlineTool/ToolStatusTitle)
_TOOL_LABEL = {
    "read_file": "Read", "write_file": "Write", "edit_file": "Edit",
    "apply_patch": "Patch", "bash": "Bash", "bash_poll": "Job", "list_dir": "List",
    "glob_files": "Glob", "grep": "Grep", "web_search": "WebSearch",
    "web_fetch": "WebFetch", "web_images": "WebImg", "web_download_image": "DlImg",
    "web_download_images": "DlImgs", "remember": "Remember", "recall": "Recall",
    "ensure_tool": "Setup", "pip_install": "PyPI", "make_pdf": "PDF", "github_api": "GitHub",
    "todo_list": "Todo", "todo_write": "Todo", "task": "Task",
    "log": "Log", "kill": "Kill",
    "browser_open": "Browser", "browser_navigate": "Browser", "browser_click": "Click",
    "browser_click_text": "ClickText", "browser_type": "Type", "browser_press": "Key",
    "browser_screenshot": "Shot", "browser_content": "Page", "browser_eval": "JS",
    "browser_wait": "Wait", "browser_scroll": "Scroll", "browser_search": "BSearch",
    "browser_back": "Back", "browser_close": "Close",
    "media_tts": "TTS", "media_image": "ImgGen", "media_scene": "Scene",
    "media_slideshow": "Shorts", "media_concat": "Concat", "media_info": "Probe",
    "media_trim": "Trim", "media_scale": "Scale", "media_to_gif": "GIF",
    "media_overlay_text": "Overlay", "media_extract_audio": "Audio",
    "skill_save": "SkillSave", "skill_find": "Skill", "skill_use": "SkillUse",
    "skill_list": "Skills", "exp_lesson_save": "Lesson", "exp_lesson_find": "Lessons",
    "exp_error_patterns": "ErrPat",     "exp_skill_save": "ExpSkill", "exp_skill_best": "BestSkill",
    "social_cycle": "Social", "social_report": "SocialRep", "social_status": "SocialSt",
    "social_post": "SocialPost", "dl_find": "Find", "dl_text": "ReadScreen",
    "dl_status": "DeskStatus", "dl_apps": "Apps", "dl_tree": "DeskTree",
    "dl_click": "Click", "dl_type": "Type", "dl_key": "Key", "dl_mouse": "Mouse",
    "dl_clipboard": "Clip",
    "rec_start": "Rec", "rec_stop": "RecStop", "rec_list": "RecList",
    "rec_show": "RecShow", "rec_play": "RecPlay", "rec_delete": "RecDel",
    "browser_status": "BStatus", "cwd": "Cwd", "chdir": "Cd",
    "lsp_supported": "LSPSup", "lsp_diagnostics": "LSPDiag", "lsp_definition": "LSPDef",
    "lsp_references": "LSPRef", "lsp_symbols": "LSPSym", "lsp_hover": "LSPHover",
    "media_status": "MediaSt",
}


MACRO_CATS_FALLBACK = ("van-phong", "trinh-duyet", "he-thong", "giai-tri",
                       "mang-xa-hoi", "khac")

# Registry lệnh / để gợi ý khi gõ sai/gõ dở (kiểu autocomplete opencode).
# Tập trung 1 nơi: vừa làm dữ liệu autocomplete, vừa làm CATALOG cho /palette
# và /help nhóm theo mục — khỏi sửa 3 chỗ khi thêm lệnh mới.
# (name, category, desc, args_hint, aliases)
_SLASH_CAT = {
    "/help":         ("Cơ bản", "/help hiển thị danh sách lệnh này", "", ("/?",)),
    "/list":         ("Cơ bản", "Liệt kê macro/skill/chat cũ — gõ số để mở", "[số]", ("/ls",)),
    "/rec":          ("Điều khiển", "Ghi thao tác desktop thành macro", "", ()),
    "/play":         ("Điều khiển", "Phát lại macro đã ghi", "<số>", ()),
    "/resume":       ("Lịch sử", "Mở lại đoạn chat cũ", "<số>", ("/open",)),
    "/done":         ("Điều khiển", "Dừng ghi macro & lưu", "", ()),
    "/clear":        ("Cơ bản", "Xoá màn hình (hiện logo REM)", "", ()),
    "/stop":         ("Điều khiển", "Dừng agent đang xử lý (giữ session)", "", ()) ,
    "/rest":         ("Hệ thống", "Hẹn máy tự ngủ sau N phút", "N", ()),
    "/models":       ("Hệ thống", "Xem model đang dùng (chat/compact)", "", ()),
    "/debug":        ("Hệ thống", "Bật/tắt chế độ gỡ lỗi", "", ("/dbg",)),
    "/think":        ("Hiển thị", "Xem đầy đủ suy luận lần trả lời cuối", "", ("/rx",)),
    "/effort":       ("Cấu hình", "Đổi mức suy luận low|medium|high", "low|medium|high", ()),
    "/del":          ("Lịch sử", "Xoá 1 session cũ", "<id>", ()),
    "/auto":         ("Quyền hạn", "Tự động — không hỏi xác nhận", "", ()),
    "/safe":         ("Quyền hạn", "Hỏi xác nhận trước tool ghi/bash/fetch", "", ()),
    "/status":       ("Hiển thị", "Xem extension + tool + session", "", ()) ,
    "/stats":        ("Hiển thị", "Thống kê dùng: tin nhắn, tool, token", "", ()) ,
    "/todos":        ("Hiển thị", "Xem danh sách công việc (todo) agent đang theo dõi", "", ("/todo",)),
    "/compact":      ("Cấu hình", "Nén quy mô context thủ công (tóm tắt lịch sử cũ)", "", ()) ,
    "/model":        ("Cấu hình", "Chọn model chat cho phiên này", "<tên>", ("/m",)) ,
    "/sessions":     ("Lịch sử", "Liệt kê tất cả session cũ", "", ()),
    "/new":          ("Cơ bản", "Tạo session mới", "", ()),
    "/palette":      ("Cơ bản", "Bảng lệnh tìm nhanh (giống opencode command palette)", "", ()) ,
    "/theme":        ("Cấu hình", "Đổi bảng màu giao diện", "tên (nhập trống để xem)", ()),
    "/plan":         ("Quyền hạn", "Chuyển preset PLAN (chỉ đọc)", "", ()),
    "/build":        ("Quyền hạn", "Quay lại preset BUILD", "", ("/agent",)),
    "/lsp":          ("Phát triển", "Kiểm tra lỗi file nguồn (clangd/pylsp)", "<file>", ()) ,
    "/mcp":          ("Hệ thống", "Xem / nạp lại MCP server ngoài", "reload", ()),
    "/init":         ("Cơ bản", "Tạo AGENTS.md cho thư mục đang làm việc", "", ()),
    "/keys":         ("Cấu hình", "Xem số Groq keys", "", ()),
    "/key":          ("Cấu hình", "Thêm Groq key", "gsk_...", ()),
    "/checkupdate":  ("Hệ thống", "Kiểm tra bản mới trên GitHub", "", ()),
    "/update":       ("Hệ thống", "Tự cập nhật bản mới nhất", "", ()),
    "/overlay":      ("Cấu hình", "Cửa sổ nổi hiện việc đang làm", "on|off|status", ()),
    "/export":       ("Cơ bản", "Xuất đoạn chat hiện tại ra markdown", "", ()),
    "/exit":         ("Cơ bản", "Thoát", "", ("/quit",)),
}
_SLASH = [cmd for cmd in _SLASH_CAT]
# autocomplete: gõ dở khớp bất kỳ phần nào của lệnh (fuzzy kiểu palette)
def _palette_suggest(frag):
    frag = frag.lower().lstrip("/")
    if not frag:
        return []
    scored = []
    for cmd, (cat, desc, args, aliases) in _SLASH_CAT.items():
        keys = [cmd[1:].lower()] + [a[1:].lower() for a in aliases if a.startswith("/")]
        for k in keys:
            if frag in k:
                scored.append((len(k), cmd))
                break
            # fuzzy: ký tự fragment xuất hiện đúng thứ tự trong tên lệnh
            it = iter(k)
            if all(any(c == f for c in it) for f in frag):
                scored.append((len(k) + 0.5, cmd))
                break
    scored.sort(key=lambda x: (x[0], x[1]))
    return [c for _, c in scored[:8]]


def _p(s, col="cy", end="\n"):
    with _OUT_LOCK:
        sys.stdout.write("\r\033[K")
        sys.stdout.write(C.get(col, "") + str(s) + C["reset"] + end)
        sys.stdout.flush()
    _redisplay_input()


def _strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _type(s, col=None):
    """In theo nhóm chữ NHANH và RÕ: mã màu (ANSI) xuất tức thì không bao giờ bị tách,
    chữ Vietnamese giữ nguyên nhiều byte. Có sẵn màu từ render → col=None."""
    if not sys.stdin.isatty() or config.DEBUG:
        _p(_strip_ansi(s), col if col is not None else "")
        return
    text = s if col is None else (C.get(col, "") + s + C["reset"])
    fast = render.disp_len(text) > 1000
    sa = 0.0 if fast else T * 1.0    # pause sau khoảng trắng
    sw = 0.0 if fast else T * 0.45   # pause sau từ
    with _OUT_LOCK:
        sys.stdout.write("\033[?25l")
        try:
            for tok in render._tokens(text):
                if tok[0] == "esc":
                    sys.stdout.write(tok[1])
                elif tok[0] == "s":
                    sys.stdout.write(tok[1])
                    if sa:
                        sys.stdout.flush()
                        time.sleep(sa)
                else:
                    sys.stdout.write((tok[1] or "") + tok[2])
                    if sw:
                        sys.stdout.flush()
                        time.sleep(sw)
        except Exception:
            sys.stdout.write(_strip_ansi(text))
        finally:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
    _redisplay_input()


def _klines(logo, colors):
    out = []
    for i, ln in enumerate(logo.strip("\n").split("\n")):
        col = colors[i % len(colors)]
        out.append(C.get(col, "") + ln + C["reset"])
    return "\n".join(out)


def _logo_banner():
    logo = _klines(config.LOGO, ["rd", "ye", "gr", "cy", "mg", "bl"])
    return logo


def _tool_title(ev):
    """Tiêu đề tool kiểu opencode: 'Bash  —  $ ls -la'."""
    name = ev.get("name", "?") if isinstance(ev, dict) else "?"
    note = ""
    args = ev.get("args") or {}
    if isinstance(args, dict):
        if name == "apply_patch":
            note = "(patch)"
        elif name in ("todo_write", "todo_list"):
            note = args.get("session_id", "")
        else:
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


def _diff_preview(name, args):
    """Trích ngắn diff để người dùng duyệt trước khi cấp quyền (kiểu opencode)."""
    if not isinstance(args, dict):
        return ""
    if name == "apply_patch":
        pt = args.get("patch_text") or args.get("patch") or ""
        if isinstance(pt, str) and pt.strip():
            n = len(pt.strip().splitlines())
            lines = "\n".join(f"  {ln[:160]}" for ln in pt.strip().splitlines()[:40])
            return lines + (f"\n  … ({n} dòng)" if n > 40 else "")
    if name == "edit_file":
        old, new = args.get("old", ""), args.get("new", "")
        p = args.get("path") or args.get("file") or "<file>"
        if isinstance(old, str) and isinstance(new, str) and (old != new):
            ol = old.splitlines()
            nl = new.splitlines()
            op = "\n".join(f"  - {ln[:160]}" for ln in ol[:10])
            np_ = "\n".join(f"  + {ln[:160]}" for ln in nl[:10])
            return f"{p}\n{op}" + (f"\n  … (-{len(ol)-10})" if len(ol) > 10 else "") + \
                   f"\n{np_}" + (f"\n  … (+{len(nl)-10})" if len(nl) > 10 else "")
    if name == "write_file":
        p = args.get("path") or args.get("file") or "<file>"
        c = args.get("content") or ""
        if isinstance(c, str) and c.strip():
            n = len(c.splitlines())
            lines = "\n".join(f"  + {ln[:160]}" for ln in c.splitlines()[:15])
            return f"{p} (tạo/ghi)\n{lines}" + (f"\n  … (+{n-15})" if n > 15 else "")
    return ""


class Repl:
    def __init__(self, manager, headless=None, resume_sid=None):
        self.manager = manager
        self.headless = headless      # None = REPL tương tác; còn lại = chạy 1 task rồi thoát
        self.sid = resume_sid or sessions.new()   # resume_sid = tiếp tục phiên cũ (giữ context)
        self.resume_sid = resume_sid
        self.presets = "build"
        self.q = queue.Queue()
        self._busy = False
        self._pending = 0
        self._status_msg = ""
        self._status_since = 0.0
        self._spin_start = 0.0
        self._spin_on = False
        self._last_out = None
        self._last_turn_use = {}    # token lượt vừa xong (footer kiểu opencode)
        self._last_turn_secs = 0.0  # thời gian lượt vừa xong (giây)
        self._tool_rows = []      # các dòng tool đã xong (giống timeline opencode)
        self._cur_title = ""
        self._live_n = 0          # số ký tự model đang soạn (stream) — hiện tiến độ
        self._live_kind = ""      # "content" | "thinking"
        self._last_think = ""     # khối suy luận gần nhất (để /think xem đầy đủ)
        self._tool_t0 = 0.0       # mốc bắt đầu tool hiện tại (hiện số giây kiểu opencode)
        self.rec_mode = ""        # tên macro đang ghi (chế độ ghi từ /rec), "" = chat thường
        self._list_items = []     # [(kind,id,desc)] của lần /list gần nhất (chọn số để mở)
        self._in_input = False    # True khi main thread đang ở input() → spinner không animate đè
        self._last_esc = 0.0      # mốc ESC gần nhất (ESC đúp ≤0.8s = dừng cứng kiểu opencode)
        self._mk_agent()

    def _mk_agent(self, sid=None):
        self._agent = Agent(self.manager, Presets.build(), sid=sid or self.sid,
                            on_event=self._on_ev)
        self._agent.askfn = self._ask

    # ── dừng opencode-style: ESC đơn = mềm (wrap-up), ESC đúp = cứng (bỏ hết) ──
    def _request_stop(self, hard=False):
        """Luồng NGHE gọi trực tiếp (không qua hàng chờ) để dừng trong ≤0.5s.
        soft: cancel LLM (poll 0.2s) + interrupt MCP tool (0.5s), giữ wrap-up.
        hard (ESC×2): thêm cờ hard_abort → worker bỏ luôn auto-resume + chỉ đạo tồn."""
        try:
            try:
                self._agent.stop(hard=hard)
            except TypeError:
                self._agent.stop()
        except Exception:
            pass
        try:
            self.manager.interrupt()
        except Exception:
            pass
        drained = 0
        while True:
            try:
                k, _ = self.q.get_nowait()
            except Exception:
                break
            if k == "quit":
                self.q.put(("quit", None))
                break
            if k == "task":
                drained += 1
        self._pending = 0
        if hard:
            _p("⏹ Dừng cứng (ESC×2) — bỏ auto-chạy tiếp + chỉ đạo tồn.", "ye")
        else:
            _p(f"⏹ Đang dừng agent…{(f' (đã bỏ {drained} câu chờ)' if drained else '')} — ESC lần nữa để dừng cứng.", "ye")

    def _input_line(self, prompt):
        """Nhập 1 dòng, bắt ESC ngay không cần Enter (kiểu opencode session_interrupt).
        - Rảnh + ESC: xóa dòng đang gõ (không submit).
        - Bận + ESC 1 lần: dừng mềm; ESC lần 2 trong 0.8s: dừng cứng.
        - Fallback input() thường khi không phải tty hoặc thiếu termios."""
        try:
            if not sys.stdin.isatty():
                return input(prompt).strip()
        except Exception:
            try:
                return input(prompt).strip()
            except Exception:
                return ""
        try:
            import termios as _tm, tty as _ty, select as _sel
        except Exception:
            try:
                return input(prompt).strip()
            except Exception:
                return ""
        try:
            sys.stdout.write(prompt)
            sys.stdout.flush()
        except Exception:
            pass
        buf = []
        try:
            fd = sys.stdin.fileno()
            old = _tm.tcgetattr(fd)
        except Exception:
            try:
                return input("").strip()
            except Exception:
                return ""
        try:
            _ty.setcbreak(fd)
            while True:
                try:
                    rl, _, _ = _sel.select([sys.stdin], [], [], 0.1)
                except Exception:
                    rl = [sys.stdin]
                if not rl:
                    continue
                try:
                    ch = sys.stdin.read(1)
                except Exception:
                    continue
                if not ch:
                    continue
                if ch in ("\r", "\n"):
                    txt = "".join(buf).strip()
                    if txt:
                        _save_history(txt)
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return txt
                if ch == "\x03":  # Ctrl+C
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    raise KeyboardInterrupt
                if ch == "\x04":  # Ctrl+D
                    if not buf:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        raise EOFError
                    continue
                if ch in ("\x7f", "\x08"):  # Backspace
                    if buf:
                        buf.pop()
                        try:
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                        except Exception:
                            pass
                    continue
                if ch == "\t":  # TAB — autocomplete lệnh / (kiểu opencode)
                    cur = "".join(buf).strip()
                    if cur.startswith("/"):
                        sugs = _palette_suggest(cur.lstrip("/")) or [c for c in _SLASH if c.startswith(cur)]
                        if sugs:
                            fill = sugs[0]
                            if len(sugs) > 1:
                                extra = "   ".join(f"{' '.join(s.split())}" for s in sugs[:4])
                                sys.stdout.write("\r\033[K" + C["dim"] + extra + C["reset"] + "\n")
                                sys.stdout.write(prompt)
                            rm = len(buf)
                            try:
                                sys.stdout.write("\b \b" * rm)
                            except Exception:
                                pass
                            buf[:] = list(fill)
                            sys.stdout.write(fill)
                            sys.stdout.flush()
                    continue
                if ch == "\x1b":  # ESC — phân biệt ESC lẻ vs phím mũi tên
                    try:
                        rl2, _, _ = _sel.select([sys.stdin], [], [], 0.05)
                    except Exception:
                        rl2 = []
                    if rl2:
                        # Escape sequence (mũi tên/F-key...) → nuốt hết, không chèn rác
                        try:
                            while _sel.select([sys.stdin], [], [], 0.02)[0]:
                                sys.stdin.read(1)
                        except Exception:
                            pass
                        continue
                    now = time.time()
                    double = (now - (self._last_esc or 0)) <= 0.8
                    self._last_esc = now
                    if self._busy:
                        self._request_stop(hard=double)
                        buf = []
                        try:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            sys.stdout.write(prompt)
                            sys.stdout.flush()
                        except Exception:
                            pass
                        continue
                    # rảnh: ESC = xóa dòng (opencode) rồi gõ tiếp
                    if buf:
                        buf = []
                        try:
                            sys.stdout.write("\r\033[K")
                            sys.stdout.write(prompt)
                            sys.stdout.flush()
                        except Exception:
                            pass
                    continue
                try:
                    o = ord(ch)
                except Exception:
                    o = 32
                if o < 32:
                    continue  # bỏ control char khác
                buf.append(ch)
                try:
                    sys.stdout.write(ch)
                    sys.stdout.flush()
                except Exception:
                    pass
        finally:
            try:
                _tm.tcsetattr(fd, _tm.TCSADRAIN, old)
            except Exception:
                pass

    # ── sự kiện từ agent (chạy trong worker thread) ──
    # Trình bày theo kiểu timeline opencode: mỗi tool = 1 dòng "✓ Bash — $ ls -la"
    def _set_status(self, msg):
        self._status_msg = msg
        self._status_since = time.time()

    def _on_ev(self, ev):
        t = ev.get("type")
        if t == "tool_start":
            self._cur_title = _tool_title(ev)
            self._tool_t0 = time.time()
            self._set_status(self._cur_title)
            try:
                nm = ev.get("name", "") if isinstance(ev, dict) else ""
                if nm in REPLAY_TOOLS:
                    _overlay_write(mode="PHÁT LẠI NHANH", tool=self._cur_title,
                                   progress="phát lại quy trình đã học")
                else:
                    _overlay_write(tool=self._cur_title)
            except Exception:
                pass
        elif t == "tool_done":
            r = (ev.get("result") or "")
            ok = not r.startswith(("[LOI]", "[TOOL LOI]", "[TU CHOI]"))
            dt = time.time() - (self._tool_t0 or time.time())
            label = _TOOL_LABEL.get(self._cur_title.split("  —  ")[0] if "  —  " in self._cur_title else self._cur_title, self._cur_title)
            row = ("  ✓ " if ok else "  ✗ ") + C["gr" if ok else "rd"] + label + C["reset"] + C["dim"] + f" done · {dt:.1f}s" + C["reset"]
            self._tool_rows.append((row, not ok))
            if len(self._tool_rows) > 14:
                self._tool_rows.pop(0)
            _p(row, "gr" if ok else "rd")
            # Diff cũ/mới kiểu opencode: hiện ngay dưới dòng ✓ khi sửa/tạo file
            # màn hình rộng → 2 cột CŨ|MỚI, hẹp → diff 1 cột
            if ok and ev.get("name") in ("edit_file", "write_file", "apply_patch") and ev.get("full"):
                try:
                    if render.term_width() >= 100:
                        d = render.side_diff_to_ansi(ev.get("full"))
                    else:
                        d = render.diff_to_ansi(ev.get("full"))
                except Exception:
                    d = ""
                if d:
                    _p("  " + C["dim"] + "─ diff ─" + C["reset"], "dim")
                    for dln in d.split("\n"):
                        _p("  " + dln, "")
        elif t == "thinking":
            self._set_status("đang suy luận")
        elif t == "llm":
            self._set_status("đang suy luận")
        elif t == "stream_delta":
            self._live_kind = ev.get("kind", "content")
            self._live_n += len(ev.get("text", ""))
            if self._live_kind == "thinking":
                self._status_msg = "đang suy luận"
        elif t == "retry":
            self._set_status("Groq quá tải — đang thử lại…")
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
        is_tty = sys.stdout.isatty() and not config.DEBUG
        last_msg = None
        while self._spin_on:
            msg = self._status_msg or ""
            # Khi user đang gõ inject (main thread ở input()) → KHÔNG animate \r
            # đè lên dòng đang gõ (gây loạn chữ + mất chữ + paste lặp 40 lần).
            # Chỉ in khi trạng thái đổi, mỗi trạng thái 1 dòng mới.
            if (not is_tty) or self._in_input:
                # Không animate frame — CHỈ in lại khi trạng thái THAY ĐỔI.
                # Tránh "treo" hiện ra hàng trăm dòng lặp lại trong log/pipe.
                cur = re.sub(r"\x1b\[[0-9;]*m", "", msg).strip()
                if cur and cur != last_msg:
                    w = int(time.time() - self._status_since)
                    tag = f"  [{w}s]{'  ⏳ lâu quá — /stop nếu kẹt' if w >= config.TOOL_SLOW_WARN else ''}"
                    _p(f"  {cur}" + (tag if w >= 3 else ""), "dim")
                    last_msg = cur
                time.sleep(0.5)
                continue
            f = SPIN[i % len(SPIN)]
            el = int(time.time() - self._spin_start)
            wait = time.time() - self._status_since
            base = f"  {msg}" if msg else ""
            if self._live_n > 0:
                base += f" · {self._live_n} ký tự"
            if el >= 60:
                base += f"  [{el // 60}p{el % 60:02d}s]"
            else:
                base += f"  [{el}s]"
            if wait >= config.TOOL_SLOW_WARN:
                base += "  ⏳ lâu quá — /stop nếu kẹt"
                col = "\033[91m"
            else:
                col = "\033[36m"
            # kiểu opencode: frame spinner màu + nội dung mờ — chậm hơn để bớt "nháy"
            with _OUT_LOCK:
                sys.stdout.write("\r" + col + f + C["dim"] + base[:150] + C["reset"] + "\033[K")
                sys.stdout.flush()
            time.sleep(0.4)
            i += 1
        with _OUT_LOCK:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

    def _clear_spin_line(self):
        with _OUT_LOCK:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()

# ── worker: xử lý câu hỏi theo hàng đợi, cho phép soạn câu mới chờ lượt ──
    def _pause_kind(self, out):
        """Phân loại kết cục của 1 lượt: None = xong hẳn; 'user' = /stop (KHÔNG tự làm tiếp);
        'quota'|'time'|'step' = bị cắt giữa chừng → TỰ ĐỘNG chạy tiếp."""
        s = (out or "").strip()
        if s.startswith("[ĐÃ DỪNG] theo yêu cầu"):
            return "user"
        if s.startswith("[TẠM DỪNG]"):
            return "quota"
        if s.startswith("[ĐÃ DỪNG] chạy quá"):
            return "time"
        if s.startswith("[DUNG]") or s.startswith("[DỪNG]"):
            return "step"
        return None

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
                try:
                    self.manager.interrupt()
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
                _task_t0 = time.time()   # mốc đầu lượt (footer usage kiểu opencode)
                self._spin_on = True
                try:
                    self.manager.reset_interrupt()
                except Exception:
                    pass
                self._set_status("đang bắt đầu")
                # Việc tay chân (desktop/web/mạng xã hội/media) → tự mở cửa sổ nổi
                # để user NHÌN THẤY từng thao tác. Lần đầu agent quan sát kỹ + tự học,
                # lần sau phát lại nhanh (overlay chuyển PHÁT LẠI NHANH ở tool_start).
                try:
                    from agentloop import route_task as _rt
                    _gr, _ = _rt(payload or "")
                except Exception:
                    _gr = []
                try:
                    if any(g in OVERLAY_GROUPS for g in (_gr or [])):
                        _overlay_ensure()
                        _overlay_write(mode="QUAN SÁT", task=payload or "",
                                       tool="", progress="quan sát lần đầu + tự học")
                    else:
                        _overlay_write(task=payload or "", tool="", progress="đang bắt đầu")
                except Exception:
                    pass
                self._tool_rows = []
                self._cur_title = ""
                self._live_n = 0
                self._live_kind = ""
                spin = threading.Thread(target=self._spinner, daemon=True)
                spin.start()
                try:
                    out = self._agent.run(payload)
                except Exception as e:
                    out = f"[LỖI] {type(e).__name__}: {e}"
                # TỰ ĐỘNG chạy tiếp nếu bị cắt giữa chừng (quota/bước) — không bắt
                # người dùng gõ 'tiếp tục'. Hết giờ (time) thì KHÔNG tự chạy tiếp
                # (task quá lớn, chạy tiếp sẽ hết giờ nữa → vòng lặp treo 40 phút
                # như log lỗi); trả kết quả dở + hướng dẫn gõ 'tiếp tục'.
                # /stop (user) và dừng cứng ESC×2 thì tôn trọng, không tự làm tiếp.
                auto_runs = 0
                quota_streak = 0
                try:
                    _hard = bool(self._agent.hard_abort.is_set())
                except Exception:
                    _hard = False
                if _hard:
                    pk0 = self._pause_kind(out)
                    if pk0:
                        _p("Đã dừng cứng — bỏ auto-chạy tiếp.", "ye")
                while auto_runs < AUTO_RESUME_MAX:
                    pk = self._pause_kind(out)
                    if not pk or pk == "user":
                        break
                    try:
                        if bool(self._agent.hard_abort.is_set()):
                            _p("Đã dừng cứng — bỏ auto-chạy tiếp.", "ye")
                            break
                    except Exception:
                        pass
                    if pk == "time":
                        _p("Hết giờ lượt này — giữ tiến độ, gõ 'tiếp tục' để chạy nốt (không tự chạy tiếp để tránh treo).", "ye")
                        break
                    if pk == "quota":
                        quota_streak += 1
                        if quota_streak >= 3:
                            _p("Groq quá tải kéo dài — dừng chế độ tự chạy tiếp.", "rd")
                            break
                    else:
                        quota_streak = 0
                    auto_runs += 1
                    _p(f"[auto] lượt bị cắt ({pk}) — tự động chạy tiếp lần {auto_runs}...", "di")
                    try:
                        out = self._agent.run("[tự động tiếp tục] Tiếp tục HOÀN THÀNH nốt công việc DANG DỞ: nếu đang ghi file lớn thì CHỈ nối thêm phần còn thiếu bằng cat >> (lệnh cấm chiếu theo hướng dẫn), CẤM ghi lại toàn bộ file đã có nội dung. Không hỏi lại, không lặp. Xong mới kết thúc.")
                    except Exception as e:
                        out = f"[LỖI] {type(e).__name__}: {e}"
                        break
                if auto_runs >= AUTO_RESUME_MAX:
                    _p(f"Đã tự chạy tiếp {AUTO_RESUME_MAX} lần chưa xong — nếu vẫn kẹt hãy báo lại bằng /stop.", "ye")
                # CHỈ ĐẠO TỒN: lệnh gõ đúng lúc agent vừa xong bước cuối → làm tiếp luôn.
                # Dừng cứng (ESC×2) thì bỏ luôn để trả máy ngay (opencode hard abort).
                try:
                    _hard2 = bool(self._agent.hard_abort.is_set())
                except Exception:
                    _hard2 = False
                try:
                    _left = [] if _hard2 else self._agent._drain_notes()
                except Exception:
                    _left = []
                if _hard2 and _left:
                    _p("Đã dừng cứng — bỏ chỉ đạo tồn.", "ye")
                    _left = []
                if _left:
                    if len(_left) > 5:
                        _p(f"[live] bỏ {len(_left)-5} chỉ đạo thừa (giữ 5 mới nhất) — chống kẹt hàng chờ.", "ye")
                        _left = _left[-5:]
                    _p(f"[live] còn {len(_left)} chỉ đạo giữa chừng — làm tiếp...", "dim")
                    try:
                        out = self._agent.run("[CHỈ ĐẠO GIỮA CHỪNG — điều chỉnh việc đang làm theo yêu cầu mới, không làm lại từ đầu]\n" + "\n".join(_left))
                    except Exception as e:
                        out = f"[LỖI] {type(e).__name__}: {e}"
                self._last_out = out
                try:
                    self._last_turn_use = groq.take_usage()
                    self._last_turn_secs = time.time() - _task_t0
                except Exception:
                    self._last_turn_use, self._last_turn_secs = {}, 0.0
                self._spin_on = False
                self._busy = False
                spin.join(timeout=1)      # đợi spinner bỏ dòng cuối xong
                self._clear_spin_line()   # rồi mới in tránh bị đè "Rem>"
                # timeline tool ĐÃ in live khi từng tool xong ở _on_ev — không in lại nữa
                if out:
                    with _OUT_LOCK:
                        sys.stdout.write(P_AGENT)
                        sys.stdout.flush()
                    think, body = render.split_thinking(out)
                    self._last_think = think
                    # Opencode-style thinking block
                    if think.strip():
                        _type(render.thinking_to_ansi(think, full=False), None)
                    # Body — render markdown sạch (opencode-style)
                    if body.strip():
                        _type(render.md_to_ansi(body), None)
                    # Footer usage kiểu opencode: token lượt này + tổng phiên.
                    try:
                        _u = self._last_turn_use or {}
                        _t = groq.session_usage()
                        _p(f"◆ ↑{_u.get('prompt', 0)} ↓{_u.get('completion', 0)} · "
                           f"{self._last_turn_secs:.0f}s · "
                           f"∑↑{_t.get('prompt', 0)} ↓{_t.get('completion', 0)}", "dim")
                    except Exception:
                        pass
                    # Đáp án xong → gợi ý phím tắt kiểu opencode.
                    # KHÔNG in "❯ " tay ở đây — vòng input() kế tiếp sẽ in prompt
                    # (in tay gây double prompt "❯ ❯" và dính chữ như log lỗi).
                    self._footer_hints()
                try:
                    _overlay_write(mode="XONG", tool="", progress="xong — lần sau phát lại nhanh")
                except Exception:
                    pass
                self._pending = 0

    # ── câu hỏi quyền (safe mode): chạy trong worker, hỏi trực tiếp ──
    def _ask(self, name, args):
        self._clear_spin_line()
        _p(f"→ tool '{name}' cần quyền", "ye")
        preview = _diff_preview(name, args)
        if preview:
            _p("  " + preview, "dim")
        else:
            mini = str(args)[:120]
            _p(f"  {mini}", "dim")
        try:
            a = input("  Cho phép? [y/N/a=luôn auto] ").strip().lower()
        except Exception:
            return False
        if a in ("a", "all", "luon", "auto"):
            self._agent.perm.set_auto(True)
            self.presets = "build"
            _p("  Đã chuyển chế độ TỰ ĐỘNG — không hỏi nữa (xoá bằng /safe).", "gr")
            return True
        return a in ("y", "yes", "ok", "cho", "phep", "1", "c")

    def _status(self):
        print(CLEAR_SEQ)
        try:
            print(_logo_banner())
        except Exception:
            pass
        _p("Gõ /help | /status | /stop | /clear | /exit", "dim")
        # Dòng trạng thái kiểu opencode: model | keys | chế độ | session
        # (chỉ đọc cache, không gọi mạng để khỏi lag mỗi lần vẽ màn hình)
        try:
            _live = (getattr(groq, "_MODELS", {}) or {}).get("items") or set()
            _model = next((m for m in config.MODEL_PREF_CHAT if m in _live), None) or config.MODEL_CHAT
        except Exception:
            _model = "?"
        try:
            _nkeys = len(groq.keys())
        except Exception:
            _nkeys = 0
        _auto = self.agent_auto()
        _mode = "auto" if _auto is True else ("safe" if _auto is False else "?")
        _p(f"◆ {_model} | {_nkeys} keys | {_mode} | {self.sid}", "cy")
        if not _nkeys:
            _p("⚠  CHƯA CÓ GROQ KEY — gõ: /key gsk_...  để thêm", "rd")

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
        ok = groq._km.ok_keys(ks)
        st = groq._km.stats_summary()
        _p(f"Có {len(ks)} Groq keys — {ok} sẵn sàng dùng", "bold")
        if st:
            print(st)
        _p("Thêm key: /key gsk_...   |   /keys reset  để xoá stats đã học", "dim")

    # ── /palette: bảng lệnh tìm nhanh kiểu opencode command palette ──
    def _palette(self):
        """Gõ 1 mảnh lệnh → liệt kê lệnh khớp + alias; nhấn số để chạy ngay."""
        if self._busy:
            _p("Agent đang bận — đợi hết lượt rồi gõ /palette.", "ye")
            return
        try:
            self._clear_spin_line()
        except Exception:
            pass
        _p("╭─❯ /palette — gõ mảnh lệnh (vd 'stop', 'key'), nhấn số để chạy ngay", "dim")
        try:
            q = input("  filter: ").strip().lower().lstrip("/")
        except Exception:
            return
        if not q:
            matches = _SLASH
        else:
            matches = _palette_suggest(q) or [c for c in _SLASH if q in c]
        if not matches:
            _p("  (không khớp lệnh nào)", "dim")
            return
        for i, c in enumerate(matches[:8], 1):
            cat, desc, args, aliases = _SLASH_CAT[c]
            hint = f" {args}" if args else ""
            alias_txt = ((" (" + ", ".join(a for a in aliases) + ")") if aliases else "")
            _p(f"  {i}. {C['cy']}{c}{hint}{C['reset']} — {desc}{C['dim']}{alias_txt}{C['reset']}", "dim")
        _p("  chọn số 1-8 (Enter = thoát), đổi filter để tìm tiếp", "dim")
        try:
            n = input("  chọn: ").strip()
        except Exception:
            return
        if n.isdigit() and 1 <= int(n) <= len(matches):
            self.slash(matches[int(n) - 1])

    # ── /theme: đổi bảng màu giao diện (lưu vào config.REPL_THEME) ──
    def _theme(self, name):
        import config as _cfg
        try:
            _cfg.REPL_THEME
        except AttributeError:
            _p("Không đọc được REPL_THEME (bản cũ config?)", "rd")
            return
        if not name:
            cur = _cfg.REPL_THEME
            _p(f"Màu hiện tại: '{cur}'", "cy")
            _p("Chọn: " + " · ".join(f"{C['cy']}{t}{C['reset']}" for t in _cfg.THEMES), "dim")
            _p("Cách dùng: /theme <tên>   (vd /theme ocean)", "dim")
            return
        if name not in _cfg.THEMES:
            _p(f"Không có theme '{name}'. Có: {', '.join(_cfg.THEMES)}", "ye")
            return
        try:
            _cfg.REPL_THEME = name
            _apply_theme_c()
        except Exception as e:
            _p(f"[LOI] đổi theme: {e}", "rd")
            return
        _p(f"Đã đổi sang theme '{name}'.", "gr")

    # ── /todos: danh sách công việc kiểu opencode todo list ──
    def _todos(self):
        tpath = os.path.join(config.DIR, "todos", f"{self.sid}.json")
        try:
            with open(tpath, "r", encoding="utf-8") as f:
                todos = json.load(f)
        except Exception:
            todos = []
        if not todos:
            _p("(chưa có todo — bảo agent 'tạo todo cho việc này' để nó theo dõi tiến độ)", "dim")
            return
        icons = {"completed": ("✓", "gr"), "in_progress": ("•", "ye"),
                 "pending": ("○", "dim"), "cancelled": ("⊘", "dim")}
        for t in todos:
            st = t.get("status", "pending")
            ic, col = icons.get(st, ("○", "dim"))
            prio = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(t.get("priority", ""), "")
            _p(f"  {C[col]}{ic}{C['reset']} {prio} {t.get('content', '?')}", "dim")
        _p(f"({sum(1 for t in todos if t.get('status') == 'completed')}/{len(todos)} xong)", "gr")

    # ── /compact: nén context thủ công (kiểu opencode compact) ──
    def _compact(self):
        if self._busy:
            _p("Agent đang bận — đợi hết lượt rồi gõ /compact.", "ye")
            return
        msgs = sessions.load(self.sid)
        if not msgs:
            _p("(chưa có lịch sử để nén)", "dim")
            return
        before = sum(len(m.get("content") or "") for m in msgs)
        _p(f"Nén context ({len(msgs)} tin, ~{before} ký tự)...", "ye")
        out = sessions.compact(self.sid, msgs, budget=0, msg_cap=0, llm_budget=60)
        after = sum(len(m.get("content") or "") for m in out)
        try:
            path = os.path.join(config.DIR, "sessions", f"{self.sid}.jsonl")
            import tempfile
            _fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".cmp")
            with os.fdopen(_fd, "w", encoding="utf-8") as f:
                for m in out:
                    f.write(json.dumps(m, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as e:
            _p(f"[LOI] không lưu được context nén: {e}", "rd")
            return
        self._mk_agent()
        _p(f"Đã nén: {before} → {after} ký tự ({len(msgs)} → {len(out)} tin).", "gr")

    # ── /model: chọn model chat cho phiên (qua REM_MODEL, persist rem_model) ──
    def _model_pick(self, arg=""):
        import config as _cfg
        try:
            ms = groq.chat_models() or [m for m in _cfg.MODEL_PREF_CHAT if m]
        except Exception:
            ms = list(_cfg.MODEL_PREF_CHAT)
        _ov = os.environ.get("REM_MODEL", "").strip()
        try:
            with open(os.path.join(config.DIR, "rem_model"), encoding="utf-8") as _f:
                _ov = _ov or _f.read().strip()
        except Exception:
            pass
        if not arg:
            if _ov:
                _p(f"Model đang dùng: {_ov}  (gõ /model để xem danh sách, /model <tên> để đổi)", "cy")
            _p("Danh sách model chat (nhấn /model <tên> để chọn):", "bold")
            for i, m in enumerate(ms[:12], 1):
                mark = "→" if m == _ov else " "
                _p(f"  {mark} {i}. {m}", ("gr" if m == _ov else "dim"))
            return
        if arg not in ms:
            _p(f"Không thấy model '{arg}'. Dùng /model để xem có sẵn.", "ye")
            return
        os.environ["REM_MODEL"] = arg
        try:
            with open(os.path.join(config.DIR, "rem_model"), "w", encoding="utf-8") as _f:
                _f.write(arg)
        except Exception:
            pass
        _p(f"Đã chọn model: {arg} (áp dụng từ câu hỏi kế tiếp).", "gr")

    # ── toast: thông báo nổi góc phải kiểu opencode (ANSI, mờ) ──
    def _toast(self, msg, kind="info"):
        txt = _strip_ansi(str(msg))[:90]
        col = {"ok": "gr", "err": "rd", "warn": "ye"}.get(kind, "dim")
        _p(f"   {C[col]}▍{C['reset']} {_strip_ansi(txt)}", "dim")

    def _clear(self):
        self._clear_spin_line()
        print(CLEAR_SEQ)

    # ── /init: tạo AGENTS.md cho thư mục/ repo hiện tại (kiểu opencode) ──
    def _init_agents(self):
        import subprocess as _sp
        try:
            cwd = _sp.check_output(["bash", "-c", "pwd"], text=True, timeout=5).strip()
        except Exception:
            cwd = os.getcwd()
        name = os.path.basename(cwd) or "project"
        ag = os.path.join(cwd, "AGENTS.md")
        if os.path.exists(ag):
            _p(f"AGENTS.md đã tồn tại tại {cwd} — bỏ qua (chỉnh tay nếu cần).", "ye")
            return
        langs = []
        try:
            out = _sp.check_output(["bash", "-c", "ls -A"], text=True, timeout=5)
            files = out.split()
            for f in files:
                fn = f.lower()
                if fn.endswith((".py", ".sh", ".md", ".c", ".cpp", ".h", ".js", ".ts", ".json", ".yml", ".yaml", ".html", ".css", ".sql", ".go", ".rs")):
                    langs.append(os.path.splitext(fn)[1].lstrip("."))
            langs = sorted(set(langs)) or ["—"]
        except Exception:
            pass
        tmpl = f"""# AGENTS.md — Hướng dẫn cho Rem Agent (và mọi AI agent)

## Dự án
{name} — ở {cwd}
Ngôn ngữ/tệp chính: {', '.join(langs)}

## Lệnh hữu ích
- Chạy ứng dụng:  (bổ sung, vd `python main.py`)
- Chạy test:      (bổ sung, vd `pytest` / `bash test.sh`)
- Build:          (bổ sung, vd `make` / `npm run build`)

## Cấu trúc
(Bổ sung sơ đồ thư mục chính ở đây — `ls -R` nếu cần quét.)

## Quy ước cho agent
- Đọc trước khi sửa; dùng `edit_file`/`apply_patch` thay vì viết cả file trừ khi tạo mới.
- Sau khi sửa code .c/.cpp/.py hãy chạy `lsp_diagnostics` để bắt lỗi tĩnh trước khi chạy/build.
- Kiểm chứng mọi thay đổi bằng cách chạy lệnh test của dự án.
- (Bổ sung quy ước riêng của nhóm ở đây.)
"""
        try:
            with open(ag, "w", encoding="utf-8") as f:
                f.write(tmpl)
            _p(f"Đã tạo {ag}", "gr")
            _p("Sửa AGENTS.md theo dự án — Rem sẽ tự nạp file này khi làm việc trong thư mục.", "dim")
        except Exception as e:
            _p(f"[LOI] không ghi AGENTS.md: {e}", "rd")

    def _send(self, text):
        """Gửi câu lệnh vào hàng đợi agent (hiện khối User như khi gõ tay)."""
        sys.stdout.write(C["lm"] + C["bold"] + "User" + C["reset"] + C["lm"] + "> " + C["reset"] + C["wh"] + text + C["reset"] + "\n")
        sys.stdout.flush()
        if self._busy:
            self._pending += 1
            _p(f"⏳ Câu hỏi đã xếp hàng (#{self._pending}).", "ye")
        self.q.put(("task", text))

    # ── @file mention: đính kèm file vào lệnh kiểu opencode attachment ──
    def _expand_mentions(self, text):
        """Thay @đường/dẫn/file bằng nội dung file (opencode attachment).
        - file: nhúng nội dung (tối đa 6000 ký tự)
        - thư mục: liệt kê 15 mục đầu
        Không khớp (email/nonexistent) → giữ nguyên dòng gõ."""
        def _rep(m):
            p = os.path.expanduser(m.group(1))
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8", errors="replace") as f:
                        c = f.read(6000)
                    _p(f"  ⤷ đã đính kèm file {p} ({len(c)} ký tự)", "dim")
                    return f"\n[FILE:{p}]\n{c}\n[/FILE]"
                except Exception:
                    _p(f"  ⚠ không đọc được {p}", "ye")
                    return ""
            if os.path.isdir(p):
                try:
                    items = sorted(os.listdir(p))[:15]
                except Exception:
                    items = []
                _p(f"  ⤷ đã đính kèm danh sách thư mục {p} ({len(items)} mục)", "dim")
                return f"\n[DIR:{p}]\n" + "\n".join(items) + "\n[/DIR]"
            return m.group(0)
        # @ ở đầu token (không phải giữa email) — thay khi đúng đường dẫn thật
        return re.sub(r"(?<!\S)@(\S+)", _rep, text)

    # ── /list: liệt kê macro/skill/chat cũ để chọn số mở ra ─────────────
    def _stats(self):
        """Thống kê dùng của session hiện tại (kiểu opencode stats)."""
        try:
            msgs = sessions.load(self.sid)
        except Exception as e:
            _p(f"[LOI] không đọc được session: {e}", "rd")
            return
        nu = sum(1 for m in msgs if m.get("role") == "user")
        na = sum(1 for m in msgs if m.get("role") == "assistant")
        tools = [m for m in msgs if m.get("role") == "tool"]
        chars = sum(len(str(m.get("content") or "")) for m in msgs)
        cnt = {}
        for m in tools:
            n = m.get("name", "?")
            cnt[n] = cnt.get(n, 0) + 1
        top = sorted(cnt.items(), key=lambda kv: -kv[1])[:6]
        try:
            nkeys = len(groq.keys())
        except Exception:
            nkeys = 0
        _p(f"Session {self.sid} — user:{nu} assistant:{na} tool:{len(tools)} "
           f"~{chars // 4} tokens (~{chars} ký tự) — Groq keys:{nkeys}", "gr")
        try:
            _rt = groq.session_usage()
            _p(f"Token thật từ API (phiên này): ↑{_rt.get('prompt', 0)} "
               f"↓{_rt.get('completion', 0)} · {_rt.get('calls', 0)} calls", "cy")
        except Exception:
            pass
        for n, c in top:
            _p(f"  {n:20} {c}", "dim")
        if not msgs:
            _p("(session trống)", "dim")

    def _export(self):
        """Xuất đoạn chat hiện tại ra markdown (lưu ~/.rem_ai/exports/<sid>.md)."""
        try:
            msgs = sessions.load(self.sid)
        except Exception as e:
            _p(f"[LOI] không đọc được session: {e}", "rd")
            return
        if not msgs:
            _p("(đoạn chat trống, không có gì để xuất)", "dim")
            return
        lines = [f"# Chat {self.sid}", ""]
        for m in msgs:
            role = m.get("role", "?")
            body = (m.get("content") or "").strip()
            if role == "user":
                lines += ["## 🙋 Bạn", "", body, ""]
            elif role == "assistant":
                if body and body != "(rỗng)":
                    lines += ["## 🤖 Rem", "", body, ""]
            elif role == "tool":
                lines.append(f"- 🔧 `{m.get('name', '?')}`: {body[:200]}")
        d = os.path.join(config.DIR, "exports")
        try:
            os.makedirs(d, exist_ok=True)
            fp = os.path.join(d, self.sid + ".md")
            with open(fp, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            _p(f"Đã xuất {len(msgs)} tin nhắn → {fp}", "gr")
        except Exception as e:
            _p(f"[LOI] không ghi được file: {e}", "rd")

    def _list_items_all(self):
        """Quét macro + skill + session → [(kind, id, desc)]."""
        items = []
        for sub, kind in (("macros", "macro"), ("skills", "skill")):
            ddir = os.path.join(config.DIR, sub)
            try:
                files = sorted(os.listdir(ddir))
            except Exception:
                continue
            for fn in files:
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(ddir, fn), encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue
                if kind == "macro":
                    items.append((kind, d.get("name", fn[:-5]),
                                  f"mục {d.get('category', '?')} · {d.get('count', 0)} bước"))
                else:
                    items.append((kind, d.get("name", fn[:-5]),
                                  f"{len(d.get('steps', []))} bước · dùng {d.get('times', 0)} lần"))
        try:
            rows = sessions.list_all()
        except Exception:
            rows = []
        for sid, t, first in rows[-15:]:
            items.append(("session", sid, f"{t} · {(first or '(trống)')[:50]}"))
        return items

    def _list_show(self):
        self._list_items = self._list_items_all()
        if not self._list_items:
            _p("(chưa có macro/skill/chat nào)", "dim")
            return
        last_kind, n = "", 0
        titles = {"macro": "— MACRO (thao tác desktop đã ghi) —",
                  "skill": "— SKILL (quy trình đã học) —",
                  "session": "— CHAT CŨ (15 đoạn gần nhất) —"}
        for kind, _id, desc in self._list_items:
            if kind != last_kind:
                _p(titles[kind], "bold")
                last_kind = kind
            n += 1
            _p(f"  {n}. {_id} — {desc}", "cy")
        _p("Gõ số hoặc /list <số> để mở · /play <số> phát macro · /rec ghi mới", "dim")

    def _list_open(self, num):
        try:
            idx = int(num) - 1
            kind, _id, _desc = self._list_items[idx]
        except Exception:
            _p(f"Không có mục {num}. Gõ /list để xem lại.", "dim")
            return
        if kind == "macro":
            try:
                with open(os.path.join(config.DIR, "macros", _id + ".json"), encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                _p(f"[LOI] không đọc được macro '{_id}'", "rd")
                return
            _p(f"# Macro '{_id}' — mục {d.get('category', '?')} ({d.get('count', 0)} bước)", "bold")
            for i, st in enumerate(d.get("actions", []), 1):
                _p(f"  {i}. {st.get('tool')}({str(st.get('args', {}))[:150]})", "cy")
            _p(f"Gõ /play {idx + 1} để AI làm lại macro này", "dim")
        elif kind == "skill":
            try:
                with open(os.path.join(config.DIR, "skills", _id + ".json"), encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                _p(f"[LOI] không đọc được skill '{_id}'", "rd")
                return
            _p(f"# Skill '{_id}' — {d.get('description', '')[:200]}", "bold")
            for i, st in enumerate(d.get("steps", []), 1):
                _p(f"  {i}. {st.get('tool')}({str(st.get('args', {}))[:150]})", "cy")
        else:
            try:
                msgs = sessions.load(_id)
            except Exception:
                msgs = []
            users = [m.get("content", "")[:80] for m in msgs if m.get("role") == "user"]
            _p(f"# Chat {_id} — {len(msgs)} tin nhắn", "bold")
            for u in users[:5]:
                _p(f"  · {u}", "cy")
            _p(f"Gõ /resume {idx + 1} để mở lại đoạn chat này", "dim")

    def _rec_start_flow(self):
        # VÀO CHẾ ĐỘ GHI thao tác: hỏi tên + mục rồi rec_start qua agent
        if self._busy:
            _p("Agent đang bận — đợi hết lượt rồi gõ /rec lại.", "ye")
            return
        try:
            nm = input("  Tên macro (Enter = tự đặt): ").strip()
        except Exception:
            return
        if not nm:
            nm = "macro-" + time.strftime("%H%M%S")
        cats = self._macro_cats()
        try:
            cat = input(f"  Mục {','.join(cats)} (Enter = khac): ").strip().lower() or "khac"
        except Exception:
            return
        if cat not in cats:
            _p(f"Mục lạ — dùng 'khac'. Hợp lệ: {','.join(cats)}", "ye")
            cat = "khac"
        self.rec_mode = re.sub(r"\s+", "_", nm)[:60]
        _p(f"⏺ VÀO CHẾ ĐỘ GHI macro '{self.rec_mode}' (mục {cat}).", "rd")
        _p("  Ra lệnh thao tác desktop như bình thường — xong gõ /done để dừng & lưu.", "dim")
        self._send(f"Dùng rec_start để bắt đầu ghi macro tên '{self.rec_mode}' mục '{cat}'")

    def slash(self, line):
        cmd = line.strip()
        parts = cmd.split()
        if cmd == "/help":
            # Help nhóm theo mục từ _SLASH_CAT (kiểu command list opencode)
            _p("Rem Agent — danh sách lệnh (gõ /palette để tìm nhanh)", "bold")
            by_cat = {}
            for c, (cat, desc, args, _a) in _SLASH_CAT.items():
                by_cat.setdefault(cat, []).append((c, desc, args))
            for cat in ["Cơ bản", "Điều khiển", "Hiển thị", "Lịch sử", "Quyền hạn", "Cấu hình", "Phát triển", "Hệ thống"]:
                items = by_cat.pop(cat, [])
                if not items:
                    continue
                _p(f"— {cat.upper()} —", "bold")
                for c, desc, args in items:
                    hint = f" {args}" if args else ""
                    pad = max(1, 12 - len(c) - len(hint))
                    _p(f"  {C['cy']}{c}{hint}{C['reset']}{' ' * pad}{desc}", "dim")
            for cat, items in by_cat.items():
                if not items:
                    continue
                _p(f"— {cat.upper()} —", "bold")
                for c, desc, args in items:
                    hint = f" {args}" if args else ""
                    pad = max(1, 12 - len(c) - len(hint))
                    _p(f"  {C['cy']}{c}{hint}{C['reset']}{' ' * pad}{desc}", "dim")
            _p("TAB: tự hoàn thành lệnh · /palette: bảng lệnh tìm nhanh · /theme: đổi màu", "dim")
        elif cmd == "/list" or cmd.startswith("/list "):
            if len(parts) > 1:
                if not self._list_items:
                    self._list_items = self._list_items_all()
                self._list_open(parts[1])
            else:
                self._list_show()
        elif cmd == "/rec":
            self._rec_start_flow()
        elif cmd == "/play" or cmd.startswith("/play "):
            if len(parts) < 2:
                _p("Cú pháp: /play <số>  (xem số trong /list)", "dim")
            else:
                if not self._list_items:
                    self._list_items = self._list_items_all()
                try:
                    kind, _id, _d = self._list_items[int(parts[1]) - 1]
                except Exception:
                    _p(f"Không có mục {parts[1]}. Gõ /list để xem lại.", "dim")
                    return True
                if kind != "macro":
                    _p("Mục này không phải macro (chỉ phát lại được macro).", "ye")
                else:
                    self._send(f"Dùng rec_play để phát lại macro tên '{_id}' (tốc độ mặc định), rồi xác nhận kết quả")
        elif cmd == "/resume" or cmd.startswith("/resume "):
            if len(parts) < 2:
                _p("Cú pháp: /resume <số>  (xem số trong /list)", "dim")
            else:
                if not self._list_items:
                    self._list_items = self._list_items_all()
                try:
                    kind, _id, _d = self._list_items[int(parts[1]) - 1]
                except Exception:
                    _p(f"Không có mục {parts[1]}. Gõ /list để xem lại.", "dim")
                    return True
                if kind != "session":
                    _p("Mục này không phải đoạn chat.", "ye")
                else:
                    self.sid = _id
                    self.rec_mode = ""
                    self._mk_agent(sid=self.sid)
                    _p(f"Đã mở lại đoạn chat {_id} (lịch sử cũ được giữ).", "gr")
        elif cmd == "/done":
            if self.rec_mode:
                nm = self.rec_mode
                self.rec_mode = ""
                _p(f"■ Dừng ghi macro '{nm}' — đang lưu...", "ye")
                self._send(f"Dùng rec_stop để dừng ghi và lưu macro '{nm}', rồi rec_show để xác nhận nội dung")
            else:
                _p("Không ở chế độ ghi (ấn 2 trong /list để ghi thao tác).", "dim")
        elif cmd == "/overlay" or cmd.startswith("/overlay "):
            arg = (parts[1] if len(parts) > 1 else "status").lower()
            if arg == "on":
                ok = _overlay_ensure()
                _overlay_write(mode="RẢNH", task="", tool="", progress="đã bật cửa sổ nổi")
                _p("Đã bật cửa sổ nổi." if ok else "Không mở được cửa sổ nổi (không có màn hình?).", "gr" if ok else "ye")
            elif arg == "off":
                _overlay_stop()
                _p("Đã tắt cửa sổ nổi.", "gr")
            else:
                _p(f"Cửa sổ nổi: {'ĐANG CHẠY' if _overlay_running() else 'đang tắt'} (gõ /overlay on|off)", "dim")
        elif cmd == "/palette":
            self._palette()
        elif cmd == "/theme" or cmd.startswith("/theme "):
            self._theme(parts[1] if len(parts) > 1 else "")
        elif cmd == "/clear":
            self._clear()
        elif cmd == "/stop":
            if self._busy:
                self._request_stop(hard=False)
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
        elif cmd == "/think":
            if self._last_think.strip():
                _type(render.thinking_to_ansi(self._last_think, full=True), None)
            else:
                _p("(chưa có suy luận nào để xem — câu trả lời không dùng thẻ thinking)", "dim")
        elif cmd == "/effort":
            # Đổi mức suy luận reasoning_effort theo docs Groq (low/medium/high),
            # áp dụng cho gpt-oss và qwen3.8 từ câu hỏi kế tiếp, không tốn gì thêm.
            lv = (parts[1].strip().lower() if len(parts) > 1 else "")
            if lv in ("low", "medium", "high"):
                os.environ["REM_REASONING"] = lv
                _p(f"Mức suy luận: {lv} (gpt-oss, qwen3.8).", "gr")
            else:
                _p(f"Mức hiện tại: {os.environ.get('REM_REASONING', 'low')} — dùng: /effort low|medium|high", "dim")
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
        elif cmd == "/todos":
            self._todos()
        elif cmd == "/compact":
            self._compact()
        elif cmd == "/model" or cmd.startswith("/model "):
            self._model_pick(parts[1] if len(parts) > 1 else "")
        elif cmd == "/status":
            self._status()
        elif cmd == "/stats":
            self._stats()
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
        elif cmd == "/lsp" or cmd.startswith("/lsp "):
            if self._busy:
                _p("Agent đang bận — đợi hết lượt chạy rồi gõ lại.", "ye")
                return True
            if len(parts) < 2:
                _p("Cú pháp: /lsp <file>   — kiểm tra lỗi file (c/cpp/python).", "dim")
                return True
            fpath = os.path.expanduser(parts[1])
            if not os.path.isfile(fpath):
                _p(f"Không thấy file: {fpath}", "rd")
                return True
            try:
                from mcp_servers.lsp_server import lsp_diagnostics
                self._clear_spin_line()
                _p(f"LSP check: {fpath}", "cy")
                out = lsp_diagnostics(fpath)
            except Exception as e:
                out = f"[LOI] {type(e).__name__}: {e}"
            _p(out, "gr" if "(không có lỗi)" in out else "ye")
        elif cmd == "/mcp" or cmd.startswith("/mcp "):
            exts = self.manager.external_list()
            if not exts:
                _p("Chưa có MCP server ngoài. Tạo file ~/.rem_ai/mcp.json dạng: "
                   '{"mcp": {"tên": {"type": "stdio", "command": ["python3", "/abs/server.py"]}}}', "dim")
                return True
            if len(parts) > 1 and parts[1] == "reload":
                _p("Đang nạp lại MCP ngoài...", "dim")
                self.manager.reload_external()
                exts = self.manager.external_list()
            for e in exts:
                st = "OK" if e.enabled else "LOI"
                _p(f"  {e.name:16} [{st}] {e.desc} — {len(e.tools)} tool" + (f" | {e.error}" if e.error else ""),
                   ("gr" if e.enabled else "rd"))
        elif cmd == "/init":
            self._init_agents()
        elif cmd == "/keys":
            self._keys()
        elif cmd.startswith("/keys reset") or cmd == "/keys reset":
            try:
                os.remove(os.path.join(config.DIR, "key_stats.json"))
            except Exception:
                pass
            try:
                from providers import groq as _g
                _g._km._stats.clear()
                _g._km._rpm_hist.clear()
            except Exception:
                pass
            _p("Đã xoá stats key đã học.", "gr")
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
        elif cmd == "/export":
            self._export()
        elif cmd == "/exit" or cmd == "/quit":
            return False
        else:
            if cmd.startswith("/"):
                frag = cmd.split()[0]
                sug = [s for s in _SLASH if s.startswith(frag) and s != frag][:5]
                if len(sug) == 1:
                    # gõ dở mà khớp duy nhất → chạy luôn (vd /lis 2 = /list 2)
                    return self.slash(sug[0] + cmd[len(frag):])
                if sug:
                    _p(f"Không rõ lệnh. Ý bạn là: {' · '.join(sug)} ?", "ye")
                    return True
            _p("Không rõ lệnh. Gõ /help.", "dim")
        return True

    def _header_box(self):
        """Hộp thông tin đầu phiên kiểu opencode: viền bo tròn + session/model/cwd/keys."""
        tw = render.term_width()
        w = min(max(tw - 6, 44), 78)
        try:
            ms = groq.chat_models()
            model = ms[0] if ms else "chưa có key"
        except Exception:
            model = "?"
        try:
            nkeys = len(groq.keys())
        except Exception:
            nkeys = 0
        title = f" REM v{config.VERSION} "
        rows = [
            f"model  {model}",
            f"dir    {os.getcwd()}",
            f"chat   {self.sid} · {nkeys} keys",
        ]
        print(C["dim"] + "╭" + title + "─" * max(0, w - render.disp_len(title) - 2) + "╮" + C["reset"])
        for r in rows:
            pad = max(0, w - 4 - render.disp_len(r))
            print(C["dim"] + "│" + C["reset"] + " " + r + " " * pad + " " + C["dim"] + "│" + C["reset"])
        bot = "╰" + "─" * (w - 2) + "╯"
        print(C["dim"] + bot + C["reset"])

    def _footer_hints(self):
        # Status bar kiểu opencode: gợi ý trái, thư mục + version phải
        try:
            tw = render.term_width()
        except Exception:
            tw = 90
        left = "/list · /new · /stop · /exit"
        try:
            right = f"{os.path.basename(os.getcwd())} · v{config.VERSION}"
            _lu = getattr(self, "_last_turn_use", {}) or {}
            if _lu.get("prompt") or _lu.get("completion"):
                right += f" · ↑{_lu.get('prompt', 0)} ↓{_lu.get('completion', 0)}"
        except Exception:
            right = ""
        try:
            pad = max(2, tw - render.disp_len(left) - render.disp_len(right))
        except Exception:
            pad = 4
        print(C["dim"] + left + " " * pad + right + C["reset"], flush=True)

    def _prompt_hint(self):
        # Opencode-style: thẻ nhập LUÔN 2 dòng (dòng gợi ý mờ + dòng ❯ nhập liệu).
        # Giữ cùng chiều cao khi bận/rảnh để không sót dòng prompt cũ gây dính chữ.
        try:
            lv = self._agent.live_count()
        except Exception:
            lv = 0
        rec = (C["rd"] + "⏺REC " + C["reset"]) if self.rec_mode else ""
        live = (C["ye"] + f"📥{lv} " + C["reset"]) if lv else ""
        card_top = (C["dim"] + "╭─❯ gõ câu hỏi · /list danh mục · /rec ghi thao tác"
                    + C["reset"] + "\n")
        if self._busy:
            top = (C["dim"] + "╭─❯ đang chạy — gõ để điều chỉnh · ESC//stop để dừng"
                   + C["reset"] + "\n")
            return top + rec + live + C["bold"] + C["cy"] + "⏳ ❯ " + C["reset"]
        if self._pending:
            top = (C["dim"] + f"╭─❯ xếp hàng ({self._pending}) — chờ lượt chạy"
                   + C["reset"] + "\n")
            return top + rec + live + C["bold"] + C["cy"] + "⏳ ❯ " + C["reset"]
        return card_top + rec + live + C["bold"] + C["cy"] + "╰─❯ " + C["reset"]

    def run(self):
        self._clear()
        if not groq.keys():
            _p("⚠  CHƯA CÓ GROQ KEY — gõ: /key gsk_...  để thêm", "rd")
        # model đã chọn bằng /model giữa các phiên → nạp lại từ file rem_model
        try:
            if not os.environ.get("REM_MODEL", "").strip():
                with open(os.path.join(config.DIR, "rem_model"), encoding="utf-8") as _f:
                    _saved_model = _f.read().strip()
                if _saved_model:
                    os.environ["REM_MODEL"] = _saved_model
        except Exception:
            pass
        if self.headless is None:
            try:
                print(_logo_banner())
            except Exception:
                pass
            _p("Gõ /help | /status | /stop | /clear | /exit", "dim")
            _p(f"Đoạn chat mới: {self.sid} (lịch sử trống — không dính chuyện cũ)", "gr")
            try:
                self._header_box()
            except Exception:
                pass
            _p("Gõ /list để xem macro/skill/chat cũ (chọn số để mở) | /rec để ghi thao tác", "dim")
            _p("Đang khởi chạy extensions...", "dim")
        else:
            try:
                self._header_box()
            except Exception:
                pass
            if self.resume_sid:
                _p(f"Tiếp tục phiên: {self.sid} (giữ context cũ)", "gr")
        self.manager.start_all()
        threading.Thread(target=self._worker, daemon=True).start()
        if self.headless is not None:
            self.q.put(("task", self.headless))
            while not self._busy:
                time.sleep(0.1)
            while self._busy:
                time.sleep(0.5)
            self.q.put(("quit", None))
            return
        while True:
            try:
                self._in_input = True
                try:
                    line = self._input_line(self._prompt_hint()).strip()
                finally:
                    self._in_input = False
            except EOFError:
                self._in_input = False
                _p("\nTạm biệt!", "dim")
                self.q.put(("quit", None))
                break
            except KeyboardInterrupt:
                self._in_input = False
                if self._busy:
                    self._request_stop(hard=False)
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
            # Gõ số trần = mở mục trong /list gần nhất (vd "2" mở mục số 2)
            if re.fullmatch(r"\d+", line):
                if self._list_items:
                    self._list_open(line)
                else:
                    _p("Gõ /list trước để xem danh sách rồi chọn số.", "dim")
                continue
            # HAI PHẦN: đang bận = luồng LÀM chạy, dòng gõ = luồng NGHE.
            # Lệnh mới lái TRỰC TIẾP việc đang chạy (inject), không xếp hàng chờ.
            if self._busy:
                if _is_inject_noise(line):
                    _p("(bỏ qua dòng rác terminal paste nhầm — không chuyển cho agent)", "dim")
                    continue
                try:
                    cur_n = self._agent.live_count()
                except Exception:
                    cur_n = 0
                if cur_n >= 5:
                    _p(f"Hàng chờ đầy ({cur_n}/5) — gõ /stop để dừng trước khi ra lệnh mới.", "ye")
                    continue
                try:
                    n = self._agent.inject(line)
                except Exception:
                    n = 0
                _p(f"📥 Đã chuyển cho agent đang chạy ({n} chỉ đạo chờ) — nó điều chỉnh ngay trong lượt này.",
                   "ye")
                continue
            # Opencode-style: highlight user input, show as "User" block.
            # Xóa dòng input vừa gõ theo đúng số hàng vật lý (prompt 2 dòng + chữ dài
            # có thể wrap) — bản cũ chỉ lùi 1 hàng nên sót chữ gây dính logo/prompt.
            try:
                with _OUT_LOCK:
                    try:
                        _vis = re.sub(r"\x1b\[[0-9;]*m", "", self._prompt_hint())
                        _total = render.disp_len(_vis) + render.disp_len(line)
                        _tw = render.term_width() or 90
                        _rows = max(1, (_total + _tw - 1) // _tw)
                        sys.stdout.write("\r\033[2K")
                        for _ in range(_rows - 1):
                            sys.stdout.write("\033[1A\033[2K")
                        sys.stdout.write("\r")
                    except Exception:
                        sys.stdout.write("\n")
                    sys.stdout.write(C["lm"] + C["bold"] + "User" + C["reset"] + C["lm"] + "> " + C["reset"] + C["wh"] + line + C["reset"] + "\n")
                    sys.stdout.flush()
            except Exception:
                _p("User> " + line, "lm")
            self.q.put(("task", self._expand_mentions(line)))
