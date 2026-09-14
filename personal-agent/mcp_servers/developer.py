import os, sys, json, time, difflib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fnmatch, glob as _glob, re, shutil, subprocess, requests as _req
from mcplib import Server, Tool, schema, clamp
from config import DIR, TMP, TERMUX, SHELL_TIMEOUT, MAX_TOOL_OUT

PVENV = os.path.join(DIR, "venv")
PVENV_PY = os.path.join(PVENV, "bin", "python")
SNAP_FILE = os.path.join(DIR, "_pip_snap.txt")
INSTALL_LOG = os.path.join(DIR, "logs", "install.log")
TODO_DIR = os.path.join(DIR, "todos")
os.makedirs(TODO_DIR, exist_ok=True)

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


# PyPI name → import name (một số gói tên pip khác tên import).
# vd pip_install("python-pptx") phải verify bằng `import pptx`, không phải `import python-pptx`.
IMPORT_ALIASES = {
    "python-pptx": "pptx",
    "pillow": "PIL",
    "scikit-learn": "sklearn",
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "pyyaml": "yaml",
    "python-dateutil": "dateutil",
    "beautifulsoup4": "bs4",
    "discord-py": "discord",
    "flask-cors": "flask_cors",
    "flask-socketio": "flask_socketio",
    "python-telegram-bot": "telegram",
    "pyjwt": "jwt",
    "pymongo": "pymongo",
    "sqlalchemy": "sqlalchemy",
    "psycopg2-binary": "psycopg2",
    "mysql-connector-python": "mysql.connector",
    "google-api-python-client": "googleapiclient",
    "python-dotenv": "dotenv",
    "pygithub": "github",
    "edge-tts": "edge_tts",
}


def _verify_import(pkg, py=None):
    py = py or sys.executable
    mod = IMPORT_ALIASES.get(pkg.lower(), pkg.replace("-", "_"))
    try:
        r = subprocess.run([py, "-c", f"import {mod}"],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def _ensure_venv():
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
    "chmod -r 777 /",
]


def _suggest_similar(path, root="."):
    """Tìm file gần nhất khi path không tồn tại."""
    base = os.path.basename(path)
    if not base:
        return ""
    candidates = []
    for dp, _, fs in os.walk(os.path.expanduser(root)):
        for f in fs:
            candidates.append(os.path.join(dp, f))
        if len(candidates) > 500:
            break
    matches = difflib.get_close_matches(base, [os.path.basename(c) for c in candidates], n=3, cutoff=0.4)
    if not matches:
        return ""
    full = [next((c for c in candidates if os.path.basename(c) == m), m) for m in matches]
    return "Gợi ý: " + ", ".join(full)


def _auto_format(path):
    """Tự format code sau khi ghi nếu formatter có trong PATH."""
    ext = os.path.splitext(path)[1]
    formatters = {
        ".py": [("ruff", ["format", "--quiet", path]), ("black", ["-q", path])],
        ".js": [("prettier", ["--write", "-q", path])],
        ".ts": [("prettier", ["--write", "-q", path])],
        ".json": [("prettier", ["--write", "-q", path])],
        ".md": [("prettier", ["--write", "-q", path])],
    }
    cmds = formatters.get(ext)
    if not cmds:
        return ""
    for name, args in cmds:
        if shutil.which(name):
            try:
                r = subprocess.run(args, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return f" (đã format bằng {name})"
            except Exception:
                pass
    return ""


def _make_diff(old, new, path="<file>"):
    """Tạo unified diff NGẮN giữa old và new. KHÔNG màu ANSI — kết quả tool
    được đưa vào context LLM. Cắt gọn để không làm tràn context (file mới lớn
    sẽ chỉ hiện số dòng, không nhân đôi toàn bộ nội dung)."""
    if not old and new and len(new) > 4000:
        n = len(new.splitlines())
        return f"(tạo mới file {path}: {len(new)} ký tự, {n} dòng)"
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = (new or "").splitlines(keepends=True)
    diff = list(difflib.unified_diff(old_lines, new_lines,
                                     fromfile=f"a/{path}", tofile=f"b/{path}",
                                     lineterm="", n=2))
    if not diff:
        return "(không có thay đổi)"
    # cắt gọn: tối đa 80 dòng / ~2500 ký tự
    body = diff[:80]
    out = []
    for ln in body:
        out.append(ln if ln.endswith("\n") else ln + "\n")
    if len(diff) > 80:
        out.append(f"...({len(diff) - 80} dòng nữa)\n")
    text = "".join(out).rstrip()
    return text[:2500] + f"\n...(diff cắt {max(0, len(text) - 2500)} ký tự)" if len(text) > 2500 else text


# ── todo storage (per-session JSON) ────────────────────────────────────────

def _todo_path(sid):
    return os.path.join(TODO_DIR, f"{sid}.json")


def _todo_load(sid):
    try:
        with open(_todo_path(sid), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _todo_save(sid, todos):
    with open(_todo_path(sid), "w", encoding="utf-8") as f:
        json.dump(todos, f, ensure_ascii=False, indent=2)


# ── core tools ──────────────────────────────────────────────────────────────

JOB_DIR = os.path.join(DIR, "jobs")
os.makedirs(JOB_DIR, exist_ok=True)


def _job_file(pid):
    return os.path.join(JOB_DIR, f"{pid}.json")


def _job_save(pid, cmd, out_path, err_path):
    try:
        with open(_job_file(pid), "w", encoding="utf-8") as f:
            json.dump({"pid": pid, "cmd": cmd[:500], "out": out_path,
                       "err": err_path, "started": time.strftime("%H:%M:%S")}, f)
    except Exception:
        pass


def _job_alive(pid):
    try:
        pid = int(pid)
    except Exception:
        return False
    # đọc /proc để phân biệt zombie (đã xong nhưng chưa reap) với đang chạy thật
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            parts = f.read().rsplit(")", 1)[-1].split()
            if parts and parts[0] == "Z":
                return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def bash_poll(pid="", action="poll"):
    """Kiểm tra/giết/liệt kê job bash chạy nền (job tự tạo khi bash timeout).
    action: poll (xem trạng thái + output mới) | kill (dừng job) | list (liệt kê)."""
    act = (action or "poll").strip().lower()
    if act == "list" or (not pid and act != "kill"):
        rows = []
        try:
            for fn in sorted(os.listdir(JOB_DIR)):
                if not fn.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(JOB_DIR, fn), encoding="utf-8") as f:
                        j = json.load(f)
                except Exception:
                    continue
                st = "chạy" if _job_alive(j.get("pid", -1)) else "xong"
                rows.append(f"- {j.get('pid')} [{st}] từ {j.get('started', '?')}: {str(j.get('cmd', ''))[:80]}")
        except Exception:
            pass
        return "\n".join(rows) if rows else "(không có job nền nào)"
    try:
        jf = _job_file(int(str(pid).strip()))
        with open(jf, encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return f"[LOI] không có job {pid} (dùng bash_poll list để xem)"
    if act == "kill":
        try:
            import signal
            os.kill(int(j["pid"]), signal.SIGTERM)
            time.sleep(0.5)
            if _job_alive(j["pid"]):
                os.kill(int(j["pid"]), signal.SIGKILL)
            return f"Đã dừng job {j['pid']}."
        except Exception as e:
            return f"[LOI] không dừng được job {pid}: {e}"
    # poll
    alive = _job_alive(j["pid"])
    try:
        with open(j.get("out", ""), encoding="utf-8", errors="replace") as f:
            out = f.read()[-MAX_TOOL_OUT:]
    except Exception:
        out = ""
    try:
        with open(j.get("err", ""), encoding="utf-8", errors="replace") as f:
            err = f.read()[-500:]
    except Exception:
        err = ""
    tail = (out + ("\n[STDERR]\n" + err if err.strip() else "")).strip() or "(chưa có output)"
    return f"Job {j['pid']} [{'ĐANG CHẠY' if alive else 'ĐÃ XONG'}] — {j.get('cmd', '')[:120]}\n{tail}"


def bash(command, timeout=SHELL_TIMEOUT):
    low = (command or "").strip().lower()
    for d in DANGER:
        if d in low:
            return f"[TU CHOI] lệnh nguy hiểm bị chặn: {d}"
    try:
        timeout = max(1, min(int(timeout or SHELL_TIMEOUT), 600))
    except Exception:
        timeout = SHELL_TIMEOUT
    # Chạy qua FILE thay vì pipe: subprocess.run với capture_output=True sẽ HANG mãi
    # khi lệnh spawn tiến trình nền (`cmd &`) — tiến trình con giữ pipe stdout mở nên
    # run() chờ EOF không bao giờ tới. redirect ra file thì shell thoát là run() trả về
    # ngay, tiến trình nền tiếp tục ghi file không sao.
    import tempfile
    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".out", delete=False) as fo, \
         tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".err", delete=False) as fe:
        out_path, err_path = fo.name, fe.name
        fo.write(""); fe.write("")
    try:
        with open(out_path, "w") as fo, open(err_path, "w") as fe:
            try:
                r = subprocess.run(["bash", "-c", command], cwd=CWD[0],
                                   stdin=subprocess.DEVNULL,
                                   stdout=fo, stderr=fe, timeout=timeout)
            except subprocess.TimeoutExpired:
                code = "timeout"
            else:
                code = r.returncode
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as fo:
            out = fo.read()
        with open(err_path, "r", encoding="utf-8", errors="replace") as fe:
            err = fe.read()
    finally:
        for p in (out_path, err_path):
            try:
                os.remove(p)
            except Exception:
                pass
    if code == "timeout":
        # CHỐNG KẸT TRIỆT ĐỂ: lệnh dài không giết — chuyển chạy nền, agent poll tiếp.
        import tempfile as _tf
        _fo = _tf.NamedTemporaryFile("w", encoding="utf-8", suffix=".out", delete=False)
        _fe = _tf.NamedTemporaryFile("w", encoding="utf-8", suffix=".err", delete=False)
        _fo.close(); _fe.close()
        try:
            p = subprocess.Popen(["bash", "-c", command], cwd=CWD[0],
                                 stdin=subprocess.DEVNULL,
                                 stdout=open(_fo.name, "w"), stderr=open(_fe.name, "w"),
                                 start_new_session=True)
            _job_save(p.pid, command, _fo.name, _fe.name)
            part = (out + "\n[STDERR]\n" + err)[-500:] if (out or err) else ""
            return (f"[CHẠY NỀN] lệnh quá {timeout}s nên đã chuyển chạy nền (job {p.pid}). "
                    f"CẤM chạy lại lệnh này — dùng bash_poll(pid='{p.pid}') để xem tiến độ/kết quả.\n{part}").strip()
        except Exception as e:
            return f"[LOI] timeout quá {timeout}s và không chuyển nền được: {e}"
    if err:
        out += "\n[STDERR]\n" + err
    return clamp(out.strip() or "(không có output)", MAX_TOOL_OUT)


def read_file(path, max_chars=8000, offset=0, limit=0):
    """Đọc file với line numbers. offset/limit = dòng (1-indexed). Thiếu file → gợi ý file gần nhất.
    Đọc THEO LUỒNG, dừng khi vượt max_chars — file log chục GB không OOM như readlines() cũ."""
    if not os.path.exists(path):
        suggest = _suggest_similar(path)
        return f"[LOI] không tìm thấy file: {path}" + (f"\n{suggest}" if suggest else "")
    if os.path.isdir(path):
        return f"[LOI] đây là thư mục: {path}"
    try:
        max_chars = int(max(max_chars or 0, 1000))   # trần đọc tối thiểu 1KB
    except Exception:
        max_chars = 8000
    try:
        offset = int(offset or 0)
        limit = int(limit or 0)
    except Exception:
        offset, limit = 0, 0
    lines = []
    total = 0
    seen = 0
    acc = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            while True:
                ln = f.readline()
                if not ln:
                    break
                seen += 1
                if offset > 0 and seen < offset:
                    continue
                total += 1
                if limit > 0 and total > limit:
                    acc += len(ln)   # vẫn tính tổng số dòng (để báo "còn N dòng")
                    continue
                lines.append(ln)
                acc += len(ln)
                if acc > max_chars * 2 and total >= 800:
                    break            # đủ dữ liệu + về dòng → dừng đọc, không nạp cả file
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    if offset > 0 or limit > 0:
        start = max(0, (offset or 1) - 1)
        end = start + len(lines)
        numbered = [f"{i + 1:4d}: {ln.rstrip()}" for i, ln in enumerate(lines, start)]
        header = f"File: {path} (dòng {max(start, 1)}-{start + len(lines)}/{seen})"
        if start + len(lines) < seen:
            header += f" — còn {seen - (start + len(lines))} dòng nữa"
        return clamp(header + "\n" + "\n".join(numbered), max_chars)
    numbered = [f"{i + 1:4d}: {ln.rstrip()}" for i, ln in enumerate(lines)]
    return clamp("\n".join(numbered), max_chars)


def write_file(path, content):
    if _blocked(path):
        return "[TU CHOI] không được ghi vào thư mục runtime/keys của agent"
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    # diff preview
    old = ""
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                old = f.read()
        except Exception:
            pass
    diff = _make_diff(old, content, path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    fmt = _auto_format(path)
    return f"Đã ghi {len(content or '')} ký tự vào {path}{fmt}\n{diff}"


def edit_file(path, old, new):
    if _blocked(path):
        return "[TU CHOI] không được sửa file trong thư mục runtime/keys của agent"
    if not os.path.isfile(path):
        suggest = _suggest_similar(path)
        return f"[LOI] file không tồn tại: {path}" + (f"\n{suggest}" if suggest else "")
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        s = f.read()
    if old not in s:
        return "[LOI] không tìm thấy đoạn cần sửa (giữ nguyên chữ hoa/thường)"
    count = s.count(old)
    orig = s
    s = s.replace(old, new, 1)
    diff = _make_diff(orig, s, path)
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)
    note = f" (có {count} lần trùng, chỉ sửa lần đầu)" if count > 1 else ""
    return f"Đã sửa xong{note}\n{diff}"


_HEAVY_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build",
               ".cache", ".next", ".turbo", "target", "vendor", ".hg", ".svn"}

def _apply_hunks(orig_text, plines):
    """Áp nhiều hunk (bottom-up theo tọa độ old) — không lệch khi có nhiều hunk/xóa."""
    # parse hunks: (old_start, old_count, body_lines)
    hunks = []
    cur_old = cur_cnt = None
    cur_body = []
    for ln in plines:
        if ln.startswith("@@"):
            if cur_old is not None:
                hunks.append((cur_old, cur_cnt, cur_body))
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", ln)
            if m:
                cur_old = int(m.group(1))
                cur_cnt = int(m.group(2) or "1")
            else:
                cur_old = None
                cur_cnt = 0
            cur_body = []
        elif cur_old is not None:
            if ln.startswith(" ") or ln.startswith("+") or ln.startswith("-") or ln.startswith("\\"):
                cur_body.append(ln)
    if cur_old is not None:
        hunks.append((cur_old, cur_cnt, cur_body))
    if not hunks:
        return None  # không có hunk hợp lệ
    lines = orig_text.splitlines(True)
    # bottom-up: hunk sau (old_start lớn) áp trước → không lệch tọa độ
    for old_start, old_cnt, body in sorted(hunks, key=lambda x: x[0], reverse=True):
        s = max(0, old_start - 1)
        e = min(len(lines), s + old_cnt)
        # kiểm tra context (tùy chọn): nếu lệch nhiều thì không crash, vẫn áp
        repl = []
        old_pos = s
        for b in body:
            if b.startswith("\\"):
                continue
            lead = b[0] if b else " "
            raw = b[1:] if len(b) > 1 else ""
            if lead == " ":
                repl.append(lines[old_pos] if old_pos < len(lines) else raw + "\n")
                old_pos += 1
            elif lead == "-":
                old_pos += 1
            elif lead == "+":
                repl.append(raw + ("\n" if not raw.endswith("\n") else ""))
        # thay đoạn [s:e) bằng repl (repl đã chứa context + dòng mới)
        lines[s:e] = repl
    return "".join(lines)


def apply_patch(patch_text):
    """Apply unified diff patch — tạo/sửa/xoá nhiều file trong 1 lần."""
    if not patch_text or not patch_text.strip():
        return "[LOI] patch_text trống"
    lines = patch_text.splitlines()
    # parse files from diff headers — bỏ qua /dev/null (tạo/xóa file)
    patches = []
    current_file = None
    current_lines = []
    for ln in lines:
        if ln.startswith("+++ b/"):
            if current_file and current_lines:
                patches.append((current_file, current_lines))
            current_file = ln[6:]
            current_lines = []
        elif ln.startswith("+++ /dev/null"):
            if current_file and current_lines:
                patches.append((current_file, current_lines))
            current_file = None  # xóa file — để hunk của file trước xử lý, bỏ qua /dev/null
            current_lines = []
        elif ln.startswith("+++ /"):
            if current_file and current_lines:
                patches.append((current_file, current_lines))
            p = ln[6:]
            if p == "/dev/null":
                current_file = None
                current_lines = []
            else:
                current_file = p
                current_lines = []
        elif ln.startswith("--- a/") or ln.startswith("--- /"):
            pass
        elif ln.startswith("@@") or ln.startswith("+") or ln.startswith("-") or ln.startswith(" "):
            if current_file:
                current_lines.append(ln)
        elif ln.startswith("\\"):
            if current_file:
                current_lines.append(ln)
    if current_file and current_lines:
        patches.append((current_file, current_lines))
    if not patches:
        return "[LOI] không tìm thấy diff header (cần +++ b/path)"
    results = []
    for fpath, plines in patches:
        if _blocked(fpath):
            results.append(f"[TU CHOI] {fpath}: blocked")
            continue
        is_new = any(l.startswith("+") for l in plines) and not any(
            l.startswith("-") and not l.startswith("---") for l in plines)
        is_del = all(l.startswith("-") for l in plines if l.strip() and not l.startswith("---"))
        if is_del:
            if os.path.isfile(fpath):
                os.remove(fpath)
                results.append(f"[XOÁ] {fpath}")
            else:
                results.append(f"[BỎ] {fpath} không tồn tại")
            continue
        if is_new and not os.path.isfile(fpath):
            content = "\n".join(l[1:] for l in plines if l.startswith("+"))
            parent = os.path.dirname(os.path.abspath(fpath))
            os.makedirs(parent, exist_ok=True)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
            results.append(f"[TẠO] {fpath} ({len(content)} ký tự)")
            continue
        if not os.path.isfile(fpath):
            results.append(f"[LOI] {fpath} không tồn tại")
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                orig_text = f.read()
        except Exception as e:
            results.append(f"[LOI] {fpath}: {e}")
            continue
        new_text = _apply_hunks(orig_text, plines)
        if new_text is None:
            results.append(f"[LOI] {fpath}: không parse được hunk")
            continue
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_text)
        fmt = _auto_format(fpath)
        hunk_count = sum(1 for ln in plines if ln.startswith("@@"))
        pdiff = _make_diff(orig_text, new_text, fpath)
        results.append(f"[SỬA] {fpath} ({hunk_count} hunk){fmt}\n{pdiff}")
    return "\n".join(results)


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
    root = os.path.abspath(os.path.expanduser(root or "."))
    if root == "/":
        return "[LOI] không cho grep toàn bộ '/' — chỉ định thư mục dự án (vd root='.') "
    include = (include or "*.py").strip() or "*.py"
    count = 0
    walked = 0
    for dp, dirs, fs in os.walk(root, topdown=True, onerror=lambda e: None):
        walked += 1
        if walked > 40000:
            hits.append(f"...(đã duyệt {walked} thư mục, dừng để tránh treo)")
            break
        # bỏ thư mục nặng trừ khi chính root là nó
        if os.path.abspath(dp) != root:
            dirs[:] = [d for d in dirs if d not in _HEAVY_DIRS and not d.startswith(".")]
        for f in fs:
            if not fnmatch.fnmatch(f, include):
                continue
            fp = os.path.join(dp, f)
            try:
                if os.path.getsize(fp) > 8 * 1024 * 1024:
                    continue
            except Exception:
                pass
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


def _glob_re(pat):
    """Pattern glob (*,**,?,[]) → regex cho đường dẫn POSIX tương đối."""
    i, out = 0, "^"
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if i + 1 < len(pat) and pat[i + 1] == "*":
                out += ".*"
                i += 1
                if i + 1 < len(pat) and pat[i + 1] == "/":
                    out += "/?"
                    i += 1
            else:
                out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out += re.escape(c)
            else:
                out += pat[i:j + 1]
                i = j
        else:
            out += re.escape(c)
        i += 1
    return re.compile(out + "$")


def glob_files(pattern, root="."):
    pat = (pattern or "**").strip() or "**"
    root = os.path.abspath(os.path.expanduser(root or "."))
    if root == "/":
        return "[LOI] không cho glob toàn bộ '/' — chỉ định thư mục dự án"
    # không có wildcard → kiểm tra trực tiếp
    if not any(c in pat for c in "*?["):
        full = os.path.join(root, pat)
        return full if os.path.exists(full) else "(không tìm thấy file nào)"
    # có wildcard → walk có trần + bỏ thư mục nặng
    try:
        rx = _glob_re(pat.lstrip("/"))
    except Exception:
        rx = None
    hits = []
    walked = 0
    for dp, dirs, fs in os.walk(root, topdown=True, onerror=lambda e: None):
        walked += 1
        if walked > 50000:
            break
        if os.path.abspath(dp) != root:
            dirs[:] = [d for d in dirs if d not in _HEAVY_DIRS]
        rel_dp = os.path.relpath(dp, root)
        for f in fs:
            rel = os.path.join(rel_dp, f) if rel_dp != "." else f
            rel_posix = rel.replace(os.sep, "/")
            ok = False
            if rx is not None:
                ok = bool(rx.match(rel_posix))
            else:
                ok = fnmatch.fnmatch(rel_posix, pat.lstrip("/"))
            if ok:
                hits.append(os.path.join(root, rel))
                if len(hits) >= 300:
                    break
        if len(hits) >= 300:
            break
    hits = sorted(hits)[:300]
    return "\n".join(hits) if hits else "(không tìm thấy file nào)"


def cwd():
    return CWD[0]


def chdir(path):
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        return f"[LOI] thư mục không tồn tại: {p}"
    CWD[0] = os.path.realpath(p)
    return CWD[0]


# ── todo tools ──────────────────────────────────────────────────────────────

def todo_list(session_id=""):
    """Xem danh sách todo hiện tại của session."""
    if not session_id:
        return "[LOI] cần session_id để xem todo"
    todos = _todo_load(session_id)
    if not todos:
        return "(chưa có todo nào — dùng todo_write để thêm)"
    status_icon = {"pending": "○", "in_progress": "◐", "completed": "●", "cancelled": "⊘"}
    priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = []
    for i, t in enumerate(todos):
        s = status_icon.get(t.get("status", "pending"), "?")
        p = priority_icon.get(t.get("priority", ""), "")
        mark = f"  {s} "
        if p:
            mark += p + " "
        lines.append(f"{i + 1}.{mark}{t.get('content', '?')}")
    return "\n".join(lines)


def todo_write(session_id, todos):
    """Ghi danh sách todo mới (thay thế toàn bộ). Mỗi item: {content, status, priority}.
    Status: pending/in_progress/completed/cancelled. Priority: high/medium/low."""
    if not session_id:
        return "[LOI] cần session_id"
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except Exception:
            return "[LOI] todos phải là JSON array"
    if not isinstance(todos, list):
        return "[LOI] todos phải là array"
    clean = []
    valid_status = {"pending", "in_progress", "completed", "cancelled"}
    valid_prio = {"high", "medium", "low", ""}
    for t in todos:
        if not isinstance(t, dict) or "content" not in t:
            continue
        clean.append({
            "content": str(t["content"])[:200],
            "status": t.get("status", "pending") if t.get("status") in valid_status else "pending",
            "priority": t.get("priority", "") if t.get("priority") in valid_prio else "",
        })
    _todo_save(session_id, clean)
    return f"Đã lưu {len(clean)} todo. Dùng todo_list(session_id='{session_id}') để xem."


# ── auto-install tools ──────────────────────────────────────────────────────

def ensure_tool(name, timeout=600):
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
    try:
        timeout = max(1, min(int(timeout or 600), 600))   # trần — LLM gửi timeout khổng lồ không treo server
    except Exception:
        timeout = 600
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
    name = (pkg or "").strip()
    if not PYPI_SAFE.match(name):
        return ("[LOI] tên package không hợp lệ — chỉ nhận tên PyPI (vd: requests, requests==2.31.0). "
                "Không nhận URL, git, script, hay editable install.")
    base = name.split("==")[0].split(">=")[0].split("<=")[0].split("!=")[0].strip()
    if not base or not re.match(r"^[a-zA-Z]", base):
        return f"[LOI] tên package '{base}' không hợp lệ"
    info = _pypi_verify(base)
    if not info:
        return (f"[LOI] package '{base}' không tồn tại trên PyPI hoặc tên không khớp chính xác. "
                f"(kiểm tra lại tên — phân biệt hoa/thường, không có package tên này)")
    version = info.get("version", "?")
    author = info.get("author") or info.get("author_email") or "?"
    py = _ensure_venv() or sys.executable
    snapshot = _venv_snapshot()
    try:
        timeout = max(1, min(int(timeout or 600), 600))   # trần — chống treo server
    except Exception:
        timeout = 600
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
    import_ok = _verify_import(base, py)
    if r.returncode != 0 or not import_ok:
        err = r.stderr[-300:] if r.stderr else "(không có stderr)"
        _log(f"FAIL_PIP {name} exit={r.returncode} import_ok={import_ok} → rollback")
        _venv_rollback(snapshot)
        return (f"[LOI] cài {base}=={version} thất bại, đã rollback. "
                f"Gói PyPI hợp lệ (tác giả: {author}), nhưng cài bị lỗi: {err}")
    _log(f"OK_PIP {base}=={version} (by {author}) → {py}")
    result = f"[OK] đã cài {base}=={version}"
    if py == PVENV_PY:
        result += " (trong venv ~/.rem_ai/venv)"
    return result


# ── PDF (chữ Việt chuẩn — KHÔNG dùng Helvetica mặc định) ─────────────────────

def _pdf_fonts():
    """Tìm font TTF hỗ trợ tiếng Việt (DejaVu/Noto/Liberation), trả (regular, bold)."""
    cands = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
    ]
    for r, b in cands:
        if os.path.isfile(r) and os.path.isfile(b):
            return r, b
    for r, b in cands:
        if os.path.isfile(r):
            return r, r
    return None, None


def make_pdf(title, body, out=None):
    """Tạo file PDF chuẩn tiếng Việt (font Unicode DejaVu) — title + các đoạn body.
    Nếu muốn PDF có ảnh minh hoạ, vẫn phải dùng web_images + script reportlab riêng,
    nhưng NHỚ đăng ký font Unicode, không dùng Helvetica (vỡ chữ tiếng Việt)."""
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib.colors import HexColor
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    except Exception as e:
        return f"[LOI] reportlab chua cai (pip_install reportlab): {e}"
    out = out or os.path.join(os.path.expanduser("~/Downloads"),
                              f"rem_{time.strftime('%Y%m%d_%H%M%S')}.pdf")
    try:
        out = os.path.abspath(os.path.expanduser(out))
    except Exception:
        out = os.path.abspath(out)
    if _blocked(out):
        return "[TU CHOI] không được ghi PDF vào thư mục runtime/keys của agent"
    reg, bold = _pdf_fonts()
    if not reg:
        return "[LOI] không tìm thấy font Unicode (DejaVu/Liberation/Noto) trên máy"
    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    except Exception:
        pass
    try:
        pdfmetrics.registerFont(TTFont("RemFont", reg))
        pdfmetrics.registerFont(TTFont("RemFont-Bold", bold or reg))
    except Exception as e:
        return f"[LOI] đăng ký font thất bại: {e}"
    try:
        pdfmetrics.registerFontFamily("RemFont", normal="RemFont", bold="RemFont-Bold",
                                      italic="RemFont", boldItalic="RemFont-Bold")
    except Exception:
        pass
    doc = SimpleDocTemplate(out, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm,
                            title=title or "Rem PDF")
    import html as _html
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("RemTitle", parent=styles["Title"], fontName="RemFont-Bold",
                              fontSize=20, leading=26, textColor=HexColor("#1b5e20")))
    styles.add(ParagraphStyle("RemBody", parent=styles["BodyText"], fontName="RemFont",
                              fontSize=11, leading=17, spaceAfter=8))
    story = [Paragraph(_html.escape(title or ""), styles["RemTitle"]), Spacer(1, 6)]
    texts = body if isinstance(body, list) else [body or ""]
    for t in texts:
        t = str(t).strip()
        if not t:
            continue
        if t.startswith("## "):
            story.append(Spacer(1, 2))
            story.append(Paragraph(_html.escape(t[3:].strip()), ParagraphStyle(
                "RemH", parent=styles["RemBody"], fontName="RemFont-Bold", fontSize=13,
                leading=18, spaceBefore=10, textColor=HexColor("#1b5e20"))))
        elif t.startswith("- "):
            story.append(Paragraph("• " + _html.escape(t[2:].strip()), styles["RemBody"]))
        else:
            story.append(Paragraph(_html.escape(t).replace("\n", "<br/>"), styles["RemBody"]))
    doc.build(story)
    size = os.path.getsize(out)
    return f"Đã tạo PDF: {out} ({size} bytes, {size / 1024:.1f} KB)."

# ── subagent (kiểu opencode Task) ────────────────────────────────────────────

def task(description, session_id="", timeout=120):
    """Chạy agent con độc lập (bị extensions.Manager._run_subagent chặn ở trên,
    nhưng vẫn giữ bản MCP này cho trường hợp gọi trực tiếp). Có timeout."""
    from agentloop import Agent
    from extensions import Manager
    from permissions import Presets
    import threading as _th
    sid = session_id or None
    try:
        timeout = max(10, min(int(timeout or 120), 240))
    except Exception:
        timeout = 120
    box = {}

    def _do():
        m = None
        try:
            m = Manager()
            m.start_all()
            agent = Agent(m, Presets.build(), sid=sid)
            box["out"] = agent.run(description)
        except Exception as e:
            box["out"] = f"[LOI] subagent: {type(e).__name__}: {e}"
        finally:
            try:
                if m is not None:
                    m.close_all()
            except Exception:
                pass

    th = _th.Thread(target=_do, daemon=True)
    th.start()
    th.join(timeout=timeout)
    if th.is_alive():
        return f"[LOI] subagent quá hạn {timeout}s — hãy chia nhỏ việc hơn"
    text = str(box.get("out") or "(trống)").strip()
    return text[:MAX_TOOL_OUT] + ("\n[SUBAGENT CẮT GỌN]" if len(text) > MAX_TOOL_OUT else "")


# ── tools table ──────────────────────────────────────────────────────────────

TOOLS = [
    Tool("bash", "Chạy lệnh shell/terminal (bash -c). Lệnh quá timeout TỰ chuyển chạy nền → dùng bash_poll xem tiếp. "
          "CẤM chạy lại lệnh vừa bị chuyển nền.",
          schema({"command": {"type": "string", "description": "Lệnh shell cần chạy"},
                  "timeout": {"type": "integer", "description": "giới hạn giây (1-600, mặc định 60)"}}), bash),
    Tool("bash_poll", "Xem/giết job bash chạy nền (job tự tạo khi bash timeout). "
          "poll = trạng thái + output; kill = dừng; list = liệt kê.",
          schema({"pid": {"type": "string", "description": "id job (bỏ trống + action=list để liệt kê)"},
                  "action": {"type": "string", "description": "poll|kill|list"}}), bash_poll),
    Tool("read_file",
         "Đọc nội dung file văn bản. Hiện line numbers tự động. "
         "Dùng offset/limit để đọc phần cụ thể (dòng 1-indexed). "
         "Thiếu file → gợi ý file gần nhất.",
         schema({"path": {"type": "string"},
                 "max_chars": {"type": "integer", "description": "giới hạn ký tự, mặc định 8000"},
                 "offset": {"type": "integer", "description": "dòng bắt đầu (1-indexed), mặc định 1"},
                 "limit": {"type": "integer", "description": "số dòng đọc, mặc định hết file"}}), read_file),
    Tool("write_file",
         "Ghi nội dung vào file (tạo mới hoặc ghi đè). Tự hiện diff preview và format code.",
         schema({"path": {"type": "string"}, "content": {"type": "string"}}), write_file),
    Tool("edit_file", "Sửa file: thay đoạn old bằng new (chính xác, phân biệt hoa/thường). Hiện diff.",
         schema({"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}), edit_file),
    Tool("apply_patch",
         "Apply unified diff patch — tạo/sửa/xoá nhiều file trong 1 lần (kiểu opencode). "
         "Input: unified diff text với headers +++ b/path và --- a/path.",
         schema({"patch_text": {"type": "string", "description": "unified diff text"}}), apply_patch),
    Tool("list_dir", "Liệt kê file/thư mục.",
         schema({"path": {"type": "string", "description": "mặc định ."}}), list_dir),
    Tool("grep", "Tìm regex trong file.",
         schema({"pattern": {"type": "string"}, "root": {"type": "string"}, "include": {"type": "string"}}), grep),
    Tool("glob_files", "Tìm file theo glob pattern (vd **/*.py).",
         schema({"pattern": {"type": "string"}, "root": {"type": "string"}}), glob_files),
    Tool("cwd", "Xem thư mục làm việc hiện tại.", schema({}), cwd),
    Tool("chdir", "Đổi thư mục làm việc.",
         schema({"path": {"type": "string"}}), chdir),
    Tool("todo_list",
         "Xem danh sách todo hiện tại của session.",
         schema({"session_id": {"type": "string", "description": "session ID"}}), todo_list),
    Tool("todo_write",
         "Ghi danh sách todo (thay thế toàn bộ). Status: pending/in_progress/completed/cancelled. Priority: high/medium/low.",
         schema({"session_id": {"type": "string"},
                 "todos": {"type": "array", "items": {
                     "type": "object",
                     "properties": {
                         "content": {"type": "string"},
                         "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
                         "priority": {"type": "string", "enum": ["high", "medium", "low"]}
                     },
                     "required": ["content"]
                 }}}), todo_write),
    Tool("ensure_tool",
         "Cài công cụ hệ thống nếu thiếu (chỉ whitelist: jq, tree, pandoc, ffmpeg, tmux, nmap...).",
         schema({"name": {"type": "string", "description": "tên tool cần cài"}}), ensure_tool),
    Tool("pip_install",
         "Cài package PyPI an toàn. Đã xác minh tên tồn tại trên PyPI (chống typosquatting). "
         "Snapshot trước khi cài → nếu import fail thì tự rollback. Ghi audit log.",
         schema({"pkg": {"type": "string", "description": "tên package, vd: requests hoặc requests==2.31.0"}}), pip_install),
    Tool("make_pdf",
         "Tạo file PDF chuẩn tiếng Việt (font Unicode DejaVu, KHÔNG vỡ dấu). "
         "title = tiêu đề; body = đoạn văn hoặc mảng đoạn (hỗ trợ '## ' tiêu đề mục, '- ' gạch đầu dòng); "
         "out = đường dẫn PDF (mặc định ~/Downloads/rem_*.pdf). "
         "LƯU Ý: text thuần chỉ đạt ~0,5MB cho hàng nghìn đoạn — PDF >10MB PHẢI gắn ảnh JPEG "
         "chất lượng cao (~40-60 ảnh 150-400KB) bằng script reportlab riêng, KHÔNG dùng make_pdf để làm PDF lớn.",
         schema({"title": {"type": "string", "description": "tiêu đề PDF"},
                 "body": {"type": "string", "description": "nội dung (string hoặc mảng đoạn văn)"},
                 "out": {"type": "string", "description": "(tùy chọn) đường dẫn file PDF"}}), make_pdf),
    Tool("task",
         "Chạy một SUBAGENT độc lập (kiểu opencode Task) làm việc nền song song: giao description "
         "mô tả rõ nhiệm vụ + yêu cầu trả về kết quả cụ thể. Subagent có đầy đủ tool của riêng nó "
         "(file, bash, web, memory, LSP) và session riêng. Rất hữu ích cho: phân tích/dò tìm trên "
         "toàn repo, viết code tách biệt, kiểm tra chéo, tóm tắt. Chờ subagent xong rồi nhận kết quả.",
         schema({"description": {"type": "string", "description": "nhiệm vụ ngắn gọn + format kết quả mong muốn"},
                 "session_id": {"type": "string", "description": "(tùy chọn) null → tự tạo session mới"}}), task),
]

if __name__ == "__main__":
    Server(TOOLS, "developer", "0.3.0").serve(sys.stdin, sys.stdout)
