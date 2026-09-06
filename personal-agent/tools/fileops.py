import os, re, fnmatch
from config import MAX_TOOL_OUT
SKIP = {".git", "__pycache__", "node_modules", ".cache", ".local"}

def _t(rel, p):
    try: s = os.stat(p)
    except Exception: return rel
    kind = "dir" if os.path.isdir(p) else "file"
    return f"{rel} [{kind} {s.st_size}B]"

def ls(path="."):
    path = path or "."
    try: e = sorted(os.listdir(path))
    except Exception as ex: return f"STDERR:\n{ex}"
    return "STDOUT:\n" + "\n".join(_t(n, os.path.join(path, n)) for n in e[:100])

def read(path, max_chars=MAX_TOOL_OUT):
    try:
        with open(path, encoding="utf-8") as f: t = f.read(max_chars + 500)
    except Exception as ex: return f"STDERR:\n{ex}"
    if len(t) > max_chars: t = t[:max_chars] + "\n...[CUT]"
    return f"STDOUT: {path} {len(t)}B\n{t}"

def write(path, content="", append=False):
    try:
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as f: f.write(content)
    except Exception as ex: return f"STDERR:\n{ex}"
    return f"STDOUT: {'append' if append else 'ghi'} xong {path} ({len(content)} ky tu)"

def edit(path, old="", new=""):
    try:
        with open(path, encoding="utf-8") as f: t = f.read()
    except Exception as ex: return f"STDERR:\n{ex}"
    n = t.count(old)
    if n == 0: return "STDERR:\nkhong tim thay van ban can sua."
    if n > 1: return f"STDERR:\n{path} co {n} cho giong nhau — cu the hon."
    with open(path, "w", encoding="utf-8") as f: f.write(t.replace(old, new, 1))
    return f"STDOUT: da sua {path} (1/1)"

def delete(path, force=False):
    try:
        if os.path.isdir(path) and not force: return "STDERR:\nchi xoa file, khong xoa thu muc."
        os.remove(path)
    except Exception as ex: return f"STDERR:\n{ex}"
    return f"STDOUT: da xoa {path}"

def glob(pattern, root="."):
    rp = os.path.normpath(root)
    hits = []
    for dp, dns, fns in os.walk(rp):
        dns[:] = [d for d in dns if d not in SKIP]
        for fn in fns:
            p = os.path.join(os.path.relpath(dp, rp), fn)
            if fnmatch.fnmatch(p, pattern) or fnmatch.fnmatch(fn, pattern):
                hits.append(p)
    return ("STDOUT:\n" + "\n".join(hits[:60])) if hits else "STDOUT:\n(0 ket qua)"

def grep(pattern, root=".", include="*"):
    rgx = re.compile(pattern)
    rp = os.path.normpath(root)
    hits = []
    for dp, dns, fns in os.walk(rp):
        dns[:] = [d for d in dns if d not in SKIP]
        for fn in fns:
            if include != "*" and not fnmatch.fnmatch(fn, include): continue
            p = os.path.join(dp, fn)
            try:
                with open(p, encoding="utf-8", errors="ignore") as f:
                    for i, ln in enumerate(f, 1):
                        if rgx.search(ln):
                            hits.append(f"{os.path.relpath(p, rp)}:{i}: {ln.strip()[:120]}")
                            if len(hits) >= 40: break
            except Exception: pass
            if len(hits) >= 40: break
        if len(hits) >= 40: break
    return ("STDOUT:\n" + "\n".join(hits)) if hits else "STDOUT:\n(0 ket qua)"