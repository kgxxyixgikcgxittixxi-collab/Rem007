import requests, base64, re, sqlite3, os, time
from config import DIR, MODEL_CHAT, MODEL_CLONE, MODEL_VISION, MODEL_FB
DB = os.path.join(DIR, "rem.db")
conn = sqlite3.connect(DB, check_same_thread=False)
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS gq(key TEXT UNIQUE)")
conn.commit()
def keys():
    return [k for (k,) in cur.execute("SELECT key FROM gq")]
def add_key(k):
    try:
        cur.execute("INSERT OR IGNORE INTO gq(key) VALUES(?)", (k,)); conn.commit(); return True
    except Exception:
        return False
def scan(t):
    return sum(1 for k in re.findall(r"gsk_[A-Za-z0-9]{40,}", t) if add_key(k))
_RR = [0]
_COOL = {}  # key -> cooldown until ts
_RATE = (413, 429)
def _post(body, model, timeout=30):
    ks = keys()
    if not ks:
        return None
    n = len(ks)
    last = None
    for _ in range(6):
        now = time.time()
        choice = None
        for i in range(n):
            k = ks[(_RR[0] + i) % n]
            if _COOL.get(k, 0) <= now:
                choice = k
                _RR[0] = (_RR[0] + 1) % n
                break
        if choice is None:
            wake = min(_COOL.values())
            d = max(0.5, min(25.0, wake - now))
            time.sleep(d)
            continue
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {choice}"},
                json={"model": model, **body}, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in _RATE:
                retry = r.headers.get("retry-after")
                _COOL[choice] = time.time() + (max(30, int(retry)) if retry else 50)
                last = "RATE"
            else:
                last = f"HTTP {r.status_code}"
        except Exception as e:
            last = type(e).__name__
    if last == "RATE":
        return "RATE"
    if last:
        print(f"[groq] that bai ({model}): {last}", file=__import__("sys").stderr)
    return None


def chat(msgs, tools=None):
    body = {"messages": msgs, "max_tokens": 4096}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    for m in [MODEL_CHAT] + MODEL_FB:
        r = _post(body, m)
        if r == "RATE":
            return None
        if r is not None:
            val = r.json()["choices"][0]["message"]
            return val
    return None
def text(p, max_tokens=700, temp=0.2):
    r = _post({"messages": [{"role": "user", "content": p}], "max_tokens": max_tokens, "temperature": temp}, MODEL_CLONE, 20)
    if r is not None and r != "RATE" and r.status_code == 200:
        return r.json()["choices"][0]["message"]["content"]
    return ""
def vision(q, img):
    try:
        d = base64.b64encode(open(img, "rb").read()).decode()
    except Exception:
        return None
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": q},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{d}"}}]}], "max_tokens": 300}
    for m in [MODEL_VISION] + MODEL_FB:
        r = _post(body, m, 25)
        if r == "RATE":
            return None
        if r is not None and r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    return None