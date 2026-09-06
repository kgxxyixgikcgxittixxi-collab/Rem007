import json, time

import config
import sessions
from providers import groq

MAX_STEPS = 20


def _sys(manager, sid, cwd):
    tools = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in manager.schemas()
    )
    return {
        "role": "system",
        "content": (
            f"Bạn là {config.NAME} ({config.VERSION}) — agent Python chạy 100% trên Groq, "
            "theo kiến trúc MCP của goose/opencode: mọi thao tác qua công cụ MCP "
            "(mỗi công cụ chạy trong tiến trình riêng biệt).\n\n"
            f"Hôm nay: {time.strftime('%Y-%m-%d %H:%M')}\n"
            f"Thư mục làm việc: {cwd}\nSession: {sid}\n\n"
            "CÁC CÔNG CỤ CÓ SẵN:\n"
            f"{tools}\n\n"
            "LUẬT:\n"
            "- Dùng tool khi cần thực tác thật (đọc file, chạy lệnh, tìm web...). KHÔNG bịa kết quả.\n"
            "- Nếu tool báo lỗi: đề xuất cách sửa hoặc thử lại.\n"
            "- Permission 'ask' sẽ hỏi người dùng — hãy giải thích ngắn lý do rồi gọi lại khi được chấp thuận.\n"
            "- Khi đã đủ thông tin và xong việc: dừng gọi tool, trả lời kết quả tiếng Việt, ngắn gọn, thực tế.\n"
            "- Không dùng emoji."
        ),
    }


class Agent:
    def __init__(self, manager, perm, sid=None):
        self.manager = manager
        self.perm = perm
        self.sid = sid or sessions.new()
        self.askfn = None

    def _cwd(self):
        try:
            return self.manager.call("cwd", {}, 10).strip()
        except Exception:
            return "?"

    def run(self, user_text):
        sessions.append(self.sid, {"role": "user", "content": user_text})
        msgs = [_sys(self.manager, self.sid, self._cwd()), *sessions.load(self.sid)]
        for _ in range(MAX_STEPS):
            msgs = sessions.compact(self.sid, msgs)
            reply = groq.chat(msgs, tools=self.manager.schemas() or None)
            if not reply:
                return "[LOI] Groq không phản hồi (có thể hết keys/quota). Dùng /keys để kiểm tra."
            tool_calls = reply.get("tool_calls") or []
            if not tool_calls:
                content = reply.get("content") or "(rỗng)"
                sessions.append(self.sid, {"role": "assistant", "content": content})
                return content
            sessions.append(
                self.sid,
                {
                    "role": "assistant",
                    "content": reply.get("content") or None,
                    "tool_calls": [
                        {
                            "id": tc.get("id"),
                            "type": "function",
                            "function": {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments") or "{}"},
                        }
                        for tc in tool_calls
                    ],
                },
            )
            for tc in tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "?")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                if not self.perm.decide(name, args, askfn=self.askfn):
                    result = f"[TU CHOI] Tool {name} bị chặn bởi permission. Hãy giải thích với người dùng."
                else:
                    try:
                        result = self.manager.call(name, args)
                    except Exception as e:
                        result = f"[LOI CHAY TOOL] {type(e).__name__}: {e}"
                sessions.append(
                    self.sid,
                    {"role": "tool", "tool_call_id": tc.get("id"), "name": name, "content": result},
                )
            msgs = [_sys(self.manager, self.sid, self._cwd()), *sessions.load(self.sid)]
        return "[DUNG] đã tới giới hạn số bước tool. Gõ /new để bắt đầu session mới."

    def say(self, text):
        sessions.append(self.sid, {"role": "assistant", "content": text})
        return text