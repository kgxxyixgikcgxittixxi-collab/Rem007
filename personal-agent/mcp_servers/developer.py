import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fnmatch, glob as _glob, re, shutil, subprocess
from mcplib import Server, Tool, schema, clamp
from config import DIR, TMP, TERMUX, SHELL_TIMEOUT, MAX_TOOL_OUT

PVENV = os.path.join(DIR, "venv")
PVENV_PY = os.path.join(PVENV, "bin", "python")

SYSTEM_TOOLS_OK = [
    "git", "curl", "wget", "jq", "ripgrep", "rg", "tree", "htop", "ffmpeg",
    "pandoc", "scrot", "xdotool", "zip", "unzip", "sqlite3", "tesseract",
    "tldr", "bat", "fd", "fzf", "micro", "make", "cmake",
]
PYPI_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(\[.*\])?(==[0-9A-Za-z.\-]+)?$")

CWD = [os.path.expanduser("~")]
ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PROTECT = (os.path.realpath(ROOT), os.path.realpath(os.path.join(DIR, "..", ".rem_ai")))


def _blocked(p):
    rp = os.path.realpath(os.path.expanduser(p))
    for base in PROTECT:
        if rp == base or rp.startswith(base + os.sep):
            return True
    return False

DANGER = [
    "rm -rf /", "rm -rf /*", "--no-preserve-root", "mkfs", "mkfs.", "dd if=", "dd of=",
    "> /dev/sd", "of=/dev/sd", "chown -r 0", ":(){", "shutdown ", "reboot", "init 0",
    "mv / ", "chmod 777 /", "chown -r /", "wipefs", "shred /dev/", "diskutil erase",
    "> /etc/passwd", "chmod -r 777 /",
]


def bash(command, timeout=SHELL_TIMEOUT):
    low = (command or "").strip().lower()
    for d in DANGER:
        if d in low:
            return f"[TU CHOI] lệnh nguy hiểm bị chặn: {d}"
    try:
        r = subprocess.run(
            ["bash", "-c", command], cwd=CWD[0],
            capture_output=True, text=True, timeout=timeout,
        )
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
        return f"[LOI] thư mục trống/tồn tại: {p}"
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


def ensure_tool(name, timeout=600):
    """Kiểm tra công cụ hệ thống; nếu thiếu thì cài qua apt/pkg. Chỉ chấp nhận tên trong whitelist."""
    name = (name or "").strip().lower()
    if not name or name != re.sub(r"[^a-z0-9\-.]", "", name):
        return "[LOI] tên công cụ không hợp lệ (chỉ chữ thường, số, dấu - và .)"
    if name.startswith("-"):
        return "[LOI] tên công cụ không hợp lệ"
    if name not in SYSTEM_TOOLS_OK:
        return f"[LOI] '{name}' không nằm trong danh sách tool được phép tự cài: {', '.join(SYSTEM_TOOLS_OK)}"
    if shutil.which(name):
        return f"[OK] {name} đã có sẵn tại {shutil.which(name)}"
    if TERMUX:
        cmd = ["pkg", "install", "-y", name]
        label = "pkg"
    else:
        cmd = ["sudo", "apt-get", "install", "-y", name]
        label = "sudo apt-get"
    try:
        r = subprocess.run(cmd, cwd=CWD[0], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[LOI] cài {name} quá lâu (timeout {timeout}s)"
    out = (r.stdout + "\n" + r.stderr).strip()
    if r.returncode != 0:
        return f"[LOI] {label} thất bại (exit {r.returncode}): {out[-800:]}"
    if shutil.which(name):
        return f"[OK] đã cài {name} → {shutil.which(name)}"
    return f"[LOI] {label} chạy xong nhưng không thấy {name} trong PATH. Chi tiết: {out[-500:]}"


def pip_install(pkg, timeout=600):
    """Cài gói Python an toàn. Nếu hệ thống chặn (PEP 668/externally-managed) sẽ tạo venv dùng chung ~/.rem_ai/venv."""
    name = (pkg or "").strip()
    if not PYPI_SAFE.match(name):
        return ("[LOI] tên gói không hợp lệ — chỉ nhận tên PyPI (vd requests, requests==2.31.0), "
                "không nhận URL/git/script tùy ý.")
    ok = [("python3", sys.executable)]
    if os.path.isdir(PVENV) and os.path.isfile(PVENV_PY):
        ok.append(("venv", PVENV_PY))
    tried = []
    for label, py in ok:
        r = subprocess.run([py, "-m", "pip", "install", "--quiet", "--upgrade", name],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return f"[OK] đã cài {name} bằng {label} ({py})"
        tried.append(f"{label}: {r.stderr[-300:] or r.stdout[-300:]}")
    # PEP 668 external-managed → tạo venv dùng chung
    try:
        os.makedirs(PVENV, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", PVENV], capture_output=True, timeout=120)
        r = subprocess.run([PVENV_PY, "-m", "pip", "install", "--quiet", "--upgrade", name],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return f"[OK] hệ thống chặn pip (PEP 668) → đã cài {name} vào venv ~/.rem_ai/venv. Dùng {PVENV_PY} để import trong script."
        return f"[LOI] pip {name} thất bại kể cả trong venv: {r.stderr[-400:]}"
    except subprocess.TimeoutExpired:
        return f"[LOI] cài {name} quá lâu (timeout {timeout}s)"
    except Exception as e:
        return f"[LOI] tạo venv thất bại: {type(e).__name__}: {e}"


TOOLS = [
    Tool("bash", "Chạy lệnh shell/terminal trên máy (bash -c). Dùng cho hầu hết việc: xem RAM/disk, chạy script, git, pip, cài gói...",
         schema({"command": {"type": "string", "description": "Lệnh shell cần chạy"}}), bash),
    Tool("read_file", "Đọc nội dung file văn bản.",
         schema({"path": {"type": "string"}, "max_chars": {"type": "integer", "description": "giới hạn ký tự, mặc định 8000"}}), read_file),
    Tool("write_file", "Ghi nội dung vào file (tạo mới hoặc ghi đè).",
         schema({"path": {"type": "string"}, "content": {"type": "string"}}), write_file),
    Tool("edit_file", "Sửa file: thay đoạn `old` bằng `new` (chính xác, có phân biệt hoa/thường).",
         schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}), edit_file),
    Tool("list_dir", "Liệt kê file/thư mục.",
         schema({"path": {"type": "string", "description": "mặc định ."}}), list_dir),
    Tool("grep", "Tìm pattern regex trong file thuộc thư mục.",
         schema({"pattern": {"type": "string"}, "root": {"type": "string", "description": "mặc định ."}, "include": {"type": "string", "description": "vd *.py"}}), grep),
    Tool("glob_files", "Tìm file theo pattern (vd **/*.py).",
         schema({"pattern": {"type": "string"}, "root": {"type": "string", "description": "mặc định ."}}), glob_files),
    Tool("cwd", "Xem thư mục làm việc hiện tại.", schema({}), cwd),
    Tool("chdir", "Đổi thư mục làm việc cho các lệnh bash sau đó.",
         schema({"path": {"type": "string"}}), chdir),
    Tool("ensure_tool", "Kiểm tra công cụ hệ thống (vd jq, ffmpeg, pandoc...) đã có chưa; nếu thiếu sẽ tự cài bằng apt/pkg. Chỉ cài các tool trong whitelist.",
         schema({"name": {"type": "string", "description": "tên công cụ, vd: jq, tree, ffmpeg"}}), ensure_tool),
    Tool("pip_install", "Cài gói Python (PyPI) nếu script cần nhưng chưa có. Bị PEP 668 chặn thì tự tạo venv dùng chung ~/.rem_ai/venv.",
         schema({"pkg": {"type": "string", "description": "tên gói PyPI, vd: requests hoặc requests==2.31.0"}}), pip_install),
]

if __name__ == "__main__":
    Server(TOOLS, "developer", "0.1.0").serve(sys.stdin, sys.stdout)