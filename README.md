# Rem007

Agent cá nhân MCP-native chạy 100% trên Groq, kiến trúc theo goose/opencode — mọi thao tác qua công cụ MCP (mỗi công cụ chạy tiến trình riêng).

## Tính năng (v3.28)

- **60+ tool / 8 MCP server**: file+shell, web, memory graph, LSP, desktop Linux, **trình duyệt Playwright**, **làm video**
- **Xoay key Groq thông minh** với 13+ key (mỗi key 1 tài khoản độc lập):
  - Token bucket per key (chủ động tiết lưu, tránh 429)
  - Circuit breaker + cooldown theo `Retry-After`, leo thang khi quota cạn
  - Backoff + jitter cho lỗi 5xx/mạng
  - Stats lưu file → học key nào tốt, sống sót qua restart (`/keys` xem)
- **Tự chữa lỗi (self-healing)**: phát hiện loop gọi tool, theo dõi lỗi lặp lại, inject gợi ý đổi hướng
- **Trình duyệt Playwright**: mở web, click, gõ, chụp ảnh, lấy nội dung, điều khiển YouTube/dashboard
- **Làm video**: TTS tiếng Việt (edge-tts + gTTS fallback), ảnh AI, ghép scene Shorts 1080x1920, cắt/nối/đổi cỡ
- Tạo game, code, file; MCP server ngoài qua `~/.rem_ai/mcp.json`

## Cài đặt

```bash
git clone https://github.com/kgxxyixgikcgxittixxi-collab/Rem007 /tmp/rem_deploy && cp /tmp/rem_deploy/personal-agent . 2>/dev/null
# hoặc clone thẳng:
cd Rem007
bash personal-agent/install.sh
Remtm        # gõ /key gsk_... để thêm Groq key, /help để xem lệnh
```

Đầu chạy 1 tác vụ rồi thoát (headless):
```bash
Remtm h "tạo game snake trong /tmp/test"
```

## Lệnh nhanh

| Lệnh | Chức năng |
|---|---|
| `/status` | xem extension + tool + session |
| `/keys` | xem sức khỏe các Groq key (xoay vòng) |
| `/key gsk_...` | thêm key |
| `/auto` / `/safe` | tự động / hỏi xác nhận |
| `/stop` | dừng agent giữa chừng |
| `/mcp reload` | nạp lại MCP ngoài |
| `/init` | tạo AGENTS.md cho dự án |

## Yêu cầu

- Python 3.10+, `requests`
- ffmpeg (làm video), Playwright + chromium (trình duyệt), edge-tts/gTTS (video)
- Groq API keys (`gsk_...`)

Thông tin chi tiết trong [personal-agent/README.md](personal-agent/README.md).