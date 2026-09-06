import os, json, sqlite3, time
from config import DIR, CTX_MSGS, CTX_CHARS, SUM_AT
from router import norm

DB = os.path.join(DIR, "rem.db")
conn = sqlite3.connect(DB, check_same_thread=False); cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS hist(id INTEGER PRIMARY KEY AUTOINCREMENT, mj TEXT)")
conn.commit()

SYS = ("Ban la AI giup Boss Rem. Hieu tieng Viet viet tat/loi go. Hoi/chao→tra loi NGAN. "
       "Menh lenh→lam. 'kiem tra X'=CHI DOC, khong cai/tao/xoa/doi. "
       "KHONG tu them viec ngoai y Boss. CURRENT_GOAL=cau moi nhat; OUT_OF_SCOPE=viec khac. "
       "Loi→TU-SUA, khong hoi Boss. gsk_ la Groq key khong phai GitHub token; ghp_ moi la GitHub token.")
MAX_ROWS = 300
MEM_DIR = os.path.join(DIR, "memories"); os.makedirs(MEM_DIR, exist_ok=True)
SES = [None]
LESSON = os.path.join(MEM_DIR, "kinhnghiem.txt")

def save(m):
    cur.execute("INSERT INTO hist(mj) VALUES(?)", (json.dumps(m),)); conn.commit()
    _prune(); _summarize()

def _prune():
    cur.execute("SELECT COUNT(*) FROM hist"); n = cur.fetchone()[0]
    if n > MAX_ROWS:
        cur.execute("SELECT id FROM hist ORDER BY id LIMIT ?", (n - MAX_ROWS,))
        cur.executemany("DELETE FROM hist WHERE id=?", [(i[0],) for i in cur.fetchall()])
        conn.commit()

def _compress(m):
    r = m.get("role", "?"); c = m.get("content") or ""
    if not isinstance(c, str): c = str(c)
    return f"[{'Boss' if r=='user' else 'AI'}]: {c[:200].replace(chr(10),' ')}"

def _summarize():
    cur.execute("SELECT id, mj FROM hist ORDER BY id")
    nm = []
    for rid, r in cur.fetchall():
        try: p = json.loads(r)
        except Exception: continue
        if p.get("role") not in ("system", "summary"): nm.append((rid, p))
    if len(nm) <= SUM_AT: return
    tc = nm[:-8]
    if len(tc) < 5: return
    sm = {"role": "summary", "content": "[TOM TAT]\n" + "\n".join(_compress(m) for _, m in tc)}
    cur.executemany("DELETE FROM hist WHERE id=?", [(i,) for i, _ in tc])
    cur.execute("SELECT MIN(id) FROM hist"); row = cur.fetchone()
    sid = (row[0] - 1) if row and row[0] is not None else 1
    cur.execute("INSERT INTO hist(id, mj) VALUES(?,?)", (sid, json.dumps(sm))); conn.commit()

def load():
    cur.execute("SELECT mj FROM hist ORDER BY id"); h = []
    for (r,) in cur.fetchall():
        try: h.append(json.loads(r))
        except Exception: pass
    if not h: return [{"role": "system", "content": SYS}]
    out = [{"role": "system", "content": SYS}] + [m for m in h if m.get("role") == "summary"][-1:] + h[-CTX_MSGS:]
    for m in out:
        c = m.get("content")
        if isinstance(c, str) and len(c) > CTX_CHARS: m["content"] = c[:CTX_CHARS] + "\n..."
    return out

def ses_new(t):
    p = os.path.join(MEM_DIR, f"ses_{time.strftime('%y%m%d_%H%M%S')}.txt")
    try: open(p, "w", encoding="utf-8").write("MO TA: " + t[:120] + "\n----\n"); SES[0] = p
    except Exception: pass

def ses_add(r, t):
    if not SES[0]: return
    try: open(SES[0], "a", encoding="utf-8").write(f"{r}: {(t or '')[:400]}\n")
    except Exception: pass

def recall(q):
    qk = set(norm(q).split()); best, bs = None, 0
    for fn in sorted(os.listdir(MEM_DIR)):
        if not fn.endswith(".txt"): continue
        try: head = open(os.path.join(MEM_DIR, fn), encoding="utf-8").read(600)
        except Exception: continue
        sc = len(qk & set(norm(head).split()))
        if sc > bs: bs, best = sc, fn
    if not best or bs < 2: return ""
    try: body = open(os.path.join(MEM_DIR, best), encoding="utf-8").read(3000)
    except Exception: return ""
    return f"[BO NHO CU]:\n{body}"

def is_mission(t):
    t = norm(t)
    return ("nhiem vu" in t) or ("hoan thanh" in t) or ("bao cao" in t) or len(t) > 60

def les_recall(q):
    try:
        if not os.path.exists(LESSON): return ""
        b = open(LESSON, encoding="utf-8").read(6000)
        ls = [l for l in b.splitlines() if l.strip()]
        qk = set(norm(q).split())
        hit = [l for l in ls if len(qk & set(norm(l).split())) >= 2]
        return "\n".join(hit[-5:]) or b[-1200:]
    except Exception: return ""

def les_save(topic, note):
    try: open(LESSON, "a", encoding="utf-8").write(f"- {topic}: {note[:300]}\n")
    except Exception: pass