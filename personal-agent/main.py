import re, json
from providers import groq
import memory, router, planner, executor
from tools import pctool
pctool.VISION = groq.vision

def brain(u):
    t0 = router.norm(u)
    for a, b in (("youtobe", "youtube"), ("coc coc", "coccoc"), ("cốc cốc", "coccoc"), ("mow ", "mo ")): t0 = t0.replace(a, b)
    ro = bool(re.search(r"(kiem tra|check)", t0))
    z = router.route(t0)
    if z == "chat" and not router.is_act(u):
        d = planner.parse(groq.text(planner.SUP_P + "\nBoss: " + u))
        if d and d.get("type") == "chat": return d.get("reply") or "..."
    elif z in ("gui", "headless"):
        ze = ""
        for i in range(3):
            d = planner.ask(z, u, ze)
            acts = [a for a in (d or {}).get("actions", []) if a.get("op") in planner.ALLOW[z]][:8] if d and d.get("type") == "task" else planner.fallback(z, u, t0)
            if acts:
                outs = executor.run_plan(acts, u, ro)
                if any(str(x).startswith("[loi") for x in outs) and i < 2:
                    ze = " | LOI: " + " | ".join(str(x) for x in outs)[:300]; continue
                if outs: return "[XONG] " + " | ".join(x for x in outs if x)[:700]
                return "[XONG] da thuc hien"
            break
    h = memory.load(); h.append({"role": "user", "content": u}); memory.save(h[-1])
    for _ in range(20):
        msg = groq.chat(h, executor.FC)
        if not msg: return "[!] groq loi."
        h.append({"role": "assistant", "content": msg.get("content") or ""}); memory.save(h[-1])
        calls = msg.get("tool_calls") or []
        if not calls: return msg.get("content") or ""
        for tc in calls[:3]:
            fn = tc.get("function", {})
            try: a = json.loads(fn.get("arguments") or "{}")
            except Exception: a = {}
            tr = executor.run_toolcall(fn.get("name"), a, u, ro)
            h.append({"role": "tool", "tool_call_id": tc.get("id"), "content": tr[:1500]}); memory.save(h[-1])
    return "[!] het vong."

def main():
    print("[BAN] Rem007 personal-agent (Groq/Qwen3.8)")
    while True:
        try: t = input("\n[Rem]: ").strip()
        except (KeyboardInterrupt, EOFError): break
        if not t: continue
        if router.is_stop(router.norm(t)): print("[DUNG]"); continue
        if t == "/key": print("[+] " + str(groq.scan(input("dan key gsk_: ").strip())) + " key"); continue
        if t == "/clear":
            memory.cur.execute("DELETE FROM hist"); memory.conn.commit(); print("[XOA]"); continue
        print("[AI]: " + (brain(t) or ""))

if __name__ == "__main__":
    main()