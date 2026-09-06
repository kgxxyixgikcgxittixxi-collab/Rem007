import requests, base64, re, sqlite3, os
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
def _post(body, model, timeout=30):
    ks = keys()
    if not ks: return None
    n = len(ks)
    for i in range(n):
        k = ks[(_RR[0] + i) % n]
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {k}"},
                json={"model": model, **body}, timeout=timeout)
            if r.status_code == 200:
                _RR[0] = (_RR[0] + 1) % n
                return r
        except Exception:
            continue
    _RR[0] = (_RR[0] + 1) % n
    return None
def chat(msgs, tools=None):
    body = {"messages": msgs, "max_tokens": 4096}
    if tools: body["tools"] = tools; body["tool_choice"] = "auto"
    for m in [MODEL_CHAT] + MODEL_FB:
        r = _post(body, m)
        if r is None: continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]
    return None
def text(p, max_tokens=700, temp=0.2):
    r = _post({"messages": [{"role": "user", "content": p}], "max_tokens": max_tokens, "temperature": temp}, MODEL_CLONE, 20)
    if r is not None and r.status_code == 200:
        return r.json()["choices"][0]["message"]["content"]
    return ""
def vision(q, img):
    try: d = base64.b64encode(open(img, "rb").read()).decode()
    except Exception: return None
    body = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": q},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{d}"}}]}], "max_tokens": 300}
    for m in [MODEL_VISION] + MODEL_FB:
        r = _post(body, m, 25)
        if r is not None and r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"]
    return None