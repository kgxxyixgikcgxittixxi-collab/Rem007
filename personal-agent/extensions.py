import os, sys, subprocess, json

import config
from mcplib import Client


SPECS = [
    {"name": "developer", "module": "developer", "desc": "file + shell + thư mục"},
    {"name": "webtool", "module": "webtool", "desc": "tìm web, đọc web, GitHub API"},
    {"name": "memory", "module": "memory_server", "desc": "bộ nhớ dài hạn graph.json"},
    {"name": "skills", "module": "skills_server", "desc": "skill tự học — lưu & tái dùng quy trình thành công"},
    {"name": "lsp", "module": "lsp_server", "desc": "LSP: clangd/pylsp (diagnostics, định nghĩa, tham chiếu, symbol, hover)"},
    {"name": "desktop_linux", "module": "desktop_linux", "desc": "điều khiển desktop Linux qua AT-SPI (cây UI, click, gõ phím, chuột, clipboard)"},
    {"name": "browser_auto", "module": "browser_auto", "desc": "trình duyệt bằng Playwright: điều khiển web, YouTube, dashboard, screenshot"},
    {"name": "media_tools", "module": "media_tools", "desc": "làm video: TTS tiếng Việt, ảnh AI, ghép scene Shorts, cắt/nối/đổi cỡ (ffmpeg)"},
    {"name": "social_auto", "module": "social_auto", "desc": "tự động hóa mạng xã hội 24/7 (Facebook, YouTube, TikTok, Instagram, Twitter)"},
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
        base = list(specs) if specs is not None else list(SPECS)
        base = base + _external_specs()
        self.extensions = [Extension(**s) for s in base]

    def start_all(self):
        for e in self.extensions:
            e.start()

    def reload_external(self):
        """(Re)start các MCP server ngoài theo mcp.json; giữ nguyên extension nội."""
        for e in self.extensions:
            if e.external:
                e.close()
        self.extensions = [e for e in self.extensions if not e.external]
        for s in _external_specs():
            e = Extension(**s)
            e.start()
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
        return out

    def call(self, name, args, timeout=120):
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