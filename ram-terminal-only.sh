#!/usr/bin/env bash
# Dồn RAM/CPU cho Terminal: terminal ưu tiên cao nhất + chống kill,
# app nền (mintUpdate, blueman, printer, overlay loop) hạ ưu tiên,
# tab browser nặng thì user tự tắt (script không kill browser để khỏi mất việc).
set -u
for t in $(pgrep -x xfce4-terminal 2>/dev/null); do
  sudo renice -n -10 -p "$t" >/dev/null 2>&1
  echo -900 | sudo tee "/proc/$t/oom_score_adj" >/dev/null 2>&1
  for c in $(pgrep -P "$t" 2>/dev/null); do
    sudo renice -n -10 -p "$c" >/dev/null 2>&1
    echo -900 | sudo tee "/proc/$c/oom_score_adj" >/dev/null 2>&1
  done
done
for p in $(pgrep -f "mintUpdate|mintreport|blueman-applet|applet.py|all-windows-on-top" 2>/dev/null); do
  sudo renice -n 15 -p "$p" >/dev/null 2>&1
  echo 600 | sudo tee "/proc/$p/oom_score_adj" >/dev/null 2>&1
done
sync; echo 1 | sudo tee /proc/sys/vm/drop_caches >/dev/null 2>&1
free -h
echo "OK: terminal NI -10 + oom -900 (không bị kill), app nền NI 15."
