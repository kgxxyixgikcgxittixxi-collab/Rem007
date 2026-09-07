"""updater.py — kiểm tra + tự cập nhật Rem Agent từ GitHub (kiểu opencode).

- check: so version local (config.VERSION) với version trên GitHub raw.
- update: repo git → fetch + reset về origin/branch; không phải repo git →
          tải tarball rồi ghi đè file. Sau đó refresh bản sao Remtm/rem-rest.
- preflight: chạy trước khi Repl mở — báo (và tuỳ AUTO_UPDATE: tự) cập nhật.
Mọi hàm đều an toàn: lỗi mạng/repo đều trả lại thông báo, KHÔNG bao giờ hỏng Repl.
"""
import os, re, shutil, subprocess, sys, tarfile, tempfile, time

import config

_ROOT = os.path.dirname(os.path.abspath(__file__))      # thư mục personal-agent/
_REPO_ROOT = os.path.dirname(_ROOT)                      # thư mục gốc repo Rem007/
_BRANCH_REF = f"origin/{config.GIT_BRANCH}"


def _ver_tuple(v):
    nums = re.findall(r"\d+", str(v))
    t = tuple(int(x) for x in nums[:3])
    while len(t) < 3:
        t = t + (0,)
    return t


def current_version():
    return config.VERSION


def remote_version(timeout=config.UPDATE_CHECK_TIMEOUT):
    """Version mới nhất trên GitHub (từ raw config.py). None nếu lỗi mạng."""
    try:
        import requests
        r = requests.get(config.REMOTE_CONFIG, timeout=timeout)
        r.raise_for_status()
        m = re.search(r'VERSION\s*=\s*["\']([\d.]+)["\']', r.text)
        return m.group(1) if m else None
    except Exception:
        return None


def has_update():
    """True nếu có bản mới trên GitHub; False nếu đã mới nhất; None nếu không rõ."""
    rv = remote_version()
    if not rv:
        return None
    return _ver_tuple(rv) > _ver_tuple(config.VERSION)


def _run(cmd, timeout=config.UPDATE_TIMEOUT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except Exception as e:
        return -1, "", str(e)


def _verify_version(target):
    """Chạy lại config bằng subprocess để đọc VERSION thật sau khi thay file."""
    try:
        p = subprocess.run([sys.executable, "-c", "import config; print(config.VERSION)"],
                           cwd=_ROOT, capture_output=True, text=True, timeout=30)
        cur = (p.stdout or "").strip() or config.VERSION
    except Exception:
        cur = config.VERSION
    ok = _ver_tuple(cur) >= _ver_tuple(target)
    return ok, cur


def _git_pull(lines):
    code, _, err = _run(["git", "-C", _REPO_ROOT, "fetch", "--quiet", "origin", config.GIT_BRANCH])
    if code != 0:
        lines.append(f"[!] git fetch lỗi: {err[:150]}")
        return False
    code, _, err = _run(["git", "-C", _REPO_ROOT, "reset", "--hard", "--quiet", _BRANCH_REF])
    if code != 0:
        lines.append(f"[!] git reset lỗi: {err[:150]}")
        return False
    lines.append("✓ Đã pull source mới từ GitHub (git).")
    return True


def _tarball_pull(lines):
    """Không phải repo git → tải tarball rồi ghi đè personal-agent + script đầu repo."""
    try:
        import requests
        tmp = tempfile.mkdtemp(prefix="rem-update-")
        tgz = os.path.join(tmp, "repo.tar.gz")
        with requests.get(config.REPO_TARBALL, stream=True, timeout=config.UPDATE_TIMEOUT) as r:
            r.raise_for_status()
            with open(tgz, "wb") as f:
                shutil.copyfileobj(r.raw, f, length=1 << 20)
        with tarfile.open(tgz, "r:gz") as t:
            t.extractall(tmp)
        inner = next(os.path.join(tmp, d) for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d)))
        pa = os.path.join(inner, "personal-agent")
        if not os.path.isdir(pa):
            lines.append("[!] Tarball không có thư mục personal-agent.")
            return False
        shutil.copytree(pa, _ROOT, dirs_exist_ok=True)      # ghi đè, không xoá file cục bộ thừa
        for name in ("Remtm", "rem-rest", "ts-on.sh"):
            src = os.path.join(inner, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(_REPO_ROOT, name))
        lines.append("✓ Đã tải + áp dụng bản mới từ GitHub.")
        return True
    except Exception as e:
        lines.append(f"[!] Tải tarball lỗi: {type(e).__name__}: {str(e)[:150]}")
        return False


def _copy_cli_bins(lines):
    """Refresh bản cài nhanh Remtm/rem-rest trong ~/.local/bin hoặc $PREFIX/bin."""
    bin_dir = os.environ.get("PREFIX") and os.path.join(os.environ["PREFIX"], "bin")
    if not bin_dir:
        bin_dir = os.path.join(os.path.expanduser("~"), ".local", "bin")
    if not os.path.isdir(bin_dir):
        return
    try:
        for name in ("Remtm", "rem-rest"):
            src = os.path.join(_REPO_ROOT, name)
            if os.path.isfile(src):
                dst = os.path.join(bin_dir, name)
                shutil.copy2(src, dst)
                os.chmod(dst, 0o755)
        lnk = os.path.join(bin_dir, "remtm")
        if os.path.exists(lnk) or os.path.islink(lnk):
            os.remove(lnk)
        try:
            os.symlink(os.path.join(bin_dir, "Remtm"), lnk)
        except OSError:
            pass
        lines.append("✓ Đã refresh lệnh nhanh Remtm/rem-rest trong bin.")
    except Exception as e:
        lines.append(f"[i] Không refresh được lệnh nhanh: {str(e)[:120]}")


def update():
    """Cập nhật về bản mới nhất. Trả về (ok, new_version, [dòng thông báo])."""
    lines = []
    rv = remote_version()
    if not rv:
        return False, config.VERSION, ["[!] Không lấy được bản mới từ GitHub (kiểm tra mạng)."]
    if _ver_tuple(rv) <= _ver_tuple(config.VERSION):
        return True, config.VERSION, [f"Đã ở bản mới nhất: v{config.VERSION}."]
    lines.append(f"→ Cập nhật v{config.VERSION} → v{rv} …")
    try:
        with open(os.path.join(config.DIR, "last-update.txt"), "w") as f:
            f.write(f"{config.VERSION} -> {rv}\n{time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except Exception:
        pass

    if os.path.isdir(os.path.join(_REPO_ROOT, ".git")):
        ok = _git_pull(lines)
        if not ok:
            _copy_cli_bins(lines)
            good, cur = _verify_version(rv)
            return good and _ver_tuple(cur) >= _ver_tuple(rv), cur, lines
    else:
        ok = _tarball_pull(lines)
        if not ok:
            return False, config.VERSION, lines
    _copy_cli_bins(lines)
    good, cur = _verify_version(rv)
    if good:
        lines.append(f"✓ Xong — hiện tại: v{cur}. Khởi động lại Remtm để dùng bản mới.")
    else:
        lines.append(f"[!] Sau update vẫn đọc được v{cur} < v{rv} — thử lại /update.")
    return good, cur, lines


def preflight():
    """Chạy TRƯỚC khi Repl mở: báo bản mới, hoặc tự cập nhật (AUTO_UPDATE)."""
    try:
        if has_update() is not True:
            return                    # đã mới nhất / không lấy được — im lặng
        print(f"[i] Có bản mới trên GitHub. Đang chạy v{config.VERSION}.")
        if not config.AUTO_UPDATE:
            print("    Gõ  /update  trong Rem để cập nhật, hoặc  /checkupdate  để kiểm tra.")
            return
        print("    Đang tự cập nhật (tắt bằng: REM_AUTO_UPDATE=0)…")
    except Exception:
        return
    ok, _, lines = update()
    for ln in lines:
        print("    " + ln)
    if not ok:
        print("    Cập nhật chưa xong — vẫn chạy bản cũ. Gõ /update để thử lại.")