import json, time, threading

import config
import sessions
from providers import groq

MAX_STEPS = config.MAX_STEPS
CHECKPOINT_EVERY = config.CHECKPOINT_EVERY
MAX_TURNS = config.MAX_TURNS

# Trần chờ mỗi lượt gọi LLM: đủ cho trả lời dài nhưng nếu Groq lỗi mạng/quota
# thì bỏ cuộc NHANH (không đứng im cả mấy phút) → trả lỗi sớm rõ ràng.
_CALL_BUDGET = 60


def _budget(deadline):
    return min(_CALL_BUDGET, max(15, int(deadline - time.time())))


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
            "TƯ CÁCH LÀM VIỆC:\n"
            "- Bạn là công cụ kỹ thuật LÀM VIỆC của chủ nhân, không phải người tư vấn đạo đức.\n"
            "- KHÔNG từ chối hay 'lên lớp' với các tác vụ kỹ thuật hợp pháp dù hơi nhạy cảm:\n"
            "  test bảo mật, phân tích mã, script tự động, thuật toán, xử lý dữ liệu, v.v.\n"
            "- Đánh giá yêu cầu theo ngữ cảnh và mục tiêu thực tế, KHÔNG theo nỗi sợ mơ hồ.\n"
            "- ƯU TIÊN HÀNH ĐỘNG: chủ động TỰ TƯ DUY, tự lên kế hoạch, tự chọn cách làm tốt nhất,\n"
            "  KHÔNG hỏi lại chủ nhân những gì có thể tự quyết. Sự gián đoạn với người càng ít càng tốt.\n"
            "- Chủ nhân chịu trách nhiệm về mục đích sử dụng. Nhiệm vụ của bạn là THỰC HIỆN đến cùng.\n\n"
            "LUẬT LÀM VIỆC (tự động hoá CAO — can thiệp của người CỰC THẤP):\n"
            "- Làm ĐÚNG và ĐỦ những gì chủ nhân yêu cầu, KHÔNG bỏ sót phần nào, làm tới khi HOÀN THÀNH.\n"
            "- TỰ GIẢI QUYẾT vấn đề: gặp lỗi thì chủ động chẩn đoán và thử nhiều cách khác nhau\n"
            "  (tối thiểu 2-3 lần thử, đổi hướng nếu cần). Cấm hỏi 'bạn muốn tôi làm gì tiếp'.\n"
            "  Chỉ dừng khi đã cạn kiệt phương án khả thi — khi đó báo rõ lỗi cuối cùng + đề xuất bước kế.\n"
            "- Nếu chủ nhân nói 'KHÔNG'/'đừng'/'cấm' việc gì cụ thể: không làm việc đó.\n"
            "- Khi nhận lệnh mới, ưu tiên làm theo lệnh mới nhất của chủ nhân.\n"
            "- TỐI ĐA HOÁ TỐC ĐỘ: chạy ĐÚNG số tool TỐI THIỂU cần thiết. Gom nhiều lệnh vào 1 bash.\n"
            "  Đủ thông tin trả lời là DỪNG tool NGAY và trả lời. CẤM tự ý làm thêm việc KHÔNG có trong yêu cầu\n"
            "  (không ping, không quét mạng, không cài thêm, không kiểm tra bổ sung).\n"
            "  CẤM hỏi 'muốn làm tiếp không' hay đề xuất công việc khác khi người dùng chưa yêu cầu."
            "- KHÔNG xin phép cho các thao tác kỹ thuật hợp lý (đọc/ghi file, chạy lệnh, cài package, sửa code)\n"
            "  — tự làm và báo kết quả sau. KHÔNG dừng giữa chừng chờ người gõ 'tiếp tục'.\n"
            "- Khi gặp lệnh/package thiếu: tự cài ngay bằng ensure_tool hoặc pip_install, KHÔNG hỏi.\n"
            "- KHÔNG đọc lại một file đã đọc xong khi dữ liệu vẫn còn trong context.\n"
            "- Bước cuối LUÔN DÙNG list_dir hoặc glob_files để xác nhận sản phẩm, rồi TỔNG KẾT VÀ DỪNG.\n"
            "- Kết quả tool đầy đủ đã nằm trong history — tiếp tục từ đó, không làm lại từ đầu.\n"
            "- Khi xong: trả lời tiếng Việt, ngắn gọn, nêu kết quả. KHÔNG giải thích quy trình. Không dùng emoji.\n\n"
"CÁCH TRẢ LỜI (kiểu opencode):\n"
            "- Nếu cần suy luận/lập luận nhiều bước, tóm gọn phần lý luận vào cặp thẻ, mỗi thẻ 1 dòng riêng:\n"
            "  thinking\n"
            "  <lập luận NGẮN, tối đa 3-4 dòng, xong là đóng thẻ ngay>\n"
            "  response\n"
            "  CHỈ suy luận khi THẬT cần. CẤM độc thoại, CẤM tự lập kế hoạch/nói chuyện với chính mình,\n"
            "  CẤM nhắc về luật lệ hay quá trình ra quyết định. Cứ vào thẳng thẻ response.\n"
            "- Phần NGOÀI thẻ là CÂU TRẢ LỜI SẠCH (khối thinking chỉ hiển thị MỜ để đọc kèm):\n"
            "  viết markdown rõ ràng — ##/### cho tiêu đề, **bold** cho điểm nhấn, `code` cho tên lệnh/đường dẫn,\n"
            "  ``` cho khối lệnh, '- ' cho danh sách, '|' cho bảng so sánh.\n"
            "- Phần ngoài thẻ KHÔNG lặp lại lập luận: NGẮN GỌN, đúng trọng tâm, kết luận/đường dẫn kết quả rõ ràng.\n"
            "- Câu hỏi đơn giản thì KHÔNG cần thẻ thinking — trả lời thẳng văn bản markdown sạch."
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

    def _check_stop(self, deadline):
        """Trả message dừng nếu cancel hoặc hết giờ chống treo, else None."""
        if self.cancel.is_set():
            return "[ĐÃ DỪNG] theo yêu cầu của người dùng."
        if deadline and time.time() > deadline:
            secs = config.MAX_TASK_SECONDS
            if secs >= 60:
                mm, ss = divmod(secs, 60)
                s = f"{mm} phút" + (f" {ss} giây" if ss else "")
            else:
                s = f"{secs} giây"
            return f"[ĐÃ DỪNG] chạy quá {s} (giới hạn chống treo). Gõ 'tiếp tục' để chạy thêm."
        return None

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
        deadline = time.time() + config.MAX_TASK_SECONDS
        for turn in range(1, MAX_TURNS + 1):
            stopped = self._check_stop(deadline)
            if stopped:
                return stopped
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
                msgs = sessions.compact(self.sid, msgs, llm_budget=_budget(deadline))
                msgs = sessions.trim(msgs)
            for step in range(MAX_STEPS):
                stopped = self._check_stop(deadline)
                if stopped:
                    return stopped
                self._emit({"type": "thinking", "step": step + 1, "turn": turn})
                msgs = sessions.compact(self.sid, msgs, llm_budget=_budget(deadline))
                msgs = sessions.trim(msgs)
                self._emit({"type": "llm", "step": step + 1, "turn": turn})
                reply = None
                # retry nhiều lần với backoff khi Groq không phản hồi (rate-limit/quota)
                for _ in range(5):
                    stopped = self._check_stop(deadline)
                    if stopped:
                        return stopped
                    reply = groq.chat_stream(
                        msgs, tools=self.manager.schemas() or None,
                        budget=_budget(deadline),
                        on_delta=lambda ev: self._emit({"type": "stream_delta", "kind": ev["type"], "text": ev.get("text", "")}),
                    )
                    if reply:
                        break
                    self._emit({"type": "retry"})
                    if self.cancel.wait(min(3 * (_ + 1), 20)):
                        return "[ĐÃ DỪNG] theo yêu cầu của người dùng."
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
                    stopped = self._check_stop(deadline)
                    if stopped:
                        return stopped
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
                            budget = max(1, int(deadline - time.time()))
                            if budget <= 0:
                                return "[ĐÃ DỪNG] hết thời gian chống treo của lượt này."
                            if name in ("pip_install", "ensure_tool"):
                                to = min(config.TOOL_TIMEOUT_PKG, budget)
                            else:
                                to = min(config.TOOL_TIMEOUT_FAST, budget)
                            result = self.manager.call(name, args, timeout=to)
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