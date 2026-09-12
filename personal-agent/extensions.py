import os, sys, subprocess, json

import config
from mcplib import Client


SPECS = [
    {"name": "developer", "module": "developer", "desc": "file + shell + thư mục"},
    {"name": "webtool", "module": "webtool", "desc": "tìm web, đọc web, GitHub API"},
    {"name": "memory", "module": "memory_server", "desc": "bộ nhớ dài hạn graph.json"},
    {"name": "skills", "module": "skills_server", "desc": "skill tự học — lưu & tái dùng quy trình thành công"},
    {"name": "experience", "module": "experience_server", "desc": "kinh nghiệm — bài học từ lỗi + procedural skills (chống lặp lỗi)"},
    {"name": "lsp", "module": "lsp_server", "desc": "LSP: clangd/pylsp (diagnostics, định nghĩa, tham chiếu, symbol, hover)"},
    {"name": "desktop_linux", "module": "desktop_linux", "desc": "điều khiển desktop Linux qua AT-SPI (cây UI, click, gõ phím, chuột, clipboard) + macro recorder (ghi/phát lại thao tác theo mục việc)"},
    {"name": "browser_auto", "module": "browser_auto", "desc": "trình duyệt bằng Playwright: điều khiển web, YouTube, dashboard, screenshot"},
    {"name": "media_tools", "module": "media_tools", "desc": "làm video: TTS tiếng Việt, ảnh AI, ghép scene Shorts, cắt/nối/đổi cỡ (ffmpeg)"},
    {"name": "social_auto", "module": "social_auto_server", "desc": "tự động hóa mạng xã hội 24/7 (Facebook, YouTube, TikTok, Instagram, Twitter)"},
]

# MCP servers NGOÀI do người dùng khai báo (kiểu opencode.json):
#   ~/.rem_ai/mcp.json   (hoặc biến REM_MCP_FILE)
#   {"mcp": {"tên": {"type": "stdio", "command": ["python3", "/abs/server.py"], "env": {"K": "V"}}}}
MCP_FILE = os.environ.get("REM_MCP_FILE", "") or os.path.join(config.DIR, "mcp.json")

LOGDIR = os.path.join(config.DIR, "logs")


def _external_specs():
    """Đọc mcp.json → danh sách specs ngoài (rỗng nếu không có file/dữ liệu)."""
    try:
        with open(MCP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except FileNotFoundError:
        return []
    except Exception:
        return []
    mcp = data.get("mcp") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        return []
    specs = []
    for name, cfg in mcp.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("type", "stdio") != "stdio":
            continue
        cmd = cfg.get("command")
        if not isinstance(cmd, list) or not cmd or not isinstance(cmd[0], str):
            continue
        cmd = [str(c) for c in cmd]
        specs.append({
            "name": "mcp:" + str(name),
            "module": None,
            "desc": "MCP ngoài: " + " ".join(cmd),
            "command": cmd,
            "env": cfg.get("env") if isinstance(cfg.get("env"), dict) else {},
        })
    return specs


# Tool subagent tích hợp (không cần MCP server riêng): agent con CHỈ ĐỌC.
TASK_DEF = {
    "name": "task",
    "description": ("SUBAGENT (kiểu Explore): giao việc tách biệt nặng (quét repo, "
                    "tìm hiểu code, tra cứu song song) cho agent con chạy trong "
                    "context riêng, chỉ trả TÓM TẮT về. Agent con CHỈ ĐỌC — không "
                    "ghi file/sửa code/chạy shell. Không lồng quá 1 tầng."),
    "parameters": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "mô tả ngắn việc (3-5 từ)"},
            "prompt": {"type": "string", "description": "chỉ đạo chi tiết + format kết quả cần trả"},
        },
        "required": ["prompt"],
    },
}


class Extension:
    def __init__(self, name, module, desc, command=None, env=None):
        self.name = name
        self.module = module
        self.desc = desc
        self.command = command      # MCP ngoài: list lệnh, không dùng module
        self.env = env or {}
        self.enabled = False
        self.error = ""
        self.client = None
        self.tools = []
        self._logf = None
        self.external = command is not None

    def start(self):
        root = os.path.dirname(os.path.abspath(__file__))
        py = sys.executable
        os.makedirs(LOGDIR, exist_ok=True)
        self._logf = open(os.path.join(LOGDIR, self.name + ".log"), "a", encoding="utf-8")
        if self.external:
            cmd = self.command
            cwd = root
            env = dict(os.environ)
            env.update(self.env)
        else:
            cmd = [py, "-m", "mcp_servers." + self.module]
            cwd = root
            env = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._logf,
                env=env,
                text=True,
                start_new_session=True,
            )
        except Exception as e:
            self.enabled = False
            self.error = repr(e)
            return
        self.client = Client(proc)
        try:
            self.client.initialize()
            self.tools = self.client.list_tools()
            self.enabled = True
        except Exception as e:
            self.enabled = False
            self.error = repr(e)
            try:
                self.client.close()
            except Exception:
                pass

    def call(self, name, args, timeout=120):
        return self.client.call_tool(name, args, timeout)

    def close(self):
        try:
            if self.client:
                self.client.close()
        except Exception:
            pass
        try:
            if self._logf:
                self._logf.close()
        except Exception:
            pass


class Manager:
    def __init__(self, specs=None):
        import threading as _th
        base = list(specs) if specs is not None else list(SPECS)
        base = base + _external_specs()
        self.extensions = [Extension(**s) for s in base]
        self._task_n = 0
        self._task_lock = _th.Lock()

    def start_all(self):
        # Khởi động song song (trước đây nối tiếp ~6s). Mỗi extension chỉ chạm
        # tiến trình/log/tools của riêng nó; tool_map tính lại sau nên không race.
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(len(self.extensions), 10)) as _ex:
            list(_ex.map(Extension.start, self.extensions))

    def reload_external(self):
        """(Re)start các MCP server ngoài theo mcp.json; giữ nguyên extension nội."""
        for e in self.extensions:
            if e.external:
                e.close()
        self.extensions = [e for e in self.extensions if not e.external]
        specs = _external_specs()
        if not specs:
            return
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(len(specs), 6)) as _ex:
            def _mk(s):
                try:
                    e = Extension(**s)
                    e.start()
                    return e
                except Exception:
                    return None
            for e in _ex.map(_mk, specs):
                if e is not None:
                    self.extensions.append(e)

    def external_list(self):
        return [e for e in self.extensions if e.external]

    def tool_map(self):
        m = {}
        for e in self.extensions:
            if not e.enabled:
                continue
            for t in e.tools:
                m.setdefault(t.get("name"), (e, t))
        return m

    def schemas(self):
        out = []
        for e in self.extensions:
            if not e.enabled:
                continue
            for t in e.tools:
                out.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t.get("name"),
                            "description": t.get("description", ""),
                            "parameters": t.get("inputSchema", {"type": "object", "properties": {}}),
                        },
                    }
                )
        if self._task_depth() == 0:
            out.append({"type": "function", "function": dict(TASK_DEF)})
        return out

    # ── task subagent (kiểu Claude Code Explore): agent con chạy việc tách biệt
    # trong context riêng, chỉ trả tóm tắt về — context chính không bị ngập.
    def _task_depth(self):
        try:
            with self._task_lock:
                return self._task_n
        except Exception:
            return 0

    def _run_subagent(self, args, timeout=120):
        """Chạy agent con: đọc-hiểu/tìm kiếm/tóm tắt việc tách biệt (CHỈ ĐỌC +
        tìm kiếm, CẤM ghi file/sửa code/chạy shell/điều khiển máy)."""
        import threading as _th
        desc = ""
        prompt = ""
        if isinstance(args, dict):
            desc = str(args.get("description", "") or "")[:200]
            prompt = str(args.get("prompt", "") or "")[:4000]
        if not prompt.strip():
            return "[LOI] task cần 'prompt' mô tả việc cho agent con"
        with self._task_lock:
            if self._task_n >= 1:
                return "[LOI] subagent đã tới giới hạn lồng nhau (1 tầng) — tự làm tiếp"
            self._task_n += 1
        try:
            import sessions as _sessions
            from permissions import PermPolicy as _Perm
            from agentloop import Agent as _Agent
            perm = _Perm()
            # BẮT BUỘC: bỏ ruleset allow-* toàn cục (full-auto của user) khỏi
            # agent con — nếu không overrides deny bên dưới bị ruleset đè,
            # agent con CHỈ ĐỌC sẽ lén có full quyền ghi/shell.
            perm.rules = []
            for t in ("write_file", "edit_file", "apply_patch", "bash", "bash_poll",
                      "chdir", "pip_install", "ensure_tool", "task",
                      "dl_click", "dl_type", "dl_key", "dl_mouse", "dl_clipboard",
                      "rec_start", "rec_stop", "rec_play", "rec_delete",
                      "browser_open", "browser_navigate", "browser_click",
                      "browser_click_text", "browser_type", "browser_press",
                      "browser_eval", "browser_wait", "browser_scroll",
                      "browser_search", "browser_back", "browser_close",
                      "social_cycle", "social_post",
                      "media_tts", "media_image", "media_scene", "media_slideshow",
                      "media_concat", "media_trim", "media_scale", "media_to_gif",
                      "media_overlay_text", "media_extract_audio"):
                perm.overrides[t] = "deny"
            sid = _sessions.new()
            child = _Agent(self, perm, sid=sid, on_event=None)
            box = {}
            def _run():
                try:
                    box["out"] = child.run(
                        f"[SUBAGENT task: {desc}]\n{prompt}\n\n"
                        "Bạn là agent con CHỈ ĐỌC: tìm hiểu/trả lời, KHÔNG sửa gì. "
                        "Cuối cùng trả TÓM TẮT gọn (dưới 1500 ký tự): kết quả + file/đường dẫn liên quan.")
                except Exception as e:
                    box["out"] = f"[LOI SUBAGENT] {type(e).__name__}: {e}"
            th = _th.Thread(target=_run, daemon=True)
            th.start()
            th.join(timeout=max(10, min(int(timeout or 120), 240)))
            if th.is_alive():
                try:
                    child.stop()
                except Exception:
                    pass
                return "[LOI] subagent quá hạn — hãy chia nhỏ việc hơn"
            out = (box.get("out") or "(rỗng)").strip()
            return out[:3000]
        finally:
            with self._task_lock:
                self._task_n = max(0, self._task_n - 1)

    def interrupt(self):
        """/stop: đánh thức mọi MCP request đang chờ trong ≤0.5s."""
        for e in self.extensions:
            try:
                if e.client:
                    e.client.cancel_pending()
            except Exception:
                pass

    def reset_interrupt(self):
        for e in self.extensions:
            try:
                if e.client:
                    e.client.reset_cancel()
            except Exception:
                pass

    def call(self, name, args, timeout=120):
        if name == "task":
            return self._run_subagent(args, timeout)
        tm = self.tool_map()
        if name not in tm:
            return f"[LOI] tool '{name}' không tồn tại trong extension nào"
        ext, t = tm[name]
        try:
            return ext.call(name, args, timeout)
        except ConnectionError:
            try:
                ext.close()
            except Exception:
                pass
            ext.start()
            if ext.enabled and any(x.get("name") == name for x in ext.tools):
                return ext.call(name, args, timeout)
            return f"[LOI] extension '{ext.name}' bị mất kết nối, đã restart nhưng không hồi phục"
        except InterruptedError:
            return "[ĐÃ DỪNG] theo yêu cầu của người dùng."
        except TimeoutError:
            return f"[LOI] tool '{name}' chạy quá lâu (timeout)"

    def status(self):
        lines = []
        for e in self.extensions:
            st = "OK" if e.enabled else "LOI"
            lines.append(f"  {e.name:10} [{st}] {e.desc} — {len(e.tools)} tool" + (f" | {e.error}" if e.error else ""))
        return "\n".join(lines)

    def tool_names(self):
        return sorted(self.tool_map().keys())

    def close_all(self):
        for e in self.extensions:
            e.close()