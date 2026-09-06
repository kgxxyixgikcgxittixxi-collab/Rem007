import re, requests
from html.parser import HTMLParser
from providers import groq

class _P(HTMLParser):
    def __init__(self):
        super().__init__(); self.out = []
    def handle_data(self, d):
        s = d.strip()
        if s and s[-1] in ".!?": self.out.append(s)

def search(q, n=6):
    try:
        r = requests.get("https://html.duckduckgo.com/html/", params={"q": q},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200: return ""
        p = _P(); p.feed(r.text)
        txt = " ".join(p.out[:120])
        links = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
        res = []
        for u, t in links[:n]:
            t = re.sub(r"<[^>]+>", "", t)
            res.append(f"- {t.strip()[:80]} | {u}")
        return "\n".join(res) or txt[:1500]
    except Exception as ex:
        return f"STDERR:\n{ex}"

def fetch(url, max_chars=2500):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200: return "STDERR:\nHTTP " + str(r.status_code)
        body = r.text
        for token in [r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", r"(?s)<[^>]+>"]:
            body = re.sub(token, " ", body)
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n\s*\n+", "\n", body)
        body = re.sub(r"\n[ \t]+", "\n", body)
        text = body.strip()
        if len(text) > max_chars: text = text[:max_chars] + "\n...[CUT]"
        return f"STDOUT: {url}\n{text}"
    except Exception as ex:
        return f"STDERR:\n{ex}"

def answer(q):
    res = search(q)
    url = ""
    m = re.search(r"\|\s*(https?://\S+)", res)
    if m: url = m.group(1).rstrip(".")
    pg = fetch(url) if url else ""
    combined = f"CAU HOI: {q}\n\nKET QUA TIM:\n{res[:1200]}\n\nNOI DUNG:\n{pg[:2200]}"
    return groq.text(combined, max_tokens=500, temp=0.2)