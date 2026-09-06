#!/bin/bash
# Bật Tailscale userspace trong Termux (KHÔNG chiếm slot VPN Android)
if ! pgrep -x tailscaled >/dev/null; then
  setsid nohup tailscaled --tun=userspace-networking \
    --socks5-server=127.0.0.1:1055 \
    --state=/root/.tailscaled.state > /tmp/tailscaled.log 2>&1 < /dev/null &
  sleep 5
fi
STATUS=$(tailscale --socket=/var/run/tailscale/tailscaled.sock ip 2>/dev/null | head -1)
if [ -z "$STATUS" ]; then
  echo "Chua dang nhap — mo link nay de dang nhap:"
  timeout 12 tailscale --socket=/var/run/tailscale/tailscaled.sock up --hostname=termux-phone 2>&1 | grep -oE "https://login\.tailscale\.com/[a-zA-Z0-9/]*" | head -1
else
  echo "Tailscale OK — IP cua may: $STATUS"
  echo "Ket noi may tinh: ssh laptop"
fi
