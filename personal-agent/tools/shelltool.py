import os, re, subprocess, time, shlex
from config import PC, WIN, MAC, DIR, SHELL_TIMEOUT

def _sudo_pass():
    v = os.environ.get("REM_SUDO_PASS", "")
    if not v:
        try: v = open(os.path.join(DIR, "sudopass")).read().strip()
        except Exception: v = ""
    return v

def _ask():
    v = _sudo_pass()
    if not v: return None
    p = os.path.join(DIR, "ask.sh")
    try:
        open(p, "w").write("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(v) + "\n")
        os.chmod(p, 0o700); return p
    except Exception: return None

HARD = [r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/(\s|$)", r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+~", r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+\*",
        r"dd\s+.*of=/dev/", r"mkfs", r"streamlit", r"gradio", r"pkill.*pyt", r"pkill.*1\.py"]

def danger(c, cur_cmd=""):
    if any(re.search(p, c) for p in HARD): return True
    if re.search(r"\b(pkill|killall)\b", c) and "kill" not in cur_cmd: return True
    if re.search(r"nohup\s+.+&", c) and "chay nen" not in cur_cmd: return True
    return False

def run(c, cur_cmd="", readonly=False):
    c = c.strip(); print("[bash]: " + c)
    e = os.environ.copy(); e.setdefault("DISPLAY", ":0")
    if "sudo" in c:
        v = _sudo_pass(); ap = _ask()
        if v and ap: c = c.replace("sudo ", "sudo -A -p '' "); e["SUDO_ASKPASS"] = ap
    if danger(c, cur_cmd): return "STDERR:\n[CHAN] lenh nguy hiem."
    if readonly and re.search(r"(rm |dd |install|>>? *[/~]|mkswap|swap|chmod |chown |mkdir |touch )", c):
        return "STDERR:\n[CHAN] luot KIEM TRA chi doc."
    if PC and not MAC and re.match(r"^open\s+", c): c = "xdg-open " + c[5:]
    if re.search(r"(xdg-open|coccoc|firefox|chrome)", c) and not c.startswith("setsid"): c = "setsid -f " + c
    try:
        p = subprocess.Popen(c, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=e,
                             preexec_fn=None if WIN else os.setsid)
    except Exception as ex:
        return f"STDERR:\n{ex}"
    t0 = time.time()
    while True:
        try: o, e2 = p.communicate(timeout=0.2); break
        except subprocess.TimeoutExpired:
            if time.time() - t0 > SHELL_TIMEOUT:
                try: p.kill()
                except Exception: pass
                return "STDERR:\nTIMEOUT"
    return (f"STDOUT:\n{o}\nSTDERR:\n{e2}" if e2 else o)