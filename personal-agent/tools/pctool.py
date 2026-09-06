import os, re, subprocess, time, random as rd, shutil, unicodedata
from config import PC, TMP
VISION = None

def _env():
    e = os.environ.copy(); e.setdefault("DISPLAY", ":0"); return e
def delay(a=0.12, b=0.5): time.sleep(rd.uniform(a, b))
def _stripd(s): return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
def ph(*c): return subprocess.run(["adb", "shell"] + list(c), capture_output=True, text=True, timeout=20)

def shot():
    if PC:
        p = os.path.join(TMP, "s.png")
        if not shutil.which("scrot"): subprocess.run(["sudo", "apt-get", "install", "-y", "scrot"], capture_output=True)
        subprocess.run(["scrot", "-o", p], env=_env(), capture_output=True, timeout=10); return p
    p = "/sdcard/rem.png"; ph("screencap", "-p", p)
    loc = os.path.join(TMP, "ph.png"); subprocess.run(["adb", "pull", p, loc], capture_output=True); return loc

def tap(x, y):
    if PC:
        e = _env(); subprocess.run(["xdotool", "mousemove", str(x), str(y)], env=e, capture_output=True); delay(0.05, 0.2)
        subprocess.run(["xdotool", "click", "1"], env=e, capture_output=True)
    else:
        x += rd.randint(-4, 4); y += rd.randint(-4, 4)
        ph("input", "swipe", str(x), str(y), str(x), str(y), str(rd.randint(60, 140)))
    delay(0.1, 0.4)

def type_text(t):
    if PC:
        e = _env()
        for ch in t:
            subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "0", "--", ch], env=e, capture_output=True)
            time.sleep(rd.uniform(0.03, 0.12))
    else: ph("input", "text", _stripd(t).replace(" ", "%s"))

def key(k):
    if PC: subprocess.run(["xdotool", "key", k], env=_env(), capture_output=True)
    else:
        mp = {"enter": "66", "back": "4", "home": "3", "recent": "187", "esc": "4"}
        ph("input", "keyevent", mp.get(k.lower(), k))

def click_desc(d):
    if not VISION: return False
    t = VISION(f"Toa do (x,y) cua: {d}. Chi tra 'x,y'.", shot())
    m = re.search(r"(\d{2,4})\s*[,;]\s*(\d{2,4})", t or "")
    if not m: return False
    tap(int(m.group(1)), int(m.group(2))); return True

def run_script(script):
    log = []
    for ln in script.splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"): continue
        try:
            if ln.startswith(("tap ", "click ")):
                q = ln.split()[1:]
                if len(q) >= 2 and q[0].lstrip("-").isdigit(): tap(int(q[0]), int(q[1]))
                else: click_desc(ln.split(" ", 1)[1])
            elif ln.startswith("type "): type_text(ln[5:])
            elif ln.startswith("key "): key(ln[4:])
            elif ln.startswith("wait "): time.sleep(float(ln[5:]))
            elif ln.startswith(("open ", "app ", "goto ")):
                n = ln.split(" ", 1)[1]
                if PC: subprocess.run(["xdg-open", n], env=_env(), capture_output=True)
                else:
                    if re.search(r"^https?://", n): ph("am", "start", "-a", "android.intent.action.VIEW", "-d", n)
                    else: ph("monkey", "-p", n, "-c", "android.intent.category.LAUNCHER", "1")
            elif ln in ("back", "home", "recent"): key(ln)
            elif ln.startswith("see "): click_desc(ln.split(" ", 1)[1])
            elif ln.startswith("look ") and VISION: log.append("[vision] " + (VISION(ln.split(" ", 1)[1] + " — NGAN gon.", shot()) or "")[:120])
            else: type_text(ln); key("enter")
            log.append(ln); delay(0.1, 0.35)
        except Exception as ex:
            return "STDOUT:\n" + "\n".join(log) + f"\nSTDERR:\n{ex}"
    return "STDOUT:\nDa thao tac:\n" + "\n".join(log)