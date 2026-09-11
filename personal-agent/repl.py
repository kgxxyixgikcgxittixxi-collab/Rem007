import subprocess, os, sys, time, threading, queue, re, json

import config
import sessions
import updater
import render
from agentloop import Agent
from extensions import Manager
from permissions import PermPolicy, Presets
from providers import groq

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
    "web_fetch": "WebFetch", "remember": "Remember", "recall": "Recall",
    "ensure_tool": "Setup", "pip_install": "PyPI", "github_api": "GitHub",
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


def _p(s, col="cy", end="\n"):
    print(C.get(col, "") + str(s) + C["reset"], end=end, flush=True)


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
    print(flush=True)


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
    def __init__(self, manager, headless=None):
        self.manager = manager
        self.headless = headless      # None = REPL tương tác; còn lại = chạy 1 task rồi thoát
        self.sid = sessions.new()
        self.presets = "build"
        self.q = queue.Queue()
        self._busy = False
        self._pending = 0
        self._status_msg = ""
        self._status_since = 0.0
        self._spin_start = 0.0
        self._spin_on = False
        self._last_out = None
        self._tool_rows = []      # các dòng tool đã xong (giống timeline opencode)
        self._cur_title = ""
        self._live_n = 0          # số ký tự model đang soạn (stream) — hiện tiến độ
        self._live_kind = ""      # "content" | "thinking"
        self._last_think = ""     # khối suy luận gần nhất (để /think xem đầy đủ)
        self._tool_t0 = 0.0       # mốc bắt đầu tool hiện tại (hiện số giây kiểu opencode)
        self.rec_mode = ""        # tên macro đang ghi (chế độ ghi từ /rec), "" = chat thường
        self._list_items = []     # [(kind,id,desc)] của lần /list gần nhất (chọn số để mở)
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
            self._tool_t0 = time.time()
            self._set_status(self._cur_title)
        elif t == "tool_done":
            r = (ev.get("result") or "")
            ok = not r.startswith(("[LOI]", "[TOOL LOI]", "[TU CHOI]"))
            dt = time.time() - (self._tool_t0 or time.time())
            label = _TOOL_LABEL.get(self._cur_title.split("  —  ")[0] if "  —  " in self._cur_title else self._cur_title, self._cur_title)
            row = ("  ✓ " if ok else "  ✗ ") + C["gr" if ok else "rd"] + label + C["reset"] + C["dim"] + f" done · {dt:.1f}s" + C["reset"]
            self._tool_rows.append((row, not ok))
            if len(self._tool_rows) > 14:
                self._tool_rows.pop(0)
            _p("\r" + row, "gr" if ok else "rd")
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
            attempt = ev.get("attempt", 1)
            if attempt <= 1 or attempt % 2 == 0:
                self._set_status(f"thử lại ({attempt}/5)...")
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
        last_warn = 0
        while self._spin_on:
            msg = self._status_msg or ""
            if not is_tty:
                # Không animate frame — CHÌ in lại khi trạng thái THAY ĐỔI.
                # Tránh "treo" hiện ra hàng trăm dòng lặp lại trong log/pipe.
                cur = re.sub(r"\x1b\[[0-9;]*m", "", msg).strip()
                if cur and cur != last_msg:
                    w = int(time.time() - self._status_since)
                    tag = f"  [{w}s]{'  ⏳ lâu quá — /stop nếu kẹt' if w >= config.TOOL_SLOW_WARN else ''}"
                    _p(f"  {cur}" + (tag if w >= 3 else ""), "dim")
                    last_msg = cur
                    last_warn = w
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
            sys.stdout.write("\r" + col + f + C["dim"] + base[:150] + C["reset"] + "\033[K")
            sys.stdout.flush()
            time.sleep(0.25)
            i += 1
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()

    def _clear_spin_line(self):
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
                self._live_n = 0
                self._live_kind = ""
                spin = threading.Thread(target=self._spinner, daemon=True)
                spin.start()
                try:
                    out = self._agent.run(payload)
                except Exception as e:
                    out = f"[LỖI] {type(e).__name__}: {e}"
                # TỰ ĐỘNG chạy tiếp nếu bị cắt giữa chừng (hết giờ/quota/bước) —
                # không bắt người dùng gõ 'tiếp tục'. /stop thì tôn trọng, không tự làm tiếp.
                auto_runs = 0
                quota_streak = 0
                while auto_runs < AUTO_RESUME_MAX:
                    pk = self._pause_kind(out)
                    if not pk or pk == "user":
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
                self._last_out = out
                self._spin_on = False
                self._busy = False
                spin.join(timeout=1)      # đợi spinner bỏ dòng cuối xong
                self._clear_spin_line()   # rồi mới in tránh bị đè "Rem>"
                # timeline tool ĐÃ in live khi từng tool xong ở _on_ev — không in lại nữa
                if out:
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
                    # Đáp án xong → gợi ý phím tắt kiểu opencode, giữ con trỏ tại ❯
                    self._footer_hints()
                    sys.stdout.write(C["bold"] + C["cy"] + "❯ " + C["reset"])
                    sys.stdout.flush()
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
        _p("Gõ /help | /status | /stop | /clear | /exit", "dim")
        if not groq.keys():
            _p(f"⚠  CHƯA CÓ GROQ KEY — gõ: /key gsk_...  để thêm", "rd")

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

    # ── /list: liệt kê macro/skill/chat cũ để chọn số mở ra ─────────────
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
            _p(
                "\n".join([
                    "/help    trợ giúp",
                    "/list    liệt kê macro/skill/chat cũ (gõ số để mở)",
                    "/rec     ghi thao tác desktop thành macro",
                    "/play <số>  phát lại macro trong /list",
                    "/resume <số>  mở lại đoạn chat cũ trong /list",
                    "/done    dừng ghi macro & lưu (khi đang ⏺ ghi)",
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
                    "/think   xem đầy đủ suy luận của lần trả lời cuối",
                    "/clear   xoá màn hình (hiện logo REM)",
                    "/checkupdate  kiểm tra bản mới trên GitHub",
                    "/update  tự cập nhật bản mới nhất (git/tarball)",
                    "/lsp <file>  kiểm tra lỗi file nguồn (clangd/pylsp)",
                    "/mcp     xem / nạp lại MCP server ngoài (~/.rem_ai/mcp.json)",
                    "/init    tạo AGENTS.md cho thư mục đang làm việc",
                    "/keys    xem số Groq keys",
                    "/key gsk_...  thêm Groq key",
                    "/exit    thoát",
                ]), "dim",
            )
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
        elif cmd == "/think":
            if self._last_think.strip():
                _type(render.thinking_to_ansi(self._last_think, full=True), None)
            else:
                _p("(chưa có suy luận nào để xem — câu trả lời không dùng thẻ thinking)", "dim")
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
        elif cmd == "/exit" or cmd == "/quit":
            return False
        else:
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
        _p("/list danh mục · /new chat mới · /stop dừng · /exit thoát", "dim")

    def _prompt_hint(self):
        # Opencode-style prompt: dấu ❯ nổi bật + ⏺ khi đang ghi macro
        rec = (C["rd"] + "⏺REC " + C["reset"]) if self.rec_mode else ""
        if self._busy:
            return rec + C["dim"] + "⏳ " + C["reset"] + C["bold"] + C["cy"] + "❯ " + C["reset"]
        if self._pending:
            return rec + C["dim"] + f"⏳({self._pending}) " + C["reset"] + C["bold"] + C["cy"] + "❯ " + C["reset"]
        return rec + C["bold"] + C["cy"] + "❯ " + C["reset"]

    def run(self):
        self._clear()
        if not groq.keys():
            _p(f"⚠  CHƯA CÓ GROQ KEY — gõ: /key gsk_...  để thêm", "rd")
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
            _p(f"Đang khởi chạy extensions...", "dim")
        else:
            try:
                self._header_box()
            except Exception:
                pass
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
            # Gõ số trần = mở mục trong /list gần nhất (vd "2" mở mục số 2)
            if re.fullmatch(r"\d+", line):
                if self._list_items:
                    self._list_open(line)
                else:
                    _p("Gõ /list trước để xem danh sách rồi chọn số.", "dim")
                continue
            # Opencode-style: highlight user input, show as "User" block
            sys.stdout.write("\033[1A\r\033[2K")
            sys.stdout.write(C["lm"] + C["bold"] + "User" + C["reset"] + C["lm"] + "> " + C["reset"] + C["wh"] + line + C["reset"] + "\n")
            sys.stdout.flush()
            if self._busy:
                self._pending += 1
                _p(f"⏳ Câu hỏi đã xếp hàng (#{self._pending}). Agent sẽ trả lời sau lượt hiện tại — gõ /stop để dừng.",
                   "ye")
            self.q.put(("task", line))