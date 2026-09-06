import json, re, time, queue, os
from providers import groq
from tools import shelltool, pctool, fileops
import web
from verifier import approve

LIVE = queue.Queue(); PEND = [None]

FC = [{"type": "function", "function": {"name": "bash", "description": "lenh shell",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}}},
      {"type": "function", "function": {"name": "pc", "description": "GUI human-like",
        "parameters": {"type": "object", "properties": {"script": {"type": "string"}}, "required": ["script"]}}},
      {"type": "function", "function": {"name": "fs", "description": "doc/ghi/sua/tim file",
        "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["read", "write", "edit", "ls", "glob", "grep", "rm"]},
            "path": {"type": "string"}, "pattern": {"type": "string"}, "include": {"type": "string"},
            "content": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}, "append": {"type": "boolean"}}, "required": ["op", "path"]}}},
      {"type": "function", "function": {"name": "web", "description": "tim kiem va doc trang web",
        "parameters": {"type": "object", "properties": {"op": {"type": "string", "enum": ["search", "open"]}, "q": {"type": "string"}, "url": {"type": "string"}}, "required": ["op"]}}}]

def run_toolcall(n, a, cur_cmd="", readonly=False):
    if n == "bash": return shelltool.run(a.get("cmd", ""), cur_cmd, readonly)
    if n == "pc": return pctool.run_script(a.get("script", ""))
    if n == "fs": return _fs(a, readonly)
    if n == "web":
        if a.get("op") == "open": return web.fetch(a.get("url", ""))
        return web.search(a.get("q", ""))
    return "STDERR:\n?"

def _fs(a, readonly=False):
    op = a.get("op"); p = a.get("path") or "."
    if readonly and op in ("write", "edit", "rm"): return "STDERR:\n[CHAN] luot KIEM TRA chi doc."
    if op == "read":
        return fileops.read(p)
    if op == "write": return fileops.write(p, a.get("content", ""), bool(a.get("append")))
    if op == "edit":
        o, nw = a.get("old", ""), a.get("new", "")
        if o == "" and nw: return fileops.write(p, nw, True)
        if o == "": return "STDERR:\nthieu old/new."
        return fileops.edit(p, o, nw)
    if op == "glob": return fileops.glob(a.get("pattern") or a.get("include") or "*", p)
    if op == "grep": return fileops.grep(a.get("pattern", ""), p, a.get("include") or "*")
    if op == "rm":
        if not approve(f"Cho phep xoa '{p}'?"): return "[BO QUA] Boss tu choi."
        return fileops.delete(p)
    return fileops.ls(p)

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
            elif op == "bash":
                outs.append(shelltool.run(a.get("cmd", ""), cur_cmd, readonly)[:700])
            elif op in ("web_search",):
                outs.append("[web] " + web.search(a.get("q", ""))[:500])
            elif op == "web_open":
                outs.append("[web] " + web.fetch(a.get("url", "") or a.get("q", ""))[:600])
            elif op in ("read", "write", "edit", "ls", "glob", "grep", "rm"):
                outs.append(_fs(a, readonly)[:700])
            elif op == "bash_read":
                c = a.get("cmd", "")
                outs.append(shelltool.run(c, cur_cmd, True)[:600] if not re.search(r"(rm |dd |install)", c) else "[BO QUA]")
            elif op == "reply": outs.append(a.get("text", ""))
        except Exception as ex: outs.append(f"[loi {op}: {ex}]")
    return outs