import os, sys, subprocess

from mcplib import Client


SPECS = [
    {"name": "developer", "module": "developer", "desc": "file + shell + thư mục"},
    {"name": "webtool", "module": "webtool", "desc": "tìm web, đọc web, GitHub API"},
    {"name": "memory", "module": "memory_server", "desc": "bộ nhớ dài hạn graph.json"},
]


class Extension:
    def __init__(self, name, module, desc):
        self.name = name
        self.module = module
        self.desc = desc
        self.enabled = False
        self.error = ""
        self.client = None
        self.tools = []

    def start(self):
        root = os.path.dirname(os.path.abspath(__file__))
        py = sys.executable
        proc = subprocess.Popen(
            [py, "-m", "mcp_servers." + self.module],
            cwd=root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
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

    def call(self, name, args, timeout=600):
        return self.client.call_tool(name, args, timeout)

    def close(self):
        try:
            if self.client:
                self.client.close()
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

    def call(self, name, args, timeout=600):
        tm = self.tool_map()
        if name not in tm:
            return f"[LOI] tool '{name}' không tồn tại trong extension nào"
        return tm[name][0].call(name, args, timeout)

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