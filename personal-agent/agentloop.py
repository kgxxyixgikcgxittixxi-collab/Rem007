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
      "python", "pygame", "game", "app", "script", "file", "thư mục", "thu muc", "git", "commit", "cài package",
      "cai package", "pip", "terminal", "lệnh", "lenh bash",
      "quạt", "quat", "tản nhiệt", "tan nhiet", "nhiệt độ", "nhiet do", "fan", "sensors",
      "fancontrol", "pwm", "nóng máy", "nong may", "lm-sensors"), "lap-trinh"),
    (("tìm", "tim kiem", "tìm kiếm", "web", "tin tức", "tin tuc", "giá", "gia ",
      "github", "đọc trang", "doc trang", "xem video", "youtube xem",
      "trình duyệt", "trinh duyet", "browser", "chrome", "firefox",
      "đăng nhập", "dang nhap", "login", "điền", "dien ", "form",
      "tab", "cuộn", "cuon ", "mở web", "mo web", "mở trang", "mở link",
      "mở youtube", "mo youtube"), "web"),
    (("mở app", "mo app", "mở ứng dụng", "mở terminal", "mo terminal",
      "click", "nhấn nút", "nhấn", "nhan ", "bấm", "bam ",
      "gõ phím", "gõ", "go phim", "chuột", "chuot ", "phím", "phim ",
      "man hinh", "màn hình", "desktop", "cửa sổ", "cua so", "chụp", "chup man",
      "ứng dụng", "ung dung", "mở", "mo ", "bật", "bat ", "tắt", "tat ",
      "đóng app", "dong app", "focus", "cửa sổ nổi",
      "macro", "ghi lại", "phát lại", "thao tác", "thao tac",
      "điều khiển máy", "dieu khien may"), "desktop"),
    (("video", "ảnh", "anh ai", "giọng", "giong noi", "giọng nói", "nói", "mp3",
      "tts", "đọc thành tiếng", "thành tiếng", "đọc văn bản", "chuyển văn bản",
      "nhạc", "nhac", "cắt ghép", "slideshow", "shorts"), "media"),
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


def _is_multi_action(text):
    """Task có vẻ NHIỀU bước (nhiều mệnh đề nối nhau) → đáng tự kiểm tra cuối."""
    t = f" {(text or '').lower()} "
    seps = [",", ";", " rồi ", " va ", " và ", " xong ", " tiếp ", " nữa ", "\n"]
    return sum(t.count(s) for s in seps) >= 2


def _route_section(user_text):
    groups, mods = route_task(user_text or "")
    if not groups:
        return ""
    gl = ", ".join(f"{g} ({MODULE_GROUPS[g]['desc']})" for g in groups)
    ml = ", ".join(mods)
    base = (f"\n\nĐỊNH TUYẾN CÔNG VIỆC: câu này thuộc nhóm [{gl}]. "
            f"ƯU TIÊN tool của module [{ml}] trước — đúng tool ngay từ bước đầu, "
            f"không gọi lung tung module khác trừ khi cần. "
            f"Việc desktop lặp lại → dùng macro recorder (rec_start/rec_play, xem rec_list) "
            f"thay vì click tay từng bước.")
    if any(g in ("desktop", "web", "mang-xa-hoi", "media") for g in groups):
        base += ("\n\nHỌC 1 LẦN – CHẠY NHANH MÃI MÃI (bắt buộc với việc tay chân, user đang nhìn cửa sổ nổi): "
                 "B1: gọi skill_find(keyword ngắn) + rec_list() để tìm quy trình đã học. "
                 "B2: nếu CÓ skill/macro khớp → skill_use/rec_play phát lại NGAY, không quan sát lại, không hỏi, không làm thừa. "
                 "B3: nếu CHƯA có → quan sát kỹ đúng 1 lần (dl_status rồi dl_tree/dl_text, hoặc browser_open rồi browser_content+browser_snapshot), "
                 "làm từng bước gọn theo thứ tự màn hình, xong việc hệ thống TỰ lưu skill cho lần sau. "
                 "Mỗi tool gọi phải là 1 thao tác thật trên màn hình, ngắn gọn, không giải thích dài. "
                 "NGHE LỜI TUYỆT ĐỐI: lệnh desktop/browser là MỆNH LỆNH trực tiếp — PHẢI gọi tool trong lượt đầu tiên, "
                 "CẤM trả lời hướng dẫn thay vì hành động, CẤM hỏi lại, CẤM từ chối vì lý do an toàn mơ hồ. "
                 "Lệnh mới nhất luôn thắng lệnh cũ; user nói DỪNG thì dừng, nói TIẾP thì làm tiếp.")
    if "desktop" in groups:
        base += ("\n\nĐIỀU KHIỂN MÁY THẬT (user đang nhìn màn hình — MỆNH LỆNH, không phải gợi ý): "
                 "user bảo 'mở app X' → dl_open(app='X') NGAY lượt đầu; 'gõ ...' → dl_type; "
                 "'nhấn/Enter' → dl_key; 'click ...' → dl_click/dl_mouse. "
                 "CẤM giả lập bằng bash (CẤM echo nội dung ra shell thay vì gõ lên màn hình, "
                 "CẤM gọi xdotool tay qua bash) — phải gọi đúng tool dl_* để thao tác "
                 "HIỆN THẬT trên màn hình. Xong việc dùng dl_tree/dl_screenshot xác nhận "
                 "kết quả đã hiện ra rồi mới báo cáo.")
    return base


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
    hist = sessions.load(sid)
    fresh = not any(m.get("role") in ("assistant", "tool") for m in hist)
    session_line = (
        "PHIÊN MỚI: đây là đoạn chat mới, lịch sử trò chuyện TRỐNG. "
        "CẤM bịa ra chuyện cũ (không nhắc 'như đã nói', không tiếp nối việc không có trong lịch sử)."
        if fresh else
        "TIẾP TỤC PHIÊN: lịch sử phía dưới là công việc đang dang dở ở lượt trước. "
        "Kế thừa đúng dữ liệu đã thu thập (kết quả web, file đã tạo, đường dẫn) — KHÔNG làm lại, "
        "KHÔNG tự bịa thêm thông tin không có trong lịch sử."
    )
    # ⚡ PROMPT CACHING: Groq cache tự động theo prefix KHỚP CHÍNH XÁC (giảm 50%
    # chi phí + token cache KHÔNG tính vào rate limit 8K TPM). Phải giữ phần
    # TĨNH (giới thiệu, tool list, luật) ổn định ở đầu prompt, mọi phần ĐỘNG
    # (thời gian, thư mục, session, route) phải dồn về CUỐI — nếu không mỗi phút
    # đổi %H:%M là phá cache, tốn gấp đôi input mỗi lần gọi.
    return {
        "role": "system",
        "content": (
            f"Bạn là {config.NAME} ({config.VERSION}) — agent Python chạy 100% trên Groq, "
            "theo kiến trúc MCP của goose/opencode: mọi thao tác qua công cụ MCP "
            "(mỗi công cụ chạy trong tiến trình riêng biệt).\n\n"
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
            "- NGHE LỜI TUYỆT ĐỐI: lệnh của chủ nhân là MỆNH LỆNH, không phải gợi ý. "
            "Gặp lệnh điều khiển máy/trình duyệt (mở app, click, gõ, đăng nhập, điền form, cuộn, đóng/mở tab...) "
            "→ PHẢI gọi tool dl_*/browser_* NGAY trong lượt tool đầu tiên. "
            "CẤM trả lời kiểu 'bạn hãy tự mở...' hay đưa hướng dẫn tay thay vì gọi tool. "
            "CẤM hỏi lại những gì đã rõ (tên app, URL, text cần gõ). Thiếu 1 chi tiết nhỏ thì TỰ CHỌN giá trị hợp lý nhất rồi làm, "
            "xong báo lại để user sửa nếu cần.\n"
            "- BẮT BUỘC GỌI TOOL: khi chủ nhân yêu cầu BẤT KỲ tác vụ nào (chụp màn hình, mở app, "
            "gõ phím, click, tạo file, tìm web, chạy lệnh...), PHẢI gọi tool tương ứng NGAY LẬP TỨC. "
            "CẤM trả lời bằng hướng dẫn/thay vì gọi tool. Nếu不确定 tool nào, gọi dl_status để kiểm tra.\n"
             "- Làm ĐÚNG và ĐỦ những gì chủ nhân yêu cầu, KHÔNG bỏ sót phần nào, làm tới khi HOÀN THÀNH.\n"
             "- Báo cáo TRUNG THỰC theo kết quả tool: chỉ báo bước nào đã có tool trả OK. "
             "CẤM báo khống bước chưa gọi tool (vd chưa dl_key mà dám nói 'đã nhấn Enter').\n"
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
            "\nREPO LẠ: gọi repo_map(root) 1 lần lấy bản đồ toàn repo (file + class/hàm) "
            "trước khi đọc/sửa lung tung nhiều file.\n"
            "\nSỬ DỤNG SUBAGENT: Với nhiệm vụ tách biệt nặng (quét toàn repo, viết code độc lập, "
            "tra cứu song song), dùng task(description, prompt, type) — agent con chạy context riêng, "
            "chỉ trả tóm tắt về. type='explore' (mặc định, CHỈ ĐỌC) cho tìm hiểu; type='general' "
            "(full tool) cho việc nhiều bước cần sửa file/chạy lệnh. "
            "Mô tả rõ việc + format kết quả cần trả về. Không dùng task cho việc nhỏ gọi trực tiếp được.\n"
            "HỎI USER KHI PHÂN VÂN: đứng trước ngã rẽ quan trọng (chọn hướng/kế hoạch/phạm vi) "
            "→ gọi ask_user(questions=[{question, options:[{label, description}]}]) để user chọn, "
            "rồi làm tiếp đúng hướng đã chọn. Dùng khi THẬT cần, không hỏi vặt.\n"
            "HƯỚNG DẪN DÙNG TOOL ĐẶC THÙ:\n"
            "- DESKTOP/CHUP MAN HINH/DIEU KHIEN UNG DUNG: dung dl_* tools. KHONG BAO GIO tu choi "
            "\"khong co tool chup man hinh\" — dl_tree THAY THE screenshot bang cach doc cay giao dien AT-SPI. "
            "LUON goi dl_status truoc, dl_tree de xem noi dung, dl_click/dl_type/dl_key de tuong tac. "
            "Vi du: mo terminal -> dl_click(name='Terminal'), go lenh -> dl_type(text='ls'), nhan Enter -> dl_key(combo='Return').\n"
            "  MỞ APP: ưu tiên dl_open(app='Terminal'|'Firefox'|'Files'...) — tự mở đúng app, không đoán click lung tung. "
            "FOCUS cửa sổ: dl_focus(title) khi nhiều cửa sổ. CHỜ phần tử: dl_wait(query) thay vì dl_tree lặp tay. "
            "CẦN ẢNH để vision kiểm tra: dl_screenshot() trả đường dẫn PNG.\n"
            "- LAM PPTX/POWERPOINT: dung bash chay python3 inline script voi python-pptx. "
            "KHONG dung pip_install — python-pptx da cai san.\n"
            "- LÀM PDF TIẾNG VIỆT: dùng make_pdf(title, body, out) — font Unicode DejaVu, KHÔNG vỡ dấu. "
            "CẤM dùng reportlab + Helvetica mặc định (vỡ chữ Việt). "
            "PDF có ảnh minh hoạ mới dùng script reportlab riêng nhưng PHẢI đăng ký font Unicode.\n"
            "- LÀM GAME: dùng write_file/apply_patch tạo mã (pygame/js/html), rồi py_compile hoặc chạy smoke-test "
            "bằng bash. Xác nhận file tồn tại bằng list_dir rồi báo.\n"
            "- TRÌNH DUYỆT (browser_*): mở trang bằng browser_open(url) → tương tác browser_click/browser_type/ "
            "browser_press → lấy nội dung browser_content → chụp browser_screenshot. Trang đăng nhập: dùng CSS "
            "selector chính xác cho browser_type (vd input[name=..]), sau đó browser_click nút. Screenshot trả về "
            "là đường dẫn PNG tuyệt đối. Profile trình duyệt được lưu → đăng nhập giữ qua các lượt.\n"
            "  MỚI v3.78: browser_snapshot() liệt kê nút/link/ô nhập để chọn selector CHUẨN (đỡ đoán mò); "
            "browser_fill_login() đăng nhập 1 phát (user+pass+submit); browser_tabs() quản lý tab; "
            "browser_wait_text() chờ chữ xuất hiện thay vì sleep mù.\n"
            "- KHỞI ĐỘNG SERVER nền qua bash: LUÔN dùng nohup kèm redirect, vd "
            "'nohup python3 -m http.server 8080 >/tmp/srv.log 2>&1 &' rồi sleep 1 và kiểm tra bằng curl."
            " CẤM chạy server kiểu 'cmd &' không redirect — sẽ làm tool bash treo và chỉ mở được trang "
            "sau khi xác nhận curl trả 200.\n"
            "- LÀM VIDEO (media_*): quy trình 1 phân cảnh = media_tts(text) tạo mp3 + media_image(prompt) tạo ảnh "
            " + media_scene(ảnh,audio,text=chữ nổi) ra mp4. Nhiều phân cảnh thì media_concat(files). "
            "Nhanh hơn: media_slideshow với JSON {'scenes':[{'text':..,'prompt':..}], 'out':'ten'}. "
            "Lấy thông tin bằng media_info, cắt media_trim, đổi cỡ Shorts media_scale.\n"
            "- TẢI/ĐỌC nội dung đơn giản: web_search/web_fetch là nhẹ nhất, browser_* chỉ khi cần JS/đăng nhập.\n"
            "- GIỌNG NÓI (voice_*): user nói 'Rem ơi ...' qua mic → voice_cmd(seconds) trả text lệnh đã chuẩn hóa "
            "(tự bỏ wake-word, gắn cờ [XÁC NHẬN] nếu nguy hiểm) → làm tiếp như lệnh gõ tay. Kiểm tra mic/loa bằng "
            "voice_status. Đọc kết quả ra loa bằng voice_say(text) khi user yêu cầu hoặc đang ở chế độ thoại. "
            "Lệnh có cờ [XÁC NHẬN] (xóa/format/shutdown/rm -rf) → PHẢI hỏi lại trước khi chạy.\n"
            "- KEY GROQ: nhiều key xoay vòng tự động với circuit breaker — khi Groq trả 429 liên tục agent tự "
            "cooldown và chuyển key khác; nếu hết key thì báo lỗi rõ, đừng tự thử lại mãi.\n\n"
            "NGHIÊN CỨU + NGUỒN THẬT (bắt buộc, chống bịa):\n"
            "- Tác vụ cần thông tin/số liệu/nội dung theo nguồn (bài giảng, đề thi, tin tức, chứng chỉ...): "
            "BẮT BUỘC gọi web_search hoặc web_fetch TRƯỚC khi viết. Nếu web_fetch báo Cloudflare: chuyển "
            "sang browser_open rồi browser_content. CẤM tự nghĩ ra nội dung thay cho việc nghiên cứu.\n"
            "- Chỉ trích dẫn URL/số liệu/tên nguồn THẬT SỰ xuất hiện trong output của web_search/web_fetch "
            "(hoặc file/bài viết đã đọc) của phiên này. Tuyệt đối CẤM tự bịa tên miền, URL, thư viện, "
            "bài báo, hay 'nguồn ảnh hưởng'. Nếu chưa đọc trực tiếp → ghi rõ 'chưa đọc trực tiếp', đừng gán.\n"
            "- Trước khi trả lời có MỤC NGUỒN/THAM KHẢO: rà từng URL lại so với output tool; sai lệch 1 ký tự "
            "(vd tienganh123 thay vì tienganhmoingay) cũng coi là bịa — phải sửa cho khớp.\n"
            "- Số liệu (thời gian, số câu, mức điểm chuẩn): chỉ dùng đúng số từ nguồn; không tự 'chốt' con số.\n"
            "- PDF/ấn phẩm cần ảnh minh hoạ: dùng web_images('chủ đề bằng tiếng Anh') để lấy URL ảnh THẬT, "
            "rồi web_download_images tải về và nhúng vào PDF bằng reportlab Image(đường dẫn). "
            "TUYỆT ĐỐI KHÔNG dùng media_image (sinh AI) cho ảnh minh hoạ PDF, không tự vẽ. "
            "Mỗi ảnh nên có caption nguồn URL. Muốn PDF lớn thì tải NHIỀU ảnh độ phân giải cao "
            "(cứ ~40-60 ảnh JPEG 150-400KB là đạt ~10MB), không cần nén."
            f"{agents_section}"
            f"{skills_section}"
            f"{exp_section}"
            f"{_route_section(user_text)}"
            # PHẦN ĐỘNG — để CUỐI cùng để không phá prefix cache của Groq
            f"\n\nTHỜI ĐIỂM/GHẾ LÀM VIỆC: hôm nay {time.strftime('%Y-%m-%d %H:%M')}, "
            f"thư mục {cwd}, session {sid}.\n"
            f"{session_line}"
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
        self.hard_abort = threading.Event()  # ESC đúp → dừng cứng, bỏ auto-resume
        self._error_patterns = {}  # pattern -> count (self-healing: track recurring errors)
        self._empty_replies = 0  # số lần model trả rỗng liên tiếp (quá 2 → dừng báo rõ)
        self._verified_done = False  # đã tự kiểm tra đủ bước cuối task chưa (tối đa 1 lần/task)
        self.last_reasoning = ""  # suy luận model lượt này (hiện UI kiểu opencode; KHÔNG lưu session/context)
        self._tool_fingerprints = set()  # track which tools have been called with what args
        self._task_hash = ""  # fingerprint of current task for resume
        self._last_steps = []  # (tool, args) đã dùng cho task hiện tại (để auto-save skill)
        self.live_notes = []        # chỉ đạo gõ GIỮA CHỪNG khi task đang chạy (luồng nghe → luồng làm)
        self._live_lock = threading.Lock()

    def _emit(self, ev):
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass

    def stop(self, hard=False):
        """Dừng task đang chạy (kiểu opencode session_interrupt).
        soft (ESC 1 lần//stop): cancel event → LLM/tool hủy ở biên lượt, được wrap-up.
        hard (ESC 2 lần): thêm cờ hard_abort → bỏ luôn auto-resume + chỉ đạo tồn,
        trả ngay không làm tiếp."""
        self.cancel.set()
        if hard:
            try:
                self.hard_abort.set()
            except Exception:
                pass
        try:
            with self._live_lock:
                self.live_notes = []
        except Exception:
            pass

    def inject(self, text):
        """Luồng NGHE → luồng LÀM: nhận lệnh mới ngay cả khi task đang chạy.
        Trả về số chỉ đạo đang chờ. Tối đa 5 — thừa thì bỏ cũ nhất (chống tồn 40)."""
        try:
            with self._live_lock:
                t = str(text or "").strip()
                if t:
                    self.live_notes.append(t)
                    if len(self.live_notes) > 5:
                        self.live_notes = self.live_notes[-5:]
                return len(self.live_notes)
        except Exception:
            return 0

    def live_count(self):
        try:
            with self._live_lock:
                return len(self.live_notes)
        except Exception:
            return 0

    def _drain_notes(self):
        try:
            with self._live_lock:
                notes, self.live_notes = self.live_notes, []
                return [n for n in notes if n]
        except Exception:
            return []

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

    def _repeat_failing(self, name):
        """Cùng 1 tool thất bại 2+ lần liên tiếp → đang kẹt (đọc 3 file khác nhau
        vẫn OK vì result_ok=True không tính)."""
        streak = 0
        for s in reversed(self._last_steps):
            if s.get("tool") == name:
                streak += 1
            else:
                break
        if streak < 2:
            return False
        fails = sum(1 for s in self._last_steps[-streak:] if not s.get("result_ok"))
        return fails >= 2

    def _cwd(self):
        try:
            return self.manager.call("cwd", {}, 10).strip()
        except Exception:
            return "?"

    def run(self, user_text):
        """Chạy agent với self-healing: tự detect loop, tự recover lỗi, tự validate kết quả."""
        self.cancel.clear()
        try:
            self.hard_abort.clear()
        except Exception:
            pass
        self._error_patterns.clear()
        self._empty_replies = 0
        self._verified_done = False
        self.last_reasoning = ""
        self._tool_fingerprints.clear()
        self._last_steps = []
        self._write_snap = False  # đã snapshot trước ghi file của turn hiện tại chưa
        self._task_hash = hashlib.md5(user_text.encode()).hexdigest()[:16]
        sessions.append(self.sid, {"role": "user", "content": user_text})
        deadline = time.time() + config.MAX_TASK_SECONDS
        last_tool_errors = []  # track recent tool errors for self-healing
        for turn in range(1, MAX_TURNS + 1):
            self._write_snap = False  # mỗi turn được 1 snapshot trước lần ghi đầu
            turn_t0 = time.time()
            turn_ok0 = sum(1 for s in self._last_steps if (s or {}).get("result_ok"))
            stopped = self._check_stop(deadline)
            if stopped:
                return self._finish(user_text, stopped)
            msgs = [_sys(self.manager, self.sid, self._cwd(), user_text), *sessions.load(self.sid)]
            if turn > 1:
                self._emit({"type": "turn", "turn": turn})
                # GIỮ system prompt ở đầu mọi turn (bản cũ gán lại msgs từ load()
                # làm RƠI system từ turn 2 → agent mất luật/tool-guide, trả lời loạn).
                msgs = [_sys(self.manager, self.sid, self._cwd(), user_text),
                        {"role": "assistant", "content": (
                            "Cuộc trò chuyện đã vượt quá số bước của một đợt. "
                            "Đây là ĐỢT TIẾP THEO — hãy TIẾP TỤC hoàn thành công việc còn dang dở "
                            "ở các bước trước. Xem lịch sử phía trên để biết tiến độ, rồi dùng tool "
                            "để làm nốt và KẾT THÚC khi xong."
                        )}, *sessions.load(self.sid)]
                msgs = sessions.compact(self.sid, msgs, llm_budget=_budget(deadline), cancel=self.cancel)
                msgs = sessions.trim(msgs)
            for step in range(MAX_STEPS):
                stopped = self._check_stop(deadline)
                if stopped:
                    return self._finish(user_text, stopped)
                # Pool quota cạn (xoay key mãi không xong tool nào): cắt sớm thay vì
                # nghiền 5 phút trong im lặng — báo rõ để user 'tiếp tục' sau 1-2 phút.
                if time.time() - turn_t0 > 150:
                    _ok_now = sum(1 for s in self._last_steps if (s or {}).get("result_ok"))
                    if _ok_now <= turn_ok0:
                        return self._finish(user_text,
                            "[TẠM DỪNG] Quota Groq cả pool đang cạn (xoay key nhiều vòng không xong tool nào). "
                            "Nghỉ 1-2 phút rồi gõ 'tiếp tục' để chạy tiếp — không mất tiến độ.")
                # CHỈ ĐẠO LIVE: lệnh gõ giữa chừng → chèn ngay vào lượt đang chạy,
                # agent điều chỉnh việc đang làm, không làm lại từ đầu.
                for _note in self._drain_notes():
                    _nm = {"role": "user", "content": (
                        "[CHỈ ĐẠO GIỮA CHỪNG — điều chỉnh việc đang làm theo yêu cầu mới, "
                        "không làm lại từ đầu]\n" + _note)}
                    sessions.append(self.sid, _nm)
                    msgs.append(dict(_nm))
                self._emit({"type": "thinking", "step": step + 1, "turn": turn})
                msgs = sessions.compact(self.sid, msgs, llm_budget=_budget(deadline), cancel=self.cancel)
                msgs = sessions.trim(msgs)
                self._emit({"type": "llm", "step": step + 1, "turn": turn})
                reply = None
                step_deadline = time.time() + _STEP_BUDGET
                # Groq quá tải/rần thì retry vô ích: giới hạn 3 lần, mỗi lần tính
                # theo BUDGET CÒN LẠI của bước (fail-fast theo deadline, không treo).
                for retry_i in range(3):
                    stopped = self._check_stop(deadline)
                    if stopped:
                        return self._finish(user_text, stopped)
                    remaining = step_deadline - time.time()
                    if remaining <= 3:
                        break
                    if retry_i > 0:
                        # Chỉ báo 1 lần duy nhất (không spam "thử lại 1/5, 2/5, 4/5"
                        # như log lỗi) — spinner/status tự chuyển nếu có dữ liệu mới.
                        if retry_i == 1:
                            self._emit({"type": "retry", "attempt": 1})
                    reply = groq.chat_stream(
                        msgs, tools=self.manager.schemas() or None,
                        budget=min(_budget(deadline, step_deadline), max(15, remaining)),
                        on_delta=lambda ev: self._emit({"type": "stream_delta", "kind": ev["type"], "text": ev.get("text", "")}),
                        cancel=self.cancel,
                    )
                    if reply:
                        break
                    # ESC//stop trong lúc chờ LLM → dừng ngay, không retry vô ích
                    if self.cancel.is_set():
                        return self._finish(user_text, "[ĐÃ DỪNG] theo yêu cầu của người dùng.")
                    try:
                        wait = groq.rate_wait_remaining()
                    except Exception:
                        wait = 0
                    if wait > 2:
                        # Org-level 429 đang active → chờ đúng Retry-After rồi thử,
                        # không sleep tràn qua hết budget bước
                        if self.cancel.wait(min(wait, 4)):
                            return self._finish(user_text, "[ĐÃ DỪNG] theo yêu cầu của người dùng.")
                if not reply:
                    # Không có phản hồi → đáng ngờ. Nếu hết giờ thì dừng rõ ràng
                    # (không tự 'continue' đốt thêm bước).
                    if time.time() >= deadline - 5:
                        return self._finish(user_text, "[TẠM DỪNG] Groq quá tải hoặc mất kết nối — gõ 'tiếp tục' để chạy lại.")
                    try:
                        sessions.checkpoint(self.sid, step + 1, msgs,
                                            summary=f"task={self._task_hash} turn={turn} step={step+1}")
                    except Exception:
                        pass
                    if self.cancel.wait(1):
                        return self._finish(user_text, "[ĐÃ DỪNG] theo yêu cầu của người dùng.")
                    continue
                tool_calls = reply.get("tool_calls") or []
                # Gom reasoning kiểu opencode để hiện UI (không lưu session/context — giữ context-fix).
                try:
                    _rc = reply.get("reasoning_content") or reply.get("reasoning") or ""
                    if _rc:
                        self.last_reasoning = (self.last_reasoning + "\n" + str(_rc)).strip()[-6000:]
                except Exception:
                    pass
                if not tool_calls:
                    content = (reply.get("content") or "").strip()
                    if not content and not any((s or {}).get("result_ok") for s in self._last_steps):
                        # Model trả RỖNG mà chưa có tool nào THÀNH CÔNG — CẤM bỏ cuộc kiểu "(rỗng)".
                        # Nhắc gọi tool ngay (tối đa 2 lần), còn nước còn tát.
                        # (Đã có tool OK rồi mà model im → giữ hành vi cũ: kết thúc êm.)
                        self._empty_replies += 1
                        if self._empty_replies <= 2 and time.time() < deadline - 10:
                            nudge = {"role": "user", "content": (
                                "[NHẮC LẦN %d: bạn vừa trả lời RỖNG, chưa gọi tool nào. "
                                "Đây là MỆNH LỆNH hành động thật trên máy: gọi tool phù hợp "
                                "NGAY trong lượt này (vd dl_status/dl_open/browser_open/bash...). "
                                "CẤM trả lời rỗng, CẤM chỉ nói mà không làm.]" % self._empty_replies)}
                            sessions.append(self.sid, nudge)
                            msgs.append(dict(nudge))
                            continue
                        content = ("[LOI] model trả rỗng %d lần liên tiếp — gõ 'tiếp tục' "
                                   "để thử lại." % self._empty_replies)
                    else:
                        self._empty_replies = 0
                    if (content and self._last_steps and not self._verified_done
                            and _is_multi_action(user_text)
                            and time.time() < deadline - 15):
                        # Task NHIỀU bước mà model đòi kết thúc — bắt tự đối chiếu 1 lần:
                        # thiếu bước thì làm nốt, đủ rồi thì thôi (chống báo khống kiểu
                        # "đã nhấn Enter" trong khi chưa gọi dl_key).
                        self._verified_done = True
                        done_list = ", ".join(str((s or {}).get("tool", "?"))
                                             for s in self._last_steps[-8:])
                        chk = {"role": "user", "content": (
                            "[TỰ KIỂM TRA LẦN CUỐI — không làm lại từ đầu: task gốc có NHIỀU bước. "
                            f"Tool đã gọi xong: {done_list}. "
                            "Đối chiếu từng bước trong task gốc: bước nào CHƯA có tool tương ứng "
                            "thì gọi tool làm nốt NGAY; CẤM báo cáo bước chưa làm. "
                            "Nếu mọi bước đã có tool OK thì chỉ trả lời kết quả gọn, không gọi thêm.]")}
                        sessions.append(self.sid, chk)
                        msgs.append(dict(chk))
                        continue
                    if not content:
                        # Không bao giờ kết thúc im lặng: báo rõ để user gõ 'tiếp tục'.
                        content = ("[LOI] task dở dang (model im lặng sau khi tool lỗi) — "
                                   "gõ 'tiếp tục' để chạy tiếp.")
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
                        if not isinstance(args, dict):
                            raise ValueError("args không phải object")
                    except ValueError:
                        sessions.append(self.sid, {"role": "tool", "tool_call_id": tc.get("id"),
                            "name": name, "content": "[LOI] arguments JSON hỏng — gọi lại tool với JSON object đúng format, không bỏ qua bước này."})
                        self._emit({"type": "tool_done", "name": name, "result": "[bad args json]"})
                        continue
                    # SELF-HEALING: detect loops
                    if self._is_looping(name, args) or self._repeat_failing(name):
                        sessions.append(self.sid, {"role": "tool", "tool_call_id": tc.get("id"),
                            "name": name, "content": "[SELF-HEAL] Phát hiện gọi lặp lại cùng thao tác. Hãy thử cách khác hoặc bỏ qua bước này."})
                        self._emit({"type": "tool_done", "name": name, "result": "[loop detected]"})
                        continue
                    args_note = str(args)[:120]
                    self._emit({"type": "tool_start", "name": name, "args": args, "args_note": args_note})
                    # Hook pre_tool kiểu Claude Code (chặn trước khi chạy)
                    _hook_ok, _hook_note = True, ""
                    try:
                        import hooks as _hooks
                        _hook_ok, _hook_note = _hooks.run_pre(name, args, self.sid)
                    except Exception:
                        pass
                    if not _hook_ok:
                        result = (f"[TU CHOI] Tool {name} bị hook chặn: {_hook_note or 'không rõ lý do'}. "
                                  f"Hãy giải thích với người dùng.")
                    elif not self.perm.decide(name, args, askfn=self.askfn):
                        result = f"[TU CHOI] Tool {name} bị chặn bởi permission. Hãy giải thích với người dùng."
                    else:
                        # Checkpoint kiểu Cline/gemini: snapshot git TRƯỚC lần ghi file
                        # đầu tiên của mỗi turn → /undo luôn hoàn tác được file.
                        if name in ("write_file", "edit_file", "apply_patch") and not getattr(self, "_write_snap", False):
                            try:
                                sessions.work_snapshot(self.sid)
                            except Exception:
                                pass
                            self._write_snap = True
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
                    _ok_now = not (result and (result.startswith("[LOI]") or result.startswith("[TOOL LOI]") or result.startswith("[TU CHOI]")))
                    try:
                        import hooks as _hooks2
                        if _hook_note:
                            result = (result or "") + f"\n[HOOK] {_hook_note}"
                        _hooks2.run_post(name, args, _ok_now, self.sid)
                    except Exception:
                        pass
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
