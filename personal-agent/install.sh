#!/bin/sh
# Rem Agent installer — auto-detect Termux / PC / Linux
# Chạy:  bash install.sh   (trong thư mục Rem007/personal-agent)
set -e

cat <<'BANNER'
███╗   ███╗
████╗ ████║
██╔████╔██║   REM AGENT — MCP-native · Groq · opencode-style
██║╚██╔╝██║
██║ ╚═╝ ██║
BANNER

# ── 1. Phát hiện môi trường ─────────────────────────────────────────────
if [ -n "$PREFIX" ] || [ -d /data/data/com.termux/files/usr ]; then
    echo "[i] Phát hiện Termux (Android)"
    mkdir -p /data/data/com.termux/files/home 2>/dev/null || true
    pkg install -y python git 2>/dev/null || echo "[!] Không cài được qua pkg (thử thủ công: pkg install python git)"
    PY=python
elif command -v python3 >/dev/null 2>&1; then
    echo "[i] Phát hiện PC/Linux (đã có python3)"
    if ! command -v git >/dev/null 2>&1; then
        sudo apt-get install -y git 2>/dev/null || true
    fi
    PY="$(command -v python3)"
else
    echo "[i] Phát hiện PC/Linux (chưa có python3, đang cài...)"
    sudo apt-get update 2>/dev/null || true
    sudo apt-get install -y python3 python3-pip python3-venv git 2>/dev/null || true
    PY="$(command -v python3)"
fi

if [ -z "$PY" ] || ! command -v "$PY" >/dev/null 2>&1; then
    echo "[!] KHÔNG tìm thấy python3. Cài thủ công: python3 + python3-pip + git"
    exit 1
fi
echo "[i] Python: $PY ($("$PY" --version 2>&1))"

# ── 2. Cài dependencies (requests + python-docx để tạo PPTX) ─────────────
echo "[i] Cài thư viện Python..."
# PEP 668/externally-managed → thử user install, fallback thành công là được
if ! "$PY" -m pip install --quiet requests 2>/dev/null; then
    "$PY" -m pip install --user --quiet requests 2>/dev/null || true
fi

# ── 3. Thiết lập keys.sqlite + thư mục ──────────────────────────────────
DIR="$HOME/.rem_ai"
mkdir -p "$DIR"
if [ ! -f "$DIR/rem.db" ]; then
    echo "[i] Chuẩn bị cơ sở dữ liệu keys..."
    "$PY" - <<'PY'
import os, sqlite3
d = os.path.expanduser("~/.rem_ai"); os.makedirs(d, exist_ok=True)
c = sqlite3.connect(os.path.join(d, "rem.db"))
c.execute("CREATE TABLE IF NOT EXISTS gq(key TEXT UNIQUE)")
c.commit(); c.close()
print("     đã tạo rem.db")
PY
fi

echo ""
echo "=== CÀI XONG! Chạy:  $PY main.py  ==="
echo "Lần đầu: gõ  /key gsk_...   rồi  /help  để bắt đầu."
echo ""

# ── 4. Cài lệnh `RemNav`/`Remtm` toàn hệ thống (kiểu opencode) ────────────
echo "[i] Cài lệnh nhanh 'Remtm'..."
BIN="$HOME/.local/bin"
if [ -n "$PREFIX" ]; then
    BIN="$PREFIX/bin"      # Termux
fi
mkdir -p "$BIN"
SRC="$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")/../Remtm"
if [ ! -f "$SRC" ]; then
    SRC="$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")/Remtm"
fi
if [ -f "$SRC" ]; then
    cp "$SRC" "$BIN/Remtm"
    chmod +x "$BIN/Remtm"
    echo "[i] Đã cài:  Remtm  ->  $BIN/Remtm"
    # Đảm bảo bin nằm trong PATH khi mở shell mới
    case "$PATH" in
        *"$BIN"*) ;;
        *)  shellrc="$HOME/.bashrc"
            if [ -f "$HOME/.zshrc" ]; then shellrc="$HOME/.zshrc"; fi
            if ! grep -q "PATH=.*$BIN" "$shellrc" 2>/dev/null; then
                echo "export PATH=\"\$PATH:$BIN\"" >> "$shellrc"
                echo "[i] Đã thêm '$BIN' vào $shellrc (mở terminal mới là dùng được)"
            fi ;;
    esac
    echo ""
    echo ">>> Giờ chỉ cần gõ:  Remtm   (ở bất kỳ đâu)  🎉"
else
    echo "[!] Không tìm thấy file Remtm (bỏ qua bước cài lệnh nhanh)"
fi
echo ""