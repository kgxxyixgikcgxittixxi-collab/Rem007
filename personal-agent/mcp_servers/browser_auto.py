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
_OUT = os.path.join(TMP, "browser")
os.makedirs(_OUT, exist_ok=True)


def _ensure():
    global _BROWSER, _PAGE
    if _PAGE is not None:
        return _PAGE
    from playwright.sync_api import sync_playwright
    _BROWSER = sync_playwright().start()
    profile = os.path.join(DIR, "browser_profile")
    os.makedirs(profile, exist_ok=True)
    try:
        # persistent context → cookies/đăng nhập sống sót qua restart
        ctx = _BROWSER.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=True,
            no_viewport=True,
            viewport={"width": 1280, "height": 800},
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                  "--disable-blink-features=AutomationControlled"],
        )
        _PAGE = ctx.pages[0] if ctx.pages else ctx.new_page()
    except Exception:
        # fallback: context thường
        b = _BROWSER.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-blink-features=AutomationControlled"],
        )
        _PAGE = b.new_page()
    _PAGE.set_default_timeout(30000)
    _PAGE.set_default_navigation_timeout(45000)
    _PAGE.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        "window.chrome={runtime:{}};")
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
    page = _ensure()
    if not re.match(r"^https?://", url) and "." in url and " " not in url:
        url = "https://" + url
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(800)
        title = page.title() or ""
        text = (page.inner_text("body") or "")[:400]
        shot = _maybe_screenshot(page, "open")
        return f"Đã mở: {page.url}\nTiêu đề: {title}\n\n{text}\n\nScreenshot: {shot}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_navigate(url):
    return browser_open(url)


def browser_click(selector, index=0):
    try:
        els = _PAGE.locator(selector)
        n = els.count() if _PAGE else 0
        if n == 0:
            return f"[LOI] Không tìm thấy phần tử: {selector}"
        i = min(max(0, int(index)), n - 1)
        els.nth(i).scroll_into_view_if_needed()
        els.nth(i).click()
        _PAGE.wait_for_timeout(500)
        return f"Đã click [{i}] {selector} (có {n} phần tử).\nURL: {_PAGE.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_click_text(text, index=0):
    try:
        page = _ensure()
        el = page.get_by_text(text, exact=False).nth(int(index))
        el.scroll_into_view_if_needed()
        el.click()
        page.wait_for_timeout(500)
        return f"Đã click text: {text}\nURL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_type(selector, text, clear=False):
    try:
        page = _ensure()
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
    try:
        page = _ensure()
        if selector:
            page.locator(selector).first.press(key)
        else:
            page.keyboard.press(key)
        return f"Đã nhấn phím: {key}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_screenshot(full=False, name=""):
    try:
        page = _ensure()
        p = os.path.join(_OUT, time.strftime("%H%M%S") + (("_" + name) if name else "") + ".png")
        page.screenshot(path=p, full_page=bool(full))
        return p
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_content(max_chars=8000, text=True):
    try:
        page = _ensure()
        if text:
            body = page.inner_text("body")
        else:
            body = page.content()
        return clamp(body, int(max_chars)) + f"\n--- URL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_eval(js):
    try:
        page = _ensure()
        out = page.evaluate(js) if not js.strip().startswith("(") else page.evaluate(js)
        return clamp(json.dumps(out, ensure_ascii=False, default=str), 8000) if not isinstance(out, str) else clamp(out, 8000)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_wait(selector=None, timeout=15000, sleep=1.0):
    try:
        page = _ensure()
        if selector:
            page.wait_for_selector(selector, timeout=int(timeout))
        else:
            time.sleep(float(sleep))
        return f"Đã chờ. URL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_scroll(direction="down", amount=600):
    try:
        page = _ensure()
        sign = 1 if direction in ("down", "bottom") else -1
        page.evaluate(f"window.scrollBy(0, {sign * int(amount)})")
        page.wait_for_timeout(300)
        return f"Đã cuộn {direction} {amount}px. URL: {page.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


def browser_search(q, n=5):
    page = _ensure()
    try:
        page.goto("https://www.bing.com/search", wait_until="domcontentloaded")
        page.fill("#sb_form_q", q)
        page.press("#sb_form_q", "Enter")
        page.wait_for_timeout(2500)
        results = page.locator("li.b_algo").all()[: int(n)]
        if not results:
            results = page.locator("li.b_algo, li.b_pag, .b_results li").all()[: int(n)]
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
    try:
        _PAGE.go_back()
        _PAGE.wait_for_timeout(500)
        return f"Đã quay lại: {_PAGE.url}"
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"


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
    Tool("browser_back", "Quay lại trang trước trong lịch sử.",
         schema({}), browser_back),
    Tool("browser_close", "Đóng trình duyệt, giải phóng tài nguyên.",
         schema({}), browser_close),
]

if __name__ == "__main__":
    Server(TOOLS, "browser_auto", "0.1.0").serve(sys.stdin, sys.stdout)