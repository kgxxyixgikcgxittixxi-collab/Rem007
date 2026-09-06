# Rem Agent

Personal AI agent chạy 100% trên **Groq (Qwen3.8-27B)** — kiến trúc module hóa theo phong cách opencode / Replit Agent. Hoạt động trên **Termux (Android)** và **PC/Linux**.

## Tính năng
- 🔀 **Router thông minh**: phân biệt `chat` / `headless` (lệnh CLI) / `gui` (mở app, bấm màn hình)
- 🧠 **Bộ nhớ** trong SQLite: tóm tắt hội thoại, tóm tắt phiên, "kinh nghiệm" tự lưu
- 🛠️ **Tool calls**: shell, đọc/ghi/sửa file (`read/write/edit/glob/grep/ls`), tìm kiếm web, điều khiển GUI (Tap/Swipe/Type qua adb hoặc xdotool)
- 🔑 **Xoay vòng nhiều Groq key** — tự thử key kế khi lỗi/rate-limit
- ✅ **An toàn**: lệnh nguy hiểm bị chặn, hoặc hỏi Boss xác nhận
- 🎨 **REPL đẹp**: màu, typewriter, lệnh `/help`, `/status`, `/keys`, `/key`, `/clear`

## Cài đặt

### Termux (Android)
```bash
pkg install -y python git
git clone https://github.com/kgxxyixgikcgxittixxi-collab/Rem007.git
cd Rem007/personal-agent
bash install.sh
python main.py
```

### PC/Linux
```bash
sudo apt install -y python3 python3-pip git
git clone https://github.com/kgxxyixgikcgxittixxi-collab/Rem007.git
cd Rem007/personal-agent
bash install.sh
python3 main.py
```

## Thêm Groq key
```bash
/key          # dán 1 hoặc nhiều key gsk_...
/keys         # xem đã có bao nhiêu key
```
Key được lưu trong `~/.rem_ai/rem.db`. Không có key → agent chỉ trả `[!] groq loi.`

## Cách dùng
```text
[Rem] kiem tra ram                 → headless, chạy `free -h`
[Rem] vao coc coc tim anime        → mở app + tìm kiếm
[Rem] hello                        → chat ngắn gọn
[Rem] ghi file hello.txt noi dung  Hello   → tool write file
[Rem] /status                      → thông tin hệ thống
```

## Cấu trúc
```
personal-agent/
├── config.py        # cấu hình, đường dẫn, model
├── router.py        # phân loại yêu cầu
├── memory.py        # lịch sử SQLite + tóm tắt
├── planner.py       # lập kế hoạch hành động (JSON)
├── executor.py      # chạy kế hoạch / tool calls
├── verifier.py      # kiểm tra, xác nhận quyền
├── web.py           # tìm kiếm web + đọc trang
├── main.py          # REPL chính
├── providers/groq.py
├── tools/shelltool.py
├── tools/fileops.py
└── tools/pctool.py
```

## License
MIT — xem [LICENSE](LICENSE). Mở cho cộng đồng, tự do fork & phát triển.