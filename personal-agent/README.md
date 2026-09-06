# Rem Agent

Personal AI agent chạy 100% trên **Groq** — kiến trúc **MCP-native** theo mô hình của **goose (Block)** và **opencode**: mọi công cụ là MCP extension, mỗi extension chạy trong một tiến trình riêng nói chuyện qua JSON-RPC/stdio. Hoạt động trên **Termux (Android)** và **PC/Linux**.

> v3.3: tự động cài công cụ thiếu — `ensure_tool` (hệ thống, whitelist) + `pip_install` (PyPI, tự xử lý PEP 668 bằng venv dùng chung); agent chủ động cài khi gặp lệnh/package thiếu.

## Kiến trúc (mô phỏng goose/opencode)

```
repl.py ── Repl (TUI, /help /status /models /plan /build /sessions /del /debug)
   │
agentloop.py ── Agent loop: LLM → tool_calls → execute → lặp
   │  ┌────────── sessions.py (JSONL journal + compact)
   │  └────────── permissions.py (allow / ask / deny — người dùng xác nhận trước tool nguy hiểm)
   │
extensions.py ── ExtensionManager: khám phá & gọi tool, tự restart khi mất kết nối
   │
   ├── mcp_servers/developer  (11 tool)  bash, read/write/edit file, grep, glob, cwd, ensure_tool, pip_install...
   ├── mcp_servers/webtool    (3 tool)  web_search, web_fetch, github_api
   └── mcp_servers/memory     (2 tool)  remember, recall (graph.json)
mcplib.py  ── Client/Server MCP over stdio (chuẩn MCP, chỉ dùng stdlib, không cần Rust)
providers/groq.py ── Groq API + xoay vòng nhiều key + tự chọn model tồn tại + native tool_calls
```

- **MCP stdio**: mỗi extension = tiến trình con `python -m mcp_servers.xxx`, giao tiếp bằng JSON-RPC chuẩn MCP (`initialize`, `tools/list`, `tools/call`). Không cần `mcp`/`fastmcp` (2 gói đó kéo Rust-dep không cài được trên Android). Mỗi extension ghi log riêng vào `~/.rem_ai/logs/<tên>.log`.
- **Model auto-resolve**: trước khi gọi, agent hỏi API `/models` của Groq và tự loại model chết (vd `llama-3.3-70b-versatile` 404) khỏi chuỗi chat/compact/vision — chọn thứ tự ưu tiên trong `config.MODEL_PREF_*`. Xem `/models` để biết model thực đang dùng.
- **Agent loop**: Groq trả `tool_calls` (schema tự khám phá từ MCP), agent kiểm tra permission rồi execute, lặp tới khi hết tool → tổng hợp tiếng Việt. Tool bị lỗi được đánh dấu `isError` chính xác để agent không tưởng thành công.
- **Tự phục hồi**: MCP server crash → ExtensionManager `close + start` lại extension và gọi lại tool 1 lần.
- **Permission** giống opencode: preset `build` (tool ghi/bash/web_fetch = "ask"), preset `plan` (cấm ghi/bash/fetch, chỉ đọc). Tool nguy hiểm (`rm -rf /`, `mkfs`, `dd if=`, ...) bị chặn cứng trong MCP server.
- **Session journal**: JSONL theo từng lượt, có `/sessions` liệt kê + `/new` bắt đầu mới + `/del <id>` xoá session cũ, tự `compact` (tóm tắt) khi hội thoại quá dài.

## Cài đặt

```bash
# Termux
pkg install -y python git
git clone https://github.com/kgxxyixgikcgxittixxi-collab/Rem007.git
cd Rem007/personal-agent
bash install.sh          # cài requests (chỉ phụ thuộc Python stdlib còn lại)
python main.py

# PC/Linux: dùng python3 + pip install requests
```

## Thêm Groq key
```bash
/key gsk_...   # dán key (có thể dán nguyên cụm chứa nhiều key, tự tách)
/keys          # xem có bao nhiêu key
```
Key lưu trong `~/.rem_ai/rem.db`.

## Lệnh REPL
```text
/status    danh sách extension + tool + session
/models    model đang dùng cho chat / compact
/sessions  liệt kê session cũ
/new       session mới
/del <id>  xoá session cũ
/plan        chuyển preset đọc-chỉ (cấm ghi/bash/web_fetch)
/build       quay lại preset thường (hỏi xác nhận khi cần)
/auto /safe  bật/tắt chế độ tự động xác nhận
/debug       bật/tắt chế độ gỡ lỗi (hiện đầy đủ, không gõ chữ)
/key /keys  quản lý Groq keys
	exit      thoát
```

## License
MIT — xem [LICENSE](LICENSE). Mở cho cộng đồng, tự do fork & phát triển.