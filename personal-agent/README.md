# Rem Agent

Personal AI agent chạy 100% trên **Groq** — kiến trúc **MCP-native** theo mô hình của **goose (Block)** và **opencode**: mọi công cụ là MCP extension, mỗi extension chạy trong một tiến trình riêng nói chuyện qua JSON-RPC/stdio. Hoạt động trên **Termux (Android)** và **PC/Linux**.

## Kiến trúc (mô phỏng goose/opencode)

```
repl.py ── Repl (TUI, /help /status /plan /build /sessions...)
   │
agentloop.py ── Agent loop: LLM → tool_calls → execute → lặp
   │  ┌────────── sessions.py (JSONL journal + compact)
   │  └────────── permissions.py (allow / ask / deny — người dùng xác nhận trước tool nguy hiểm)
   │
extensions.py ── ExtensionManager: khám phá & gọi tool
   │
   ├── mcp_servers/developer  (9 tool)  bash, read/write/edit file, grep, glob, cwd...
   ├── mcp_servers/webtool    (3 tool)  web_search, web_fetch, github_api
   └── mcp_servers/memory     (2 tool)  remember, recall (graph.json)
mcplib.py  ── Client/Server MCP over stdio (chuẩn MCP, chỉ dùng stdlib, không cần Rust)
providers/groq.py ── Groq API + xoay vòng nhiều key + native tool_calls
```

- **MCP stdio**: mỗi extension = tiến trình con `python -m mcp_servers.xxx`, giao tiếp bằng JSON-RPC chuẩn MCP (`initialize`, `tools/list`, `tools/call`). Không cần `mcp`/`fastmcp` (2 gói đó kéo Rust-dep không cài được trên Android).
- **Agent loop**: Groq trả `tool_calls` (schema tự khám phá từ MCP), agent kiểm tra permission rồi execute, lặp tới khi hết tool → tổng hợp tiếng Việt.
- **Permission** giống opencode: preset `build` (tool ghi/bash/web_fetch = "ask"), preset `plan` (cấm ghi/bash/fetch, chỉ đọc). Tool nguy hiểm (`rm -rf /`, `mkfs`, `dd if=`) bị chặn cứng trong MCP server.
- **Session journal**: JSONL theo từng lượt, có `/sessions` liệt kê + `/new` bắt đầu mới, tự `compact` (tóm tắt) khi hội thoại quá dài.

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
/sessions  liệt kê session cũ
/new       session mới
/plan        chuyển preset đọc-chỉ (cấm ghi/bash/web_fetch)
/build       quay lại preset thường (hỏi xác nhận khi cần)
/key /keys  quản lý Groq keys
	exit      thoát
```

## License
MIT — xem [LICENSE](LICENSE). Mở cho cộng đồng, tự do fork & phát triển.