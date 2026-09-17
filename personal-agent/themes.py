"""themes.py — hệ theme kiểu opencode cho Remtm (giữ logo REM).

- Đọc tui.json (~/.rem_ai/tui.json, tương thích opencode tui.json subset):
    {"theme": "opencode"|"tokyonight"|..., "diff_style": "auto"|"stacked"|"side",
     "attention": {"enabled": bool}, "mouse": bool}
- Theme dirs (ưu tiên sau thắng trước, giống opencode):
    builtin < ~/.rem_ai/themes/*.json < ./.opencode/themes/*.json
- Mỗi theme JSON: {"theme": {"primary": "#...", "success": ..., ...}} hoặc flat.
- apply(theme_name) -> dict màu ANSI cho repl.C (chỉ đổi UI, KHÔNG đổi logo REM).
"""
import json
import os

TUI_FILE = os.path.join(os.path.expanduser("~"), ".rem_ai", "tui.json")
THEME_DIRS = [
    os.path.join(os.path.expanduser("~"), ".rem_ai", "themes"),
    os.path.join(os.getcwd(), ".opencode", "themes"),
]

# builtin tối giản (hex) — đủ phủ UI opencode, logo REM giữ nguyên trong config.LOGO
BUILTIN = {
    "opencode": {"primary": "#7dd3fc", "secondary": "#a5b4fc", "accent": "#f0abfc",
                 "success": "#86efac", "warning": "#fcd34d", "error": "#fca5a5",
                 "info": "#7dd3fc", "textMuted": "#6b7280", "border": "#374151"},
    "tokyonight": {"primary": "#7aa2f7", "secondary": "#bb9af7", "accent": "#ff9e64",
                   "success": "#9ece6a", "warning": "#e0af68", "error": "#f7768e",
                   "info": "#7dcfff", "textMuted": "#565f89", "border": "#414868"},
    "catppuccin": {"primary": "#89b4fa", "secondary": "#cba6f7", "accent": "#f5c2e7",
                   "success": "#a6e3a1", "warning": "#f9e2af", "error": "#f38ba8",
                   "info": "#89dceb", "textMuted": "#6c7086", "border": "#45475a"},
    "gruvbox": {"primary": "#83a598", "secondary": "#d3869b", "accent": "#fabd2f",
                "success": "#b8bb26", "warning": "#fabd2f", "error": "#fb4934",
                "info": "#83a598", "textMuted": "#928374", "border": "#504945"},
    "nord": {"primary": "#88c0d0", "secondary": "#81a1c1", "accent": "#8fbcbb",
             "success": "#a3be8c", "warning": "#ebcb8b", "error": "#bf616a",
             "info": "#88c0d0", "textMuted": "#4c566a", "border": "#434c5e"},
    "matrix": {"primary": "#00ff41", "secondary": "#00ff41", "accent": "#00ff41",
               "success": "#00ff41", "warning": "#ccff00", "error": "#ff003c",
               "info": "#00ff41", "textMuted": "#008f11", "border": "#003b00"},
    "one-dark": {"primary": "#61afef", "secondary": "#c678dd", "accent": "#56b6c2",
                 "success": "#98c379", "warning": "#e5c07b", "error": "#e06c75",
                 "info": "#61afef", "textMuted": "#5c6370", "border": "#3e4451"},
}


def _hex_to_ansi(h, fallback="96"):
    try:
        h = str(h).strip().lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        return f"\033[38;2;{r};{g};{b}m"
    except Exception:
        return f"\033[{fallback}m"


def tui_file():
    """Đường dẫn tui.json: OPENCODE_TUI_CONFIG (kiểu opencode) > ~/.rem_ai/tui.json."""
    try:
        custom = (os.environ.get("OPENCODE_TUI_CONFIG") or "").strip()
        if custom:
            return os.path.expanduser(custom)
    except Exception:
        pass
    return TUI_FILE


def load_tui():
    try:
        with open(tui_file(), "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_tui(patch):
    try:
        d = load_tui()
        d.update(patch or {})
        fp = tui_file()
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        tmp = fp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)
        return True
    except Exception:
        return False


def list_themes():
    names = sorted(BUILTIN.keys())
    for td in THEME_DIRS:
        try:
            for fn in sorted(os.listdir(td)):
                if fn.endswith(".json"):
                    n = fn[:-5]
                    if n not in names:
                        names.append(n)
        except Exception:
            pass
    return names


def _load_theme_def(name):
    for td in THEME_DIRS:
        fp = os.path.join(td, (name or "") + ".json")
        try:
            with open(fp, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
            th = d.get("theme") if isinstance(d.get("theme"), dict) else d
            if isinstance(th, dict) and th:
                return th
        except Exception:
            pass
    return BUILTIN.get(name or "", BUILTIN["opencode"])


def current_theme():
    return str(load_tui().get("theme") or "opencode")


def diff_style():
    return str(load_tui().get("diff_style") or "auto")


def attention_enabled():
    try:
        return bool((load_tui().get("attention") or {}).get("enabled", False))
    except Exception:
        return False


def apply(name=""):
    """Trả về dict patch cho repl.C (ANSI). Không đụng tới logo."""
    name = (name or current_theme() or "opencode").strip() or "opencode"
    th = _load_theme_def(name)
    if not isinstance(th, dict):
        th = BUILTIN["opencode"]
    get = lambda k, fb: th.get(k, fb)

    def ansi(v, fb):
        s = str(v or "").strip()
        if not s or s == "none":
            return f"\033[{fb}m"
        if s.startswith("#"):
            return _hex_to_ansi(s, fb)
        # tên màu opencode -> ansi gần đúng
        named = {"primary": "96", "secondary": "94", "accent": "95",
                 "success": "92", "warning": "93", "error": "91", "info": "96"}
        if s in named:
            return f"\033[{named[s]}m"
        return _hex_to_ansi(s, fb) if len(s) in (3, 6) else f"\033[{fb}m"

    return {
        "cy": ansi(get("primary", "#7dd3fc"), "96"),
        "bl": ansi(get("secondary", "#a5b4fc"), "94"),
        "mg": ansi(get("accent", "#f0abfc"), "95"),
        "gr": ansi(get("success", "#86efac"), "92"),
        "ye": ansi(get("warning", "#fcd34d"), "93"),
        "rd": ansi(get("error", "#fca5a5"), "91"),
        "dim": "\033[2m",
        "bold": "\033[1m",
        "reset": "\033[0m",
    }
