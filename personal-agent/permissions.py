import os

ALLOW = {
    "read_file", "list_dir", "grep", "glob_files", "cwd", "web_search", "recall",
}
ASK = {
    "bash", "write_file", "edit_file", "chdir", "web_fetch", "github_api",
}
DENY = set()


class PermPolicy:
    def __init__(self):
        self.auto = os.environ.get("REM_SAFE", "0") != "1"
        self.overrides = {}  # tool -> "allow"/"ask"/"deny"

    def set_auto(self, on):
        self.auto = on

    def policy(self, name):
        if name in self.overrides:
            return self.overrides[name]
        if name in DENY:
            return "deny"
        if name in ASK:
            return "ask"
        return "allow" if name in ALLOW else "ask"

    def decide(self, name, args=None, askfn=None):
        pol = self.policy(name)
        if pol == "deny":
            return False
        if pol == "allow" or self.auto:
            return True
        if askfn:
            return bool(askfn(name, args or {}))
        return False


class Presets:
    @staticmethod
    def plan():
        p = PermPolicy()
        for t in ("write_file", "edit_file", "bash", "chdir", "web_fetch"):
            p.overrides[t] = "deny"
        return p

    @staticmethod
    def build():
        return PermPolicy()