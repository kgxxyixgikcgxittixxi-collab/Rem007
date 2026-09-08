"""desktop_linux — điều khiển desktop LINUX qua accessibility tree (AT-SPI),
KHÔNG cần chụp màn hình / OCR / đoán pixel (tương tự agent-desktop trên macOS).

Dựa vào:
  - pyatspi (python3-pyatspi + gir1.2-atspi-2.0 + at-spi2-core) : cây giao diện + action
  - xdotool (X11) : phím chuột/bàn phím khi AT-SPI không có action / cần phím gõ

Thiếu thư viện → mỗi tool tự báo hướng cài; server vẫn nạp được, không crash.
Phiên chạy:   python3 -m mcp_servers.desktop_linux
"""

import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import threading, shutil, subprocess, time

from mcplib import Server, Tool, schema

_lock = threading.Lock()

# roles được phép nhận ref để tương tác (giống tập role interactive của agent-desktop)
_INTERACTIVE = {
    "button", "text", "textbox", "push button", "entry", "checkbox", "check box",
    "link", "menu item", "tab", "slider", "combo box", "combobox", "tree item",
    "cell", "radio button", "radiobutton", "switch", "incrementor", "password text",
}

_snap = {"apps": [], "map": {}, "time": 0.0}
SNAP_TTL = 600.0


def _at():
    try:
        import pyatspi
        return pyatspi
    except Exception:
        return None


def _xd():
    return shutil.which("xdotool")


def _run(cmd, timeout=30):
    """Chạy xdotool/wmctrl lệnh list -> (ok, stdout)."""
    p = _xd()
    if not p:
        return False, "[LOI] thiếu xdotool — cài: apt install xdotool (X11)"
    try:
        rr = subprocess.run([p, *cmd], capture_output=True, text=True, timeout=timeout)
        return rr.returncode == 0, (rr.stdout or rr.stderr or "").strip()
    except Exception as e:
        return False, f"[LOI] {type(e).__name__}: {e}"


def dl_status(a=None):
    at = _at()
    xd = _xd()
    rows = [
        ("pyatspi", "có" if at else "thiếu — apt install python3-pyatspi at-spi2-core"),
        ("xdotool", "có" if xd else "thiếu — apt install xdotool (chỉ input X11)"),
    ]
    try:
        subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "toolkit-accessibility"],
                       capture_output=True, text=True, timeout=10)
    except Exception:
        pass
    return "Môi trường điều khiển desktop Linux:\n" + "\n".join(f"- {k}: {v}" for k, v in rows)


def _desktop():
    at = _at()
    if not at:
        return None
    try:
        return at.Registry.getDesktop(0)
    except Exception:
        return None


def _obj_role(o):
    try:
        return o.getRoleName()
    except Exception:
        return "?"


def _obj_name(o):
    try:
        return (o.name or "").strip()
    except Exception:
        return ""


def _walk(o, depth, max_depth, limit, out, indent):
    """Duyệt cây, gán ref at#id cho phần tử tương tác được."""
    if len(out) >= limit or depth > max_depth:
        return
    role = _obj_role(o)
    name = _obj_name(o)
    n = len(out)
    tag = ""
    if role.lower() in _INTERACTIVE:
        ref = f"at{n}"
        obj_id = id(o)
        _snap["map"][ref] = {"obj": o, "role": role, "name": name}
        tag = f" [{ref}]"
    line = f"{'  ' * depth}{role}{tag}" + (f" '{name}'" if name else "")
    out.append(line)
    try:
        for ch in o:
            _walk(ch, depth + 1, max_depth, limit, out, indent + " ")
    except Exception:
        pass


def dl_apps(a=None):
    desk = _desktop()
    if desk is None:
        return "[LOI] không truy cập được AT-SPI (cần phiên desktop X11/Wayland + at-spi2-core)"
    rows = []
    try:
        for app in desk:
            nm = _obj_name(app)
            rows.append(nm or f"app#{id(app)}")
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    return "\n".join(rows) if rows else "(không có app nào bật accessibility)"


def dl_tree(app="", max_depth=6, limit=300, a=None):
    desk = _desktop()
    if desk is None:
        return "[LOI] không truy cập được AT-SPI (cần phiên desktop + at-spi2-core; cài: apt install python3-pyatspi)"
    with _lock:
        _snap["map"] = {}
    out = []
    try:
        for appobj in desk:
            if app and _obj_name(appobj).lower() != app.lower():
                continue
            _walk(appobj, 0, int(max_depth), int(limit), out, "")
        _snap["time"] = time.time()
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    if not out:
        return "(cây rỗng — vài app cần bật accessibility; với GNOME: gsettings set org.gnome.desktop.interface toolkit-accessibility true)"
    head = f"Cây AT-SPI ({len(out)} dòng, snapshot hợp lệ trong {SNAP_TTL:.0f}s)."
    return head + "\n" + "\n".join(out) + "\nDùng ref at<id> cho dl_click/dl_type (đời snapshot ngắn — snapshot lại trước khi click)."


def _resolve(ref):
    with _lock:
        e = _snap["map"].get(ref)
    return e.get("obj") if e else None


def _button_center(o):
    try:
        x, y, w, h = o.getExtents(_at().DESKTOP_COORDS)
        return int(x + w / 2), int(y + h / 2)
    except Exception:
        return None, None


def dl_click(ref="", name="", role="", a=None):
    at = _at()
    obj = None
    info = ""
    if ref:
        obj = _resolve(ref)
        info = f"ref {ref}"
    elif name or role:
        desk = _desktop()
        if desk is None:
            return "[LOI] thiếu AT-SPI"
        target = (name or "").lower()
        rrole = (role or "").lower()
        for appobj in desk:
            if _find_named(appobj, target, rrole) is not None:
                obj = _find_named(appobj, target, rrole)
                break
        info = f"name='{name}' role='{role}'"
    if obj is None:
        return f"[LOI] không tìm thấy {info}. Snapshot lại rồi thử ref at#id."

    # 1) AT-SPI action (không cần chuột thật)
    try:
        acts = obj.getAction().getActionName(0) if obj.getAction() else None
        if acts:
            obj.doAction(0)
            return f"OK: action '{acts}' đã thực thi trên {info}."
    except Exception:
        pass
    # 2) fallback xdotool giữa phần tử
    if _xd():
        cx, cy = _button_center(obj)
        if cx is not None:
            ok, msg = _run(["mousemove", str(cx), str(cy)])
            if ok:
                _run(["click", "1"])
                return f"OK: click chuột tại ({cx},{cy}) ({info}) qua xdotool."
    return f"[LOI] không có action AT-SPI và không có xdotool/bounds cho {info}."


def _find_named(o, name, role):
    if _obj_name(o).lower() == name and (not role or _obj_role(o).lower() == role):
        return o
    try:
        for ch in o:
            r = _find_named(ch, name, role)
            if r is not None:
                return r
    except Exception:
        pass
    return None


def dl_type(text, clear=False, a=None):
    """Gõ text vào phần tử đang focus (xdotool). clear=True thì xoá giá trị cũ trước."""
    if not _xd():
        return "[LOI] thiếu xdotool — cài: apt install xdotool"
    if clear:
        _run(["key", "ctrl+a"])
    ok, msg = _run(["type", "--clearmodifiers", "--delay", "12", str(text)])
    return f"OK: đã gõ {len(text)} ký tự." if ok else msg


def dl_key(combo, a=None):
    if not _xd():
        return "[LOI] thiếu xdotool — cài: apt install xdotool"
    ok, msg = _run(["key", str(combo)])
    return f"OK: đã nhấn {combo}." if ok else msg


def dl_mouse(x, y, action="click", drag_to=None, a=None):
    if not _xd():
        return "[LOI] thiếu xdotool"
    x, y = int(x), int(y)
    if action == "move":
        ok, msg = _run(["mousemove", str(x), str(y)])
        return f"OK: trỏ chuột ({x},{y})." if ok else msg
    ok, msg = _run(["mousemove", str(x), str(y)])
    if drag_to:
        d = [int(v.strip()) for v in str(drag_to).split(",")]
        if len(d) == 2:
            _run(["mousedown", "1"])
            _run(["mousemove", str(d[0]), str(d[1])])
            ok2, _ = _run(["mouseup", "1"])
            return "OK: kéo thả." if ok2 else "[LOI] kéo thả"
    btn = {"click": "1", "right": "3", "double": "--repeat 2 1"}.get(action, "1")
    ok, msg = _run(["click", *btn.split()])
    return f"OK: {action} tại ({x},{y})." if ok else msg


def dl_clipboard(copy="", a=None):
    """copy != '' → ghi nội dung vào clipboard; ngược lại đọc clipboard."""
    xclip = shutil.which("xclip") or shutil.which("xsel")
    if not xclip:
        return "[LOI] cần xclip hoặc xsel để dùng clipboard"
    if copy:
        try:
            rr = subprocess.run([xclip, "-selection", "c"] if "xclip" in xclip else [xclip, "-ib"],
                                input=str(copy), capture_output=True, text=True, timeout=10)
        except Exception as e:
            return f"[LOI] {type(e).__name__}: {e}"
        return f"OK: đã ghi {len(str(copy))} ký tự vào clipboard." if rr.returncode == 0 else "[LOI] ghi clipboard"
    try:
        rr = subprocess.run([xclip, "-selection", "c", "-o"] if "xclip" in xclip else [xclip, "-b"],
                            capture_output=True, text=True, timeout=10)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    return (rr.stdout or "(rỗng)").strip()[:1000] if rr.returncode == 0 else "[LOI] đọc clipboard"


TOOLS = [
    Tool("dl_status", "Kiểm tra môi trường điều khiển desktop (pyatspi/xdotool) còn thiếu gì.",
         schema({}), dl_status),
    Tool("dl_apps", "Liệt kê các ứng dụng desktop đang mở cây accessibility (AT-SPI).",
         schema({}), dl_apps),
    Tool("dl_tree", "Xuất cây giao diện (AT-SPI) của app — KHÔNG cần screenshot. Trả phần tử tương tác kèm ref at<id> để click/type. Trước khi tương tác luôn snapshot (gọi dl_tree) lại lần cuối.",
         schema({"app": {"type": "string", "description": "tên app cần lọc (rỗng = app đang focus)", "default": ""},
                 "max_depth": {"type": "integer", "default": 6},
                 "limit": {"type": "integer", "default": 300}}),
         dl_tree),
    Tool("dl_click", "Click phần tử theo ref at<id> (từ dl_tree) HOẶC theo name/role; ưu tiên action AT-SPI, fallback xdotool.",
         schema({"ref": {"type": "string", "default": ""},
                 "name": {"type": "string", "default": ""},
                 "role": {"type": "string", "default": "button"}}),
         dl_click),
    Tool("dl_type", "Gõ text vào phần tử đang focus (xdotool).",
         schema({"text": {"type": "string"},
                 "clear": {"type": "boolean", "default": False}}),
         dl_type),
    Tool("dl_key", "Nhấn tổ hợp phím, vd 'ctrl+s', 'Return', 'alt+Tab'.",
         schema({"combo": {"type": "string"}}),
         dl_key),
    Tool("dl_mouse", "Điều khiển chuột theo tọa độ: click/right/double/move/kéo-thả.",
         schema({"x": {"type": "integer"}, "y": {"type": "integer"},
                 "action": {"type": "string", "default": "click"},
                 "drag_to": {"type": "string", "default": ""}}),
         dl_mouse),
    Tool("dl_clipboard", "Đọc nội dung clipboard (cần xclip/xsel).",
         schema({"copy": {"type": "string", "default": ""}}),
         dl_clipboard),
]

if __name__ == "__main__":
    Server(TOOLS, "desktop_linux", "0.1.0").serve(sys.stdin, sys.stdout)