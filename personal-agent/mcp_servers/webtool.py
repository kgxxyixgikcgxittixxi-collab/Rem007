import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import html as _html, re
import requests
from mcplib import Server, Tool, schema, clamp
from config import TMP, MAX_TOOL_OUT

UA = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}


def _strip(h):
    return re.sub(r"\s+", " ", _html.unescape(h or "")).strip()


def _real_url(u):
    from urllib.parse import unquote
    if "uddg=" in u:
        return unquote(u.split("uddg=", 1)[1].split("&", 1)[0])
    return u


def _int(v, default):
    """Ép số an toàn — LLM thỉnh thoảng gửi chuỗi ("5") hoặc chữ rác;
    int() thẳng sẽ nổ cả MCP server."""
    try:
        v = int(v)
        return v if v > 0 else default
    except Exception:
        return default


def web_search(q, n=5):
    last = ""
    for url in (
        "https://lite.duckduckgo.com/lite/",
        "https://html.duckduckgo.com/html/",
    ):
        try:
            r = requests.get(url, params={"q": q}, headers=UA, timeout=20)
        except Exception as e:
            last = f"[LOI] {type(e).__name__}: {e}"
            continue
        if r.status_code != 200:
            last = f"[LOI] HTTP {r.status_code}"
            continue
        titles = re.findall(r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
        snips = re.findall(r'(?:result-snippet|result__snippet)[^>]*>(.*?)</(?:td|a)>', r.text, re.S)
        if not titles:
            last = "(không có kết quả)"
            continue
        out = []
        for i, (u, t) in enumerate(titles[:max(1, _int(n, 5))], 1):
            s = _strip(snips[i - 1]) if i - 1 < len(snips) else ""
            out.append(f"{i}. {_strip(t)}\n   {_real_url(u)}\n   {s}")
        return "\n".join(out)
    return last


def web_fetch(url, max_chars=40000):
    max_chars = _int(max_chars, 40000)
    try:
        r = requests.get(url, headers=UA, timeout=25)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    if r.status_code != 200:
        return f"[LOI] HTTP {r.status_code}"
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        return clamp(r.text, max_chars)
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text, flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return clamp(_strip(txt), max_chars)


def github_api(path, token=None):
    base = "https://api.github.com"
    if not path.startswith("/"):
        path = "/" + path
    headers = {"Accept": "application/vnd.github+json", **UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = requests.get(base + path, headers=headers, timeout=25)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    try:
        body = r.json()
    except Exception:
        body = r.text
    if isinstance(body, dict):
        body["_http"] = r.status_code
    return clamp(body if isinstance(body, str) else __import__("json").dumps(body, ensure_ascii=False, indent=1), 20000)


TOOLS = [
    Tool("web_search", "Tìm kiếm trên web (DuckDuckGo). Kết quả: tiêu đề + link + mô tả.",
         schema({"q": {"type": "string"}, "n": {"type": "integer", "description": "số kết quả, mặc định 5"}}), web_search),
    Tool("web_fetch", "Đọc nội dung 1 trang web thành văn bản.",
         schema({"url": {"type": "string"}, "max_chars": {"type": "integer", "description": "mặc định 40000"}}), web_fetch),
    Tool("github_api", "Gọi REST API của GitHub (vd: /user, /repos/Rem007/Rem).",
         schema({"path": {"type": "string", "description": "đường dẫn API bắt đầu bằng /"}, "token": {"type": "string"}}), github_api),
]

if __name__ == "__main__":
    Server(TOOLS, "webtool", "0.1.0").serve(sys.stdin, sys.stdout)