# Rem Agent

Personal AI agent chạy 100% trên **Groq** — kiến trúc **MCP-native** theo mô hình của **goose (Block)** và **opencode**: mọi công cụ là MCP extension, mỗi extension chạy trong một tiến trình riêng nói chuyện qua JSON-RPC/stdio. Hoạt động trên **Termux (Android)** và **PC/Linux**.

> v3.27: **PPTX chuẩn** — tạo bài thuyết trình PowerPoint đúng chuẩn OOXML (mẫu: `examples/presentations/bai_thuyet_trinh_moi_truong.py`), `requirements.txt` + `install.sh` tự cài python-pptx (kèm fallback lxml trên ARM/Termux). Trước đó v3.26: **Desktop Linux** (`desktop_linux` — điều khiển desktop qua AT-SPI, không cần screenshot: dl_tree/click/type/key/mouse/clipboard) + gpt-oss-120b làm model chat + **auto-resume** khi dừng giữa chừng (quota/time/lỗi) + headless mode `Remtm run "<task>"`. v3.26.1: gpt-oss `reasoning_effort=low` cho phản hồi nhanh hơn. V3.25: **Subagent** (`task` — agent con chạy độc lập kiểu opencode Task, đủ tool riêng + session riêng), **MCP server ngoài** qua `~/.rem_ai/mcp.json` (stdio, có `/mcp` `/mcp reload`), **`/init`** sinh `AGENTS.md`. V3.24: **LSP** (clangd/pylsp: diagnostics/definition/references/symbols/hover) + `/lsp <file>`.

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
   ├── mcp_servers/developer  (14 tool)  bash, read/write/edit file, grep, glob, cwd, ensure_tool, pip_install, todo_list/todo_write, apply_patch...
   ├── mcp_servers/webtool    (3 tool)  web_search, web_fetch, github_api
   ├── mcp_servers/memory     (2 tool)  remember, recall (graph.json)
   └── mcp_servers/lsp        (6 tool)  lsp_diagnostics, lsp_definition, lsp_references, lsp_symbols, lsp_hover, lsp_supported (clangd/pylsp)
mcplib.py  ── Client/Server MCP over stdio (chuẩn MCP, chỉ dùng stdlib, không cần Rust)
providers/groq.py ── Groq API + xoay vòng nhiều key + tự chọn model tồn tại + native tool_calls
```

- **MCP stdio**: mỗi extension = tiến trình con `python -m mcp_servers.xxx`, giao tiếp bằng JSON-RPC chuẩn MCP (`initialize`, `tools/list`, `tools/call`). Không cần `mcp`/`fastmcp` (2 gói đó kéo Rust-dep không cài được trên Android). Mỗi extension ghi log riêng vào `~/.rem_ai/logs/<tên>.log`.
- **Model auto-resolve**: trước khi gọi, agent hỏi API `/models` của Groq và tự loại model chết (vd `llama-3.3-70b-versatile` 404) khỏi chuỗi chat/compact/vision — chọn thứ tự ưu tiên trong `config.MODEL_PREF_*`. Xem `/models` để biết model thực đang dùng.
- **Agent loop**: Groq trả `tool_calls` (schema tự khám phá từ MCP), agent kiểm tra permission rồi execute, lặp tới khi hết tool → tổng hợp tiếng Việt. Tool bị lỗi được đánh dấu `isError` chính xác để agent không tưởng thành công.
- **Tự phục hồi**: MCP server crash → ExtensionManager `close + start` lại extension và gọi lại tool 1 lần.
- **Permission** giống opencode: preset `build` (tool ghi/bash/web_fetch = "ask"), preset `plan` (cấm ghi/bash/fetch, chỉ đọc). Tool nguy hiểm (`rm -rf /`, `mkfs`, `dd if=`, ...) bị chặn cứng trong MCP server.
- **Session journal**: JSONL theo từng lượt, có `/sessions` liệt kê + `/new` bắt đầu mới + `/del <id>` xoá session cũ, tự `compact` (tóm tắt) khi hội thoại quá dài.

## Subagent (Task tool)
Tool `task(description)` chạy một **agent con độc lập** (kiểu opencode Task): tiến trình MCP của riêng nó, session riêng, permission auto, giới hạn chống treo (MAX_TASK_SECONDS). Dùng cho nhiệm vụ tách biệt: quét toàn repo, viết code độc lập, tra cứu song song.

## MCP server ngoài
Khai báo trong `~/.rem_ai/mcp.json` (hoặc biến `REM_MCP_FILE`):
```json
{"mcp": {"tên": {"type": "stdio", "command": ["python3", "/abs/path/server.py"], "env": {"K": "V"}}}}
```
Mỗi server ngoài nói MCP chuẩn qua stdio, xuất hiện trong `/status` (tiền tố `mcp:`), xem/nạp lại bằng `/mcp` và `/mcp reload`.

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
/lsp <file>  kiểm tra lỗi file nguồn nhanh (clangd/pylsp)
/mcp     xem MCP server ngoài;  /mcp reload  nạp lại (~/.rem_ai/mcp.json)
/init    tạo AGENTS.md cho thư mục đang làm việc
/clear       xoá màn hình (hiện lại logo REM)
/checkupdate /update  kiểm tra + tự cập nhật bản mới từ GitHub
/key /keys  quản lý Groq keys
	exit      thoát
```

## Cài đặt nhanh cho người dùng mới

### Cách 1 — Dùng lệnh nhanh `Remtm` (giống gõ `opencode`) [khuyên dùng]
```bash
git clone https://github.com/kgxxyixgikcgxittixxi-collab/Rem007.git
cd Rem007/personal-agent
bash install.sh          # tự phát hiện Termux/PC, cài python + deps + tạo DB,
                         # VÀ tự cài lệnh `Remtm` vào PATH cho bạn
Remtm                    # ← giờ chỉ cần gõ Remtm ở BẤT KỲ đâu là mở agent
/key gsk_...             # thêm Groq key đầu tiên (lưu ở ~/.rem_ai/rem.db), rồi /help
```
`install.sh` tự đặt `Remtm` vào:
- **PC/Linux** → `~/.local/bin/Remtm` (và tự thêm vào `PATH` trong `~/.bashrc`/`~/.zshrc`)
- **Termux (Android)** → `$PREFIX/bin/Remtm`

> Nếu đã chạy `install.sh` lần trước mà chưa thấy `Remtm`, mở **terminal mới** là có. Hoặc cài tay:
> ```bash
> cp Rem007/Remtm ~/.local/bin/Remtm && chmod +x ~/.local/bin/Remtm
> ```

### Cách 2 — Chạy trực tiếp (không cài lệnh nhanh)
```bash
git clone https://github.com/kgxxyixgikcgxittixxi-collab/Rem007.git
cd Rem007/personal-agent
bash install.sh     # tự phát hiện Termux/PC, cài python + deps + tạo DB
python3 main.py      # mở Rem Agent (hoặc python nếu ở Termux)
/key gsk_...        # thêm Groq key, rồi /help
```

## License
MIT — xem [LICENSE](LICENSE). Mở cho cộng đồng, tự do fork & phát triển.