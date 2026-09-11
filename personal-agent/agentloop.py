import json, os, sys, time, threading, hashlib, re

import config
import sessions
from providers import groq

MAX_STEPS = config.MAX_STEPS
CHECKPOINT_EVERY = config.CHECKPOINT_EVERY
MAX_TURNS = config.MAX_TURNS

_CALL_BUDGET = 60
_STEP_BUDGET = 75
_SELF_HEAL_MAX_RETRIES = 3


def _budget(deadline, step_deadline=None):
    if step_deadline:
        return min(_STEP_BUDGET, max(15, int(step_deadline - time.time())))
    return min(_CALL_BUDGET, max(15, int(deadline - time.time())))


# ── Định tuyến module theo công việc ─────────────────────────────────────
# Mỗi nhóm công việc → module MCP phù hợp. Agent ĐỌC phần này để chọn tool đúng
# ngay từ đầu (đỡ gọi lung tung), và macro recorder cũng phân mục theo nhóm này.
MODULE_GROUPS = {
    "lap-trinh":   {"desc": "code, file, shell, sửa lỗi, git, cài package",
                    "modules": ["developer", "lsp"]},
    "web":         {"desc": "tìm kiếm web, đọc trang, GitHub, tin tức",
                    "modules": ["webtool", "browser_auto"]},
    "desktop":     {"desc": "mở app, click, gõ phím, điều khiển máy tính",
                    "modules": ["desktop_linux"]},
    "media":       {"desc": "video, ảnh AI, giọng nói TTS, nhạc, cắt ghép",
                    "modules": ["media_tools"]},
    "mang-xa-hoi": {"desc": "đăng bài, bình luận, theo dõi Facebook/YouTube/TikTok",
                    "modules": ["social_auto"]},
    "ghi-nho":     {"desc": "ghi nhớ, học quy trình, xem lại bài học",
                    "modules": ["memory", "skills", "experience"]},
}
# macro recorder dùng cùng bộ mục + thêm mục chung
MACRO_CATS = dict(MODULE_GROUPS)
MACRO_CATS["khac"] = {"desc": "việc khác không thuộc nhóm trên", "modules": []}

_ROUTE_KW = [
    (("code", "lập trình", "lap trinh", "sửa lỗi", "sua loi", "bug", "hàm", "class",
      "python", "script", "file", "thư mục", "thu muc", "git", "commit", "cài package",
      "cai package", "pip", "terminal", "lệnh", "lenh bash"), "lap-trinh"),
    (("tìm", "tim kiem", "tìm kiếm", "web", "tin tức", "tin tuc", "giá", "gia ",
      "github", "đọc trang", "doc trang", "xem video", "youtube xem"), "web"),
    (("mở app", "mo app", "mở ứng dụng", "click", "nhấn nút", "gõ phím", "man hinh",
      "màn hình", "desktop", "cửa sổ", "cua so", "chụp", "ứng dụng",
      "macro", "ghi lại", "phát lại", "thao tác"), "desktop"),
    (("video", "ảnh", "anh ai", "giọng", "giong noi", "giọng nói", "nói", "mp3",
      "tts", "nhạc", "nhac", "cắt ghép", "slideshow", "shorts"), "media"),
    (("facebook", "đăng bài", "dang bai", "tiktok", "instagram", "twitter",
      "bình luận", "mạng xã hội", "theo dõi", "đăng youtube"), "mang-xa-hoi"),
    (("nhớ", "nho ", "ghi nhớ", "skill", "bài học", "bai hoc", "kinh nghiệm"), "ghi-nho"),
]


def route_task(user_text):
    """Đoán nhóm công việc từ câu lệnh → (groups, modules). Không đoán được → ([], [])."""
    t = f" {(user_text or '').lower()} "
    groups = []
    for kws, g in _ROUTE_KW:
        if any(k in t for k in kws):
            groups.append(g)
    mods = []
    for g in groups:
        for m in MODULE_GROUPS[g]["modules"]:
            if m not in mods:
                mods.append(m)
    return groups, mods


def _route_section(user_text):
    groups, mods = route_task(user_text or "")
    if not groups:
        return ""
    gl = ", ".join(f"{g} ({MODULE_GROUPS[g]['desc']})" for g in groups)
    ml = ", ".join(mods)
    return (f"\n\nĐỊNH TUYẾN CÔNG VIỆC: câu này thuộc nhóm [{gl}]. "
            f"ƯU TIÊN tool của module [{ml}] trước — đúng tool ngay từ bước đầu, "
            f"không gọi lung tung module khác trừ khi cần. "
            f"Việc desktop lặp lại → dùng macro recorder (rec_start/rec_play, xem rec_list) "
            f"thay vì click tay từng bước.")


def _load_skills_prompt():
    """Load các skill đã học vào system prompt (tự tiến hoá kiểu GenericAgent)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from mcp_servers.skills_server import skill_list as _sk_list
        listing = _sk_list()
        if not listing or listing.startswith("(chưa"):
            return ""
        return "\n\n" + listing
    except Exception:
        return ""


def _load_experience_prompt():
    """Nạp kinh nghiệm đã học (lessons + procedural skills) — compact, chống tràn context."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import experience as _exp
        section = _exp.get_experience_prompt(preview=True)
        return ("\n\nKINH NGHIỆM ĐÃ HỌC (đúng thì làm theo, sai thì tránh lặp lại):\n" + section) if section else ""
    except Exception:
        return ""


def _load_agents_md(cwd):
    """Scan thư mục project cho AGENTS.md/CLAUDE.md — inject vào system prompt."""
    instructions = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        for base in (cwd, os.path.expanduser("~")):
            fp = os.path.join(base, name)
            if os.path.isfile(fp):
                try:
                    with open(fp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read(4000).strip()
                    if content:
                        instructions.append(f"[{name} từ {base}]\n{content}")
                except Exception:
                    pass
    # conda: kiểm tra thư mục .opencode và .config/opencode
    for subdir in (".opencode", os.path.join(".config", "opencode")):
        fp = os.path.join(cwd, subdir, "AGENTS.md")
        if os.path.isfile(fp):
            try:
                with open(fp, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(4000).strip()
                if content:
                    instructions.append(f"[{subdir}/AGENTS.md]\n{content}")
            except Exception:
                pass
    return "\n\n".join(instructions)


def _sys(manager, sid, cwd, user_text=""):
    tools = "\n".join(
        f"- {t['function']['name']}: {t['function']['description']}"
        for t in manager.schemas()
    )
    agents_md = _load_agents_md(cwd)
    agents_section = f"\n\nHƯỚNG DẪN DỰ ÁN (từ AGENTS.md):\n{agents_md}" if agents_md else ""
    skills_section = _load_skills_prompt()
    exp_section = _load_experience_prompt()
    return {
        "role": "system",
        "content": (
            f"Bạn là {config.NAME} ({config.VERSION}) — agent Python chạy 100% trên Groq, "
            "theo kiến trúc MCP của goose/opencode: mọi thao tác qua công cụ MCP "
            "(mỗi công cụ chạy trong tiến trình riêng biệt).\n\n"
            f"Hôm nay: {time.strftime('%Y-%m-%d %H:%M')}\n"
            f"Thư mục làm việc: {cwd}\nSession: {sid}\n\n"
            "PHIÊN MỚI: đây là đoạn chat mới, lịch sử trò chuyện TRỐNG. CẤM bịa ra chuyện cũ "
            "(không nhắc 'như đã nói', không tiếp nối việc không có trong lịch sử). "
            "KINH NGHIỆM/SKILL/HƯỚNG DẪN DỰ ÁN bên dưới là kiến thức chung, KHÔNG phải lịch sử trò chuyện. "
            "Chỉ trả lời đúng câu hỏi hiện tại; chưa đủ thông tin thì dùng tool kiểm tra, không suy đoán.\n\n"
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
            "- BẮT BUỘC GỌI TOOL: khi chủ nhân yêu cầu BẤT KỲ tác vụ nào (chụp màn hình, mở app, "
            "gõ phím, click, tạo file, tìm web, chạy lệnh...), PHẢI gọi tool tương ứng NGAY LẬP TỨC. "
            "CẤM trả lời bằng hướng dẫn/thay vì gọi tool. Nếu不确定 tool nào, gọi dl_status để kiểm tra.\n"
            "- Làm ĐÚNG và ĐỦ những gì chủ nhân yêu cầu, KHÔNG bỏ sót phần nào, làm tới khi HOÀN THÀNH.\n"
            "- TỰ GIẢI QUYẾT vấn đề: gặp lỗi thì chủ động chẩn đoán và thử nhiều cách khác nhau\n"
            "  (tối thiểu 2-3 lần thử, đổi hướng nếu cần). Cấm hỏi 'bạn muốn tôi làm gì tiếp'.\n"
            "  Chỉ dừng khi đã cạn kiệt phương án khả thi — khi đó báo rõ lỗi cuối cùng + đề xuất bước kế.\n"
            "- Nếu chủ nhân nói 'KHÔNG'/'đừng'/'cấm' việc gì cụ thể: không làm việc đó.\n"
            "- Khi nhận lệnh mới, ưu tiên làm theo lệnh mới nhất của chủ nhân.\n"
            "- TỐI ĐA HOÁ TỐC ĐỘ: chạy ĐÚNG số tool TỐI THIỂU cần thiết. Gom nhiều lệnh vào 1 bash.\n"
            "- GHI FILE LỚN (>5KB): KHÔNG bao giờ tạo nội dung khổng lồ trong 1 tool call duy nhất\n"
            "  (dễ bị mạng cắt giữa chừng → thất bại lặp lại). Ghi thành NHIỀU BƯỚC NHỎ:\n"
            "  MỖI BƯỚC bám tối đa ~2000 KÝ TỰ (10-20 dòng). Bước 1 dùng write_file phần đầu,\n"
            "  các bước sau nối tiếp bằng bash: cat >> file <<'XEOF' ... XEOF\n"
            "  LUÔN giữ tiến độ: mỗi bước chỉ thêm DỮ LIỆU MỚI, KHÔNG ghi lại toàn bộ file.\n"
            "  CẤM dùng write_file cho file ĐÃ CÓ nội dung (>1KB) — lúc đó CHỈ nối thêm bằng cat >>. "
            "File >1KB chỉ được tạo bằng write_file ĐÚNG 1 lần ở bước đầu tiên.\n"
            "  Đủ thông tin trả lời là DỪNG tool NGAY và trả lời. CẤM tự ý làm thêm việc KHÔNG có trong yêu cầu\n"
            "  (không ping, không quét mạng, không cài thêm, không kiểm tra bổ sung).\n"
            "  CẤM hỏi 'muốn làm tiếp không' hay đề xuất công việc khác khi người dùng chưa yêu cầu."
            "- KHÔNG xin phép cho các thao tác kỹ thuật hợp lý (đọc/ghi file, chạy lệnh, cài package, sửa code)\n"
            "  — tự làm và báo kết quả sau. KHÔNG dừng giữa chừng chờ người gõ 'tiếp tục'.\n"
            "- Khi gap package/thu vien thieu: DUNG BASH thu truoc (python3 -c 'import X'), neu thi "
            " moi dung pip_install/ensure_tool. NHIEU KHI da co san ma khong biet.\n"
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
            "- Câu hỏi đơn giản thì KHÔNG cần thẻ thinking — trả lời thẳng văn bản markdown sạch.\n\n"
            f"SỬ DỤNG TODO: Khi bắt đầu task nhiều bước, dùng todo_write(session_id='{sid}', todos=[...]) "
            "để theo dõi tiến độ. Dùng todo_list(session_id='{sid}') để kiểm tra. "
            "Mỗi todo: {{content: '...', status: 'pending'|'in_progress'|'completed'|'cancelled', priority: 'high'|'medium'|'low'}}."
            "\nSỬ DỤNG LSP: Sau khi mở hoặc sửa file nguồn (.c/.cpp/.py), dùng lsp_diagnostics(file) "
            "để lấy lỗi/cảnh báo, lsp_definition/lsp_references/lsp_symbols/lsp_hover để phân tích mã. "
            "Trước khi chạy/biên dịch, luôn lsp_diagnostics để tự sửa lỗi tĩnh."
            "\nSỬ DỤNG SUBAGENT: Với nhiệm vụ tách biệt nặng (quét toàn repo, viết code độc lập, "
            "tra cứu song song), dùng task(description) — agent con có đủ tool riêng. "
            "Mô tả rõ việc + format kết quả cần trả về. Không dùng task cho việc nhỏ gọi trực tiếp được.\n"
            "HƯỚNG DẪN DÙNG TOOL ĐẶC THÙ:\n"
            "- DESKTOP/CHUP MAN HINH/DIEU KHIEN UNG DUNG: dung dl_* tools. KHONG BAO GIO tu choi "
            "\"khong co tool chup man hinh\" — dl_tree THAY THE screenshot bang cach doc cay giao dien AT-SPI. "
            "LUON goi dl_status truoc, dl_tree de xem noi dung, dl_click/dl_type/dl_key de tuong tac. "
            "Vi du: mo terminal -> dl_click(name='Terminal'), go lenh -> dl_type(text='ls'), nhan Enter -> dl_key(combo='Return').\n"
            "- LAM PPTX/POWERPOINT: dung bash chay python3 inline script voi python-pptx. "
            "KHONG dung pip_install — python-pptx da cai san.\n"
            "- LÀM GAME: dùng write_file/apply_patch tạo mã (pygame/js/html), rồi py_compile hoặc chạy smoke-test "
            "bằng bash. Xác nhận file tồn tại bằng list_dir rồi báo.\n"
            "- TRÌNH DUYỆT (browser_*): mở trang bằng browser_open(url) → tương tác browser_click/browser_type/ "
            "browser_press → lấy nội dung browser_content → chụp browser_screenshot. Trang đăng nhập: dùng CSS "
            "selector chính xác cho browser_type (vd input[name=..]), sau đó browser_click nút. Screenshot trả về "
            "là đường dẫn PNG tuyệt đối. Profile trình duyệt được lưu → đăng nhập giữ qua các lượt.\n"
            "- KHỞI ĐỘNG SERVER nền qua bash: LUÔN dùng nohup kèm redirect, vd "
            "'nohup python3 -m http.server 8080 >/tmp/srv.log 2>&1 &' rồi sleep 1 và kiểm tra bằng curl."
            " CẤM chạy server kiểu 'cmd &' không redirect — sẽ làm tool bash treo và chỉ mở được trang "
            "sau khi xác nhận curl trả 200.\n"
            "- LÀM VIDEO (media_*): quy trình 1 phân cảnh = media_tts(text) tạo mp3 + media_image(prompt) tạo ảnh "
            " + media_scene(ảnh,audio,text=chữ nổi) ra mp4. Nhiều phân cảnh thì media_concat(files). "
            "Nhanh hơn: media_slideshow với JSON {'scenes':[{'text':..,'prompt':..}], 'out':'ten'}. "
            "Lấy thông tin bằng media_info, cắt media_trim, đổi cỡ Shorts media_scale.\n"
            "- TẢI/ĐỌC nội dung đơn giản: web_search/web_fetch là nhẹ nhất, browser_* chỉ khi cần JS/đăng nhập.\n"
            "- KEY GROQ: nhiều key xoay vòng tự động với circuit breaker — khi Groq trả 429 liên tục agent tự "
            "cooldown và chuyển key khác; nếu hết key thì báo lỗi rõ, đừng tự thử lại mãi.\n"
            f"{agents_section}"
            f"{skills_section}"
            f"{exp_section}"
            f"{_route_section(user_text)}"
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
        self._error_patterns = {}  # pattern -> count (self-healing: track recurring errors)
        self._tool_fingerprints = set()  # track which tools have been called with what args
        self._task_hash = ""  # fingerprint of current task for resume
        self._last_steps = []  # (tool, args) đã dùng cho task hiện tại (để auto-save skill)

    def _emit(self, ev):
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def stop(self):
        self.cancel.set()

    def _check_stop(self, deadline):
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

    def _track_error(self, error_msg):
        """Track recurring error patterns for self-healing."""
        # Extract pattern: first 80 chars of error
        pattern = (error_msg or "")[:80]
        if pattern:
            self._error_patterns[pattern] = self._error_patterns.get(pattern, 0) + 1

    def _auto_save_skill(self, user_text, steps):
        """Tự lưu skill khi task thành công (tự tiến hoá kiểu GenericAgent)."""
        if not steps or len(steps) < 2:
            return
        # Bỏ các bước thất bại (result_ok=False) — chỉ học bước thành công
        good = [s for s in steps if s.get("result_ok")]
        if len(good) < 2:
            return
        # Tên skill rút ra từ câu lệnh
        words = re.sub(r"[^\w\sà-ỹÀ-Ỹ]", " ", user_text.lower()).split()
        words = [w for w in words if len(w) > 2 and w not in ("bang", "cua", "voi", "cho", "trong", "qua", "dung", "luu", "tao", "va", "mot", "kien", "trao", "sau", "khi", "xong", "them")][:5]
        name = "_".join(words) or "skill_tu_hoc"
        try:
            from mcp_servers.skills_server import skill_save
            skill_save(name, user_text[:200], good, tags="auto")
        except Exception:
            pass

    def _finish(self, user_text, out):
        """MỌI đường kết thúc task đều qua đây — agent LUÔN tự học:
        - Thành công (text thường) → lưu skill + procedural skill.
        - Thất bại/timeout/quota (text bắt đầu bằng [) → lưu lesson.
        - User chủ động dừng → không học. Không bao giờ raise."""
        try:
            if not out or out == "(rỗng)":
                return out
            low = out.lower()
            if "theo yêu cầu" in low or "theo yeu cau" in low:
                return out
            if out.startswith("["):
                import experience as _exp
                _exp.auto_learn(user_text, self._last_steps, False, error_msg=out[:300])
            else:
                self._auto_save_skill(user_text, self._last_steps)
                import experience as _exp
                _exp.auto_learn(user_text, self._last_steps, True)
        except Exception:
            pass
        return out

    def _should_self_heal(self, error_msg):
        """Check if we've seen this error before and should try alternative approach."""
        pattern = (error_msg or "")[:80]
        return self._error_patterns.get(pattern, 0) >= 2

    def _tool_fingerprint(self, name, args):
        """Generate fingerprint for tool call to detect loops."""
        key = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)[:200]}"
        return hashlib.md5(key.encode()).hexdigest()[:12]

    def _is_looping(self, name, args):
        """Detect if agent is calling same tool with same args repeatedly."""
        fp = self._tool_fingerprint(name, args)
        if fp in self._tool_fingerprints:
            return True
        self._tool_fingerprints.add(fp)
        return False

    def _cwd(self):
        try:
            return self.manager.call("cwd", {}, 10).strip()
        except Exception:
            return "?"

    def run(self, user_text):
        """Chạy agent với self-healing: tự detect loop, tự recover lỗi, tự validate kết quả."""
        self.cancel.clear()
        self._error_patterns.clear()
        self._tool_fingerprints.clear()
        self._last_steps = []
        self._task_hash = hashlib.md5(user_text.encode()).hexdigest()[:16]
        sessions.append(self.sid, {"role": "user", "content": user_text})
        deadline = time.time() + config.MAX_TASK_SECONDS
        last_tool_errors = []  # track recent tool errors for self-healing
        for turn in range(1, MAX_TURNS + 1):
            stopped = self._check_stop(deadline)
            if stopped:
                return self._finish(user_text, stopped)
            msgs = [_sys(self.manager, self.sid, self._cwd(), user_text), *sessions.load(self.sid)]
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
                    return self._finish(user_text, stopped)
                self._emit({"type": "thinking", "step": step + 1, "turn": turn})
                msgs = sessions.compact(self.sid, msgs, llm_budget=_budget(deadline))
                msgs = sessions.trim(msgs)
                self._emit({"type": "llm", "step": step + 1, "turn": turn})
                reply = None
                step_deadline = time.time() + _STEP_BUDGET
                for retry_i in range(5):
                    stopped = self._check_stop(deadline)
                    if stopped:
                        return self._finish(user_text, stopped)
                    reply = groq.chat_stream(
                        msgs, tools=self.manager.schemas() or None,
                        budget=_budget(deadline, step_deadline),
                        on_delta=lambda ev: self._emit({"type": "stream_delta", "kind": ev["type"], "text": ev.get("text", "")}),
                    )
                    if reply:
                        break
                    # Tiến trình KHÔNG mất: mọi kết quả tool đã append vào sessions DB
                    # ngay sau mỗi tool; retry LLM chỉ giữ nguyên msgs và thử lại.
                    try:
                        wait = groq.rate_wait_remaining()
                    except Exception:
                        wait = 0
                    if retry_i < 4:  # chi hien retry o 4 lan dau, lan cuoi bo qua
                        if wait > 1:
                            self._emit({"type": "retry", "attempt": retry_i + 1, "wait": round(wait, 1)})
                        else:
                            self._emit({"type": "retry", "attempt": retry_i + 1})
                    if self.cancel.wait(min(2 * (retry_i + 1), 12)):
                        return self._finish(user_text, "[DUNG] theo yeu cau cua nguoi dung.")
                if not reply:
                    if time.time() < deadline - 5:
                        try:
                            sessions.checkpoint(self.sid, step + 1, msgs,
                                                summary=f"task={self._task_hash} llm_retry turn={turn} step={step+1}")
                        except Exception:
                            pass
                        time.sleep(1)
                        continue
                    return self._finish(user_text, "[TAM DUNG] Groq dang qua tai/quota het — het thoi gian luot nay, cong viec chua xong. Go 'tiep tuc' de chay not doan con dang do.")
                tool_calls = reply.get("tool_calls") or []
                if not tool_calls:
                    if turn > 1:
                        content = reply.get("content") or ""
                    else:
                        content = reply.get("content") or "(rỗng)"
                    sessions.append(self.sid, {"role": "assistant", "content": content})
                    return self._finish(user_text, content)
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
                        return self._finish(user_text, stopped)
                    fn = tc.get("function") or {}
                    name = fn.get("name", "?")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    # SELF-HEALING: detect loops
                    if self._is_looping(name, args):
                        sessions.append(self.sid, {"role": "tool", "tool_call_id": tc.get("id"),
                            "name": name, "content": "[SELF-HEAL] Phát hiện gọi lặp lại cùng thao tác. Hãy thử cách khác hoặc bỏ qua bước này."})
                        self._emit({"type": "tool_done", "name": name, "result": "[loop detected]"})
                        continue
                    args_note = str(args)[:120]
                    self._emit({"type": "tool_start", "name": name, "args": args, "args_note": args_note})
                    if not self.perm.decide(name, args, askfn=self.askfn):
                        result = f"[TU CHOI] Tool {name} bị chặn bởi permission. Hãy giải thích với người dùng."
                    else:
                        try:
                            budget = max(1, int(deadline - time.time()))
                            if budget <= 0:
                                return self._finish(user_text, "[ĐÃ DỪNG] hết thời gian chống treo của lượt này.")
                            if name in ("pip_install", "ensure_tool", "task"):
                                to = min(config.TOOL_TIMEOUT_PKG, budget)
                            else:
                                to = min(config.TOOL_TIMEOUT_FAST, budget)
                            result = self.manager.call(name, args, timeout=to)
                        except Exception as e:
                            result = f"[LOI CHAY TOOL] {type(e).__name__}: {e}"
                    # SELF-HEALING: track errors and inject recovery hints
                    self._last_steps.append({
                        "tool": name,
                        "args": args,
                        "result_ok": not (result and (result.startswith("[LOI]") or result.startswith("[TOOL LOI]") or result.startswith("[TU CHOI]"))),
                    })
                    if result and (result.startswith("[LOI]") or result.startswith("[TOOL LOI]")):
                        self._track_error(result)
                        last_tool_errors.append((name, result[:200]))
                        if len(last_tool_errors) > 5:
                            last_tool_errors.pop(0)
                        if self._should_self_heal(result):
                            result += "\n[SELF-HEAL] Lỗi này đã xảy ra nhiều lần. Hãy thử hướng khác: đổi tool, đổi tham số, hoặc bỏ qua bước này."
                            try:
                                import experience as _exp
                                _exp.record_lesson(
                                    user_text, result[:300], "Lỗi tool lặp lại trong cùng task",
                                    _exp.extract_lesson(user_text, result) or "Đổi hướng khi cùng 1 lỗi lặp lại.",
                                    tags=name,
                                )
                            except Exception:
                                pass
                    # Diff cũ/mới kiểu opencode: gửi kèm kết quả đầy đủ của tool sửa file
                    # để REPL vẽ diff inline (event result vẫn cắt gọn như cũ).
                    _full = result if name in ("edit_file", "write_file", "apply_patch") else ""
                    self._emit({"type": "tool_done", "name": name, "result": str(result)[:200], "full": _full})
                    sessions.append(
                        self.sid,
                        {"role": "tool", "tool_call_id": tc.get("id"), "name": name, "content": result},
                    )
                msgs = [_sys(self.manager, self.sid, self._cwd(), user_text), *sessions.load(self.sid)]
                if (step + 1) % CHECKPOINT_EVERY == 0:
                    try:
                        summary = f"task={self._task_hash} turn={turn} step={step+1} errors={len(self._error_patterns)}"
                        cp = sessions.checkpoint(self.sid, step + 1, msgs, summary=summary)
                        self._emit({"type": "checkpoint", "path": cp, "step": step + 1})
                    except Exception:
                        pass
            self._emit({"type": "turn_roll", "turn": turn})
        return self._finish(user_text, "[DUNG] đã tới giới hạn tổng số bước. Gõ 'tiếp tục' nếu muốn chạy thêm nữa.")

    def say(self, text):
        sessions.append(self.sid, {"role": "assistant", "content": text})
        return text