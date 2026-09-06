import subprocess, shutil, py_compile, sys

def typecheck(path):
    try: py_compile.compile(path, doraise=True); return True
    except Exception: return False

def approve(msg):
    if not sys.stdin or not sys.stdin.isatty(): return False
    sys.stdout.write(f"[?] {msg} [y/N]: "); sys.stdout.flush()
    try: a = sys.stdin.readline().strip().lower()
    except Exception: return False
    return a in ("y", "yes", "ok", "c", "co")

def win_exists():
    if not shutil.which("wmctrl"): return True
    return bool(subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout.strip())

def evidence(log):
    return f"[BANG CHUNG] {len(log)} lenh: " + "; ".join(log[:6])