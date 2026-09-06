import os, sys, subprocess

import config
from mcplib import Client


SPECS = [
    {"name": "developer", "module": "developer", "desc": "file + shell + thư mục"},
    {"name": "webtool", "module": "webtool", "desc": "tìm web, đọc web, GitHub API"},
    {"name": "memory", "module": "memory_server", "desc": "bộ nhớ dài hạn graph.json"},
]

LOGDIR = os.path.join(config.DIR, "logs")


class Extension:
    def __init__(self, name, module, desc):
        self.name = name
        self.module = module
        self.desc = desc
        self.enabled = False
        self.error = ""
        self.client = None
        self.tools = []
        self._logf = None

    def start(self):
        root = os.path.dirname(os.path.abspath(__file__))
        py = sys.executable
        os.makedirs(LOGDIR, exist_ok=True)
        self._logf = open(os.path.join(LOGDIR, self.name + ".log"), "a", encoding="utf-8")
        proc = subprocess.Popen(
            [py, "-m", "mcp_servers." + self.module],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._logf,
            text=True,
            start_new_session=True,
        )
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
    def __init__(self, specs=SPECS):
        self.extensions = [Extension(**s) for s in specs]

    def start_all(self):
        for e in self.extensions:
            e.start()

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