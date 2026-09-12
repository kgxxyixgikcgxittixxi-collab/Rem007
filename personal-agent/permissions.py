import os, json, fnmatch

ALLOW = {
    "read_file", "list_dir", "grep", "glob_files", "cwd", "web_search",
    "recall", "remember", "todo_list", "todo_write",
    "lsp_supported", "lsp_diagnostics", "lsp_definition", "lsp_references",
    "lsp_symbols", "lsp_hover",
    # chỉ-đọc / xem (không đổi trạng thái) → cho qua luôn
    "skill_find", "skill_use", "skill_list",
    "exp_lesson_find", "exp_error_patterns", "exp_skill_best",
    "dl_status", "dl_apps", "dl_tree", "dl_find", "dl_text",
    "rec_list", "rec_show",
    "browser_status", "browser_content",
    "media_status", "media_info",
    "social_report", "social_status",
}
ASK = {
    "bash", "bash_poll", "write_file", "edit_file", "apply_patch", "chdir",
    "web_fetch", "github_api", "ensure_tool", "pip_install", "task",
    # ghi nhớ / học (đổi trạng thái) → hỏi ở safe mode
    "skill_save", "exp_lesson_save", "exp_skill_save",
    # điều khiển desktop
    "dl_click", "dl_type", "dl_key", "dl_mouse", "dl_clipboard",
    "rec_start", "rec_stop", "rec_play", "rec_delete",
    # trình duyệt
    "browser_open", "browser_navigate", "browser_click", "browser_click_text",
    "browser_type", "browser_press", "browser_screenshot", "browser_eval",
    "browser_wait", "browser_scroll", "browser_search", "browser_back",
    "browser_close",
    # media: tốn tài nguyên / ghi file
    "media_tts", "media_image", "media_scene", "media_slideshow",
    "media_concat", "media_trim", "media_scale", "media_to_gif",
    "media_overlay_text", "media_extract_audio",
    # mạng xã hội: đăng/tương tác
    "social_cycle", "social_post",
}
DENY = set()

# ruleset: wildcard pattern matching — vd "bash:rm -rf *" sẽ bị deny
# Format: (pattern, action) — pattern = "tool" hoặc "tool:arg_pattern"
_RULES_FILE = os.path.join(os.path.expanduser("~"), ".rem_ai", "permissions.json")


def _load_rules():
    try:
        with open(_RULES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_rules(rules):
    try:
        os.makedirs(os.path.dirname(_RULES_FILE), exist_ok=True)
        with open(_RULES_FILE, "w", encoding="utf-8") as f:
            json.dump(rules, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _match_pattern(pattern, tool_name, args):
    """Wildcard matching cho permission rules.
    Pattern: "tool" hoặc "tool:arg_value" (supports * wildcards)."""
    parts = pattern.split(":", 1)
    if len(parts) == 1:
        return fnmatch.fnmatch(tool_name, parts[0])
    tool_pat, arg_pat = parts
    if not fnmatch.fnmatch(tool_name, tool_pat):
        return False
    # match against first string arg
    for v in args.values():
        if isinstance(v, str) and fnmatch.fnmatch(v, arg_pat):
            return True
    return False


class PermPolicy:
    def __init__(self):
        self.auto = os.environ.get("REM_SAFE", "0") != "1"
        # FULL-AUTO do user cấp: file ~/.rem_ai/full_auto chứa "1" → luôn auto,
        # kể cả khi REM_SAFE=1. Mặc định từ nay: tự động chạy, không hỏi.
        try:
            with open(os.path.join(os.path.expanduser("~"), ".rem_ai", "full_auto"), encoding="utf-8") as f:
                if f.read().strip() == "1":
                    self.auto = True
        except Exception:
            pass
        self.overrides = {}  # tool -> "allow"/"ask"/"deny"
        self.session_approved = set()  # per-session approval memory
        self.rules = _load_rules()  # config ruleset

    def set_auto(self, on):
        self.auto = on

    def approve_session(self, tool_name, args=None):
        """Duyệt tool cho session hiện tại (không hỏi lại)."""
        key = tool_name
        if args:
            for v in args.values():
                if isinstance(v, str) and len(v) < 100:
                    key = f"{tool_name}:{v}"
                    break
        self.session_approved.add(key)

    def policy(self, name, args=None):
        # 1) config ruleset (wildcard)
        for rule in self.rules:
            pat = rule.get("pattern", "")
            action = rule.get("action", "")
            if pat and action and _match_pattern(pat, name, args or {}):
                return action
        # 2) session memory — đã approve rồi thì allow
        if name in self.session_approved:
            return "allow"
        for approved in self.session_approved:
            if ":" in approved:
                tool_pat, arg_pat = approved.split(":", 1)
                if fnmatch.fnmatch(name, tool_pat):
                    return "allow"
        # 3) explicit overrides
        if name in self.overrides:
            return self.overrides[name]
        if name in DENY:
            return "deny"
        if name in ASK:
            return "ask"
        return "allow" if name in ALLOW else "ask"

    def decide(self, name, args=None, askfn=None):
        pol = self.policy(name, args)
        if pol == "deny":
            return False
        if pol == "allow" or self.auto:
            # auto mode + ask → auto-approve và ghi nhớ
            if pol == "ask":
                self.approve_session(name, args)
            return True
        if askfn:
            ok = bool(askfn(name, args or {}))
            if ok:
                self.approve_session(name, args)
            return ok
        return False


class Presets:
    @staticmethod
    def plan():
        p = PermPolicy()
        for t in ("write_file", "edit_file", "apply_patch", "bash", "chdir", "web_fetch"):
            p.overrides[t] = "deny"
        return p

    @staticmethod
    def build():
        return PermPolicy()
