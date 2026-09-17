import os, sys, re, time, json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcplib import Server, Tool, schema, clamp
from config import TMP, DIR

# Điều khiển trình duyệt thật bằng Playwright (chromium) — dùng cho:
#   - tạo video/tự động (quay màn hình, YouTube Studio, dashboard...)
#   - đăng nhập để lấy dữ liệu trang cần session
#   - thao tác web phức tạp cần JS/click nhiều bước.
# Trình duyệt sống trong tiến trình này → trạng thái (tab, login) giữ nguyên
# giữa các lượt gọi tool.

_BROWSER = None
_PAGE = None
_ERR = ""  # lỗi khởi động trình duyệt gần nhất (để trả về thay vì crash server)
_OUT = os.path.join(TMP, "browser")
os.makedirs(_OUT, exist_ok=True)


def _need_page():
    """Lấy page hoặc thông báo lỗi — KHÔNG bao giờ raise (raise ngoài try sẽ
    giết cả MCP server, client treo tới timeout)."""
    global _ERR
    try:
        p = _ensure()
    except Exception as e:
        _ERR = f"[LOI] không khởi động được trình duyệt: {type(e).__name__}: {e}"
        return None
    if p is None:
        return None
    return p


_CF_TITLE = ("just a moment", "attention required", "verify you are human")


def _is_cf_page(page):
    """True nếu trang đang hiện thử thách Cloudflare/Turnstile."""
    try:
        t = (page.title() or "").lower()
        if any(k in t for k in _CF_TITLE):
            return True
        body = (page.inner_text("body") or "")[:2000].lower()
        return any(k in body for k in ("verify you are human", "just a moment",
                                      "checking your browser", "cf-challenge", "turnstile"))
    except Exception:
        return False


def _ensure():
    global _BROWSER, _PAGE
    if _PAGE is not None:
        return _PAGE
    from playwright.sync_api import sync_playwright
    _BROWSER = sync_playwright().start()
    profile = os.path.join(DIR, "browser_profile")
    os.makedirs(profile, exist_ok=True)
    _args = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
             "--disable-blink-features=AutomationControlled",
             "--disable-automation", "--disable-infobars"]
    try:
        # persistent context → cookies/đăng nhập + cf_clearance sống sót qua restart
        ctx = _BROWSER.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=True,
            no_viewport=True,
            viewport={"width": 1280, "height": 800},
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            locale="vi-VN", timezone_id="Asia/Ho_Chi_Minh",
            args=_args,
        )
        _PAGE = ctx.pages[0] if ctx.pages else ctx.new_page()
    except Exception:
        # fallback: context thường
        b = _BROWSER.chromium.launch(headless=True, args=_args)
        _PAGE = b.new_page(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            locale="vi-VN", timezone_id="Asia/Ho_Chi_Minh")
    _PAGE.set_default_timeout(30000)
    _PAGE.set_default_navigation_timeout(45000)
    _PAGE.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};"
        "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
        "Object.defineProperty(navigator,'languages',{get:()=>['vi-VN','vi','en-US','en']});")
    try:
        _PAGE.set_viewport_size({"width": 1280, "height": 800})
    except Exception:
        pass
    return _PAGE


def _maybe_screenshot(page, name="shot"):
    try:
        p = os.path.join(_OUT, time.strftime("%H%M%S") + f"_{name}.png")
        page.screenshot(path=p, full_page=False)
        return p
    except Exception:
        return ""


def browser_status():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return f"Playwright thiếu: {e}"
    if _PAGE is not None:
        try:
            url = _PAGE.url
            return f"Đang mở: {url}\nScreenshot thư mục: {_OUT}"
        except Exception:
            return "Đã khởi tạo nhưng trang đang lỗi."
    return (f"Chưa mở trình duyệt. Playwright OK, sẽ dùng chromium headless.\n"
            f"Screenshot thư mục: {_OUT}")


def browser_open(url):
    page = _need_page()
    if page is None:
        return _ERR or '[LOI] trình duyệt chưa sẵn sàng'
    if not re.match(r"^https?://", url) and "." in url and " " not in url:
        url = "https://" + url
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        # Cloudflare Turnstile/IUAM: chờ tối đa ~12s cho tự giải rồi đọc lại
        if _is_cf_page(page):
            for _ in range(6):
                page.wait_for_timeout(2000)
                if not _is_cf_page(page):
                    break
        title = page.title() or ""
        text = (page.inner_text("body") or "")[:400]
        shot = _maybe_screenshot(page, "open")
        flag = " (Cloudflare đã tự giải xong)" if not _is_cf_page(page) and title else ""
        if _is_cf_page(page):
            return (f"Đã mở: {page.url}\nTiêu đề: {title}\nTRANG ĐANG BỊ CLOUDFLARE CHẶN "
                    f"(Turnstile/IUAM chưa qua). Đợi 10-20s rồi gọi browser_content() lại, "
                    f"hoặc giải tay 1 lần (cookie cf_clearance sẽ được lưu).\n\n{text}\n\nScreenshot: {shot}")
        return f"Đã mở: {page.url}\nTiêu đề: {title}{flag}\n\n{text}\n\nScreenshot: {shot}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_navigate(url):
    return browser_open(url)


def browser_click(selector, index=0):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        els = page.locator(selector)
        n = els.count()
        if n == 0:
            return f"[LOI] Không tìm thấy phần tử: {selector}"
        i = min(max(0, int(index)), n - 1)
        els.nth(i).scroll_into_view_if_needed()
        els.nth(i).click()
        page.wait_for_timeout(500)
        return f"Đã click [{i}] {selector} (có {n} phần tử).\nURL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_click_text(text, index=0):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        el = page.get_by_text(text, exact=False).nth(int(index))
        el.scroll_into_view_if_needed()
        el.click()
        page.wait_for_timeout(500)
        return f"Đã click text: {text}\nURL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_type(selector, text, clear=False):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        text = str(text or "")
        if len(text) > 4000:
            return "[LOI] text quá dài (>4000 ký tự) — chia nhỏ ra"
        el = page.locator(selector).first
        el.scroll_into_view_if_needed()
        el.click()
        if clear:
            el.fill("")
        el.type(text, delay=15)
        return f"Đã gõ {len(text)} ký tự vào {selector}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_press(key, selector=None):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        if selector:
            page.locator(selector).first.press(key)
        else:
            page.keyboard.press(key)
        return f"Đã nhấn phím: {key}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_screenshot(full=False, name=""):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        p = os.path.join(_OUT, time.strftime("%H%M%S") + (("_" + name) if name else "") + ".png")
        page.screenshot(path=p, full_page=bool(full))
        return p
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_content(max_chars=8000, text=True):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        try:
            max_chars = max(200, min(int(max_chars or 8000), 60000))
        except Exception:
            max_chars = 8000
        if text:
            body = page.inner_text("body")
        else:
            body = page.content()
        return clamp(body, max_chars) + f"\n--- URL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_eval(js):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        out = page.evaluate(js) if not js.strip().startswith("(") else page.evaluate(js)
        return clamp(json.dumps(out, ensure_ascii=False, default=str), 8000) if not isinstance(out, str) else clamp(out, 8000)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_wait(selector=None, timeout=15000, sleep=1.0):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        if selector:
            try:
                timeout = max(500, min(int(timeout or 15000), 30000))
            except Exception:
                timeout = 15000
            page.wait_for_selector(selector, timeout=timeout)
        else:
            try:
                sleep = max(0.1, min(float(sleep or 1.0), 10.0))
            except Exception:
                sleep = 1.0
            time.sleep(sleep)
        return f"Đã chờ. URL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_scroll(direction="down", amount=600):
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        sign = 1 if direction in ("down", "bottom") else -1
        page.evaluate(f"window.scrollBy(0, {sign * int(amount)})")
        page.wait_for_timeout(300)
        return f"Đã cuộn {direction} {amount}px. URL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_search(q, n=5):
    page = _need_page()
    if page is None:
        return _ERR or '[LOI] trình duyệt chưa sẵn sàng'
    try:
        try:
            n = max(1, min(int(n or 5), 10))
        except Exception:
            n = 5
        page.goto("https://www.bing.com/search", wait_until="domcontentloaded")
        page.fill("#sb_form_q", q)
        page.press("#sb_form_q", "Enter")
        page.wait_for_timeout(2500)
        results = page.locator("li.b_algo").all()[:n]
        if not results:
            results = page.locator("li.b_algo, li.b_pag, .b_results li").all()[:n]
        out = []
        for i, r in enumerate(results, 1):
            try:
                link = r.locator("a").first
                href = link.get_attribute("href") or ""
                title = (link.inner_text() or "").strip()
                snip = ""
                try:
                    snip = (r.locator(".b_caption p, .b_caption, p").first.inner_text() or "")[:220]
                except Exception:
                    pass
                if title and href.startswith("http"):
                    out.append(f"{i}. {title}\n   {href}\n   {snip}")
            except Exception:
                continue
        return "\n".join(out) if out else "Không có kết quả từ Bing.\n" + browser_content(2000)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_back():
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        page.go_back()
        page.wait_for_timeout(500)
        return f"Đã quay lại: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_snapshot(max_items=60, a=None):
    """LIỆT KÊ phần tử tương tác trên trang (như dl_tree cho web): nút/link/ô nhập kèm selector gợi ý.
    Gọi sau browser_open để chọn selector CHUẨN, đỡ đoán mò."""
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        try:
            max_items = max(10, min(int(max_items or 60), 150))
        except Exception:
            max_items = 60
        js = """() => {
          const out = [];
          const els = document.querySelectorAll('a,button,input,select,textarea,[role=button],[onclick]');
          for (const el of els) {
            if (out.length >= 150) break;
            const r = el.getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            const tag = (el.tagName || '?').toLowerCase();
            const txt = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g,' ').slice(0,60);
            let sel = tag;
            if (el.id) sel += '#' + el.id;
            else if (el.name) sel += `[name=${el.name}]`;
            else if (el.className && typeof el.className === 'string') {
              const c = el.className.trim().split(/\\s+/)[0];
              if (c) sel += '.' + c;
            }
            const typ = el.getAttribute('type') || '';
            out.push(`${tag}${typ ? '['+typ+']' : ''} | ${txt || '(no text)'} | ${sel}`);
          }
          return out;
        }"""
        items = page.evaluate(js) or []
        items = items[:max_items]
        if not items:
            return "(trang không có phần tử tương tác nào)"
        head = f"Trang {page.url} — {len(items)} phần tử (dùng selector cột cuối cho browser_click/browser_type):"
        return head + "\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(items))
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_fill_login(user_selector="", pass_selector="", username="", password="", submit_selector="", a=None):
    """ĐĂNG NHẬP 1 PHÁT: điền user+pass rồi bấm submit. Giảm 3-4 tool còn 1, nghe lời hơn."""
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    if not user_selector or not pass_selector:
        return "[LOI] cần user_selector + pass_selector (lấy từ browser_snapshot). vd input[name=email], input[type=password]"
    try:
        page.locator(user_selector).first.scroll_into_view_if_needed()
        page.locator(user_selector).first.click()
        page.locator(user_selector).first.fill("")
        page.locator(user_selector).first.type(str(username or ""), delay=20)
        page.locator(pass_selector).first.click()
        page.locator(pass_selector).first.fill("")
        page.locator(pass_selector).first.type(str(password or ""), delay=20)
        if submit_selector:
            try:
                page.locator(submit_selector).first.scroll_into_view_if_needed()
                page.locator(submit_selector).first.click()
            except Exception:
                page.keyboard.press("Enter")
        else:
            page.keyboard.press("Enter")
        page.wait_for_timeout(2000)
        title = page.title() or ""
        return f"Đã điền + submit login. URL: {page.url}\nTiêu đề: {title}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_tabs(action="list", index=0, url="", a=None):
    """QUẢN LÝ TAB: list/new/switch/close. action=list|new|switch|close."""
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    try:
        ctx = page.context
        tabs = list(ctx.pages)
        act = (action or "list").strip().lower()
        if act == "list":
            lines = [f"{i}. { (p.url or '(trống)')[:100]}" for i, p in enumerate(tabs)]
            return f"Đang có {len(tabs)} tab (tab hiện tại: {page.url}):\n" + "\n".join(lines)
        if act == "new":
            np = ctx.new_page()
            if url:
                np.goto(url, wait_until="domcontentloaded")
            global _PAGE
            _PAGE = np
            return f"Đã mở tab mới: {np.url}"
        if act == "switch":
            i = max(0, min(int(index or 0), len(tabs) - 1))
            _PAGE = tabs[i]
            try:
                _PAGE.bring_to_front()
            except Exception:
                pass
            return f"Đã chuyển sang tab {i}: {_PAGE.url}"
        if act == "close":
            i = max(0, min(int(index or 0), len(tabs) - 1))
            if len(tabs) <= 1:
                return "[LOI] chỉ còn 1 tab — dùng browser_close để đóng trình duyệt"
            tabs[i].close()
            if tabs[i] == page:
                _PAGE = ctx.pages[0] if ctx.pages else None
            return f"Đã đóng tab {i}."
        return "[LOI] action phải là list|new|switch|close"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_wait_text(text="", timeout=15, a=None):
    """CHỜ chữ xuất hiện trên trang (tới 30s): thay vì sleep mù."""
    page = _need_page()
    if page is None:
        return _ERR or "[LOI] trình duyệt chưa sẵn sàng"
    if not (text or "").strip():
        return "[LOI] cần text cần chờ"
    try:
        timeout = max(2, min(int(timeout or 15), 30))
    except Exception:
        timeout = 15
    try:
        page.get_by_text(text, exact=False).first.wait_for(timeout=timeout * 1000)
        return f"Thấy chữ '{text}' trên {page.url}."
    except Exception:
        return f"[LOI] chờ {timeout}s vẫn chưa thấy '{text}' trên {page.url}."


def browser_close():
    global _BROWSER, _PAGE
    try:
        if _BROWSER is not None:
            _BROWSER.stop()
    except Exception:
        pass
    _BROWSER = None
    _PAGE = None
    return "Đã đóng trình duyệt."


TOOLS = [
    Tool("browser_status", "Kiểm tra trạng thái trình duyệt Playwright.",
         schema({}), browser_status),
    Tool("browser_open", "Mở một URL trong trình duyệt. Trả về tiêu đề trang + chữ đầu trang.",
         schema({"url": {"type": "string", "description": "URL hoặc tên miền"}}), browser_open),
    Tool("browser_navigate", "Điều hướng trình duyệt tới URL (giống browser_open).",
         schema({"url": {"type": "string"}}), browser_navigate),
    Tool("browser_click", "Click một phần tử bằng CSS selector.",
         schema({"selector": {"type": "string", "description": "vd: button#submit, .btn, input[name=x]"},
                 "index": {"type": "integer", "description": "thứ tự phần tử nếu có nhiều, mặc định 0"}}), browser_click),
    Tool("browser_click_text", "Click một phần tử theo text hiển thị (vd 'Đăng nhập').",
         schema({"text": {"type": "string"}, "index": {"type": "integer", "description": "mặc định 0"}}), browser_click_text),
    Tool("browser_type", "Gõ văn bản vào ô input (theo CSS selector).",
         schema({"selector": {"type": "string"}, "text": {"type": "string"},
                 "clear": {"type": "boolean", "description": "xoá nội dung cũ trước, mặc định false"}}), browser_type),
    Tool("browser_press", "Nhấn phím bàn phím (vd: Enter, Backspace, ctrl+s, Tab). Có thể nhắm vào 1 ô.",
         schema({"key": {"type": "string"}, "selector": {"type": "string", "description": "tuỳ chọn"}}), browser_press),
    Tool("browser_screenshot", "Chụp ảnh màn hình trang web hiện tại → trả về đường dẫn file PNG.",
         schema({"full": {"type": "boolean", "description": "toàn trang, mặc định false"},
                 "name": {"type": "string", "description": "tên file tuỳ chọn"}}), browser_screenshot),
    Tool("browser_content", "Lấy nội dung văn bản (hoặc HTML) của trang hiện tại.",
         schema({"max_chars": {"type": "integer", "description": "mặc định 8000"},
                 "text": {"type": "boolean", "description": "true=lấy text, false=lấy HTML, mặc định true"}}), browser_content),
    Tool("browser_eval", "Chạy JavaScript trong trang hiện tại.",
         schema({"js": {"type": "string", "description": "đoạn JS cần chạy"}}), browser_eval),
    Tool("browser_wait", "Chờ 1 phần tử xuất hiện hoặc chờ vài giây.",
         schema({"selector": {"type": "string", "description": "CSS selector cần chờ (tuỳ chọn)"},
                 "timeout": {"type": "integer", "description": "ms, mặc định 15000"},
                 "sleep": {"type": "number", "description": "giây chờ nếu không có selector, mặc định 1"}}), browser_wait),
    Tool("browser_scroll", "Cuộn trang lên/xuống.",
         schema({"direction": {"type": "string", "description": "up/down, mặc định down"},
                 "amount": {"type": "integer", "description": "số px, mặc định 600"}}), browser_scroll),
    Tool("browser_search", "Tìm kiếm web bằng Bing và trả về danh sách kết quả.",
         schema({"q": {"type": "string"}, "n": {"type": "integer", "description": "số kết quả, mặc định 5"}}), browser_search),
    Tool("browser_snapshot", "LIỆT KÊ nút/link/ô nhập trên trang kèm selector gợi ý (như dl_tree cho web). Gọi sau browser_open để chọn selector CHUẨN.",
         schema({"max_items": {"type": "integer", "default": 60}}), browser_snapshot),
    Tool("browser_fill_login", "ĐĂNG NHẬP 1 PHÁT: điền user+pass rồi submit (lấy selector từ browser_snapshot).",
         schema({"user_selector": {"type": "string"}, "pass_selector": {"type": "string"},
                 "username": {"type": "string"}, "password": {"type": "string"},
                 "submit_selector": {"type": "string", "description": "tuỳ chọn, rỗng = nhấn Enter"}}), browser_fill_login),
    Tool("browser_tabs", "QUẢN LÝ TAB: list/new/switch/close.",
         schema({"action": {"type": "string", "description": "list|new|switch|close", "default": "list"},
                 "index": {"type": "integer", "default": 0},
                 "url": {"type": "string", "default": ""}}), browser_tabs),
    Tool("browser_wait_text", "CHỜ chữ xuất hiện trên trang (tới 30s).",
         schema({"text": {"type": "string"}, "timeout": {"type": "integer", "default": 15}}), browser_wait_text),
    Tool("browser_back", "Quay lại trang trước trong lịch sử.",
         schema({}), browser_back),
    Tool("browser_close", "Đóng trình duyệt, giải phóng tài nguyên.",
         schema({}), browser_close),
]

if __name__ == "__main__":
    Server(TOOLS, "browser_auto", "0.1.0").serve(sys.stdin, sys.stdout)
