import json, re, time, queue
from providers import groq
from tools import shelltool, pctool

LIVE = queue.Queue(); PEND = [None]

FC = [{"type": "function", "function": {"name": "bash", "description": "lenh shell",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
      {"type": "function", "function": {"name": "pc", "description": "GUI human-like",
        "parameters": {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]}}}]

def run_toolcall(n, a, cur_cmd="", readonly=False):
    if n == "bash": return shelltool.run(a.get("cmd", ""), cur_cmd, readonly)
    if n == "pc": return pctool.run_script(a.get("script", ""))
    return "STDERR:\n?"

def _s_in(site, kw):
    img = pctool.shot()
    t = groq.vision(f"Toa do O TIM KIEM trang {site}. Chi 'x,y'.", img)
    m = re.search(r"(\d{2,4})\s*[,;]\s*(\d{2,4})", t or "")
    if m:
        pctool.tap(int(m.group(1)), int(m.group(2))); time.sleep(0.4)
        pctool.type_text(kw); pctool.key("enter")
        return f"da tim '{kw}' trong {site}"
    pctool.type_text(kw); pctool.key("enter")
    return f"da tim '{kw}'"

def run_plan(acts, cur_cmd="", readonly=False):
    outs = []; t0 = time.time()
    for a in acts:
        if time.time() - t0 > 60: outs.append("[NGUNG 60s]"); break
        try: m = LIVE.get_nowait()
        except Exception: m = None
        if m: PEND[0] = m; break
        op = a.get("op")
        try:
            if op in ("open", "goto"): shelltool.run("xdg-open " + (a.get("url") or a.get("app") or ""), cur_cmd, readonly)
            elif op == "search": pctool.type_text(a.get("kw", "")); pctool.key("enter")
            elif op == "search_in": outs.append(_s_in(a.get("site") or "browser", a.get("kw", "")))
            elif op in ("click", "see"):
                if pctool.click_desc(a.get("mo ta") or a.get("desc", "")): outs.append("da click")
            elif op == "type": pctool.type_text(a.get("text", ""))
            elif op == "key": pctool.key(a.get("k", "Return"))
            elif op == "wait": time.sleep(min(float(a.get("s", 1)), 10))
            elif op == "look":
                t = groq.vision(a.get("q", "mo ta man hinh"), pctool.shot())
                if t: outs.append(t)
            elif op == "bash_read":
                c = a.get("cmd", "")
                outs.append(shelltool.run(c, cur_cmd, True)[:600] if not re.search(r"(rm |dd |install)", c) else "[BO QUA]")
            elif op == "reply": outs.append(a.get("text", ""))
        except Exception as ex: outs.append(f"[loi {op}: {ex}]")
    return outs