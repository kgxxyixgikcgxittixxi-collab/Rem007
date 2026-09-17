# VOICE — điều khiển Remtm bằng mic (tiếng Việt)

Chế độ song song: vừa gõ phím vừa nói — mic chỉ là thêm 1 đường vào lệnh.

## 1. Cài đặt

```bash
sudo apt install -y alsa-utils portaudio19-dev   # arecord/aplay + build PyAudio
pip install -r requirements-voice.txt
python3 -c "from mcp_servers.voice_server import voice_status; print(voice_status())"
```

Offline (không mạng) thì bỏ comment `faster-whisper`/`vosk` trong
`requirements-voice.txt` rồi cài; vosk cần thêm model tiếng Việt giải nén
vào `~/.rem_ai/vosk/`.

## 2. Cách dùng

- Repl (do integrator nối sau): `/voice on` bật nghe liên tục, `/voice off` tắt.
- Tool trực tiếp:
  - `voice_status()` — kiểm tra mic/STT/TTS/loa.
  - `voice_listen(seconds=6)` — thu mic → trả text đã nghe.
  - `voice_cmd(seconds=6)` — như trên + bỏ "Rem ơi", gắn cờ lệnh nguy hiểm.
  - `voice_say(text)` — đọc trả lời ra loa.

## 3. Luồng xử lý

```
mic → voice_listen/voice_cmd (text) → Agent.run (full tool dl_*/browser_*)
      → text trả lời → voice_say (TTS ra loa)
```

## 4. Lưu ý

- Wake-word: bắt đầu bằng **"Rem ơi"** (vd "Rem ơi, mở YouTube").
  `voice_cmd` tự bỏ tiền tố này trước khi giao cho agent.
- Lệnh nguy hiểm (xóa, format, shutdown, `rm -rf`...): tool chỉ gắn cờ
  `[XÁC NHẬN]` — agent/Repl PHẢI hỏi lại người dùng trước khi chạy.
- `voice_say` phát loa nền (không treo server); file giữ trong
  `~/.rem_ai/voice/audio/`, file thu trong `~/.rem_ai/voice/wav/`.
