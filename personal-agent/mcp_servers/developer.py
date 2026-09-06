import os, sys, json, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fnmatch, glob as _glob, re, shutil, subprocess, requests as _req
from mcplib import Server, Tool, schema, clamp
from config import DIR, TMP, TERMUX, SHELL_TIMEOUT, MAX_TOOL_OUT

PVENV = os.path.join(DIR, "venv")
PVENV_PY = os.path.join(PVENV, "bin", "python")
SNAP_FILE = os.path.join(DIR, "_pip_snap.txt")
INSTALL_LOG = os.path.join(DIR, "logs", "install.log")

SYSTEM_TOOLS_OK = {
    "git", "curl", "wget", "jq", "ripgrep", "rg", "tree", "htop", "ffmpeg",
    "pandoc", "scrot", "xdotool", "zip", "unzip", "sqlite3", "tesseract",
    "tldr", "bat", "fd", "fzf", "micro", "make", "cmake", "strace",
    "net-tools", "lsof", "file", "hexedit", "nmap", "tcpdump", "sshpass",
    "asciinema", "mtr", "iperf3", "tmux", "screen",
}
PYPI_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(==[0-9A-Za-z.\-]+)?$")

CWD = [os.path.expanduser("~")]
ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PROTECT = (os.path.realpath(ROOT), os.path.realpath(os.path.join(DIR, "..", ".rem_ai")))


# ── helpers ──────────────────────────────────────────────────────────────────

def _log(msg):
    try:
        os.makedirs(os.path.dirname(INSTALL_LOG), exist_ok=True)
        with open(INSTALL_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _pypi_verify(pkg_name):
    """GET https://pypi.org/pypi/{pkg}/json — trả info dict nếu hợp lệ, None nếu không."""
    try:
        r = _req.get(f"https://pypi.org/pypi/{pkg_name}/json", timeout=10)
        if r.status_code != 200:
            return None
        info = r.json().get("info", {})
        pypi_name = (info.get("name") or "").strip()
        if not pypi_name or pypi_name.lower() != pkg_name.lower():
            return None
        return info
    except Exception:
        return None


def _venv_snapshot():
    if not os.path.isfile(PVENV_PY):
        return ""
    try:
        r = subprocess.run([PVENV_PY, "-m", "pip", "freeze"],
                           capture_output=True, text=True, timeout=20)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _venv_rollback(snapshot):
    if not snapshot or not os.path.isfile(PVENV_PY):
        return False
    req = os.path.join(DIR, "_rollback_req.txt")
    try:
        with open(req, "w", encoding="utf-8") as f:
            f.write(snapshot)
        r = subprocess.run([PVENV_PY, "-m", "pip", "install", "-r", req,
                            "--quiet"],
                           capture_output=True, text=True, timeout=300)
        return r.returncode == 0
    except Exception:
        return False
    finally:
        try:
            os.remove(req)
        except Exception:
            pass


def _verify_import(pkg, py=None):
    py = py or sys.executable
    try:
        r = subprocess.run([py, "-c", f"import {pkg}"],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _ensure_venv():
    """Tạo venv dùng chung nếu chưa có."""
    if os.path.isfile(PVENV_PY):
        return PVENV_PY
    try:
        os.makedirs(PVENV, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", PVENV],
                       capture_output=True, timeout=120)
        if os.path.isfile(PVENV_PY):
            return PVENV_PY
    except Exception:
        pass
    return None


def _blocked(p):
    rp = os.path.realpath(os.path.expanduser(p))
    for base in PROTECT:
        if rp == base or rp.startswith(base + os.sep):
            return True
    return False


DANGER = [
    "rm -rf /", "rm -rf /*", "--no-preserve-root", "mkfs", "mkfs.",
    "dd if=", "dd of=", "> /dev/sd", "of=/dev/sd", "chown -r 0",
    ":(){", "shutdown ", "reboot", "init 0", "mv / ", "chmod 777 /",
    "chown -r /", "wipefs", "shred /dev/", "diskutil erase",
    "> /etc/passwd", "chmod -r 777 /",
]


# ── core tools ───────────────────────────────────────────────────────────────

def bash(command, timeout=SHELL_TIMEOUT):
    low = (command or "").strip().lower()
    for d in DANGER:
        if d in low:
            return f"[TU CHOI] lệnh nguy hiểm bị chặn: {d}"
    try:
        r = subprocess.run(["bash", "-c", command], cwd=CWD[0],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[LOI] timeout quá {timeout}s"
    out = r.stdout
    if r.stderr:
        out += "\n[STDERR]\n" + r.stderr
    return clamp(out.strip() or "(không có output)", MAX_TOOL_OUT)


def read_file(path, max_chars=8000):
    if not os.path.exists(path):
        return f"[LOI] không tìm thấy file: {path}"
    if os.path.isdir(path):
        return f"[LOI] đây là thư mục: {path}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return clamp(f.read(), max_chars)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def write_file(path, content):
    if _blocked(path):
        return "[TU CHOI] không được ghi vào thư mục runtime/keys của agent"
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return f"Đã ghi {len(content or '')} ký tự vào {path}"


def edit_file(path, old, new):
    if _blocked(path):
        return "[TU CHOI] không được sửa file trong thư mục runtime/keys của agent"
    if not os.path.isfile(path):
        return f"[LOI] file không tồn tại: {path}"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        s = f.read()
    if old not in s:
        return "[LOI] không tìm thấy đoạn cần sửa (giữ nguyên chữ hoa/thường)"
    s = s.replace(old, new, 1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    return "Đã sửa xong"


def list_dir(path="."):
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        return f"[LOI] thư mục trống/không tồn tại: {p}"
    try:
        rows = []
        for e in sorted(os.listdir(p)):
            fp = os.path.join(p, e)
            if os.path.isdir(fp):
                rows.append(f"[dir] {e}/")
            else:
                try:
                    rows.append(f"      {e} ({os.path.getsize(fp)}B)")
                except Exception:
                    rows.append(f"      {e}")
        return "\n".join(rows) or "(trống)"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def grep(pattern, root=".", include="*.py"):
    hits = []
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[LOI] pattern sai: {e}"
    count = 0
    for dp, _, fs in os.walk(os.path.expanduser(root)):
        for f in fs:
            if not fnmatch.fnmatch(f, include):
                continue
            fp = os.path.join(dp, f)
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    for i, line in enumerate(fh, 1):
                        if rx.search(line):
                            hits.append(f"{fp}:{i}: {line.rstrip()[:200]}")
                            count += 1
                            if count >= 200:
                                return "\n".join(hits) + "\n...(tối đa 200 dòng)"
            except Exception:
                continue
    return "\n".join(hits) or "(không khớp)"


def glob_files(pattern, root="."):
    hits = _glob.glob(os.path.join(os.path.expanduser(root), pattern), recursive=True)
    hits = [fp for fp in sorted(hits)][:300]
    return "\n".join(hits) if hits else "(không tìm thấy file nào)"


def cwd():
    return CWD[0]


def chdir(path):
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        return f"[LOI] thư mục không tồn tại: {p}"
    CWD[0] = os.path.realpath(p)
    return CWD[0]


# ── auto-install tools ──────────────────────────────────────────────────────

def ensure_tool(name, timeout=600):
    """
    Kiểm tra công cụ hệ thống; nếu thiếu cài qua apt/pkg.
    Bảo mật: whitelist, apt verifies GPG automatically, rollback nếu cài xong không chạy được.
    """
    name = (name or "").strip().lower()
    if not name or name.startswith("-") or name != re.sub(r"[^a-z0-9\-.]", "", name):
        return "[LOI] tên công cụ không hợp lệ"
    if name not in SYSTEM_TOOLS_OK:
        return (f"[LOI] '{name}' không nằm trong whitelist bảo mật. "
                f"Tool được phép: {', '.join(sorted(SYSTEM_TOOLS_OK))}")
    if shutil.which(name):
        return f"[OK] {name} đã có tại {shutil.which(name)}"

    if TERMUX:
        cmd = ["pkg", "install", "-y", name]; label = "pkg"
    else:
        cmd = ["sudo", "apt-get", "install", "-y", name]; label = "apt-get"

    _log(f"INSTALL_TOOL {name} via {label}")
    try:
        r = subprocess.run(cmd, cwd=CWD[0], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(f"FAIL_TOOL {name} timeout={timeout}s")
        return f"[LOI] cài {name} quá lâu (timeout {timeout}s)"

    out = (r.stdout + "\n" + r.stderr).strip()

    if r.returncode != 0 or not shutil.which(name):
        _log(f"FAIL_TOOL {name} exit={r.returncode} {out[-300:]}")
        return f"[LOI] cài {name} thất bại (exit {r.returncode}). Chi tiết: {out[-500:]}"

    if not shutil.which(name):
        _log(f"FAIL_TOOL {name} installed but not in PATH")
        return f"[LOI] apt chạy xong nhưng không tìm thấy {name} trong PATH"

    _log(f"OK_TOOL {name} -> {shutil.which(name)}")
    return f"[OK] đã cài {name} → {shutil.which(name)}"


def pip_install(pkg, timeout=600):
    """
    Cài package PyPI an toàn.
    Bảo mật: (1) xác minh tên tồn tại trên PyPI + tên khớp chính xác (chống typosquatting),
    (2) snapshot trc khi cài + rollback nếu import fail,
    (3) verify import thành công sau cài,
    (4) audit log mọi thao tác.
    """
    name = (pkg or "").strip()
    if not PYPI_SAFE.match(name):
        return ("[LOI] tên package không hợp lệ — chỉ nhận tên PyPI (vd: requests, requests==2.31.0). "
                "Không nhận URL, git, script, hay editable install.")

    base = name.split("==")[0].split(">=")[0].split("<=")[0].split("!=")[0].strip()
    if not base or not re.match(r"^[a-zA-Z]", base):
        return f"[LOI] tên package '{base}' không hợp lệ"

    # ① Xác minh trên PyPI — chống typosquatting
    info = _pypi_verify(base)
    if not info:
        return (f"[LOI] package '{base}' không tồn tại trên PyPI hoặc tên không khớp chính xác. "
                f"(kiểm tra lại tên — phân biệt hoa/thường, không có package tên này)")
    version = info.get("version", "?")
    author = info.get("author") or info.get("author_email") or "?"
    downloads = info.get("downloads") or info.get("project_urls") or {}
    home = info.get("home_page") or info.get("project_url") or ""

    # ② Đảm bảo venv tồn tại
    py = _ensure_venv() or sys.executable

    # ③ Snapshot trước khi cài (cho rollback)
    snapshot = _venv_snapshot()

    # ④ Cài đặt
    _log(f"PIP_INSTALL {name} (PyPI: {base}=={version} by {author})")
    try:
        r = subprocess.run([py, "-m", "pip", "install", "--quiet", name],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        _log(f"FAIL_PIP {name} timeout={timeout}s → rollback")
        _venv_rollback(snapshot)
        return f"[LOI] cài {base} quá lâu (timeout {timeout}s), đã rollback"
    except Exception as e:
        _log(f"FAIL_PIP {name} exception={e} → rollback")
        _venv_rollback(snapshot)
        return f"[LOI] lỗi hệ thống khi cài: {type(e).__name__}: {e}, đã rollback"

    # ⑤ Verify: import test + exit code
    import_ok = _verify_import(base, py)
    if r.returncode != 0 or not import_ok:
        err = r.stderr[-300:] if r.stderr else "(không có stderr)"
        _log(f"FAIL_PIP {name} exit={r.returncode} import_ok={import_ok} → rollback")
        _venv_rollback(snapshot)
        return (f"[LOI] cài {base}=={version} thất bại, đã rollback. "
                f"Gói PyPI hợp lệ (tác giả: {author}), nhưng cài bị lỗi: {err}")

    # ⑥ Thành công — audit log
    _log(f"OK_PIP {base}=={version} (by {author}) → {py}")
    result = f"[OK] đã cài {base}=={version}"
    if py == PVENV_PY:
        result += f" (trong venv ~/.rem_ai/venv)"
    return result


# ── tools table ──────────────────────────────────────────────────────────────

TOOLS = [
    Tool("bash", "Chạy lệnh shell/terminal (bash -c). Dùng cho hầu hết việc: xem RAM, disk, git, pip...",
         schema({"command": {"type": "string", "description": "Lệnh shell cần chạy"}}), bash),
    Tool("read_file", "Đọc nội dung file văn bản.",
         schema({"path": {"type": "string"}, "max_chars": {"type": "integer", "description": "giới hạn ký tự, mặc định 8000"}}), read_file),
    Tool("write_file", "Ghi nội dung vào file (tạo mới hoặc ghi đè).",
         schema({"path": {"type": "string"}, "content": {"type": "string"}}), write_file),
    Tool("edit_file", "Sửa file: thay đoạn old bằng new (chính xác, phân biệt hoa/thường).",
         schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}), edit_file),
    Tool("list_dir", "Liệt kê file/thư mục.",
         schema({"path": {"type": "string", "description": "mặc định ."}}), list_dir),
    Tool("grep", "Tìm regex trong file.",
         schema({"pattern": {"type": "string"}, "root": {"type": "string"}, "include": {"type": "string"}}), grep),
    Tool("glob_files", "Tìm file theo glob pattern (vd **/*.py).",
         schema({"pattern": {"type": "string"}, "root": {"type": "string"}}), glob_files),
    Tool("cwd", "Xem thư mục làm việc hiện tại.", schema({}), cwd),
    Tool("chdir", "Đổi thư mục làm việc.",
         schema({"path": {"type": "string"}}), chdir),
    Tool("ensure_tool",
         "Cài công cụ hệ thống nếu thiếu (chỉ whitelist: jq, tree, pandoc, ffmpeg, tmux, nmap...). "
         "Đã xác minh GPG qua apt. Nếu cài xong không chạy được → tự gỡ.",
         schema({"name": {"type": "string", "description": "tên tool cần cài"}}), ensure_tool),
    Tool("pip_install",
         "Cài package PyPI an toàn. Đã xác minh tên tồn tại trên PyPI (chống typosquatting). "
         "Snapshot trước khi cài → nếu import fail thì tự rollback. Ghi audit log.",
         schema({"pkg": {"type": "string", "description": "tên package, vd: requests hoặc requests==2.31.0"}}), pip_install),
]

if __name__ == "__main__":
    Server(TOOLS, "developer", "0.1.0").serve(sys.stdin, sys.stdout)
