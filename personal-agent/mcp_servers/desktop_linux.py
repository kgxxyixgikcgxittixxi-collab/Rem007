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

import threading, shutil, subprocess, time, json, re, inspect, functools

from mcplib import Server, Tool, schema
from config import DIR

_lock = threading.Lock()

# roles được phép nhận ref để tương tác (giống tập role interactive của agent-desktop)
_INTERACTIVE = {
    "button", "text", "textbox", "push button", "entry", "checkbox", "check box",
    "link", "menu item", "tab", "slider", "combo box", "combobox", "tree item",
    "cell", "radio button", "radiobutton", "switch", "incrementor", "password text",
}

_snap = {"apps": [], "map": {}, "time": 0.0}
SNAP_TTL = 600.0

# Trạng thái ghi macro (định nghĩa sớm để decorator dùng được cho dl_* bên dưới)
_REC = {"active": False, "playing": False, "name": "", "category": "khac",
        "actions": [], "t0": 0.0}
_REC_MAX = 200


def _rec_hook(tool, args):
    """Móc ghi: decorator @_recorded gọi hàm này trước khi chạy action."""
    if not _REC["active"] or _REC["playing"]:
        return
    try:
        if len(_REC["actions"]) >= _REC_MAX:
            return
        a = dict(args or {})
        a.pop("a", None)
        # ref at<N> chết theo phiên → đổi sang name/role bền vững nếu tra được
        if a.get("ref") and not (a.get("name") or a.get("role")):
            info = (_snap.get("map") or {}).get(a["ref"]) or {}
            if info.get("name") or info.get("role"):
                a = {"name": info.get("name", ""), "role": info.get("role", "")}
        # cắt text dài (tránh macro phình + dính dữ liệu nhạy cảm dài)
        for k in ("text", "copy"):
            if isinstance(a.get(k), str) and len(a[k]) > 1000:
                a[k] = a[k][:1000]
        _REC["actions"].append({"tool": tool, "args": a, "ok": None,
                               "dt": round(time.time() - _REC["t0"], 1)})
    except Exception:
        pass


def _rec_mark(ok):
    """Đánh dấu bước vừa ghi là thành công/thất bại (decorator gọi sau khi chạy)."""
    try:
        if _REC["active"] and not _REC["playing"] and _REC["actions"]:
            _REC["actions"][-1]["ok"] = bool(ok)
    except Exception:
        pass


def _recorded(tool):
    """Decorator cho dl_*: ghi action + tự đánh dấu thành công/thất bại theo kết quả."""
    def deco(fn):
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrap(*args, **kwargs):
            named = {}
            try:
                bound = sig.bind_partial(*args, **kwargs)
                named = dict(bound.arguments)
            except Exception:
                named = dict(kwargs)
            _rec_hook(tool, named)
            out = fn(*args, **kwargs)
            _rec_mark(not (isinstance(out, str) and out.startswith(("[LOI]", "[TU CHOI]"))))
            return out
        return wrap
    return deco


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


@_recorded("dl_click")
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


@_recorded("dl_type")
def dl_type(text, clear=False, a=None):
    """Gõ text vào phần tử đang focus (xdotool). clear=True thì xoá giá trị cũ trước."""
    if not _xd():
        return "[LOI] thiếu xdotool — cài: apt install xdotool"
    if clear:
        _run(["key", "ctrl+a"])
    ok, msg = _run(["type", "--clearmodifiers", "--delay", "12", str(text)])
    return f"OK: đã gõ {len(text)} ký tự." if ok else msg


@_recorded("dl_key")
def dl_key(combo, a=None):
    if not _xd():
        return "[LOI] thiếu xdotool — cài: apt install xdotool"
    ok, msg = _run(["key", str(combo)])
    return f"OK: đã nhấn {combo}." if ok else msg


@_recorded("dl_mouse")
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


@_recorded("dl_clipboard")
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


def _obj_state(o):
    """Lấy trạng thái phần tử: visible, focused, enabled, editable."""
    at = _at()
    if not at:
        return {}
    state = {}
    try:
        ss = o.getState()
        state["visible"] = ss.contains(at.StateType.VISIBLE)
        state["focused"] = ss.contains(at.StateType.FOCUSED)
        state["enabled"] = ss.contains(at.StateType.ENABLED)
        state["editable"] = ss.contains(at.StateType.EDITABLE)
        state["selected"] = ss.contains(at.StateType.SELECTED)
    except Exception:
        pass
    return state


def _obj_bounds(o):
    """Lấy tọa độ + kích thước phần tử."""
    at = _at()
    if not at:
        return None
    try:
        x, y, w, h = o.getExtents(at.DESKTOP_COORDS)
        return {"x": int(x), "y": int(y), "w": int(w), "h": int(h)}
    except Exception:
        return None


def dl_find(query, app="", a=None):
    """Tìm phần tử theo ngôn ngữ tự nhiên: 'nút save', 'ô tìm kiếm', 'menu File'.
    Trả về danh sách ref at<id> kèm tên, role, trạng thái."""
    desk = _desktop()
    if desk is None:
        return "[LOI] không truy cập được AT-SPI"
    with _lock:
        _snap["map"] = {}
    q = query.lower().strip()
    results = []

    def _search(o, depth):
        if len(results) >= 20 or depth > 10:
            return
        role = _obj_role(o)
        name = _obj_name(o)
        role_lower = role.lower()
        name_lower = name.lower()
        matched = False
        # Match theo tên
        if q in name_lower:
            matched = True
        # Match theo role (hỗ trợ tên tiếng Việt)
        role_aliases = {
            "nút": ["button", "push button", "toggle button"],
            "button": ["button", "push button"],
            "textbox": ["text", "textbox", "entry"],
            "ô nhập": ["text", "textbox", "entry"],
            "ô nhập liệu": ["text", "textbox", "entry"],
            "menu": ["menu", "menu item", "menu bar"],
            "tab": ["tab", "page tab"],
            "checkbox": ["checkbox", "check box"],
            "ô ticks": ["checkbox", "check box"],
            "link": ["link"],
            "combo": ["combo box", "combobox"],
            "dropdown": ["combo box", "combobox"],
        }
        for alias, roles in role_aliases.items():
            if alias in q and role_lower in roles:
                matched = True
                break
        if matched:
            n = len(results)
            ref = f"at{n}"
            state = _obj_state(o)
            bounds = _obj_bounds(o)
            state_str = ", ".join(f"{k}={v}" for k, v in state.items() if v is not None)
            bounds_str = f"at ({bounds['x']},{bounds['y']}) {bounds['w']}x{bounds['h']}" if bounds else ""
            _snap["map"][ref] = {"obj": o, "role": role, "name": name}
            results.append(f"[{ref}] {role} '{name}' — {state_str} {bounds_str}")
        try:
            for ch in o:
                _search(ch, depth + 1)
        except Exception:
            pass

    try:
        for appobj in desk:
            if app and _obj_name(appobj).lower() != app.lower():
                continue
            _search(appobj, 0)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    if not results:
        return f"Không tìm thấy phần tử nào khớp '{query}'. Thử dl_tree để xem toàn bộ."
    header = f"Tìm thấy {len(results)} phần tử cho '{query}':"
    return header + "\n" + "\n".join(results) + "\nDùng ref at<id> cho dl_click."


def dl_text(app="", a=None):
    """Đọc toàn bộ text hiển thị trên desktop / 1 app (thay cho screenshot OCR)."""
    desk = _desktop()
    if desk is None:
        return "[LOI] không truy cập được AT-SPI"
    texts = []

    def _collect(o, depth):
        if len(texts) > 500:
            return
        role = _obj_role(o)
        name = _obj_name(o)
        if name and role.lower() not in ("application", "frame", "filler", "separator", "panel"):
            texts.append(name)
        try:
            # Thử đọc text nội dung (document, terminal, editor)
            iface = o.queryInterface("Text")
            if iface:
                t = iface.getText(0, min(iface.getCharacterCount(), 2000))
                if t and t.strip() and t.strip() != name:
                    texts.append(f"  [{role}] {t.strip()[:500]}")
        except Exception:
            pass
        try:
            for ch in o:
                _collect(ch, depth + 1)
        except Exception:
            pass

    try:
        for appobj in desk:
            if app and _obj_name(appobj).lower() != app.lower():
                continue
            _collect(appobj, 0)
    except Exception as e:
        return f"[LOI] {type(e).__name__}: {e}"
    if not texts:
        return "(không có text nào)"
    return "\n".join(texts[:300])


# ── Macro recorder: GHI thao tác desktop rồi PHÁT LẠI ──────────────────────
# Cách dùng: rec_start(name, category) → làm việc bằng dl_* như bình thường →
# rec_stop() lưu. Lần sau: rec_play(name) là AI làm lại toàn bộ.
# Macro phân mục rõ theo công việc (giống nhóm module của agent).

MACRO_DIR = os.path.join(DIR, "macros")
os.makedirs(MACRO_DIR, exist_ok=True)

MACRO_CATS = {
    "van-phong": "soạn thảo văn bản, bảng tính, tài liệu",
    "trinh-duyet": "lướt web, tra cứu, xem video",
    "he-thong": "cài đặt hệ thống, quản lý file, terminal",
    "giai-tri": "nhạc, phim, game",
    "mang-xa-hoi": "đăng bài, nhắn tin, bình luận",
    "khac": "việc khác không thuộc nhóm trên",
}

# (trạng thái _REC/_REC_MAX đã định nghĩa ở đầu file để decorator dùng)
def _macro_slug(name):
    s = "".join(c if (c.isalnum() or c in "_-") else "_" for c in (name or "").strip().lower())
    return re.sub(r"_+", "_", s).strip("_")[:60] or "macro"


def _macro_file(name):
    return os.path.join(MACRO_DIR, _macro_slug(name) + ".json")


# (_rec_hook/_rec_mark/_recorded đã định nghĩa ở đầu file)
def rec_start(name="", category="khac", a=None):
    cat = (category or "khac").strip().lower().replace(" ", "-")
    if cat not in MACRO_CATS:
        return f"[LOI] mục không hợp lệ. Chọn 1 trong: {', '.join(MACRO_CATS)}"
    if _REC["active"]:
        return f"[LOI] đang ghi macro '{_REC['name']}' — gọi rec_stop trước đã"
    nm = _macro_slug(name)
    _REC.update(active=True, playing=False, name=nm, category=cat, actions=[],
                t0=time.time())
    return (f"ĐANG GHI macro '{nm}' (mục {cat}: {MACRO_CATS[cat]}). "
            f"Giờ thực hiện thao tác bằng dl_click/dl_type/dl_key/dl_mouse như bình thường, "
            f"xong gọi rec_stop để lưu. LƯU Ý: đừng gõ mật khẩu khi đang ghi.")


def rec_stop(a=None):
    if not _REC["active"]:
        return "[LOI] không có macro nào đang ghi (gọi rec_start trước)"
    raw = [x for x in _REC["actions"]
           if not (x["tool"] == "dl_clipboard" and not x["args"].get("copy"))]
    dropped = sum(1 for x in raw if x.get("ok") is False)
    acts = []
    for x in raw:
        if x.get("ok") is False:
            continue
        x = dict(x)
        x.pop("ok", None)
        acts.append(x)
    if not acts and dropped:
        _REC.update(active=False, playing=False, name="", actions=[])
        return (f"[LOI] macro '{data['name']}' rỗng — cả {dropped} bước ghi đều lỗi nên đã tự bỏ hết, "
                f"KHÔNG lưu macro rỗng. Hãy rec_start lại và kiểm tra từng bước (dl_status trước).")
    data = {"name": _REC["name"], "category": _REC["category"], "actions": acts,
            "created": time.strftime("%Y-%m-%d %H:%M"), "count": len(acts)}
    try:
        with open(_macro_file(_REC["name"]), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception as e:
        _REC.update(active=False, actions=[])
        return f"[LOI] không lưu được macro: {e}"
    msg = (f"Đã lưu macro '{data['name']}' (mục {data['category']}: "
           f"{MACRO_CATS[data['category']]}) — {len(acts)} bước.")
    if dropped:
        msg += f" (đã tự bỏ {dropped} bước lỗi lúc ghi)."
    msg += f" Lần sau chỉ cần rec_play(name='{data['name']}') là AI làm lại."
    _REC.update(active=False, playing=False, name="", actions=[])
    return msg


def rec_list(category="", a=None):
    cat = (category or "").strip().lower()
    groups = {}
    try:
        for fn in sorted(os.listdir(MACRO_DIR)):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(MACRO_DIR, fn), encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                continue
            c = d.get("category", "khac")
            if cat and c != cat:
                continue
            groups.setdefault(c, []).append(d)
    except Exception:
        pass
    if not groups:
        return "(chưa có macro nào — ghi bằng rec_start, làm việc, rồi rec_stop)"
    out = []
    for c in MACRO_CATS:
        if c not in groups:
            continue
        out.append(f"## {c} — {MACRO_CATS[c]}")
        for d in groups[c]:
            out.append(f"- {d.get('name')}: {d.get('count', 0)} bước (lưu {d.get('created', '?')})")
    return "\n".join(out)


def rec_show(name="", a=None):
    try:
        with open(_macro_file(name), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return f"[LOI] không có macro '{name}' (xem rec_list)"
    lines = [f"# Macro '{d.get('name')}' — mục {d.get('category')} ({d.get('count', 0)} bước)"]
    for i, st in enumerate(d.get("actions", []), 1):
        lines.append(f"{i}. {st.get('tool')}({str(st.get('args', {}))[:160]}) sau {st.get('dt', 0)}s")
    return "\n".join(lines)


def rec_delete(name="", a=None):
    try:
        os.remove(_macro_file(name))
        return f"Đã xoá macro '{name}'."
    except Exception:
        return f"[LOI] không có macro '{name}' để xoá"


_REC_FN = None  # gán sau khi định nghĩa xong (tránh forward-reference)


def rec_play(name="", speed=1.0, a=None):
    """Phát lại macro: AI tự làm lại toàn bộ thao tác đã ghi."""
    if _REC["active"]:
        return "[LOI] đang ghi macro khác — rec_stop trước rồi mới phát"
    try:
        with open(_macro_file(name), encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return f"[LOI] không có macro '{name}' (xem rec_list)"
    acts = d.get("actions", [])
    if not acts:
        return f"[LOI] macro '{name}' rỗng, không có gì để phát"
    try:
        sp = max(0.25, min(float(speed or 1.0), 4.0))
    except Exception:
        sp = 1.0
    _REC["playing"] = True
    ok, fail = 0, []
    try:
        for i, st in enumerate(acts):
            fn = (_REC_FN or {}).get(st.get("tool"))
            if not fn:
                fail.append(f"b{i + 1}: tool lạ {st.get('tool')}")
                continue
            if i:
                time.sleep(max(0.2, min(float(st.get("dt", 0.5)) / sp, 5.0)))
            try:
                r = fn(**(st.get("args") or {}))
                if isinstance(r, str) and (r.startswith("[LOI]") or r.startswith("[TU CHOI]")):
                    fail.append(f"b{i + 1}: {r[:100]}")
                else:
                    ok += 1
            except Exception as e:
                fail.append(f"b{i + 1}: {type(e).__name__} {str(e)[:80]}")
    finally:
        _REC["playing"] = False
    rep = f"Phát macro '{d.get('name')}': {ok}/{len(acts)} bước OK."
    if fail:
        rep += " Lỗi:\n" + "\n".join(f"- {x}" for x in fail[:8])
    return rep


_REC_FN = {"dl_click": dl_click, "dl_type": dl_type, "dl_key": dl_key,
           "dl_mouse": dl_mouse, "dl_clipboard": dl_clipboard}


TOOLS = [
    Tool("dl_status", "KIỂM TRA desktop: xem pyatspi/xdotool có sẵn không. LUÔN gọi đầu tiên.",
         schema({}), dl_status),
    Tool("dl_apps", "LIỆT KÊ app đang mở trên desktop (thay cho screenshot — đọc AT-SPI tree).",
         schema({}), dl_apps),
    Tool("dl_tree", "NHÌN desktop: xuất toàn bộ giao diện dạng text (KHÔNG cần screenshot/OCR). "
         "Trả cây AT-SPI với ref at<id> để click/type. Đây là CÔNG CỤ CHÍNH để xem nội dung màn hình.",
         schema({"app": {"type": "string", "description": "tên app cần lọc (rỗng = app đang focus)", "default": ""},
                 "max_depth": {"type": "integer", "default": 6},
                 "limit": {"type": "integer", "default": 300}}),
         dl_tree),
    Tool("dl_find", "TÌM PHẦN TỬ theo ngôn ngữ tự nhiên: 'nút save', 'ô tìm kiếm', 'menu File'. "
         "Trả ref at<id> kèm tên, role, trạng thái. Không cần biết chính xác tên app.",
         schema({"query": {"type": "string", "description": "mô tả cần tìm (tiếng Việt hoặc Anh)"},
                 "app": {"type": "string", "description": "lọc theo app (rỗng = tìm tất cả)", "default": ""}}),
         dl_find),
    Tool("dl_text", "ĐỌC TEXT hiển thị trên desktop / 1 app (thay cho screenshot OCR). "
         "Trả toàn bộ text đang thấy trên màn hình.",
         schema({"app": {"type": "string", "description": "lọc theo app (rỗng = tất cả)", "default": ""}}),
         dl_text),
    Tool("dl_click", "CLICK phần tử trên desktop: theo ref at<id> (từ dl_tree/dl_find) HOẶC theo name/role.",
         schema({"ref": {"type": "string", "default": ""},
                 "name": {"type": "string", "default": ""},
                 "role": {"type": "string", "default": "button"}}),
         dl_click),
    Tool("dl_type", "GÕ TEXT vào phần tử đang focus trên desktop.",
         schema({"text": {"type": "string"},
                 "clear": {"type": "boolean", "default": False}}),
         dl_type),
    Tool("dl_key", "NHẤN PHÍM: tổ hợp phím như 'ctrl+c', 'Return', 'alt+Tab'.",
         schema({"combo": {"type": "string"}}),
         dl_key),
    Tool("dl_mouse", "ĐIỀU KHIỂN CHUỘT theo tọa độ: click/right/double/move/drag.",
         schema({"x": {"type": "integer"}, "y": {"type": "integer"},
                 "action": {"type": "string", "default": "click"},
                 "drag_to": {"type": "string", "default": ""}}),
         dl_mouse),
    Tool("dl_clipboard", "ĐỌC/GHI clipboard (cần xclip/xsel).",
         schema({"copy": {"type": "string", "default": ""}}),
         dl_clipboard),
    Tool("rec_start", "BẮT ĐẦU GHI macro: mọi thao tác dl_* sau đó được ghi lại theo mục công việc. "
         "Xong gọi rec_stop để lưu. Lần sau rec_play là AI làm lại.",
         schema({"name": {"type": "string", "description": "tên macro (vd mo_nhac)"},
                 "category": {"type": "string",
                              "description": "mục: van-phong|trinh-duyet|he-thong|giai-tri|mang-xa-hoi|khac",
                              "default": "khac"}}),
         rec_start),
    Tool("rec_stop", "DỪNG GHI và lưu macro vừa ghi (phân mục theo công việc).",
         schema({}), rec_stop),
    Tool("rec_list", "LIỆT KÊ macro đã lưu, PHÂN MỤC rõ theo công việc.",
         schema({"category": {"type": "string", "description": "lọc theo mục (rỗng = tất cả)", "default": ""}}),
         rec_list),
    Tool("rec_show", "XEM CHI TIẾT từng bước của 1 macro.",
         schema({"name": {"type": "string"}}),
         rec_show),
    Tool("rec_play", "PHÁT LẠI macro: AI tự làm lại toàn bộ thao tác đã ghi.",
         schema({"name": {"type": "string"},
                 "speed": {"type": "number", "description": "tốc độ phát (0.25-4, mặc định 1)", "default": 1.0}}),
         rec_play),
    Tool("rec_delete", "XOÁ 1 macro đã lưu.",
         schema({"name": {"type": "string"}}),
         rec_delete),
]

if __name__ == "__main__":
    Server(TOOLS, "desktop_linux", "0.1.0").serve(sys.stdin, sys.stdout)