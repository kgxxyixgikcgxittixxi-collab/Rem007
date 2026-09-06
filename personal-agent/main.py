import os, sys, time, re, json, random as rd
from providers import groq
import memory, router, planner, executor, web, verifier
from tools import pctool
from config import VERSION, NAME, MODEL_CHAT, DEBUG
pctool.VISION = groq.vision

GRN, YLW, RED, CYN, BLD, RST = "\033[92m", "\033[93m", "\033[91m", "\033[96m", "\033[1m", "\033[0m"
STATS = {"chat": 0, "gui": 0, "headless": 0, "tools": 0}
GREETS = ("Xin chao Boss Rem! Em nghe ro nha.", "Day la Rem Agent - e san sang giup Boss.",
          "Chao Boss! Em day, can gi cu gọi nha.", "Xin chao! Rem agent 24/7 som duong.")

def brain(u):
    t0 = router.norm(u)
    for a, b in (("youtobe", "youtube"), ("coc coc", "coccoc"), ("cốc cốc", "coccoc"), ("mow ", "mo ")): t0 = t0.replace(a, b)
    ro = bool(re.search(r"(kiem tra|check)", t0))
    z = router.route(t0)
    STATS[z] = STATS.get(z, 0) + 1
    if z == "chat" and not router.is_act(u):
        if router.is_greet(t0): return rd.choice(GREETS)
        d = planner.parse(groq.text(planner.SUP_P + "\nBoss: " + u))
        if d and d.get("type") == "chat" and len((d.get("reply") or "")) >= 2:
            return d.get("reply")[:400]
    elif z in ("gui", "headless"):
        ze = ""
        for i in range(3):
            d = planner.ask(z, u, ze)
            acts = [a for a in (d or {}).get("actions", []) if a.get("op") in planner.ALLOW[z]][:8] if d and d.get("type") == "task" else planner.fallback(z, u, t0)
            if acts:
                outs = executor.run_plan(acts, u, ro)
                STATS["tools"] += sum(1 for a in acts if a.get("op") not in ("wait", "reply"))
                bad = [x for x in outs if str(x).startswith("[loi") or ("STDERR:" in str(x) and "[CHAN]" not in str(x))]
                if bad and i < 2:
                    ze = " | LOI: " + " | ".join(str(x)[:150] for x in bad)[:300]; continue
                if outs: return "[XONG] " + " | ".join(x for x in outs if x)[:700]
                return "[XONG] da thuc hien"
            break
    h = memory.load(); h.append({"role": "user", "content": u}); memory.save(h[-1])
    last = None; no_tool = False
    for _ in range(20):
        msg = groq.chat(h, None if no_tool else executor.FC)
        if not msg: return "[!] groq loi - kiem tra key."
        h.append({"role": "assistant", "content": msg.get("content") or ""}); memory.save(h[-1])
        calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""
        if not calls:
            if content: return content
            if not no_tool:
                no_tool = True
                continue
            return "Em da nghe, nhung chua hieu y Boss. Thu dien dat lai nha."
        for tc in calls[:3]:
            fn = tc.get("function", {})
            try: a = json.loads(fn.get("arguments") or "{}")
            except Exception: a = {}
            tr = executor.run_toolcall(fn.get("name"), a, u, ro)
            h.append({"role": "tool", "tool_call_id": tc.get("id"), "content": tr[:1500]}); memory.save(h[-1])
            if not content: last = tr[:400]
    return "Em da lam het 20 buoc ma chua xong - thu noi khac nha."

def _say(t):
    if not sys.stdin or not sys.stdin.isatty():
        print(t); return
    sys.stdout.write(GRN + "[AI] " + RST)
    for ch in t:
        sys.stdout.write(ch); sys.stdout.flush()
        time.sleep(0.004)
    sys.stdout.write("\n")

def _keys():
    ks = groq.keys(); n = len(ks)
    ms = "".join(k[:14] + "..." for k in ks[:6])
    return f"{n} key | {ms}" if n else "0 key"

def _st():
    memory.cur.execute("SELECT COUNT(*) FROM hist"); n = memory.cur.fetchone()[0]
    return (f"{CYN}{NAME} v{VERSION}{RST} | model {MODEL_CHAT}\n"
            f"keys: {_keys()} | hist: {n} dong | zone: {STATS} | cwd: {os.getcwd()}\n"
            f"session: {memory.SES[0] or 'chua tao'} | chat/gui/headless nhay theo lenh")

def _lint():
    d = os.path.dirname(os.path.abspath(__file__)); bad = []
    for root, dns, fns in os.walk(d):
        dns[:] = [x for x in dns if x not in ("__pycache__", ".git")]
        for f in fns:
            if f.endswith(".py") and not verifier.typecheck(os.path.join(root, f)):
                bad.append(f)
    return ("".join(f"{RED}[LOI]{RST} {b}" for b in bad) or f"{GRN}[OK]{RST} tat ca .py hop le") if bad else f"{GRN}[OK]{RST} tat ca .py hop le"

def help():
    return (f"{BLD}LENH:{RST}\n"
            f"  {CYN}/key{RST} them key gsk_        {CYN}/keys{RST} xem so key\n"
            f"  {CYN}/clear{RST} xoa lich su        {CYN}/status{RST} thong tin he thong\n"
            f"  {CYN}/lint{RST} kiem tra cau truc code\n"
            f"  {CYN}/exit{RST} / {CYN}Ctrl+D{RST} thoat\n"
            f"{BLD}TY DU:{RST} kiem tra ram | tim kiem [chu de] | ghi/doc/sua file | vao coc [app] | /status")

def main():
    print(f"\n  {BLD}{CYN}{NAME} v{VERSION}{RST}  —  {GRN}100% Groq ({MODEL_CHAT}){RST}")
    print(f"  nhap {GRN}/help{RST} xem lenh, {GRN}/key{RST} de nap key gsk_ dau tien\n")
    while True:
        try: t = input(f"{CYN}[Rem]{RST} ").strip()
        except (KeyboardInterrupt, EOFError): break
        if not t: continue
        if router.is_stop(router.norm(t)): print(f"{YLW}[DUNG]{RST}"); continue
        if t == "/exit" or t == "/quit": break
        if t == "/help": print(help()); continue
        if t == "/status": print(_st()); continue
        if t == "/lint": print(_lint()); continue
        if t == "/key": print(f"{GRN}[+]{RST} " + str(groq.scan(input("dan key gsk_: ").strip())) + " key"); continue
        if t == "/keys": print(_keys()); continue
        if t == "/clear":
            memory.cur.execute("DELETE FROM hist"); memory.conn.commit(); print(f"{GRN}[XOA]{RST}"); continue
        lm = memory.les_recall(t)
        memo = memory.recall(t) + ("\n" + lm if lm else "")
        if memo and DEBUG: print(f"{YLW}[MEM]{RST} " + memo[:300])
        try: r = brain(t)
        except Exception as e: r = f"{RED}[?]{RST} " + str(e)
        if r: _say(r)

if __name__ == "__main__":
    main()