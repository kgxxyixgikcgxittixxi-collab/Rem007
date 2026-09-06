import subprocess, shutil, py_compile

def typecheck(path):
    try: py_compile.compile(path, doraise=True); return True
    except Exception: return False

def win_exists():
    if not shutil.which("wmctrl"): return True
    return bool(subprocess.run(["wmctrl", "-l"], capture_output=True, text=True).stdout.strip())

def evidence(log):
    return f"[BANG CHUNG] {len(log)} lenh: " + "; ".join(log[:6])