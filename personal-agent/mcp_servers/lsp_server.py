import os, sys, subprocess, threading, json, time, re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcplib import Server, Tool, schema, clamp

# ---- Cấu hình LSP theo loại file (giống opencode lsp config) ----
# mỗi loại: {"extensions": [...], "command": [lệnh, cờ...], "wait_ms": giây chờ khởi động}
LSP_CONFIG = {
    "c": {"extensions": [".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"], "command": ["clangd", "--background-index"], "wait_ms": 2500},
    "python": {"extensions": [".py"], "command": ["pylsp"], "wait_ms": 3500},
}

_EXT_TO_LANG = {}
for _lang, _cfg in LSP_CONFIG.items():
    for _ext in _cfg["extensions"]:
        _EXT_TO_LANG[_ext] = _lang


class LSPClient:
    """LSP client tối giản nói JSON-RPC qua stdio (không cần thư viện ngoài)."""

    def __init__(self, lang, cwd=None):
        self.lang = lang
        cfg = LSP_CONFIG[lang]
        self.wait_ms = cfg["wait_ms"]
        self.cwd = cwd or os.path.expanduser("~")
        cmd = cfg["command"]
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=self.cwd,
        )
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._diags = {}
        self._opened = {}
        self._ready = threading.Event()
        self._init_reply = None
        self._err = None
        threading.Thread(target=self._reader, daemon=True).start()
        try:
            self._initialize()
        except Exception as e:
            self._err = repr(e)
        self._ready.set()

    def _reader(self):
        # LSP over stdio dùng framing "Content-Length: N\r\n\r\n<JSON>".
        # Đọc qua os.read trên fd thô (tránh buffering của TextIOWrapper), buffer bytes rồi parse frame.
        import os as _os
        fd = self.proc.stdout.fileno()
        buf = b""
        while True:
            try:
                chunk = _os.read(fd, 65536)
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            while True:
                frame_end = buf.find(b"\r\n\r\n")
                if frame_end < 0:
                    break
                header = buf[:frame_end].decode("utf-8", "replace")
                m = re.search(r"Content-Length:\s*(\d+)", header, re.I)
                if not m:
                    buf = buf[frame_end + 4:]
                    continue
                length = int(m.group(1))
                start = frame_end + 4
                if len(buf) < start + length:
                    break
                body = buf[start:start + length]
                buf = buf[start + length:]
                try:
                    msg = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    continue
                self._handle(msg)

    def _handle(self, msg):
        if msg.get("id") is not None:
            # (a) phản hồi cho request của mình
            with self._lock:
                entry = self._pending.pop(msg.get("id"), None)
            if entry:
                entry[1][0] = msg
                entry[0].set()
                return
            # (b) request server → client (vd window/workDoneProgress/create, workspace/...):
            #     phải trả lời, không thì server block chờ (làm mọi thứ kẹt).
            if msg.get("method"):
                self._send({"jsonrpc": "2.0", "id": msg["id"], "result": None})
        elif msg.get("method") == "textDocument/publishDiagnostics":
            uri = msg.get("params", {}).get("uri", "")
            self._diags[uri] = msg.get("params", {}).get("diagnostics", [])

    def rpc(self, method, params=None, timeout=20):
        with self._lock:
            self._id += 1
            mid = self._id
            fut = threading.Event()
            holder = [None]
            self._pending[mid] = (fut, holder)
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        if not fut.wait(timeout):
            with self._lock:
                self._pending.pop(mid, None)
            raise TimeoutError(f"LSP timeout: {method}")
        resp = holder[0]
        if resp is None:
            raise RuntimeError(f"LSP no response: {method}")
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"LSP error {err.get('code')}: {err.get('message')} ({method})")
        return resp.get("result")

    def _send(self, data):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        frame = b"Content-Length: " + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload
        try:
            self.proc.stdin.write(frame)
            self.proc.stdin.flush()
        except Exception:
            pass

    def notify(self, method, params=None):
        try:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})
        except Exception:
            pass

    def _initialize(self):
        root = self.cwd
        root_uri = "file://" + root
        params = {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {
                "textDocument": {
                    "publishDiagnostics": {"relatedInformation": True},
                    "definition": {"linkSupport": True},
                    "hover": {"contentFormat": ["plaintext", "markdown"]},
                },
                "workspace": {"workspaceFolders": True},
            },
            "workspaceFolders": [{"uri": root_uri, "name": os.path.basename(root) or "root"}],
            "clientInfo": {"name": "rem-agent", "version": "3"},
        }
        wait_ms = max(100, self.wait_ms)
        result = self.rpc("initialize", params, timeout=wait_ms / 1000)
        self._init_reply = result
        self.notify("initialized", {})

    def open(self, path):
        """Mở file (didOpen) để server phân tích & đẩy diagnostics."""
        text = self._read_text(path)
        uri = "file://" + os.path.abspath(path)
        self._opened[uri] = self._opened.get(uri, 0) + 1
        self.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": _lang_id(self.lang), "version": self._opened[uri], "text": text},
        })
        return uri, text

    def _ensure_open(self, path):
        """Đảm bảo file đã didOpen; nếu LSP báo chưa thêm file thì gửi lại."""
        uri = "file://" + os.path.abspath(path)
        if uri not in self._opened or self._opened[uri] == 0:
            self.open(path)
        return uri

    @staticmethod
    def _read_text(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            raise RuntimeError(f"không đọc được file: {e}")

    def _pos(self, path, text, line, char):
        uri = "file://" + os.path.abspath(path)
        return {"textDocument": {"uri": uri}, "position": {"line": max(0, line - 1), "character": max(0, char - 1)}}

    def wait_diags(self, uri, seconds=2.0):
        """Chờ một chút để server trả publishDiagnostics sau khi đã didOpen."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._diags.get(uri):
                return self._diags[uri]
            time.sleep(0.1)
        return self._diags.get(uri, [])

    def diagnostics(self, path):
        self._ensure_ready()
        uri, _ = self.open(path)
        # chờ diagnostics đẩy về; lần đầu server (đặc biệt clangd) cần khởi động lâu hơn
        diags = self.wait_diags(uri, seconds=2.0)
        if diags is None:
            diags = []
        if not diags:
            diags = self.wait_diags(uri, seconds=3.0) or []
        return self._fmt_diags(path, diags)

    @staticmethod
    def _fmt_diags(path, diags):
        if not diags:
            return f"(không có lỗi/nghi vấn nào cho {path})"
        lines = []
        sev = {1: "LỖI", 2: "CẢNH BÁO", 3: "INFO", 4: "HINT"}
        for d in diags:
            s = sev.get(d.get("severity"), "INFO")
            pos = d.get("range", {}).get("start", {})
            ln = pos.get("line", 0) + 1
            ch = pos.get("character", 0) + 1
            msg = d.get("message", "")
            src = d.get("source", "")
            lines.append(f"  {path}:{ln}:{ch} [{s}] {msg}" + (f" ({src})" if src else ""))
        n_err = sum(1 for d in diags if d.get("severity") == 1)
        return f"{n_err} lỗi, {len(diags)} vấn đề:\n" + "\n".join(lines)

    def _loc(self, result):
        """Biến kết quả definition/references thành chuỗi vị trí đọc được."""
        if result is None:
            return "(không tìm thấy)"
        items = result if isinstance(result, list) else [result]
        out = {}
        for it in items:
            loc = it
            if isinstance(it, dict) and "targetUri" in it:  # LocationLink
                loc = {"uri": it.get("targetUri"), "range": it.get("targetSelectionRange") or it.get("targetRange")}
            uri = (loc.get("uri") or "").replace("file://", "")
            rng = loc.get("range") or {}
            start = rng.get("start") or {}
            out.setdefault(f"{uri}:{start.get('line', 0) + 1}:{start.get('character', 0) + 1}", 0)
            out[list(out.keys())[-1]] += 1
        return "\n".join(f"  {k}" + (f" ({v} lần)" if len(items) > 1 and v > 1 else "") for k, v in out.items())

    def _ensure_ready(self):
        """LSP server chunk đã dùng được (initialize xong + đã qua cửa sổ khởi động)."""
        self._ready.wait(timeout=max(1, self.wait_ms / 1000 + 1))
        time.sleep(0.6)

    def _retry(self, fn, path, *args, **kws):
        """Gọi request LSP; nếu gặp lỗi 'chưa thêm document' thì mở lại file rồi thử lần nữa."""
        self._ensure_ready()
        self._ensure_open(path)
        try:
            return fn(*args, **kws)
        except RuntimeError as e:
            if "non-added document" in str(e) or "not added" in str(e) or "not open" in str(e):
                time.sleep(0.5)
                self.open(path)
                return fn(*args, **kws)
            raise

    def definition(self, path, line, char):
        text = self._read_text(path)
        params = self._pos(path, text, line, char)
        result = self._retry(lambda: self.rpc("textDocument/definition", params, timeout=20), path)
        return self._loc(result)

    def references(self, path, line, char, include_decl=True):
        self._read_text(path)
        uri = self._ensure_open(path)
        params = {"textDocument": {"uri": uri}, "position": {"line": max(0, line - 1), "character": max(0, char - 1)},
                  "context": {"includeDeclaration": include_decl}}
        result = self._retry(lambda: self.rpc("textDocument/references", params, timeout=20), path)
        return self._loc(result)

    def symbols(self, path):
        uri = self._ensure_open(path)
        result = self._retry(lambda: self.rpc("textDocument/documentSymbol", {"textDocument": {"uri": uri}}, timeout=20), path)
        return self._fmt_symbols(result)

    @staticmethod
    def _fmt_symbols(result, indent=0):
        if not result:
            return "(không tìm thấy symbol nào)"
        if isinstance(result, list) and result and "name" not in result[0] and "location" in result[0]:
            lines = []
            for s in result:
                loc = s.get("location") or {}
                rng = loc.get("range") or {}
                start = rng.get("start") or {}
                lines.append(f"  {start.get('line', 0) + 1}:{s.get('name', '?')}")
            return "\n".join(lines) or "(rỗng)"
        lines = []
        pad = "  " * indent
        for s in result or []:
            sname = s.get("name", "?")
            skind = s.get("kind", 0)
            rng = s.get("selectionRange") or s.get("range") or {}
            start = rng.get("start") or {}
            lines.append(f"{pad}{start.get('line', 0) + 1}:{start.get('character', 0) + 1}: {sname} ({skind})")
            children = s.get("children")
            if children:
                lines.append(LSPClient._fmt_symbols(children, indent + 1))
        return "\n".join(lines) if lines else "(rỗng)"

    def hover(self, path, line, char):
        text = self._read_text(path)
        params = self._pos(path, text, line, char)
        result = self._retry(lambda: self.rpc("textDocument/hover", params, timeout=20), path)
        if not result:
            return "(không có thông tin hover)"
        return self._fmt_hover(result)

    @staticmethod
    def _fmt_hover(result):
        contents = result.get("contents")
        parts = []
        if isinstance(contents, str):
            parts.append(contents)
        elif isinstance(contents, list):
            for c in contents:
                if isinstance(c, str):
                    parts.append(c)
                elif isinstance(c, dict):
                    parts.append(c.get("value", ""))
        elif isinstance(contents, dict):
            parts.append(contents.get("value", ""))
        return "\n".join(x for x in parts if x) or "(rỗng)"

    def close(self):
        try:
            self.shutdown = self.rpc("shutdown", {}, timeout=5)
        except Exception:
            pass
        try:
            self.notify("exit")
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass


def _lang_id(lang):
    return {"python": "python", "c": "cpp"}.get(lang, "plaintext")


# ---- Trạng thái client (cache theo thư mục + ngôn ngữ) ----
_lock = threading.Lock()
_clients = {}  # (cwd, lang) -> [LSPClient, last_used]
_MAX_CLIENTS = 4  # quá số này → đuổi client lâu không dùng (trước đây mở bao nhiêu
# clangd/pylsp cũng sống mãi → rò rỉ tiến trình khi làm nhiều thư mục)


def _client_touch(key, cl):
    """Ghi nhận dùng client; trả về client bị đuổi (đóng NGOÀI khóa để khỏi kẹt)."""
    _clients[key] = [cl, time.time()]
    victim = None
    while len(_clients) > _MAX_CLIENTS:
        old = min(_clients, key=lambda k: _clients[k][1])
        if old == key:
            break
        victim = _clients.pop(old, [None])[0]
        break
    return victim


def _close_quiet(cl):
    try:
        if cl is not None:
            cl.close()
    except Exception:
        pass


def _client_for(path, cwd):
    ext = os.path.splitext(path)[1].lower()
    lang = _EXT_TO_LANG.get(ext)
    if not lang:
        return None, f"(chưa hỗ trợ loại file này: {ext or '(không có đuôi)'}. Hỗ trợ: {', '.join(sorted(set(_EXT_TO_LANG.values())))} )"
    key = (cwd, lang)
    with _lock:
        hit = _clients.get(key)
        if hit is not None:
            hit[1] = time.time()
            return hit[0], None
        try:
            cl = LSPClient(lang, cwd=cwd)
        except Exception as e:
            return None, f"[LOI] không khởi động được LSP '{lang}': {type(e).__name__}: {e}"
        victim = _client_touch(key, cl)
    _close_quiet(victim)
    return cl, None


def _int(v, default):
    """Ép số dòng/cột an toàn (LLM có thể gửi chuỗi)."""
    try:
        v = int(v)
        return v if v > 0 else default
    except Exception:
        return default


def _resolve(file, root="."):
    p = file if os.path.isabs(file) else os.path.join(os.path.expanduser(root), file)
    return os.path.realpath(p)


def _py_syntax_check(path):
    """Fallback chắc chắn cho Python: parse bằng ast để bắt lỗi cú pháp (pylsp không đẩy diag nếu thiếu pyflakes)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
        import ast
        ast.parse(src)
        return ""
    except SyntaxError as e:
        ln = e.lineno or 1
        ch = (e.offset or 0) + 1
        return f"  {path}:{ln}:{ch} [LỖI] {e.msg}\n  {e.text or ''}\n  {' ' * (ch - 1)}^"
    except Exception as e:
        return f"  {path}:1:1 [LỖI] {type(e).__name__}: {e}"


def lsp_diagnostics(file, root="."):
    path = _resolve(file, root)
    if not os.path.isfile(path):
        return f"[LOI] file không tồn tại: {path}"
    # Python: kiểm tra cú pháp bằng ast TRƯỚC (không cần LSP vẫn bắt được lỗi).
    # Trước đây thiếu pylsp là trả LOI luôn, phí mất check miễn phí này.
    extra = _py_syntax_check(path) if path.lower().endswith(".py") else ""
    cl, err = _client_for(path, os.path.dirname(path) or os.getcwd())
    if err:
        if extra:
            return extra
        if path.lower().endswith(".py"):
            return f"(cú pháp Python OK — {err} nên chưa kiểm tra sâu được)"
        return err
    try:
        lsp_out = clamp(cl.diagnostics(path), 4000)
        if extra and "không có lỗi" in lsp_out:
            return extra
        if extra:
            return lsp_out + "\n" + extra
        return lsp_out
    except Exception as e:
        return extra or f"[LOI LSP] {type(e).__name__}: {e}"


def lsp_definition(file, line=1, char=1):
    path = _resolve(file)
    if not os.path.isfile(path):
        return f"[LOI] file không tồn tại: {path}"
    cl, err = _client_for(path, os.path.dirname(path) or os.getcwd())
    if err:
        return err
    try:
        return cl.definition(path, _int(line, 1), _int(char, 1))
    except Exception as e:
        return f"[LOI LSP] {type(e).__name__}: {e}"


def lsp_references(file, line=1, char=1):
    path = _resolve(file)
    if not os.path.isfile(path):
        return f"[LOI] file không tồn tại: {path}"
    cl, err = _client_for(path, os.path.dirname(path) or os.getcwd())
    if err:
        return err
    try:
        return cl.references(path, _int(line, 1), _int(char, 1))
    except Exception as e:
        return f"[LOI LSP] {type(e).__name__}: {e}"


def lsp_symbols(file):
    path = _resolve(file)
    if not os.path.isfile(path):
        return f"[LOI] file không tồn tại: {path}"
    cl, err = _client_for(path, os.path.dirname(path) or os.getcwd())
    if err:
        return err
    try:
        return clamp(cl.symbols(path), 4000)
    except Exception as e:
        return f"[LOI LSP] {type(e).__name__}: {e}"


def lsp_hover(file, line=1, char=1):
    path = _resolve(file)
    if not os.path.isfile(path):
        return f"[LOI] file không tồn tại: {path}"
    cl, err = _client_for(path, os.path.dirname(path) or os.getcwd())
    if err:
        return err
    try:
        return cl.hover(path, _int(line, 1), _int(char, 1))
    except Exception as e:
        return f"[LOI LSP] {type(e).__name__}: {e}"


def lsp_supported():
    langs = []
    for lang, cfg in LSP_CONFIG.items():
        ok = "có" if _which(cfg["command"][0]) else "thiếu"
        langs.append(f"  {lang} ({', '.join(cfg['extensions'])}): lệnh '{cfg['command'][0]}' {ok}")
    return "\n".join(langs)


def _which(cmd):
    from shutil import which
    return which(cmd)


def close_all():
    with _lock:
        for cl in list(_clients.values()):
            try:
                cl.close()
            except Exception:
                pass
        _clients.clear()


TOOLS = [
    Tool("lsp_diagnostics", "Xem lỗi/cảnh báo do Language Server phân tích cho một file (loại .py, .c/.cpp...). "
         "Gọi sau khi sửa code để kiểm tra sạch.",
         schema({"file": {"type": "string", "description": "đường dẫn file"}, "root": {"type": "string", "description": "thư mục gốc, mặc định ."}}), lsp_diagnostics),
    Tool("lsp_definition", "Tìm nơi khai báo/định nghĩa của symbol tại vị trí (dòng, cột) trong file.",
         schema({"file": {"type": "string"}, "line": {"type": "integer", "description": "dòng (từ 1)"}, "char": {"type": "integer", "description": "cột (từ 1)"}}), lsp_definition),
    Tool("lsp_references", "Tìm mọi chỗ tham chiếu tới symbol tại vị trí (dòng, cột) trong file.",
         schema({"file": {"type": "string"}, "line": {"type": "integer"}, "char": {"type": "integer"}}), lsp_references),
    Tool("lsp_symbols", "Liệt kê symbol (hàm, class, biến...) trong file — bản đồ cấu trúc code.",
         schema({"file": {"type": "string"}}), lsp_symbols),
    Tool("lsp_hover", "Xem thông tin kiểu/mô tả của symbol tại vị trí (dòng, cột) trong file.",
         schema({"file": {"type": "string"}, "line": {"type": "integer"}, "char": {"type": "integer"}}), lsp_hover),
    Tool("lsp_supported", "Xem danh sách ngôn ngữ được hỗ trợ bởi LSP và trạng thái lệnh.",
         schema({}), lambda: lsp_supported()),
]

if __name__ == "__main__":
    srv = Server(TOOLS, "lsp", "0.1.0")
    try:
        srv.serve(sys.stdin, sys.stdout)
    finally:
        close_all()
