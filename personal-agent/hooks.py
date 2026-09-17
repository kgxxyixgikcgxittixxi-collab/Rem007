"""hooks.py — hook sự kiện kiểu Claude Code / crush (PreToolUse/PostToolUse).

Cấu hình trong ~/.rem_ai/hooks.json:
  {"pre_tool": ["~/.rem_ai/hooks/chan.sh", "python3 /abs/check.py"],
   "post_tool": ["notify-send Remtm 'xong tool'"]}

- pre_tool: nhận JSON {event, tool, args, sid} qua stdin, timeout 10s.
  exit != 0 → CHẶN tool, stderr (300 ký tự đầu) làm lý do.
  exit 0 + stdout bắt đầu bằng "DENY:" → chặn với lý do sau đó.
  exit 0 + stdout khác rỗng → cho qua, stdout thành ghi chú [HOOK] kèm kết quả.
- post_tool: nhận {event, tool, args, result_ok}, bỏ qua output, không bao giờ lỗi.
Mọi hàm best-effort: không có file/thiếu lệnh/timeout đều cho qua, KHÔNG crash agent.
"""
import json
import os
import subprocess

import config

HOOKS_FILE = os.environ.get("REM_HOOKS_FILE", "") or os.path.join(config.DIR, "hooks.json")
_TIMEOUT = 10


def _load():
    try:
        with open(HOOKS_FILE, "r", encoding="utf-8") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cmds(key):
    try:
        items = _load().get(key) or []
        return [c for c in items if isinstance(c, str) and c.strip()]
    except Exception:
        return []


def _run_one(cmd, payload):
    """Chạy 1 lệnh hook. Trả (rc, out, err) đã cắt gọn. Timeout → (None, '', 'timeout')."""
    try:
        import shlex
        parts = shlex.split(cmd, posix=True)
        if not parts:
            return 0, "", ""
        p = subprocess.run(parts, input=json.dumps(payload, ensure_ascii=False),
                           capture_output=True, text=True, timeout=_TIMEOUT)
        return p.returncode, (p.stdout or "")[:2000], (p.stderr or "")[:500]
    except subprocess.TimeoutExpired:
        return None, "", "timeout"
    except FileNotFoundError:
        return 0, "", ""
    except Exception:
        return 0, "", ""


def run_pre(tool, args, sid=""):
    """Trả (allowed: bool, note: str). allowed=False → chặn tool với note làm lý do."""
    payload = {"event": "pre_tool", "tool": tool, "args": args or {}, "sid": sid}
    notes = []
    for cmd in _cmds("pre_tool"):
        rc, out, err = _run_one(cmd, payload)
        if rc is None:
            continue  # timeout → bỏ qua hook này, không chặn oan
        out_s = (out or "").strip()
        if rc != 0:
            reason = (err or out_s or f"hook '{cmd}' exit {rc}").strip()[:300]
            return False, reason
        if out_s.startswith("DENY:"):
            return False, out_s[5:].strip()[:300] or "bị hook chặn"
        if out_s:
            notes.append(out_s[:300])
    return True, ("; ".join(notes))[:600]


def run_post(tool, args, result_ok=True, sid=""):
    payload = {"event": "post_tool", "tool": tool, "args": args or {},
               "result_ok": bool(result_ok), "sid": sid}
    for cmd in _cmds("post_tool"):
        try:
            _run_one(cmd, payload)
        except Exception:
            pass
