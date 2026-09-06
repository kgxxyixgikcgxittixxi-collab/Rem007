import json, time

import config
import sessions
from providers import groq

MAX_STEPS = 25


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
            "LUẬT BẮT BUỘC:\n"
            "- Làm đúng công việc, từng bước tiến tới KẾT QUẢ. KHÔNG tự ý đọc/lang thang thêm file ngoài yêu cầu.\n"
            "- KHÔNG đọc lại/verify lại một file đã đọc xong. Dữ liệu cũ vẫn còn trong context.\n"
            "- Bước cuối LUÔN DÙNG list_dir hoặc glob_files để xác nhận sản phẩm, rồi TỔNG KẾT VÀ DỪNG (không gọi tool nữa).\n"
            "- Nếu tool báo lỗi: sửa 1 lần, lỗi lần 2 thì bỏ qua và tiếp tục; không cày cùng 1 lỗi.\n"
            "- Context có giới hạn: giữ số bước tool dưới 10; nêu rõ ràng điều cần thiết.\n"
            "- Không bao giờ ghi/sửa vào /data/data/com.termux/files/home/personal-agent (runtime) hay ~/.rem_ai.\n"
            "- Khi gặp lệnh/công cụ/package Python bị thiếu (command not found, No module named, file không tìm thấy trong PATH): nếu cần để hoàn thành việc, hãy CHỦ ĐỘNG cài qua tool ensure_tool (công cụ hệ thống trong whitelist) hoặc pip_install (gói Python), rồi tiếp tục — không bỏ cuộc vì thiếu công cụ.\n"
            "- Khi xong: trả lời tiếng Việt, ngắn gọn, nêu đủ kết quả. Không dùng emoji."
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
            msgs = sessions.trim(msgs)
            reply = groq.chat(msgs, tools=self.manager.schemas() or None)
            if not reply:
                time.sleep(3)
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