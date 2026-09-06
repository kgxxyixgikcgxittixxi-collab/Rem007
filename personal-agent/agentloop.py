import json, time, threading

import config
import sessions
from providers import groq

MAX_STEPS = config.MAX_STEPS
CHECKPOINT_EVERY = config.CHECKPOINT_EVERY
MAX_TURNS = config.MAX_TURNS


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
            "- Khi gặp lệnh/package thiếu: tự cài ngay bằng ensure_tool hoặc pip_install, KHÔNG hỏi người dùng, KHÔNG giải thích đang cài gì. Làm xong mới báo kết quả cuối.\n"
            "- Khi xong: trả lời tiếng Việt, ngắn gọn, nêu kết quả. KHÔNG giải thích quy trình đã làm. Không dùng emoji."
        ),
    }


class Agent:
    def __init__(self, manager, perm, sid=None, on_event=None):
        self.manager = manager
        self.perm = perm
        self.sid = sid or sessions.new()
        self.askfn = None
        self.on_event = on_event
        self.cancel = threading.Event()

    def _emit(self, ev):
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def stop(self):
        self.cancel.set()

    def _cwd(self):
        try:
            return self.manager.call("cwd", {}, 10).strip()
        except Exception:
            return "?"

    def run(self, user_text):
        """Chạy agent tới khi xong việc. Tự chuyển đợt (turn) mới khi hết MAX_STEPS mà
        LLM vẫn còn tool_calls — không cần người dùng phải gõ 'tiếp tục'. Chỉ dừng khi:
        LLM trả content không kèm tool (task xong), /stop, lỗi Groq, hoặc hết MAX_TURNS."""
        self.cancel.clear()
        sessions.append(self.sid, {"role": "user", "content": user_text})
        for turn in range(1, MAX_TURNS + 1):
            if self.cancel.is_set():
                return "[ĐÃ DỪNG] theo yêu cầu của người dùng."
            msgs = [_sys(self.manager, self.sid, self._cwd()), *sessions.load(self.sid)]
            if turn > 1:
                self._emit({"type": "turn", "turn": turn})
                msgs = sessions.load(self.sid)
                msgs = [{"role": "assistant", "content": (
                    "Cuộc trò chuyện đã vượt quá số bước của một đợt. "
                    "Đây là ĐỢT TIẾP THEO — hãy TIẾP TỤC hoàn thành công việc còn dang dở "
                    "ở các bước trước. Xem lịch sử phía trên để biết tiến độ, rồi dùng tool "
                    "để làm nốt và KẾT THÚC khi xong."
                )}, *msgs]
                msgs = sessions.compact(self.sid, msgs)
                msgs = sessions.trim(msgs)
            for step in range(MAX_STEPS):
                if self.cancel.is_set():
                    return "[ĐÃ DỪNG] theo yêu cầu của người dùng."
                self._emit({"type": "thinking", "step": step + 1, "turn": turn})
                msgs = sessions.compact(self.sid, msgs)
                msgs = sessions.trim(msgs)
                self._emit({"type": "llm", "step": step + 1, "turn": turn})
                reply = None
                # retry nhiều lần với backoff khi Groq không phản hồi (rate-limit/quota)
                for _ in range(5):
                    if self.cancel.is_set():
                        return "[ĐÃ DỪNG] theo yêu cầu của người dùng."
                    reply = groq.chat(msgs, tools=self.manager.schemas() or None)
                    if reply:
                        break
                    self._emit({"type": "retry"})
                    time.sleep(min(3 * (_ + 1), 20))  # 3s, 6s, 9s, 12s...
                if not reply:
                    return "[LOI] Groq không phản hồi (quota/rate-limit). Chờ 1 lúc rồi gõ lại, hoặc /keys."
                tool_calls = reply.get("tool_calls") or []
                if not tool_calls:
                    if turn > 1:
                        # đợt sau: ghi tóm tắt cuối, dừng
                        content = reply.get("content") or ""
                    else:
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
                    if self.cancel.is_set():
                        return "[ĐÃ DỪNG] theo yêu cầu của người dùng."
                    fn = tc.get("function") or {}
                    name = fn.get("name", "?")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    args_note = str(args)[:120]
                    self._emit({"type": "tool_start", "name": name, "args": args, "args_note": args_note})
                    if not self.perm.decide(name, args, askfn=self.askfn):
                        result = f"[TU CHOI] Tool {name} bị chặn bởi permission. Hãy giải thích với người dùng."
                    else:
                        try:
                            result = self.manager.call(name, args)
                        except Exception as e:
                            result = f"[LOI CHAY TOOL] {type(e).__name__}: {e}"
                    self._emit({"type": "tool_done", "name": name, "result": str(result)[:200]})
                    sessions.append(
                        self.sid,
                        {"role": "tool", "tool_call_id": tc.get("id"), "name": name, "content": result},
                    )
                msgs = [_sys(self.manager, self.sid, self._cwd()), *sessions.load(self.sid)]
                if (step + 1) % CHECKPOINT_EVERY == 0:
                    try:
                        cp = sessions.checkpoint(self.sid, step + 1, msgs)
                        self._emit({"type": "checkpoint", "path": cp, "step": step + 1})
                    except Exception:
                        pass
            # đã chạy hết MAX_STEPS mà vẫn còn tool_calls → tự chuyển đợt mới
            self._emit({"type": "turn_roll", "turn": turn})
        return "[DUNG] đã tới giới hạn tổng số bước. Gõ 'tiếp tục' nếu muốn chạy thêm nữa."

    def say(self, text):
        sessions.append(self.sid, {"role": "assistant", "content": text})
        return text