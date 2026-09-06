import json, re
from providers import groq
from router import norm

SUP_P = 'Bien cau thanh JSON {"type":"chat"|"task","reply":"...","actions":[...]}. chi hoi=>reply.'
SUP_G = ('Clone GUI. CHI tra JSON task. Ops: open/goto/search_in/search/click/type/key/wait/look/social/close. '
         'VD "vao coc coc tim anime"=>[{"op":"open","app":"coccoc"},{"op":"wait","s":2},{"op":"search_in","site":"youtube","kw":"anime"}].')
SUP_H = ('Clone HEADLESS. Ops: bash_read, bash, read, write, edit, ls, glob, grep, web_search, web_open, reply. '
         'VD "kiem tra ram"=>[{"op":"bash_read","cmd":"free -h"}]. '
         'VD "ghi file hello.txt noi dung Xin chao"=>[{"op":"write","path":"hello.txt","content":"Xin chao"}]. '
         'VD "tim kiem python la gi"=>[{"op":"web_search","q":"python la gi"}].')
ALLOW = {"headless": {"bash_read", "bash", "reply", "read", "write", "edit", "ls", "glob", "grep", "web_search", "web_open"},
         "gui": {"open", "close", "focus", "goto", "search", "search_in", "click", "type", "key", "wait", "look", "social", "see", "web_open"}}
STOPW = {"vao","mo","mow","mở","truy","cap","app","giup","toi","tim","kiem","search","go","nhan","web","youtube",
         "facebook","tiktok","zalo","di","nhe","a","em","o","tren","may","coccoc","coc","cốc","google","roi","ban","la"}

def kwo(t): return " ".join(x for x in t.split() if x not in STOPW)

def parse(t):
    if not t: return None
    m = re.search(r"\{.*\}", t, re.S)
    if not m: return None
    try: d = json.loads(m.group(0))
    except Exception: return None
    if not isinstance(d, dict): return None
    a = d.get("actions") or []; rp = (d.get("reply") or "").strip()
    if isinstance(a, list) and a and (d.get("type") == "task" or not rp):
        return {"type": "task", "actions": [x for x in a if isinstance(x, dict) and x.get("op")][:8]}
    if rp or d.get("type") == "chat": return {"type": "chat", "reply": rp or "Em đang nghe Boss ạ."}
    return None

def fallback(z, u, t):
    acts = []
    if z == "headless":
        tok = re.search(r"(ghp_[A-Za-z0-9]{10,}|github_pat_\S+)", u)
        if tok and "github" in t:
            return [{"op": "bash_read", "cmd": f"curl -s -H 'Authorization: token {tok.group(0)}' https://api.github.com/user | head -c 500"}]
        mw = re.search(r"ghi file (\S+) noi dung (.+)", u, re.S | re.I)
        if mw: return [{"op": "write", "path": mw.group(1), "content": mw.group(2).strip()}]
        mrd = re.search(r"(?:doc|mo|hien thi) file (\S+)", u, re.I)
        if mrd: return [{"op": "read", "path": mrd.group(1)}]
        me = re.search(r"sua file (\S+).*?[\"'](.+?)[\"']\s*thanh\s*[\"'](.+?)[\"']", u, re.S | re.I)
        if me: return [{"op": "edit", "path": me.group(1), "old": me.group(2), "new": me.group(3)}]
        mls = re.search(r"(?:noi dung|danh sach file|file gi) ?(?:trong|o)?\s*(\S+)", u)
        if mls and "file" in t: return [{"op": "ls", "path": mls.group(1)}]
        qs = re.search(r"(?:tim kiem|search|tra cuu)\s+([\wẠ-ỹ ._/-]+)", u, re.I)
        if qs: return [{"op": "web_search", "q": qs.group(1).strip()[:120]}]
        CHECKS = {"ram": "free -h", "bo nho": "free -h", "dia": "df -h", "cpu": "lscpu | head -18", "he thong": "uname -a; uptime", "wifi": "dev 2>/dev/null || iwconfig 2>/dev/null | head -20"}
        for k, c in CHECKS.items():
            if k in t: return [{"op": "bash_read", "cmd": c}]
        if "kiem tra" in t or "check" in t: return [{"op": "bash_read", "cmd": "df -h;free -h;uname -a"}]
        return acts
    m = re.search(r"https?://\S+", u)
    if m: acts.append({"op": "goto", "url": m.group(0)})
    if any(w in t for w in ("dang nhap", "bam", "click", "chup")): acts.append({"op": "click", "mo ta": "nut dang nhap"})
    elif acts:
        kw = kwo(t)
        if kw: acts += [{"op": "wait", "s": 2}, {"op": "search_in", "site": "browser", "kw": kw}]
    return acts

def ask(zone, u, extra=""):
    p = (SUP_H if zone == "headless" else SUP_G) + "\nMUC TIEN: " + u + extra + "\nBoss: " + u
    return parse(groq.text(p))