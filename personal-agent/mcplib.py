import json, threading

PROTOCOL_VERSION = "2024-11-05"


def recv_msg(stream):
    while True:
        line = stream.readline()
        if not line:
            return None
        if isinstance(line, bytes):
            line = line.decode("utf-8", "replace")
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        try:
            return json.loads(line)
        except ValueError:
            return None


def send_msg(stream, msg):
    # BrokenPipe (client đóng kết nối) được để caller xử lý:
    # - Server.serve: bắt và thoát lặng lẽ (không traceback).
    # - Client.request: bắt và báo ConnectionError để auto-restart.
    stream.write(json.dumps(msg, ensure_ascii=False) + "\n")
    stream.flush()


def clamp(text, n=60000):
    text = str(text)
    if len(text) > n:
        text = text[:n] + f"\n...[cắt gọn, {len(text) - n} ký tự]..."
    return text


def atomic_write_json(path, obj):
    """Ghi JSON atomic (tmp + replace): nhiều tiến trình Remtm/MCP cùng viết
    1 file (skills, memory, macro...) không lo rách file/mất dữ liệu khi crash."""
    import os
    import tempfile as _tf
    d = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    fd, tmp = _tf.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


class Tool:
    def __init__(self, name, description, parameters, func):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.func = func

    def definition(self):
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {
                "type": "object",
                "properties": self.parameters.get("properties", {}),
                "required": self.parameters.get("required", []),
            },
        }

    def run(self, args):
        try:
            out = self.func(**args) if isinstance(args, dict) else self.func(args)
        except Exception as e:
            out = f"[LOI] {type(e).__name__}: {e} | {args}"
        return clamp(out)


def schema(props, required=None):
    return {"properties": props, "required": required or list(props.keys())}


class Server:
    def __init__(self, tools, name, version):
        self.tools = {t.name: t for t in tools}
        self.name = name
        self.version = version
        self._running = True

    def serve(self, stdin, stdout):
        while self._running:
            req = recv_msg(stdin)
            if req is None:
                return
            if not isinstance(req, dict):
                continue
            resp = {"jsonrpc": "2.0", "id": req.get("id")}
            method = req.get("method")
            params = req.get("params") or {}
            try:
                if method == "initialize":
                    resp["result"] = {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": self.name, "version": self.version},
                    }
                elif method == "notifications/initialized":
                    continue
                elif method == "notifications/exit":
                    self._running = False
                    continue
                elif method == "ping":
                    resp["result"] = {}
                elif method == "tools/list":
                    resp["result"] = {
                        "tools": [t.definition() for t in self.tools.values()],
                        "nextCursor": None,
                    }
                elif method == "tools/call":
                    name = params.get("name")
                    tool = self.tools.get(name)
                    if not tool:
                        resp["error"] = {"code": -32602, "message": f"Unknown tool: {name}"}
                    else:
                        args = params.get("arguments") or {}
                        if not isinstance(args, dict):
                            args = {"value": args}
                        text = tool.run(args)
                        is_err = text.startswith(("[TOOL LOI]", "[LOI]", "[TU CHOI]"))
                        resp["result"] = {"content": [{"type": "text", "text": text}], "isError": is_err}
                elif method == "shutdown":
                    resp["result"] = None
                else:
                    resp["error"] = {"code": -32601, "message": f"Method not found: {method}"}
            except Exception as e:
                resp["error"] = {"code": -32000, "message": repr(e)}
            if req.get("id") is not None:
                try:
                    send_msg(stdout, resp)
                except (BrokenPipeError, ConnectionResetError, ValueError, OSError):
                    # Client đã đóng pipe (tắt agent / restart extension) → thoát lặng, không traceback.
                    return


class Client:
    def __init__(self, proc):
        self.proc = proc
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._reader = threading.Thread(target=self._loop, daemon=True)
        self._reader.start()

    def _loop(self):
        try:
            while True:
                msg = recv_msg(self.proc.stdout)
                if msg is None:
                    break
                if isinstance(msg, dict) and msg.get("id") is not None:
                    with self._lock:
                        entry = self._pending.pop(msg["id"], None)
                    if entry:
                        entry[1][0] = msg
                        entry[0].set()
        finally:
            with self._lock:
                pend, self._pending = self._pending, {}
                dead = self.proc.poll() is not None
            err = RuntimeError("MCP server đã đóng kết nối" + (f" (exit {self.proc.returncode})" if dead else ""))
            for fut, holder in pend.values():
                holder[1] = err
                fut.set()

    def request(self, method, params=None, timeout=120):
        with self._lock:
            self._id += 1
            mid = self._id
            fut = threading.Event()
            holder = [None, None]
            self._pending[mid] = (fut, holder)
        data = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}
        try:
            send_msg(self.proc.stdin, data)
        except Exception as e:
            with self._lock:
                self._pending.pop(mid, None)
            raise ConnectionError(f"Không thể gửi tới MCP server: {e}")
        if not fut.wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise TimeoutError(f"MCP timeout: {method}")
        if holder[1] is not None:
            raise holder[1]
        resp = holder[0]
        if resp is None:
            raise RuntimeError(f"MCP no response: {method}")
        if "error" in resp:
            raise RuntimeError(f"MCP error {resp['error']} ({method})")
        return resp.get("result")

    def send_notification(self, method, params=None):
        send_msg(self.proc.stdin, {"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self):
        r = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "rem-agent", "version": "3"},
            },
            15,
        )
        self.send_notification("notifications/initialized")
        return r

    def list_tools(self):
        r = self.request("tools/list", {}, 30) or {}
        return r.get("tools") or []

    def call_tool(self, name, arguments, timeout=600):
        r = self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout)
        if r is None:
            return "[TOOL LOI] rỗng response"
        content = r.get("content") or []
        text = "".join(c.get("text", "") for c in content)
        if r.get("isError"):
            return "[TOOL LOI] " + text
        return text

    def close(self):
        try:
            self.send_notification("notifications/exit")
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass