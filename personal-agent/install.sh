#!/bin/sh
# Rem Agent installer — auto-detect Termux / PC-Linux
set -e
echo "[i] Rem Agent installer"

if [ -n "$PREFIX" ] || [ -d /data/data/com.termux/files/usr ]; then
    echo "[i] Termux detected"
    pkg install -y python git 2>/dev/null || true
    PY=python
else
    echo "[i] PC/Linux detected"
    if ! command -v python3 >/dev/null; then
        sudo apt-get install -y python3 python3-pip git 2>/dev/null || true
    fi
    PY=python3
fi

echo "[i] Installing python deps..."
"$PY" -m pip install --quiet requests 2>/dev/null || "$PY" -m pip install --quiet requests

echo "[+] Done. Run: $PY main.py"
echo "[+] First add keys: /key"