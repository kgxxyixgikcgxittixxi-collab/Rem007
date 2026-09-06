import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fnmatch, glob as _glob, re, subprocess
from mcplib import Server, Tool, schema, clamp
from config import DIR, SHELL_TIMEOUT, MAX_TOOL_OUT

CWD = [os.path.expanduser("~")]

DANGER = [
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "> /dev/sd", "chown -r 0",
    ":(){", "shutdown ", "reboot", "init 0", "mv / ", "chmod 777 /",
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


def read_file(path, max_chars=40000):
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
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return f"Đã ghi {len(content or '')} ký tự vào {path}"


def edit_file(path, old, new):
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


TOOLS = [
    Tool("bash", "Chạy lệnh shell/terminal trên máy (bash -c). Dùng cho hầu hết việc: xem RAM/disk, chạy script, git, pip, cài gói...",
         schema({"command": {"type": "string", "description": "Lệnh shell cần chạy"}}), bash),
    Tool("read_file", "Đọc nội dung file văn bản.",
         schema({"path": {"type": "string"}, "max_chars": {"type": "integer", "description": "giới hạn ký tự, mặc định 40000"}}), read_file),
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
]

if __name__ == "__main__":
    Server(TOOLS, "developer", "0.1.0").serve(sys.stdin, sys.stdout)