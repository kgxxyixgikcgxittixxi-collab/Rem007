import subprocess, os, sys, time, threading, queue, re, json

import config
import sessions
import updater
import render
import themes
from mcplib import atomic_write_json
from agentloop import Agent
from permissions import Presets
from providers import groq

_OUT_LOCK = threading.RLock()
# Repl đang nhận phím (để _p/_type/spinner không \r\K xóa dòng đang gõ gây mất chữ).
_ACTIVE_REPL = None
# True khi worker đang in đáp án nguyên khối → _p/_type bên trong chỉ ghi, không xóa/vẽ lại input.
_HOLD_REDRAW = False


def _input_state():
    """Trả về Repl đang ở trong input() — None nếu không ai đang gõ."""
    try:
        r = _ACTIVE_REPL
        if r is not None and getattr(r, "_in_input", False):
            return r
    except Exception:
        pass
    return None


def _prompt_rows(prompt_plain, buf_text, tw):
    """Số hàng vật lý của (prompt nhiều dòng + buffer đang gõ)."""
    try:
        tw = int(tw or 90) or 90
        parts = (prompt_plain or "").split("\n")
        rows = 0
        for pl in (parts[:-1] if parts else []):
            rows += max(1, (render.disp_len(pl) + tw - 1) // tw)
        last = (parts[-1] if parts else "") + (buf_text or "")
        rows += max(1, (render.disp_len(last) + tw - 1) // tw)
        return max(1, rows)
    except Exception:
        return 1


def _erase_input_locked(r):
    """Xóa sạch dòng input đang gõ (đúng số hàng wrap) — gọi khi đã giữ _OUT_LOCK."""
    try:
        tw = render.term_width() or 90
        pp = re.sub(r"\x1b\[[0-9;]*m", "", str(getattr(r, "_input_prompt", "") or ""))
        bb = "".join(getattr(r, "_input_buf", []) or [])
        rows = _prompt_rows(pp, bb, tw)
        sys.stdout.write("\r\033[2K")
        for _ in range(rows - 1):
            sys.stdout.write("\033[1A\033[2K")
        sys.stdout.write("\r")
        sys.stdout.flush()
    except Exception:
        pass


def _redraw_input_locked(r):
    """Vẽ lại prompt + ký tự đã gõ sau khi in output chen ngang — gọi khi giữ _OUT_LOCK."""
    try:
        sys.stdout.write(str(getattr(r, "_input_prompt", "") or "")
                         + "".join(getattr(r, "_input_buf", []) or []))
        sys.stdout.flush()
    except Exception:
        pass
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

# ── Theme engine: MỘT hệ duy nhất là themes.py (tui.json, kiểu opencode).
# Theme cũ (config.THEMES/rem_theme: default/ocean/sunset/mono) được migrate
# 1 lần sang tui.json rồi bỏ — tránh 2 hệ cùng vá dict C[] giẫm nhau.
_THEMES_PATH = os.path.join(config.DIR, "rem_theme")
def _apply_theme_c():
    try:
        if os.path.isfile(_THEMES_PATH) and not themes.load_tui().get("theme"):
            old = open(_THEMES_PATH, encoding="utf-8").read().strip()
            if old in themes.list_themes():
                themes.save_tui({"theme": old})
            try:
                os.remove(_THEMES_PATH)
            except Exception:
                pass
    except Exception:
        pass
    try:
        _patch = themes.apply()
        if isinstance(_patch, dict):
            C.update(_patch)
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
    "dl_clipboard": "Clip", "dl_open": "Open", "dl_focus": "Focus",
    "dl_wait": "WaitEl", "dl_screenshot": "DeskShot",
    "browser_snapshot": "Snap", "browser_fill_login": "Login",
    "browser_tabs": "Tabs", "browser_wait_text": "WaitTxt",
    "rec_start": "Rec", "rec_stop": "RecStop", "rec_list": "RecList",
    "rec_show": "RecShow", "rec_play": "RecPlay", "rec_delete": "RecDel",
    "browser_status": "BStatus", "cwd": "Cwd", "chdir": "Cd",
    "lsp_supported": "LSPSup", "lsp_diagnostics": "LSPDiag", "lsp_definition": "LSPDef",
    "lsp_references": "LSPRef", "lsp_symbols": "LSPSym", "lsp_hover": "LSPHover",
    "media_status": "MediaSt",
    "voice_status": "Mic", "voice_listen": "Nghe", "voice_cmd": "LenhNoi",
    "voice_say": "Noi",
}


MACRO_CATS_FALLBACK = ("van-phong", "trinh-duyet", "he-thong", "giai-tri",
                       "mang-xa-hoi", "khac")

# Registry lệnh / TẬP TRUNG 1 nơi (xem _SLASH_CAT bên dưới: vừa autocomplete,
# vừa catalog cho /palette và /help nhóm theo mục).


def _attention_notify(title="Rem xong việc", msg=""):
    """Nếu themes.attention_enabled(): kêu '\a' + notify-send (best-effort, không crash)."""
    try:
        try:
            on = themes.attention_enabled()
        except Exception:
            on = False
        if not on:
            return
    except Exception:
        return
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        subprocess.run(["notify-send", str(title or "Rem")[:120], str(msg or "")[:200]],
                       timeout=3, capture_output=True)
    except Exception:
        pass


def _fuzzy_find(q):
    """Fuzzy tìm file trong cwd cho @đường_dẫn. Trả về filepath hoặc None."""
    import glob as _glob
    q = (q or "").strip().strip("'\"")
    q = re.sub(r"[,\\.;:\\)\\]]+$", "", q)
    if not q:
        return None
    try:
        p = os.path.expanduser(q)
        if os.path.isfile(p):
            return p
        if os.path.isfile(os.path.join(os.getcwd(), q)):
            return os.path.join(os.getcwd(), q)
    except Exception:
        pass
    try:
        base = os.path.basename(q)
        for pat in (q, "**/*" + base + "*"):
            try:
                hits = _glob.glob(os.path.join(os.getcwd(), pat), recursive=True)
            except Exception:
                continue
            hits = [h for h in hits if os.path.isfile(h)]
            if hits:
                hits.sort(key=lambda h: (len(h), h))
                return hits[0]
    except Exception:
        pass
    try:
        ql = os.path.basename(q).lower()
        best = None
        scanned = 0
        for root, dirs, files in os.walk(os.getcwd()):
            try:
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".git")]
            except Exception:
                pass
            for fn in files:
                if ql in fn.lower():
                    fp = os.path.join(root, fn)
                    if best is None or len(fp) < len(best):
                        best = fp
                    scanned += 1
                    if scanned > 50:
                        return best
            scanned += 1
            if scanned > 2000:
                break
        return best
    except Exception:
        return None


# @agent mentions kiểu opencode: gọi agent con / chuyển preset ngay trong tin nhắn.
_AGENT_MENTIONS = {"explore", "general", "plan", "build"}


def _expand_mentions(text):
    """Chèn nội dung file cho mỗi @đường_dẫn (tối đa ~2000 ký tự mỗi file).
    Bỏ qua @agent (explore/general/plan/build) — luồng chính xử lý riêng."""
    try:
        toks = re.findall(r"@(\S+)", text or "")
        if not toks:
            return text
        out = text
        for tok in toks:
            if tok.lower().rstrip(",.;:)]}") in _AGENT_MENTIONS:
                continue
            fp = _fuzzy_find(tok)
            if not fp:
                continue
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(2000)
                snippet = f"\n--- @{tok} -> {fp} ---\n{content}\n--- end ---\n"
                out = out.replace("@" + tok, snippet, 1)
            except Exception:
                continue
        return out
    except Exception:
        return text


def _custom_dirs():
    return [os.path.join(os.path.expanduser("~"), ".rem_ai", "commands"),
            os.path.join(os.getcwd(), ".opencode", "commands"),
            os.path.join(os.path.expanduser("~"), ".config", "opencode", "commands")]


def _load_custom_commands():
    """Load *.md custom commands (frontmatter description/agent/model/subtask/template)
    + mục 'command' trong opencode.json (global ~/.config/opencode + project .opencode).
    Thứ tự thắng: project > global > file md (giống opencode)."""
    cmds = {}
    for d in _custom_dirs():
        try:
            files = sorted(os.listdir(d))
        except Exception:
            continue
        for fn in files:
            if not fn.endswith(".md"):
                continue
            name = fn[:-3]
            if name in cmds:
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    raw = f.read()
            except Exception:
                continue
            desc, agent, model, subtask, tmpl = "", "", "", False, raw
            if raw.startswith("---"):
                try:
                    p2 = raw.split("---", 2)
                    if len(p2) >= 3:
                        fm, tmpl = p2[1], p2[2].lstrip("\n")
                        for ln in fm.splitlines():
                            if ":" in ln:
                                k, v = ln.split(":", 1)
                                k = k.strip().lower()
                                v = v.strip().strip("'\"")
                                if k == "description":
                                    desc = v
                                elif k == "agent":
                                    agent = v
                                elif k == "model":
                                    model = v
                                elif k == "subtask":
                                    subtask = v.lower() in ("true", "1", "yes")
                except Exception:
                    pass
            cmds[name] = {"description": desc, "agent": agent, "model": model,
                          "subtask": subtask, "template": tmpl,
                          "path": os.path.join(d, fn)}
    # Merge opencode.json "command" (ghi đè file md trùng tên, kiểu opencode)
    for _cfg in (os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.json"),
                 os.path.join(os.path.expanduser("~"), ".config", "opencode", "opencode.jsonc"),
                 os.path.join(os.getcwd(), ".opencode", "opencode.json"),
                 os.path.join(os.getcwd(), ".opencode", "opencode.jsonc")):
        try:
            with open(_cfg, encoding="utf-8") as f:
                _raw = f.read()
            import re as _re
            _raw = _re.sub(r"//[^\n]*", "", _raw)  # jsonc: bỏ comment //
            _data = json.loads(_raw) or {}
            _cc = _data.get("command") or {}
            if isinstance(_cc, dict):
                for _n, _c in _cc.items():
                    if not isinstance(_c, dict):
                        continue
                    cmds[str(_n)] = {
                        "description": str(_c.get("description") or ""),
                        "agent": str(_c.get("agent") or ""),
                        "model": str(_c.get("model") or ""),
                        "subtask": bool(_c.get("subtask", False)),
                        "template": str(_c.get("template") or ""),
                        "path": _cfg,
                    }
        except Exception:
            continue
    return cmds


def _expand_custom_template(tmpl, argstr, arglist, manager=None):
    out = (tmpl or "").replace("$ARGUMENTS", argstr or "")
    for i, a in enumerate((arglist or [])[:9], 1):
        out = out.replace(f"${i}", a)

    def _repl(m):
        c = m.group(1)
        try:
            if manager is not None:
                try:
                    r = manager.call("bash", {"command": c}, timeout=30)
                    return str(r)[:2000]
                except Exception:
                    pass
            r = subprocess.run(c, shell=True, capture_output=True, text=True, timeout=30)
            o = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
            return o[:2000] or ""
        except Exception as e:
            return f"[LOI {e}]"
    try:
        out = re.sub(r"!`([^`]+)`", _repl, out)
    except Exception:
        pass
    try:
        out = _expand_mentions(out)
    except Exception:
        pass
    return out


def _import_session_file(fp):
    """Đọc JSON session opencode-style hoặc JSONL Rem → list msgs chuẩn {role,content}."""
    msgs = []
    with open(fp, "r", encoding="utf-8") as f:
        raw_txt = f.read()
    data = None
    try:
        data = json.loads(raw_txt)
    except Exception:
        data = None
    cands = []
    if isinstance(data, list):
        cands = data
    elif isinstance(data, dict):
        for k in ("messages", "msgs", "history", "data", "turns"):
            if isinstance(data.get(k), list):
                cands = data[k]
                break
        else:
            # thử JSONL từng dòng
            cands = []
            for ln in raw_txt.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    cands.append(json.loads(ln))
                except Exception:
                    continue
            if not cands:
                # dict đơn?
                if "role" in data or "content" in data or "text" in data:
                    cands = [data]
    else:
        for ln in raw_txt.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                cands.append(json.loads(ln))
            except Exception:
                continue
    for r in cands:
        if not isinstance(r, dict):
            continue
        role = r.get("role") or r.get("type") or "user"
        role = str(role).lower().strip()
        if role in ("human", "user_message"):
            role = "user"
        elif role in ("ai", "assistant_message", "model"):
            role = "assistant"
        if role not in ("user", "assistant", "tool", "system"):
            role = "user"
        content = r.get("content", r.get("text", r.get("body", "")))
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, str):
                    parts.append(p)
                elif isinstance(p, dict):
                    parts.append(str(p.get("text") or p.get("content") or p.get("input") or ""))
            content = "\n".join(x for x in parts if x)
        elif isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        content = str(content or "")
        if not content.strip() and not r.get("tool_calls"):
            continue
        m = {"role": role, "content": content}
        for k in ("tool_call_id", "name", "tool_calls"):
            if r.get(k) is not None:
                m[k] = r[k]
        msgs.append(m)
    return msgs


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
    "/thinking":     ("Hiển thị", "Hiện/ẩn khối suy luận khi trả lời", "[on|off]", ()),
    "/effort":       ("Cấu hình", "Đổi mức suy luận low|medium|high", "low|medium|high", ()),
    "/del":          ("Lịch sử", "Xoá 1 session cũ", "<id>", ()),
    "/auto":         ("Quyền hạn", "Tự động — không hỏi xác nhận", "", ()),
    "/safe":         ("Quyền hạn", "Hỏi xác nhận trước tool ghi/bash/fetch", "", ()),
    "/status":       ("Hiển thị", "Xem extension + tool + session", "", ()) ,
    "/stats":        ("Hiển thị", "Thống kê dùng: tin nhắn, tool, token", "", ()) ,
    "/todos":        ("Hiển thị", "Xem danh sách công việc (todo) agent đang theo dõi", "", ("/todo",)),
    "/compact":      ("Cấu hình", "Nén quy mô context thủ công (tóm tắt lịch sử cũ)", "", ("/summarize",)) ,
    "/summarize":    ("Cấu hình", "Ép tóm tắt context ngay (như /compact)", "", ()),
    "/model":        ("Cấu hình", "Chọn model chat cho phiên này", "<tên>", ("/m",)) ,
    "/sessions":     ("Lịch sử", "Liệt kê tất cả session cũ", "", ()),
    "/continue":     ("Lịch sử", "Tiếp tục session cũ", "<id>", ()),
    "/rename":       ("Lịch sử", "Đặt tên session hiện tại", "<tên>", ()),
    "/fork":         ("Lịch sử", "Nhân bản session hiện tại thành mới", "", ()),
    "/undo":         ("Lịch sử", "Lùi 1 turn (kèm hoàn tác file, cần git)", "", ()),
    "/redo":         ("Lịch sử", "Làm lại turn vừa undo (kèm file)", "", ()),
    "/new":          ("Cơ bản", "Tạo session mới", "", ()),
    "/palette":      ("Cơ bản", "Bảng lệnh tìm nhanh (giống opencode command palette)", "", ()) ,
    "/theme":        ("Cấu hình", "Đổi bảng màu giao diện", "tên (nhập trống để xem)", ("/themes",)),
    "/details":      ("Hiển thị", "Bật/tắt chi tiết tool (3 dòng đầu result)", "[on|off]", ()),
    "/plan":         ("Quyền hạn", "Chuyển preset PLAN (chỉ đọc)", "", ()),
    "/build":        ("Quyền hạn", "Quay lại preset BUILD", "", ("/agent",)),
    "/lsp":          ("Phát triển", "Kiểm tra lỗi file nguồn (clangd/pylsp)", "<file>", ()) ,
    "/mcp":          ("Hệ thống", "Xem / nạp lại MCP server ngoài", "reload", ()),
    "/init":         ("Cơ bản", "Tạo AGENTS.md cho thư mục đang làm việc", "", ()),
    "/keys":         ("Cấu hình", "Xem số Groq keys", "", ()),
    "/key":          ("Cấu hình", "Thêm Groq key", "gsk_...", ()),
    "/connect":      ("Cấu hình", "Thêm provider/key (chọn Groq, dán key)", "", ()),
    "/checkupdate":  ("Hệ thống", "Kiểm tra bản mới trên GitHub", "", ()),
    "/update":       ("Hệ thống", "Tự cập nhật bản mới nhất", "", ()),
    "/overlay":      ("Cấu hình", "Cửa sổ nổi hiện việc đang làm", "on|off|status", ()),
    "/export":       ("Cơ bản", "Xuất đoạn chat hiện tại ra markdown", "", ()),
    "/editor":       ("Cơ bản", "Mở $EDITOR soạn tin rồi gửi", "", ()),
    "/share":        ("Cơ bản", "Export markdown local (~/.rem_ai/exports/)", "", ()),
    "/unshare":      ("Cơ bản", "Gỡ share local", "", ()),
    "/import":       ("Cơ bản", "Nhập JSON opencode hoặc JSONL Rem", "<file>", ()),
    "/voice":        ("Cơ bản", "Mic/STT/TTS tiếng Việt", "[status|nghe|nói|...]", ()),
    "/keybinds":     ("Cơ bản", "Bảng phím tắt", "", ()),
    "/q":            ("Cơ bản", "Thoát (như /exit)", "", ()),
    "/exit":         ("Cơ bản", "Thoát", "", ("/quit",)),
}
_SLASH = [cmd for cmd in _SLASH_CAT]
# Kèm alias để autocomplete/gợi ý/so khớp lệnh `/...` cũng trúng
# (vd /quit, /themes, /ls, /todo, /m, /summarize).
_SLASH += [a for _c in _SLASH_CAT.values() for a in _c[3]
           if isinstance(a, str) and a.startswith("/") and a not in _SLASH]
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
        r = _input_state()
        if r is not None and not _HOLD_REDRAW:
            # Đang gõ → xóa đúng số hàng input, in output, vẽ lại buffer (không mất chữ).
            _erase_input_locked(r)
            sys.stdout.write(C.get(col, "") + str(s) + C["reset"] + end)
            sys.stdout.flush()
            _redraw_input_locked(r)
        else:
            if r is None:
                sys.stdout.write("\r\033[K")
            sys.stdout.write(C.get(col, "") + str(s) + C["reset"] + end)
            sys.stdout.flush()
    _redisplay_input()


def _strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def _type(s, col=None):
    """In đáp án NHANH (không typewriter): gõ chữ nào hiện chữ đó theo màu sẵn có.
    Trước đây sleep từng token (T=0.015s/từ) GIỮ KHÓA _OUT_LOCK — vừa chậm vừa làm
    chữ user gõ bị nhịn (phải chờ Enter mới hiện). Giờ in tức thì, bỏ hẹn giờ."""
    if not sys.stdin.isatty() or config.DEBUG:
        _p(_strip_ansi(s), col if col is not None else "")
        return
    # Gọn: cắt dòng trắng thừa ở cuối đáp án (model hay trả "\n\n" cuối câu
    # → hiện thành 2-3 dòng trắng trống dưới mỗi câu trả lời).
    s = re.sub(r"(\n\s*)+\Z", "", s)
    if not s.strip():
        return
    text = s if col is None else (C.get(col, "") + s + C["reset"])
    with _OUT_LOCK:
        global _HOLD_REDRAW
        r = _input_state()
        hold = _HOLD_REDRAW
        if r is not None and not hold:
            # Đang gõ → đẩy input lên trên, in đáp án ở dòng mới, xong vẽ lại input.
            _erase_input_locked(r)
        elif not hold:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        sys.stdout.write("\033[?25l")
        try:
            sys.stdout.write(text)
        except Exception:
            sys.stdout.write(_strip_ansi(text))
        finally:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()
        sys.stdout.write("\n")
        sys.stdout.flush()
        if r is not None and not hold:
            _redraw_input_locked(r)
    _redisplay_input()


def _klines(logo, colors):
    out = []
    for i, ln in enumerate(logo.strip("\n").split("\n")):
        col = colors[i % len(colors)]
        out.append(C.get(col, "") + ln + C["reset"])
    return "\n".join(out)


def _logo_banner():
    # opencode-style: banner gọn — chỉ hiện logo lớn khi terminal đủ rộng,
    # còn lại trả về dòng tiêu đề tối giản (tránh nối mòn giao diện cũ chiếm nửa màn hình)
    try:
        import shutil as _sh
        tw = _sh.get_terminal_size().columns
    except Exception:
        tw = 80
    if tw >= 78:
        logo = _klines(config.LOGO, ["rd", "ye", "gr", "cy", "mg", "bl"])
        return logo
    # màn hình hẹp → chỉ 1 dòng opencode-style title
    return C["dim"] + "─ " + C["bold"] + C["cy"] + f"◆ REM v{config.VERSION}" + C["reset"] + C["dim"] + " · opencode edition" + C["reset"]


def _tool_title(ev):
    """Tiêu đề tool kiểu opencode: '⏺ Bash · $ ls -la' (giữ ngắn, dim phần args)."""
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
                    note = str(v).strip().split("\n")[0]
                    break
    label = _TOOL_LABEL.get(name, name)
    if name == "bash" and note:
        note = "$ " + note
    # opencode cắt args còn ~64 ký tự, dim phần sau
    note = (note or "")[:64]
    return f"{label}  ·  {note}" if note else label


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
        self._input_buf = []        # buffer ký tự đang gõ (để _p vẽ lại khi chen ngang)
        self._input_prompt = ""     # prompt hiện tại (để _p vẽ lại khi chen ngang)
        self._last_esc = 0.0      # mốc ESC gần nhất (ESC đúp ≤0.8s = dừng cứng kiểu opencode)
        self.verbose = False        # /details: khi bật in thêm 3 dòng đầu result tool
        self._verbose = False       # alias tương thích
        self.show_thinking = True   # /thinking: hiện khối suy luận khi agent trả lời (kiểu opencode)
        self._hist = self._hist_load()  # lịch sử lệnh (↑/↓) kiểu opencode
        self._hist_i = None         # vị trí đang xem trong history (None = đang gõ mới)
        self._hist_draft = ""       # dòng đang gõ dở trước khi bấm ↑
        try:
            # Áp theme lúc khởi động (chỉ đổi màu UI dict C, GIỮ NGUYÊN logo REM)
            _patch = themes.apply()
            if isinstance(_patch, dict):
                C.update(_patch)
        except Exception:
            pass
        global _ACTIVE_REPL
        _ACTIVE_REPL = self
        self._mk_agent()

    def _hist_load(self):
        """Đọc lịch sử lệnh (~/.rem_ai/history, tối đa 200 dòng cuối)."""
        try:
            fp = os.path.join(config.DIR, "history")
            with open(fp, encoding="utf-8") as f:
                return [ln.rstrip("\n") for ln in f if ln.strip()][-200:]
        except Exception:
            return []

    def _hist_push(self, line):
        """Lưu 1 lệnh vào history (bỏ trùng liên tiếp, tối đa 500)."""
        try:
            line = (line or "").strip()
            if not line:
                return
            if self._hist and self._hist[-1] == line:
                return
            self._hist.append(line)
            self._hist = self._hist[-500:]
            fp = os.path.join(config.DIR, "history")
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(self._hist[-500:]) + "\n")
            os.replace(tmp, fp)
        except Exception:
            pass
        finally:
            try:
                self._hist_i = None
                self._hist_draft = ""
            except Exception:
                pass

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
            with _OUT_LOCK:
                sys.stdout.write(prompt)
                sys.stdout.flush()
        except Exception:
            pass
        buf = []
        self._input_buf = buf
        self._input_prompt = prompt
        try:
            fd = sys.stdin.fileno()
            old = _tm.tcgetattr(fd)
        except Exception:
            try:
                return input("").strip()
            except Exception:
                return ""
        try:
            # RAW (không phải cbreak): tắt ISIG để Ctrl+C thành byte \x03 cho code
            # tự xử lý (đang gõ → xóa dòng; rảnh → thoát). cbreak giữ ISIG nên
            # SIGINT giáng thẳng xuống process, nhánh "\x03" thành dead code và
            # đang gõ dở vẫn bị thoát oan.
            _ty.setraw(fd)
            import codecs as _cd
            _dec = _cd.getincrementaldecoder("utf-8")("replace")

            def _rd1(_wait=0.0):
                """Đọc 1 ký tự từ fd bằng os.read (KHÔNG qua buffer của sys.stdin).
                Bắt buộc: select()+sys.stdin.read() kẹt khi wrapper đã nuốt chunk
                vào buffer (select trên fd không thấy) → treo khi gõ nhanh/paste.
                Ghép byte UTF-8 dần (tiếng Việt 2-3 bytes)."""
                _ch = ""
                for _k in range(4):
                    if _k and _wait >= 0:
                        try:
                            if not _sel.select([fd], [], [], max(0.0, _wait or 0.05))[0]:
                                break
                        except Exception:
                            break
                    try:
                        _b = os.read(fd, 1)
                    except Exception:
                        break
                    if not _b:
                        break
                    try:
                        _ch += _dec.decode(_b)
                    except Exception:
                        break
                    if _ch:
                        break
                if not _ch:
                    try:
                        _dec.reset()
                    except Exception:
                        pass
                return _ch

            while True:
                # CHỐNG KẸT CHỮ (gõ bị dính tới khi Enter): echo KHÔNG khóa _OUT_LOCK.
                # Worker (spinner/_p/_type) giữ khóa luân phiên → nếu echo đợi lock,
                # phím gõ bị nhịn không hiện tới khi Enter nhả lock. Echo lock-free
                # giúp chữ hiện NGAY; khi worker vẽ/redraw đè thì nó tự vẽ lại buf.
                try:
                    rl, _, _ = _sel.select([fd], [], [], 0.1)
                except Exception:
                    rl = [fd]
                if not rl:
                    continue
                try:
                    ch = _rd1()
                except Exception:
                    continue
                if not ch:
                    continue
                if not ch:
                    continue
                if ch in ("\r", "\n"):
                    try:  # lock-free: Enter không đợi _OUT_LOCK (tránh kẹt khi worker đang in)
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                    except Exception:
                        pass
                    _txt = "".join(buf).strip()
                    if _txt:
                        _save_history(_txt)
                    return _txt
                if ch == "\x03":  # Ctrl+C
                    if buf:
                        # Đang gõ dở → xóa dòng (kiểu opencode), KHÔNG thoát.
                        # (Bản cũ thoát luôn cả khi đang gõ dở.)
                        try:
                            with _OUT_LOCK:
                                _erase_input_locked(self)
                                del buf[:]
                                self._hist_i = None
                                _redraw_input_locked(self)
                        except Exception:
                            pass
                        continue
                    with _OUT_LOCK:
                        try:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                        except Exception:
                            pass
                    raise KeyboardInterrupt
                if ch == "\x04":  # Ctrl+D
                    if not buf:
                        with _OUT_LOCK:
                            try:
                                sys.stdout.write("\n")
                                sys.stdout.flush()
                            except Exception:
                                pass
                        raise EOFError
                    continue
                if ch in ("\x7f", "\x08"):  # Backspace
                    with _OUT_LOCK:
                        if buf:
                            buf.pop()
                            # opencode-style: vẽ lại CẢ buffer sau mỗi phím (xóa cũ + in list)
                            # — không gõ lẻ từng ký tự để khỏi lệch cursor ở mép wrap → dính dòng
                            _erase_input_locked(self)
                            _redraw_input_locked(self)
                    continue
                if ch == "\t":  # TAB — autocomplete lệnh / (kiểu opencode)
                    cur = "".join(buf).strip()
                    if cur.startswith("/"):
                        sugs = _palette_suggest(cur.lstrip("/")) or [c for c in _SLASH if c.startswith(cur)]
                        if sugs:
                            fill = sugs[0]
                            if len(sugs) > 1:
                                # Nhiều khớp → hiện gợi ý bằng _p (xóa/vẽ lại input
                                # đúng số hàng, không in tay "\n" gây dính chữ).
                                extra = "   ".join(f"{' '.join(s.split())}" for s in sugs[:4])
                                _p(extra, "dim")
                            rm = len(buf)
                            try:
                                sys.stdout.write("\b \b" * rm)
                            except Exception:
                                pass
                            buf[:] = list(fill)
                            sys.stdout.write(fill)
                            sys.stdout.flush()
                        continue
                    # Tab kiểu opencode: dòng trống → chuyển agent PLAN ↔ BUILD.
                    # Phân biệt paste (còn ký tự chờ sau Tab, hoặc đang gõ dở)
                    # → chèn spaces giữ nguyên liệu, không chuyển oan.
                    # VẼ LẠI CÙNG DÒNG (không in dòng mới — pill [plan]/[build]
                    # trong prompt cho thấy đã chuyển).
                    try:
                        _more = bool(_sel.select([fd], [], [], 0.02)[0])
                    except Exception:
                        _more = False
                    if _more or buf:
                        for _sp in "  ":
                            buf.append(_sp)
                            try:
                                sys.stdout.write(_sp)
                                sys.stdout.flush()
                            except Exception:
                                pass
                        continue
                    try:
                        with _OUT_LOCK:
                            _erase_input_locked(self)
                            try:
                                if getattr(self, "presets", "build") == "plan":
                                    self._agent.perm = Presets.build()
                                    self.presets = "build"
                                else:
                                    self._agent.perm = Presets.plan()
                                    self.presets = "plan"
                            except Exception:
                                pass
                            try:
                                self._input_prompt = self._prompt_hint()
                            except Exception:
                                pass
                            self._hist_i = None
                            _redraw_input_locked(self)
                    except Exception:
                        pass
                    continue
                if ch == "\x1b":  # ESC — phân biệt ESC lẻ vs phím mũi tên
                    try:
                        rl2, _, _ = _sel.select([fd], [], [], 0.05)
                    except Exception:
                        rl2 = []
                    if rl2:
                        # Escape sequence: đọc thêm để phân biệt ↑/↓ (history)
                        # với phím khác. Không đọc được → nuốt như cũ.
                        _seq = ""
                        try:
                            for _ in range(3):
                                if _sel.select([fd], [], [], 0.03)[0]:
                                    _seq += _rd1()
                                else:
                                    break
                        except Exception:
                            pass
                        if _seq in ("[A", "[B"):
                            # ↑/↓ kiểu opencode: lùi/tới lịch sử lệnh.
                            # (Bản cũ nuốt hết phím mũi tên dù /keybinds vẫn ghi
                            # "↑/↓ lịch sử lệnh".)
                            try:
                                with _OUT_LOCK:
                                    _hist = getattr(self, "_hist", []) or []
                                    if _hist:
                                        _i = getattr(self, "_hist_i", None)
                                        if _seq == "[A":
                                            if _i is None:
                                                try:
                                                    self._hist_draft = "".join(buf)
                                                except Exception:
                                                    self._hist_draft = ""
                                                _i = len(_hist) - 1
                                            else:
                                                _i = max(0, _i - 1)
                                            _erase_input_locked(self)
                                            del buf[:]
                                            buf.extend(list(_hist[_i]))
                                            self._hist_i = _i
                                            _redraw_input_locked(self)
                                        else:
                                            if _i is not None:
                                                _erase_input_locked(self)
                                                if _i + 1 >= len(_hist):
                                                    del buf[:]
                                                    try:
                                                        buf.extend(list(self._hist_draft or ""))
                                                    except Exception:
                                                        pass
                                                    self._hist_i = None
                                                else:
                                                    _i = _i + 1
                                                    del buf[:]
                                                    buf.extend(list(_hist[_i]))
                                                    self._hist_i = _i
                                                _redraw_input_locked(self)
                            except Exception:
                                pass
                            continue
                        # Phím khác (←/→/F-key/Alt...) → nuốt hết, không chèn rác
                        try:
                            while _sel.select([fd], [], [], 0.02)[0]:
                                os.read(fd, 32)
                        except Exception:
                            pass
                        continue
                    now = time.time()
                    double = (now - (self._last_esc or 0)) <= 0.8
                    self._last_esc = now
                    if self._busy:
                        self._request_stop(hard=double)
                        buf.clear()
                        try:
                            with _OUT_LOCK:
                                sys.stdout.write("\n")
                                sys.stdout.flush()
                                sys.stdout.write(prompt)
                                sys.stdout.flush()
                        except Exception:
                            pass
                        continue
                    # rảnh: ESC = xóa dòng (opencode) rồi gõ tiếp
                    if buf:
                        buf.clear()
                        try:
                            with _OUT_LOCK:
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
                    with _OUT_LOCK:
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
            row = ("  ✓ " if ok else "  ✗ ") + C["gr" if ok else "rd"] + label + C["reset"] + C["dim"] + f" · {dt:.1f}s" + C["reset"]
            self._tool_rows.append((row, not ok))
            if len(self._tool_rows) > 14:
                self._tool_rows.pop(0)
            _p(row, "gr" if ok else "rd")
            # /details: khi bật in thêm 3 dòng đầu result tool
            try:
                if getattr(self, "verbose", False) or getattr(self, "_verbose", False):
                    for _ln in str(r or "").strip().splitlines()[:3]:
                        _p("  │ " + _ln[:160], "dim")
            except Exception:
                pass
            # Diff cũ/mới kiểu opencode: hiện ngay dưới dòng ✓ khi sửa/tạo file.
            # Tôn trọng tui.json diff_style: stacked = luôn 1 cột, side = 2 cột
            # khi đủ rộng, auto = theo độ rộng terminal.
            if ok and ev.get("name") in ("edit_file", "write_file", "apply_patch") and ev.get("full"):
                try:
                    try:
                        _ds = themes.diff_style()
                    except Exception:
                        _ds = "auto"
                    _wide = render.term_width() >= 100
                    if _ds == "stacked" or (_ds == "auto" and not _wide):
                        d = render.diff_to_ansi(ev.get("full"))
                    else:
                        d = render.side_diff_to_ansi(ev.get("full")) if _wide else render.diff_to_ansi(ev.get("full"))
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
            r = _input_state()
            if r is not None:
                pass  # đang gõ → _p đã vẽ lại input, không xóa đè
            else:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()

    def _clear_spin_line(self):
        with _OUT_LOCK:
            if _input_state() is not None:
                return  # đang gõ → không \r\K xóa dòng input (gây mất chữ)
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
                    global _HOLD_REDRAW
                    with _OUT_LOCK:
                        r_out = _input_state()
                        if r_out is not None:
                            _erase_input_locked(r_out)
                        _HOLD_REDRAW = True
                        sys.stdout.write(P_AGENT)
                        sys.stdout.flush()
                    try:
                        think, body = render.split_thinking(out)
                        self._last_think = think
                        # Opencode-style thinking block (tắt bằng /thinking)
                        if think.strip() and self.show_thinking:
                            _type(render.thinking_to_ansi(think, full=False), None)
                        # Body — render markdown sạch (opencode-style)
                        if body.strip():
                            _type(render.md_to_ansi(body), None)
                        # Footer usage kiểu opencode: token lượt này + tổng phiên.
                        try:
                            _u = self._last_turn_use or {}
                            _t = groq.session_usage()
                            if _u.get("prompt") or _u.get("completion"):
                                _p(f"◆ ↑{_u.get('prompt', 0)} ↓{_u.get('completion', 0)} · "
                                   f"{self._last_turn_secs:.0f}s · "
                                   f"∑↑{_t.get('prompt', 0)} ↓{_t.get('completion', 0)}", "dim")
                        except Exception:
                            pass
                        # Đáp án xong → gợi ý phím tắt kiểu opencode.
                        # KHÔNG in "❯ " tay ở đây — vòng input() kế tiếp sẽ in prompt
                        # (in tay gây double prompt "❯ ❯" và dính chữ như log lỗi).
                        self._footer_hints()
                    finally:
                        with _OUT_LOCK:
                            _HOLD_REDRAW = False
                            if r_out is not None:
                                _redraw_input_locked(r_out)
                            sys.stdout.flush()
                try:
                    _overlay_write(mode="XONG", tool="", progress="xong — lần sau phát lại nhanh")
                except Exception:
                    pass
                try:
                    _attention_notify("Rem xong việc", (payload or "")[:120])
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
            try:
                title = sessions.get_title(sid)
            except Exception:
                title = first
            _p(f"{mark} {sid}  ({t})  {title[:60]}", "cy")
        _p("Gõ /sessions <id> để chuyển · /rename <tên> đặt tên · /fork nhân bản", "dim")

    def _switch_session(self, sid):
        """Chuyển sang session sid (kiểm tra tồn tại). Trả True nếu chuyển được."""
        sid = (sid or "").strip()
        if not sid:
            return False
        try:
            ok = any(s == sid for s, _, _ in sessions.list_all())
        except Exception:
            ok = False
        if not ok:
            # cho phép id rút gọn (tiền tố duy nhất)
            try:
                cands = [s for s, _, _ in sessions.list_all() if s.startswith(sid)]
                if len(cands) == 1:
                    sid = cands[0]
                    ok = True
            except Exception:
                pass
        if not ok:
            _p(f"Không thấy session '{sid}'. Gõ /sessions để xem.", "ye")
            return False
        self.sid = sid
        self.rec_mode = ""
        self._mk_agent(sid=self.sid)
        _p(f"Đã chuyển session → {sid}", "gr")
        return True

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

    # ── /theme: đổi bảng màu giao diện (lưu vào tui.json, kiểu opencode) ──
    def _theme(self, name):
        name = (name or "").strip()
        if not name:
            try:
                names = themes.list_themes()
                cur = themes.current_theme()
            except Exception:
                _p("[LOI] không đọc được danh sách theme", "rd")
                return
            for n in names:
                mark = "*" if n == cur else " "
                _p(f"{mark} {n}", "cy" if n == cur else "dim")
            _p("Cách dùng: /theme <tên>   (vd /theme tokyonight)", "dim")
            return
        try:
            if name not in themes.list_themes():
                _p(f"Không có theme '{name}'. Có: {', '.join(themes.list_themes())}", "ye")
                return
            if themes.save_tui({"theme": name}):
                try:
                    _patch = themes.apply(name)
                    if isinstance(_patch, dict):
                        C.update(_patch)
                except Exception:
                    pass
                _p(f"Đã chuyển theme → {name}.", "gr")
            else:
                _p(f"[LOI] không lưu được theme '{name}'", "rd")
        except Exception as e:
            _p(f"[LOI] theme: {type(e).__name__}: {e}", "rd")

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

    def _run_agent_mention(self, line):
        """Xử lý @agent kiểu opencode. Trả None nếu đã tiêu thụ, str để gửi tiếp."""
        toks = [t.lower().rstrip(",.;:)]}") for t in re.findall(r"@(\S+)", line or "")]
        hits = [t for t in toks if t in _AGENT_MENTIONS]
        if not hits:
            return line
        # @plan/@build: chuyển preset rồi gửi phần còn lại như tin thường
        if "plan" in hits:
            try:
                self._agent.perm = Presets.plan()
                self.presets = "plan"
                _p("(preset PLAN cho tin này — tool ghi/bash bị cấm)", "dim")
            except Exception:
                pass
            line = re.sub(r"@plan\b", "", line, flags=re.I).strip()
            hits = [h for h in hits if h != "plan"]
        if "build" in hits:
            try:
                self._agent.perm = Presets.build()
                self.presets = "build"
            except Exception:
                pass
            line = re.sub(r"@build\b", "", line, flags=re.I).strip()
            hits = [h for h in hits if h != "build"]
        if not hits:
            return line
        # @explore/@general: agent con chạy đồng bộ, chỉ trả tóm tắt (subtask)
        kind = "general" if "general" in hits else "explore"
        prompt = re.sub(r"@(?:explore|general)\b", "", line, flags=re.I).strip()
        if not prompt:
            _p(f"Cú pháp: @{kind} <việc cần làm>  (vd @{kind} tìm chỗ xử lý đăng nhập)", "dim")
            return None
        if self._busy:
            _p(f"Agent đang bận — đã xếp hàng, agent chính sẽ tự dùng task {kind}.", "ye")
            return line
        _p(f"⏳ @{kind} đang làm (context riêng)...", "cy")
        try:
            out = self.manager._run_subagent(
                {"description": f"@{kind}", "prompt": prompt, "type": kind}, timeout=180)
        except Exception as e:
            out = f"[LOI] @{kind}: {type(e).__name__}: {e}"
        _p(f"— @{kind} xong —", "bold")
        _p(out or "(rỗng)", "gr")
        return None

    def _send(self, text):
        """Gửi câu lệnh vào hàng đợi agent (hiện khối User như khi gõ tay)."""
        sys.stdout.write(C["lm"] + C["bold"] + "User" + C["reset"] + C["lm"] + "> " + C["reset"] + text + "\n")
        sys.stdout.flush()
        try:
            # Snapshot git trước lượt chạy để /undo hoàn tác file kiểu opencode
            sessions.work_snapshot(self.sid)
        except Exception:
            pass
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

    def _export(self, open_editor=False):
        """Xuất đoạn chat hiện tại ra markdown (lưu ~/.rem_ai/exports/<sid>.md).
        open_editor=True (kiểu opencode /export): mở file bằng $EDITOR. Trả path hoặc ''."""
        try:
            msgs = sessions.load(self.sid)
        except Exception as e:
            _p(f"[LOI] không đọc được session: {e}", "rd")
            return ""
        if not msgs:
            _p("(đoạn chat trống, không có gì để xuất)", "dim")
            return ""
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
            return ""
        if open_editor:
            ed = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
            try:
                subprocess.run([ed, fp])
            except FileNotFoundError:
                _p(f"Không mở được $EDITOR='{ed}' (file vẫn ở {fp}).", "ye")
            except Exception:
                pass
        return fp

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

    def _run_bang(self, line):
        """Dòng bắt đầu `!` → chạy bash trực tiếp qua manager (không qua LLM)."""
        bcmd = (line[1:] if line.startswith("!") else line).strip()
        if not bcmd:
            _p("Cú pháp: !<lệnh bash>  (vd !ls -la)", "dim")
            return
        try:
            out = self.manager.call("bash", {"command": bcmd}, timeout=60)
        except Exception:
            try:
                r = subprocess.run(bcmd, shell=True, capture_output=True, text=True, timeout=60)
                out = ((r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")).strip()
                out = out or "(không có output)"
            except Exception as e:
                out = f"[LOI] {type(e).__name__}: {e}"
        _p(f"$ {bcmd}", "dim")
        _p(str(out)[:4000] or "(không có output)", "")

    def _run_custom(self, name, argstr):
        """Chạy custom command /tên (template như user prompt). Trả True nếu đã chạy.
        Hỗ trợ frontmatter kiểu opencode: agent (subagent → chạy context riêng),
        subtask=true (ép chạy subagent), model (đổi model cho lần chạy này)."""
        try:
            cmds = _load_custom_commands()
        except Exception:
            return False
        if name not in cmds:
            return False
        c = cmds[name]
        args = (argstr or "").split() if (argstr or "").strip() else []
        try:
            expanded = _expand_custom_template(c.get("template", ""), argstr or "", args, manager=self.manager)
        except Exception as e:
            _p(f"[LOI] custom command '{name}': {e}", "rd")
            return True
        prompt = (expanded or "").strip() or (argstr or "")
        if not prompt.strip():
            _p(f"(custom '{name}' trống — không gửi)", "dim")
            return True
        agent = str(c.get("agent") or "").strip().lower()
        model = str(c.get("model") or "").strip()
        subtask = bool(c.get("subtask", False)) or agent in ("general", "explore", "plan")
        # Đổi model cho lần chạy (kiểu opencode custom command model)
        _old_fav = ""
        if model:
            try:
                _old_fav = groq.get_favorite()
                if groq.set_favorite(model):
                    _p(f"(custom '{name}': model → {model})", "dim")
            except Exception:
                pass
        try:
            if subtask and not self._busy:
                # Chạy trong subagent (không ngập context chính)
                kind = "general" if agent in ("general", "build") else "explore"
                _p(f"⏳ /{name} chạy subagent {kind}...", "cy")
                try:
                    out = self.manager._run_subagent(
                        {"description": name, "prompt": prompt, "type": kind}, timeout=240)
                except Exception as e:
                    out = f"[LOI] /{name}: {type(e).__name__}: {e}"
                _p(out or "(rỗng)", "gr")
            else:
                if agent == "plan":
                    try:
                        self._agent.perm = Presets.plan()
                        self.presets = "plan"
                    except Exception:
                        pass
                elif agent == "build":
                    try:
                        self._agent.perm = Presets.build()
                        self.presets = "build"
                    except Exception:
                        pass
                self._send(prompt)
        finally:
            if model:
                # Subtask xong → trả model cũ; primary path giữ model mới cho cả
                # lượt agent (trả lại sau khi hàng chờ rỗng thì phức tạp — giữ lại
                # và báo rõ, user đổi lại bằng /models).
                if not (subtask and not self._busy):
                    pass
                else:
                    try:
                        if _old_fav:
                            groq.set_favorite(_old_fav)
                        else:
                            try:
                                os.remove(groq._MODEL_FILE)
                            except Exception:
                                pass
                    except Exception:
                        pass
        return True

    def slash(self, line):
        cmd = line.strip()
        parts = cmd.split()
        # Custom command được ưu tiên TRƯỚC builtin (giống opencode: custom
        # trùng tên sẽ override lệnh có sẵn).
        if cmd.startswith("/"):
            try:
                _frag0 = cmd.split()[0]
                _cname = _frag0[1:]
                if _cname and _cname not in ("exit", "quit", "q"):
                    try:
                        _cmds0 = _load_custom_commands()
                    except Exception:
                        _cmds0 = {}
                    if _cname in _cmds0:
                        if self._run_custom(_cname, cmd[len(_frag0):].strip()):
                            return True
            except Exception:
                pass
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
            try:
                _cc = _load_custom_commands()
                _builtins = {s.lstrip("/") for s in _SLASH}
                _extras = sorted(n for n in _cc if n not in _builtins)
                if _extras:
                    _p("— lệnh riêng của bạn: " + " · ".join("/" + n for n in _extras[:20]), "dim")
            except Exception:
                pass
            _p("@file chèn nội dung file vào prompt · @explore/@general gọi agent con · !cmd chạy bash trực tiếp", "dim")
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
        elif cmd == "/models" or cmd.startswith("/models "):
            try:
                ms = list(groq.chat_models()[:8]) or []
            except Exception:
                ms = []
            try:
                fav = groq.get_favorite()
            except Exception:
                fav = ""
            if len(parts) >= 2 and parts[1].isdigit():
                try:
                    i = int(parts[1]) - 1
                    sel = ms[i] if 0 <= i < len(ms) else ""
                except Exception:
                    sel = ""
                if not sel:
                    _p(f"Số không đúng (1-{len(ms)}).", "ye")
                elif groq.set_favorite(sel):
                    _p(f"Đã ghim model chat → {sel} (dùng cho các lượt sau).", "gr")
                else:
                    _p("[LOI] không lưu được model.", "rd")
            else:
                if not ms:
                    _p("(chưa có keys — gõ /connect để thêm)", "ye")
                for i, m in enumerate(ms, 1):
                    mark = "★" if (m == fav or (not fav and i == 1)) else " "
                    _p(f"{mark} {i}. {m}", "cy" if mark == "★" else "dim")
                try:
                    _p(f"Compact: {', '.join(groq.clone_models()[:2]) or '(chưa có keys)'}", "dim")
                except Exception:
                    pass
                if ms:
                    _p("Gõ /models <số> để ghim model chat yêu thích.", "dim")
        elif cmd == "/debug":
            config.DEBUG = not config.DEBUG
            _p(f"Chế độ gỡ lỗi: {'BẬT' if config.DEBUG else 'TẮT'}", "gr")
        elif cmd == "/think":
            if self._last_think.strip():
                _type(render.thinking_to_ansi(self._last_think, full=True), None)
            else:
                _p("(chưa có suy luận nào để xem — câu trả lời không dùng thẻ thinking)", "dim")
        elif cmd == "/thinking" or cmd.startswith("/thinking "):
            arg = (parts[1].lower() if len(parts) > 1 else "toggle")
            if arg in ("on", "true", "1", "hiện", "hien", "bật", "bat"):
                self.show_thinking = True
            elif arg in ("off", "false", "0", "ẩn", "an", "tắt", "tat"):
                self.show_thinking = False
            else:
                self.show_thinking = not self.show_thinking
            _p(f"Hiện khối suy luận: {'BẬT' if self.show_thinking else 'TẮT'}", "gr")
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
        elif cmd == "/sessions" or cmd.startswith("/sessions ") or cmd == "/continue" or cmd.startswith("/continue "):
            _arg = cmd.split(None, 1)[1].strip() if len(cmd.split(None, 1)) > 1 else ""
            if _arg:
                self._switch_session(_arg)
            else:
                self._sessions()
        elif cmd == "/rename" or cmd.startswith("/rename "):
            _t = cmd[len("/rename"):].strip()
            if not _t:
                _p(f"Tên hiện tại: {sessions.get_title(self.sid)}", "dim")
                _p("Cú pháp: /rename <tên mới>", "dim")
            elif sessions.set_title(self.sid, _t):
                _p(f"Đã đặt tên session → {_t[:80]}", "gr")
            else:
                _p("[LOI] không lưu được tên.", "rd")
        elif cmd == "/fork":
            try:
                nid = sessions.fork(self.sid)
            except Exception as e:
                nid = ""
                _p(f"[LOI] fork: {type(e).__name__}: {e}", "rd")
            if nid:
                self.sid = nid
                self.rec_mode = ""
                self._mk_agent(sid=self.sid)
                _p(f"Đã fork session → {nid} (lịch sử được giữ, rẽ nhánh từ đây).", "gr")
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
            rv = updater.remote_version(force=True)   # check tay → bỏ cache, lấy version thật
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
        elif cmd == "/connect" or cmd.startswith("/connect "):
            _parg = (parts[1].lower() if len(parts) > 1 else "")
            _provs = {"1": "groq", "groq": "groq"}
            if _parg in _provs:
                _prov = _provs[_parg]
            else:
                _p("Chọn provider:", "bold")
                _p("  1. groq  (được hỗ trợ — dán key gsk_...)", "cy")
                _p("  (OpenAI/Anthropic/Gemini: sắp có — hiện Remtm chạy trên Groq)", "dim")
                try:
                    _sel = input("Số hoặc tên [1]: ").strip().lower() or "1"
                except Exception:
                    return True
                _prov = _provs.get(_sel, "")
                if not _prov:
                    _p("Provider chưa được hỗ trợ.", "ye")
                    return True
            try:
                _k = input("Dán API key: ").strip()
            except Exception:
                return True
            if not _k:
                _p("(trống — không thêm)", "dim")
            elif groq.add_key(_k):
                _p(f"Đã kết nối {_prov} ({len(groq.keys())} keys tổng).", "gr")
            else:
                _p("Key không hợp lệ.", "rd")
        elif cmd == "/export":
            self._export(open_editor=True)
        elif cmd == "/keybinds":
            _p("\n".join([
                "Phím tắt Remtm (tương đương opencode ctrl+x leader):",
                "  Tab        chuyển agent PLAN ↔ BUILD (khi dòng trống)",
                "  ESC        dừng agent đang chạy (mềm) · ESC×2 dừng cứng",
                "  Ctrl+C     ngắt dòng / thoát khi rảnh · Ctrl+D thoát khi dòng trống",
                "  ↑/↓        lịch sử lệnh · Backspace xóa",
                "  /...       lệnh (thay leader ctrl+x): /new=/sessions mới (opencode <leader>n),",
                "             /compact (<leader>c), /editor (<leader>e), /themes (<leader>t),",
                "             /models (<leader>m), /undo (<leader>u), /redo (<leader>r),",
                "             /export (<leader>x), /exit (<leader>q)",
                "  @file      chèn file · @explore/@general agent con · !cmd bash",
            ]), "dim")
        elif cmd == "/undo":
            try:
                popped = sessions.undo_last_turn(self.sid)
            except Exception as e:
                _p(f"[LOI] undo: {type(e).__name__}: {e}", "rd")
                popped = []
            if not popped:
                _p("(không có gì để undo)", "dim")
            else:
                _p(f"Đã undo 1 turn ({len(popped)} tin nhắn). Gõ /redo để khôi phục.", "gr")
                # Hoàn tác file agent đã đổi trong turn (kiểu opencode, qua git)
                try:
                    rfiles, rmsg = sessions.work_undo(self.sid)
                except Exception as e:
                    rfiles, rmsg = [], f"{type(e).__name__}: {e}"
                if rfiles:
                    _p(f"Đã hoàn tác {len(rfiles)} file:", "ye")
                    for _rf in rfiles[:15]:
                        _p(f"  ↩ {_rf}", "dim")
                    if len(rfiles) > 15:
                        _p(f"  ... và {len(rfiles) - 15} file nữa", "dim")
                elif rmsg and "không đổi file" not in rmsg and "không có snapshot" not in rmsg:
                    _p(f"(file: {rmsg})", "dim")
        elif cmd == "/redo":
            try:
                msgs = sessions.redo_pop(self.sid)
            except Exception as e:
                _p(f"[LOI] redo: {type(e).__name__}: {e}", "rd")
                msgs = []
            if not msgs:
                _p("(không có gì để redo)", "dim")
            else:
                _p(f"Đã redo {len(msgs)} tin nhắn.", "gr")
                try:
                    ok, rmsg = sessions.work_redo(self.sid)
                except Exception as e:
                    ok, rmsg = False, f"{type(e).__name__}: {e}"
                if rmsg and "không có gì" not in rmsg:
                    _p(f"(file: {rmsg})", "gr" if ok else "ye")
        elif cmd in ("/compact", "/summarize") or cmd.startswith("/compact ") or cmd.startswith("/summarize "):
            try:
                msgs = sessions.load(self.sid)
                before = sum(len(str(m.get("content") or "")) for m in msgs)
                new = sessions.compact(self.sid, msgs)
                after = sum(len(str(m.get("content") or "")) for m in new)
                try:
                    if new is not msgs and len(new) != len(msgs) and hasattr(sessions, "_write_all"):
                        sessions._write_all(self.sid, new)
                except Exception:
                    pass
                _p(f"Đã compact: {before} → {after} ký tự ({len(msgs)} → {len(new)} tin).", "gr")
            except Exception as e:
                _p(f"[LOI] compact: {type(e).__name__}: {e}", "rd")
        elif cmd == "/details" or cmd.startswith("/details "):
            arg = (parts[1].lower() if len(parts) > 1 else "")
            if arg in ("on", "1", "true", "bat", "bật"):
                self.verbose = True
            elif arg in ("off", "0", "false", "tat", "tắt"):
                self.verbose = False
            else:
                self.verbose = not getattr(self, "verbose", False)
            try:
                self._verbose = self.verbose
            except Exception:
                pass
            _p(f"Chi tiết tool: {'BẬT' if self.verbose else 'TẮT'} (khi bật in thêm 3 dòng đầu result).", "gr")
        elif cmd == "/editor" or cmd.startswith("/editor "):
            ed = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
            import tempfile as _tf
            try:
                with _tf.NamedTemporaryFile(mode="w+", suffix=".md", delete=False, encoding="utf-8") as tf:
                    tfpath = tf.name
                try:
                    subprocess.run([ed, tfpath])
                except FileNotFoundError:
                    _p(f"Không mở được $EDITOR='{ed}'. Đặt EDITOR=vi|nano rồi thử lại.", "rd")
                    try:
                        os.remove(tfpath)
                    except Exception:
                        pass
                    return True
                try:
                    with open(tfpath, encoding="utf-8") as f:
                        content = f.read().strip()
                except Exception:
                    content = ""
                try:
                    os.remove(tfpath)
                except Exception:
                    pass
                if content:
                    try:
                        content = _expand_mentions(content)
                    except Exception:
                        pass
                    self._send(content)
                else:
                    _p("(editor trống — không gửi)", "dim")
            except Exception as e:
                _p(f"[LOI] editor: {type(e).__name__}: {e}", "rd")
        elif cmd == "/themes" or cmd.startswith("/themes "):
            self._theme(parts[1] if len(parts) > 1 else "")
        elif cmd == "/share" or cmd.startswith("/share "):
            fp = self._export()
            # opencode copy link share vào clipboard — bản local copy đường dẫn
            # file export để dán cho người khác.
            if fp:
                _copied = False
                for _cc in (["xclip", "-selection", "clipboard"],
                            ["xsel", "--clipboard", "--input"]):
                    try:
                        _r = subprocess.run(_cc, input=fp, capture_output=True,
                                            text=True, timeout=5)
                        if _r.returncode == 0:
                            _copied = True
                            break
                    except Exception:
                        continue
                if _copied:
                    _p(f"Đã copy đường dẫn share vào clipboard: {fp}", "gr")
                else:
                    _p(f"(chưa copy được clipboard — thiếu xclip/xsel; file ở {fp})", "dim")
            else:
                _p("(stub local — chưa upload mạng, file ở ~/.rem_ai/exports/)", "dim")
        elif cmd == "/unshare" or cmd.startswith("/unshare "):
            _p("(stub local — chưa có link mạng để gỡ, file export vẫn ở ~/.rem_ai/exports/)", "dim")
        elif cmd == "/import" or cmd.startswith("/import "):
            if len(parts) < 2:
                _p("Cú pháp: /import <file>  (JSON session opencode-style hoặc JSONL Rem)", "dim")
            else:
                fp = os.path.expanduser(parts[1])
                if not os.path.isfile(fp):
                    _p(f"Không thấy file: {fp}", "rd")
                else:
                    try:
                        imported = _import_session_file(fp)
                        cnt = 0
                        for m in imported:
                            try:
                                sessions.append(self.sid, m)
                                cnt += 1
                            except Exception:
                                continue
                        _p(f"Đã import {cnt} tin nhắn từ {fp} vào session hiện tại.", "gr")
                    except Exception as e:
                        _p(f"[LOI] import: {type(e).__name__}: {e}", "rd")
        elif cmd == "/voice" or cmd.startswith("/voice "):
            _varg = cmd[len("/voice"):].strip()
            _vlow = _varg.lower()
            try:
                if not _varg or _vlow in ("status", "stt", "mic", "check"):
                    _p(self.manager.call("voice_status", {}, timeout=30), "gr")
                elif _vlow.startswith("nghe"):
                    _sec = 6
                    try:
                        for _tok in _varg.split()[1:]:
                            _n = int("".join(c for c in _tok if c.isdigit()) or "0")
                            if 1 <= _n <= 30:
                                _sec = _n
                                break
                    except Exception:
                        pass
                    _p(f"🎤 Đang nghe {_sec}s — nói đi (bắt đầu bằng 'Rem ơi')...", "ye")
                    _heard = self.manager.call("voice_cmd", {"seconds": _sec}, timeout=60)
                    _p(_heard, "cy")
                    if not _heard.startswith("[LOI]"):
                        _m = re.search(r"Lệnh thoại \([^)]*\):\s*(.+?)(\n\[XÁC NHẬN\])?\s*$", _heard, re.S)
                        _txt = (_m.group(1).strip() if _m else "").strip()
                        if _txt:
                            self._send(_txt)
                elif _vlow.startswith("nói ") or _vlow.startswith("noi "):
                    _txt = _varg.split(" ", 1)[1].strip() if " " in _varg else ""
                    if not _txt:
                        _p("Cú pháp: /voice nói <nội dung cần đọc>", "dim")
                    else:
                        _p(self.manager.call("voice_say", {"text": _txt[:2000]}, timeout=60), "gr")
                else:
                    # /voice <lệnh thoại gõ tay> → gửi thẳng cho agent
                    self._send(_varg)
            except Exception as e:
                _p(f"[LOI] voice: {type(e).__name__}: {e} (cài: pip install -r requirements-voice.txt)", "rd")
        elif cmd in ("/exit", "/quit", "/q"):
            return False
        else:
            if cmd.startswith("/"):
                # Custom commands trước (/*.md), rồi mới gợi ý/báo lạ
                try:
                    _frag0 = cmd.split()[0]
                    _cname = _frag0[1:]
                    _carg = cmd[len(_frag0):].strip()
                    if _cname and self._run_custom(_cname, _carg):
                        return True
                except Exception:
                    pass
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
        """Hộp thông tin đầu phiên kiểu opencode: viền mảnh + title pill + 3 dòng metadata."""
        tw = render.term_width()
        w = min(max(tw - 6, 48), 78)
        try:
            ms = groq.chat_models()
            model = ms[0] if ms else "chưa có key"
        except Exception:
            model = "?"
        try:
            nkeys = len(groq.keys())
        except Exception:
            nkeys = 0
        # opencode đặt title ở viền trên: "─ REM vX · opencode ─"
        title = f" ◆ REM v{config.VERSION} · opencode "
        top = C["dim"] + "╭" + title + "─" * max(0, w - render.disp_len(title) - 2) + "╮" + C["reset"]
        # cwd rút gọn nếu dài quá w
        cwd = os.getcwd()
        if render.disp_len(cwd) > w - 10:
            cwd = "…" + cwd[-(w - 11):]
        # key pill màu theo số lượng (opencode: dot xanh/vàng/đỏ)
        if nkeys >= 10:
            kpill = C["gr"] + f"{nkeys} keys" + C["reset"]
            kplain = f"{nkeys} keys"
        elif nkeys >= 3:
            kpill = C["ye"] + f"{nkeys} keys" + C["reset"]
            kplain = f"{nkeys} keys"
        else:
            kpill = C["rd"] + f"{nkeys} keys" + C["reset"]
            kplain = f"{nkeys} keys"
        rows = [
            (f"model  {C['cy']}{model}{C['reset']} · {kpill}", f"model  {model} · {kplain}"),
            (f"dir    {C['dim']}{cwd}{C['reset']} · chat {C['dim']}{self.sid}{C['reset']}", f"dir    {cwd} · chat {self.sid}"),
        ]
        print(top)
        for rendered, plain in rows:
            pad = max(0, w - 4 - render.disp_len(plain))
            print(C["dim"] + "│" + C["reset"] + " " + rendered + " " * pad + " " + C["dim"] + "│" + C["reset"])
        bot = "╰" + "─" * (w - 2) + "╯"
        print(C["dim"] + bot + C["reset"])

    def _footer_hints(self):
        # Status bar đáy kiểu opencode: gợi ý phím tắt trái — model/dir phải (dim toàn dòng)
        try:
            tw = render.term_width()
        except Exception:
            tw = 90
        left = "↵ send · / for commands · @ file · ! bash · esc interrupt"
        try:
            ms = groq.chat_models()
            mshort = (ms[0].split("/")[-1] if ms else "?")[:18]
            _lu = getattr(self, "_last_turn_use", {}) or {}
            if _lu.get("prompt") or _lu.get("completion"):
                mshort += f" ↑{_lu.get('prompt', 0)} ↓{_lu.get('completion', 0)}"
        except Exception:
            mshort = "?"
        try:
            right = f"{mshort} · {os.path.basename(os.getcwd())} · v{config.VERSION}"
        except Exception:
            right = f"v{config.VERSION}"
        try:
            pad = max(2, tw - render.disp_len(left) - render.disp_len(right))
        except Exception:
            pad = 4
        with _OUT_LOCK:
            sys.stdout.write(C["dim"] + left + " " * pad + right + C["reset"] + "\n")
            sys.stdout.flush()

    def _prompt_hint(self):
        # Opencode-style: thẻ nhập LUÔN 2 dòng (dòng gợi ý mờ + dòng ❯ nhập liệu).
        # Giữ cùng chiều cao khi bận/rảnh để không sót dòng prompt cũ gây dính chữ.
        # KHÔNG in "❯ " tay — để _input_line echo tự hiện (chống dính chữ khi paste).
        try:
            lv = self._agent.live_count()
        except Exception:
            lv = 0
        rec = (C["rd"] + "⏺ REC " + C["reset"]) if self.rec_mode else ""
        live = (C["ye"] + f"📥{lv} " + C["reset"]) if lv else ""
        card_top = (C["dim"] + "╭─ type a message · / for commands · @ file · ! bash"
                    + C["reset"] + "\n")
        try:
            _pill = (C["ye"] + "[plan]" + C["reset"] if getattr(self, "presets", "build") == "plan"
                     else C["gr"] + "[build]" + C["reset"])
        except Exception:
            _pill = "[build]"
        if self._busy:
            top = (C["dim"] + "╭─ running — type to steer · esc to interrupt"
                   + C["reset"] + "\n")
            return top + rec + live + C["bold"] + C["cy"] + "⏳ ❯ " + C["reset"] + _pill + " "
        if self._pending:
            top = (C["dim"] + f"╭─ queued ({self._pending}) — waiting"
                   + C["reset"] + "\n")
            return top + rec + live + C["bold"] + C["cy"] + "⏳ ❯ " + C["reset"] + _pill + " "
        return card_top + rec + live + C["bold"] + C["cy"] + "╰─❯ " + C["reset"] + _pill + " "

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
            try:
                self._header_box()
            except Exception:
                pass
            _p("/list macro/skill/chat cũ · /rec ghi thao tác · Tab chuyển plan/build", "dim")
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
                    try:
                        if line.strip():
                            self._hist_push(line.strip())
                    except Exception:
                        pass
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
            # `!cmd` → bash trực tiếp qua manager (không qua LLM)
            if line.startswith("!"):
                try:
                    self._run_bang(line)
                except Exception as e:
                    _p(f"[LOI] bash: {e}", "rd")
                continue
            # `@file` → fuzzy tìm file, chèn ~2000 ký tự vào prompt gửi agent
            try:
                if "@" in line:
                    line = _expand_mentions(line)
            except Exception:
                pass
            # `@agent` kiểu opencode: @explore/@general gọi agent con (không ngập
            # context chính), @plan/@build chuyển preset cho tin này.
            try:
                if "@" in line:
                    _nl = self._run_agent_mention(line)
                    if _nl is None:
                        continue
                    line = _nl
                    if not line.strip():
                        continue
            except Exception:
                pass
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
            # Xóa đúng số hàng vật lý của prompt 2 dòng + input wrap — bản cũ gộp
            # chung disp_len(prompt+input) bỏ qua xuống dòng nên thiếu 1 hàng, gây dính.
            try:
                with _OUT_LOCK:
                    try:
                        _vis = re.sub(r"\x1b\[[0-9;]*m", "", self._prompt_hint())
                        _tw = render.term_width() or 90
                        _parts = _vis.split("\n")
                        # prompt có N dòng, dòng cuối nối liền input
                        _rows = 0
                        for _pl in _parts[:-1]:
                            _rows += max(1, (render.disp_len(_pl) + _tw - 1) // _tw)
                        _last = _parts[-1] if _parts else ""
                        _rows += max(1, (render.disp_len(_last) + render.disp_len(line) + _tw - 1) // _tw)
                        sys.stdout.write("\r\033[2K")
                        for _ in range(_rows - 1):
                            sys.stdout.write("\033[1A\033[2K")
                        sys.stdout.write("\r")
                    except Exception:
                        sys.stdout.write("\n")
                    sys.stdout.write(C["lm"] + C["bold"] + "User" + C["reset"] + C["lm"] + "> " + C["reset"] + line + "\n")
                    sys.stdout.flush()
            except Exception:
                _p("User> " + line, "lm")
            self.q.put(("task", self._expand_mentions(line)))
