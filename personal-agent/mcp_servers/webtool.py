import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import html as _html, re, os, hashlib
import requests
from mcplib import Server, Tool, schema, clamp
from config import TMP, MAX_TOOL_OUT

UA = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}

# ── Cloudflare: phát hiện + fallback 3 tầng ──────────────────────────────
# Tầng 1: requests thường (nhanh). Tầng 2: curl_cffi giả TLS Chrome
# (kiểu dự án lexiforest/curl_cffi, zinzied/cloudscraper trên GitHub).
# Tầng 3: trình duyệt thật browser_open (giải JS/Turnstile).
_CF_MARKERS = (
    "just a moment", "verify you are human", "attention required",
    "enable javascript and cookies", "cf-challenge", "cf_turnstile",
    "cf-turnstile", "cf_clearance", "checking your browser",
    "security verification", "ddos protection by",
)


def _is_cf_block(status, text):
    """True nếu trang trả về thử thách Cloudflare/bot-wall."""
    if status in (403, 503) and text:
        low = (text or "")[:8000].lower()
        return any(m in low for m in _CF_MARKERS)
    return False


def _fetch_curl(url, timeout=25):
    """Tầng 2: curl_cffi giả fingerprint TLS Chrome — qua được
    Cloudflare mức TLS/JA3 mà requests thường bị 403."""
    try:
        from curl_cffi import requests as _cr
    except Exception:
        return None, "thiếu curl_cffi"
    for imp in ("chrome", "safari"):
        try:
            r = _cr.get(url, headers=UA, timeout=timeout, impersonate=imp)
            if r.status_code == 200 and not _is_cf_block(r.status_code, r.text):
                return r, ""
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    return None, last


# ── Ảnh minh hoạ TỪ WEB (không sinh AI) ───────────────────────────────────
_IMG_DIR = os.path.join(TMP, "web_images")
os.makedirs(_IMG_DIR, exist_ok=True)


def _web_images_commons(q, n):
    """Ảnh thật tự do từ Wikimedia Commons (API không cần key, ổn định)."""
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php",
                         params={"action": "query", "generator": "search",
                                 "gsrsearch": q, "gsrnamespace": 6,
                                 "gsrlimit": min(n * 3, 50),
                                 "prop": "imageinfo", "iiprop": "url|size",
                                 "iiurlwidth": 1400, "format": "json"},
                         headers=UA, timeout=25)
        j = r.json()
        rows = []
        for p in (j.get("query") or {}).get("pages", {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            u = ii.get("thumburl") or ii.get("url")
            if u and any(e in (ii.get("url") or "").lower() for e in (".jpg", ".jpeg", ".png", ".webp")):
                rows.append(f"- {p.get('title', '')[:80]} | {u}")
            if len(rows) >= n:
                break
        return rows
    except Exception:
        return []


def _web_images_bing(q, n):
    """Fallback: quét Bing Images trực tiếp (không cần key)."""
    try:
        from curl_cffi import requests as _cr
        r = _cr.get("https://www.bing.com/images/search",
                    params={"q": q, "form": "HDRSC2"}, timeout=25, impersonate="chrome")
        urls = re.findall(r'murl&quot;:&quot;([^&]+?)&quot;', r.text, re.S)
        rows = []
        seen = set()
        for u in urls:
            u = u.replace("\\/", "/")
            if u not in seen and any(e in u.lower() for e in (".jpg", ".jpeg", ".png", ".webp")):
                seen.add(u)
                rows.append(f"- ảnh | {u}")
            if len(rows) >= n:
                break
        return rows
    except Exception:
        return []


def web_images(q, n=8):
    """Tìm các URL ảnh THẬT trên web (Wikimedia Commons → fallback Bing Images)."""
    n = max(1, _int(n, 8))
    rows = _web_images_commons(q, n)
    if not rows:
        rows = _web_images_bing(q, n)
    return "\n".join(rows) if rows else "(không có ảnh — thử đổi chủ đề tiếng Anh khác)"


def _dl_one(url, name=""):
    """Tải 1 ảnh về ~/.rem_ai/tmp/web_images/. Trả về (path, size, ctype)."""
    try:
        h = hashlib.md5(str(url).encode()).hexdigest()[:12]
        # Ưu tiên curl_cffi (qua TLS mạnh), fallback requests — stream để không OOM
        data, ctype_hdr = b"", ""
        try:
            from curl_cffi import requests as _cr
            r = _cr.get(url, headers={**UA, "Referer": "https://duckduckgo.com/"},
                        timeout=30, stream=True, impersonate="chrome")
            if r.status_code != 200:
                return None, 0, f"HTTP {r.status_code}"
            ctype_hdr = r.headers.get("content-type", "")
            if not ctype_hdr.lower().startswith("image/"):
                return None, 0, f"không phải ảnh ({ctype_hdr[:60]})"
            buf = bytearray()
            for chunk in r.iter_content(chunk_size=1 << 16):
                if chunk:
                    buf.extend(chunk)
                    if len(buf) > 12 * 1024 * 1024:
                        return None, 0, "ảnh quá lớn (>12MB)"
            data = bytes(buf)
        except Exception as e:
            # curl_cffi không stream/lỗi → fallback requests stream
            try:
                r = requests.get(url, headers={**UA, "Referer": "https://duckduckgo.com/"},
                                 timeout=30, stream=True)
                if r.status_code != 200:
                    return None, 0, f"HTTP {r.status_code}"
                ctype_hdr = r.headers.get("content-type", "")
                if not ctype_hdr.lower().startswith("image/"):
                    return None, 0, f"không phải ảnh ({ctype_hdr[:60]})"
                buf = bytearray()
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        buf.extend(chunk)
                        if len(buf) > 12 * 1024 * 1024:
                            return None, 0, "ảnh quá lớn (>12MB)"
                data = bytes(buf)
            except Exception as e2:
                return None, 0, f"{type(e2).__name__}: {e2}"
        if not data:
            return None, 0, "rỗng"
        ct = ctype_hdr or ""
        ext = ".png" if "png" in ct else (".jpg" if "jpeg" in ct or "jpg" in ct else ".webp")
        safe = re.sub(r"[^\w]+", "_", str(name))[:30] or ""
        p = os.path.join(_IMG_DIR, f"{safe}{h}{ext}" if safe else f"{h}{ext}")
        with open(p, "wb") as f:
            f.write(data)
        return p, len(data), ""
    except Exception as e:
        return None, 0, f"{type(e).__name__}: {e}"


def web_download_image(url, name=""):
    """Tải 1 ảnh từ URL về máy, trả về đường dẫn để nhúng vào PDF/HTML."""
    p, sz, err = _dl_one(url, name)
    if not p:
        return f"[LOI] tải ảnh lỗi: {err}"
    return f"{p} ({sz} bytes)"


def web_download_images(urls):
    """Tải NHIỀU ảnh cùng lúc. urls = danh sách URL (hoặc chuỗi tách bằng xuống dòng).
    Mỗi ảnh tối đa ~30s (trong _dl_one), tối đa 20 URL → tránh block server 50 phút."""
    if isinstance(urls, str):
        urls = re.split(r"[\n;]+", urls)
    if isinstance(urls, (list, tuple)) and len(urls) > 20:
        urls = list(urls)[:20]
    tot = 0
    rows = []
    for i, u in enumerate(urls, 1):
        u = (u or "").strip()
        if not u or not u.startswith("http"):
            continue
        p, sz, err = _dl_one(u, name=f"img{i}")
        if p:
            tot += sz
            rows.append(f"{p} {sz} {u}")
        else:
            rows.append(f"[skip {err}] {u}")
    rows.append(f"TỔNG: {tot} bytes ({max(0, len(rows) - 0)} mục)")
    return "\n".join(rows)


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


def _to_text(html_text, max_chars):
    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html_text or "", flags=re.S)
    txt = re.sub(r"<[^>]+>", " ", txt)
    return clamp(_strip(txt), max_chars)


def web_fetch(url, max_chars=40000):
    max_chars = _int(max_chars, 40000)
    # Tầng 1: requests thường
    try:
        r = requests.get(url, headers=UA, timeout=25)
        if r.status_code == 200 and not _is_cf_block(200, r.text):
            ctype = r.headers.get("content-type", "")
            return clamp(r.text, max_chars) if "json" in ctype else _to_text(r.text, max_chars)
        first_err = f"HTTP {r.status_code} (Cloudflare)" if _is_cf_block(r.status_code, r.text) else f"HTTP {r.status_code}"
    except Exception as e:
        r = None
        first_err = f"{type(e).__name__}: {e}"
    # Tầng 2: curl_cffi giả TLS Chrome
    rc, cerr = _fetch_curl(url)
    if rc is not None:
        ctype = rc.headers.get("content-type", "") if hasattr(rc, "headers") else ""
        return clamp(rc.text, max_chars) if "json" in ctype else _to_text(rc.text, max_chars)
    # Tầng 3: bó tay → hướng sang trình duyệt thật
    return (f"[LOI] Trang chặn bot ({first_err}; curl fallback: {cerr}). "
            f"Hãy dùng browser_open('{url}') rồi browser_content() để đọc bằng trình duyệt thật.")


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
    Tool("web_images", "Tìm URL ảnh THẬT trên web (DuckDuckGo Images) theo chủ đề. Trả về từng dòng: tiêu đề | URL.",
         schema({"q": {"type": "string", "description": "chủ đề cần ảnh, viết tiếng Anh cho đúng kết quả, vd 'english grammar tenses chart'"}, "n": {"type": "integer", "description": "số ảnh muốn, mặc định 8"}}), web_images),
    Tool("web_download_image", "Tải 1 ảnh từ URL web về máy (dùng để minh họa PDF). Trả về đường dẫn + size bytes.",
         schema({"url": {"type": "string"}, "name": {"type": "string", "description": "tên gợi nhớ, tuỳ chọn"}}), web_download_image),
    Tool("web_download_images", "Tải NHIỀU ảnh cùng lúc (dấu xuống dòng hoặc ; giữa các URL). Trả về từng đường dẫn + tổng bytes.",
         schema({"urls": {"type": "string", "description": "các URL tách bằng dấu xuống dòng"}}), web_download_images),
]

if __name__ == "__main__":
    Server(TOOLS, "webtool", "0.1.0").serve(sys.stdin, sys.stdout)
