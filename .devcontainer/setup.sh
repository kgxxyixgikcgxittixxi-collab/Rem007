#!/bin/bash
# Setup Rem007 trong GitHub Codespaces (chạy 1 lần khi tạo Codespace).
# Nhẹ máy local vì mọi thứ chạy trên cloud.
set -e
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT/personal-agent"

echo "[codespace] Cài system deps (ffmpeg, playwright chromium deps)..."
sudo apt-get update -qq || true
sudo apt-get install -y -qq ffmpeg git 2>&1 | tail -n 3 || true
# Deps cho Playwright chromium trên Ubuntu (bỏ qua lỗi vặt)
python3 -m pip install --quiet "playwright>=1.40" || python3 -m pip install --quiet --break-system-packages "playwright>=1.40" || true
python3 -m playwright install-deps chromium 2>&1 | tail -n 3 || sudo $(which playwright 2>/dev/null || echo playwright) install-deps chromium 2>&1 | tail -n 3 || true

echo "[codespace] Chạy install.sh của repo (requests, pptx, edge-tts, chromium)..."
bash install.sh

echo ""
echo "[codespace] XONG. Chạy thử:"
echo "  ./Remtm h \"trả lời đúng 1 chữ: ok\""
echo "Lần đầu thêm key: ./Remtm rồi gõ  /key gsk_...  (key chỉ nằm trong Codespace này, không push lên git)"
